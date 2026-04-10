"""Continuous experiment runner with persistent co-evolution archives."""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import math
import multiprocessing
import os
import platform
import random
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable
from .simulation import (
    PolicyDecision,
    ScenarioConfig,
    ScenarioMetrics,
    default_scenarios,
    ensure_attacker_genome_io,
    ensure_genome_io,
    evaluate_across_scenarios,
)

ensure_evolvo_importable()

from evolvo import GFSLGenome, GFSLInstruction, GFSLEvolver


@dataclass(frozen=True)
class ExperimentConfig:
    out_dir: Path
    profile: str = "fast"
    seed: int = 1337
    population_size: int = 18
    generations: int = 16
    initial_instructions: int = 12
    rounds: int = 1
    attacker_population_size: int = 12
    attacker_generations: int = 6
    elite_pool: int = 12
    archive_limit: int = 64
    resume: bool = True
    parallel_workers: int = 0
    parallel_backend: str = "auto"  # auto|process|thread|off
    use_supervised_guide: bool = True
    preferred_device: str = "auto"  # auto|cpu|cuda|mps
    parent_pool_ratio: float = 0.60
    stagnation_patience: int = 4
    mutation_floor: float = 0.12
    mutation_ceiling: float = 0.55
    mutation_step: float = 0.05
    statistical_predictive: bool = True
    quick_cycle_fraction: float = 0.14
    mid_cycle_fraction: float = 0.50
    quick_keep_ratio: float = 0.55
    mid_keep_ratio: float = 0.30
    key_variant_count: int = 2
    novelty_bonus: float = 0.03
    predictive_penalty: float = 0.05
    auto_statistical_tuning: bool = True
    device_mhz: float = 100.0
    provider_mhz: float = 300.0
    max_test_time_seconds: float = 10.0
    target_generation_seconds: float = 2.4
    max_eval_cache_entries: int = 25000


@dataclass(frozen=True)
class ResourcePlan:
    cpu_count: int
    parallel_workers: int
    parallel_backend: str
    gpu_backend: str
    gpu_available: bool
    torch_available: bool
    platform: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _invalidate_genome_caches(genome: GFSLGenome) -> None:
    genome._signature = None
    genome._effective_instructions = None
    if hasattr(genome, "_pcpl_eval_sig"):
        delattr(genome, "_pcpl_eval_sig")
    if hasattr(genome, "_pcpl_eval_sig_key"):
        delattr(genome, "_pcpl_eval_sig_key")


def _append_random_instruction_fast(
    genome: GFSLGenome,
    *,
    max_attempts: int = 8,
) -> bool:
    """Add one valid random instruction without expensive probability trees."""
    slot_count = max(1, int(genome.validator.slot_count))
    for _ in range(max(1, int(max_attempts))):
        instruction = GFSLInstruction(slot_count=slot_count)
        valid = True
        for slot_idx in range(slot_count):
            options = genome.validator.get_valid_options(instruction, slot_idx)
            if not options:
                valid = False
                break
            instruction.slots[slot_idx] = random.choice(options)
        if not valid:
            continue
        genome.instructions.append(instruction)
        genome.validator.update_state(instruction)
        try:
            genome._ensure_activity_size()  # type: ignore[attr-defined]
        except Exception:
            pass
        _invalidate_genome_caches(genome)
        return True
    return False


@dataclass
class PredictiveStageController:
    quick_cycle_fraction: float
    mid_cycle_fraction: float
    quick_keep_ratio: float
    mid_keep_ratio: float
    key_variant_count: int
    auto_tune: bool = True

    @staticmethod
    def from_config(config: ExperimentConfig) -> "PredictiveStageController":
        controller = PredictiveStageController(
            quick_cycle_fraction=float(config.quick_cycle_fraction),
            mid_cycle_fraction=float(config.mid_cycle_fraction),
            quick_keep_ratio=float(config.quick_keep_ratio),
            mid_keep_ratio=float(config.mid_keep_ratio),
            key_variant_count=int(config.key_variant_count),
            auto_tune=bool(config.auto_statistical_tuning),
        )
        if str(config.profile).lower() == "full":
            controller.quick_cycle_fraction = min(controller.quick_cycle_fraction, 0.10)
            controller.mid_cycle_fraction = min(controller.mid_cycle_fraction, 0.45)
            controller.quick_keep_ratio = min(controller.quick_keep_ratio, 0.35)
            controller.mid_keep_ratio = min(controller.mid_keep_ratio, 0.20)
            controller.key_variant_count = min(controller.key_variant_count, 1)
        controller.clamp()
        return controller

    def clamp(self) -> None:
        self.quick_cycle_fraction = clamp(self.quick_cycle_fraction, 0.08, 0.65)
        self.mid_cycle_fraction = clamp(
            max(self.mid_cycle_fraction, self.quick_cycle_fraction + 0.12),
            0.25,
            0.95,
        )
        self.quick_keep_ratio = clamp(self.quick_keep_ratio, 0.35, 0.92)
        self.mid_keep_ratio = clamp(self.mid_keep_ratio, 0.12, 0.80)
        self.mid_keep_ratio = min(self.mid_keep_ratio, max(0.12, self.quick_keep_ratio - 0.05))
        self.key_variant_count = max(1, min(6, int(self.key_variant_count)))

    def apply_feedback(self, stats: Dict[str, float]) -> None:
        self.clamp()
        if not self.auto_tune:
            return

        population = max(1.0, float(stats.get("population", 1.0)))
        quick_kept = float(stats.get("quick_kept", 0.0))
        mid_kept = float(stats.get("mid_kept", 0.0))
        quick_rate = quick_kept / population
        mid_rate = mid_kept / population
        mid_over_quick = mid_kept / max(1.0, quick_kept)
        probe_samples = max(1.0, float(stats.get("probe_samples", 0.0)))
        probe_win_rate = float(stats.get("probe_wins", 0.0)) / probe_samples
        novelty_quick = float(stats.get("novelty_quick", 0.0))
        novelty_mid = float(stats.get("novelty_mid", 0.0))
        novelty = 0.5 * (novelty_quick + novelty_mid)
        batch_seconds = max(0.0, float(stats.get("batch_seconds", 0.0)))
        target_seconds = max(0.5, float(stats.get("target_batch_seconds", 3.0)))

        over_budget = batch_seconds > target_seconds

        # Enforce staged selectivity: if a stage keeps almost everyone, tighten it.
        if population >= 3.0 and quick_rate > 0.90:
            self.quick_keep_ratio -= 0.07
            self.quick_cycle_fraction -= 0.02
        if quick_kept >= 2.0 and mid_over_quick > 0.88:
            self.mid_keep_ratio -= 0.05
            self.mid_cycle_fraction -= 0.015

        if over_budget:
            # Runtime budget has priority over deeper exploration.
            over = min(3.0, batch_seconds / target_seconds)
            self.quick_keep_ratio -= min(0.20, 0.045 * over)
            self.mid_keep_ratio -= min(0.16, 0.035 * over)
            self.quick_cycle_fraction -= min(0.16, 0.040 * over)
            self.mid_cycle_fraction -= min(0.12, 0.030 * over)
            if over > 1.4 and self.key_variant_count > 1:
                self.key_variant_count -= 1
            self.clamp()
            return

        if probe_win_rate > 0.20:
            self.quick_keep_ratio += 0.08
            self.mid_keep_ratio += 0.06
            self.quick_cycle_fraction += 0.05
            self.mid_cycle_fraction += 0.05
            self.key_variant_count += 1
        elif probe_win_rate < 0.05 and novelty < 0.20 and quick_rate > 0.62:
            self.quick_keep_ratio -= 0.05
            self.mid_keep_ratio -= 0.04
            self.quick_cycle_fraction -= 0.03
            self.mid_cycle_fraction -= 0.03
            if self.key_variant_count > 2:
                self.key_variant_count -= 1
        else:
            # Softly converge to target stage-throughput.
            self.quick_keep_ratio += 0.03 * (0.55 - quick_rate)
            self.mid_keep_ratio += 0.03 * (0.30 - mid_rate)
            if novelty > 0.45:
                self.quick_cycle_fraction += 0.015
                self.mid_cycle_fraction += 0.015
            elif novelty < 0.15:
                self.quick_cycle_fraction -= 0.01

        if batch_seconds > 0.0 and batch_seconds < (0.55 * target_seconds) and novelty > 0.20:
            # If we're comfortably below budget, allow a bit more exploration depth.
            self.quick_cycle_fraction += 0.01
            self.mid_cycle_fraction += 0.01

        self.clamp()

    def to_dict(self) -> Dict[str, Any]:
        self.clamp()
        return {
            "quick_cycle_fraction": float(self.quick_cycle_fraction),
            "mid_cycle_fraction": float(self.mid_cycle_fraction),
            "quick_keep_ratio": float(self.quick_keep_ratio),
            "mid_keep_ratio": float(self.mid_keep_ratio),
            "key_variant_count": int(self.key_variant_count),
            "auto_tune": bool(self.auto_tune),
        }


def _seed_controller_from_payload(
    controller: PredictiveStageController,
    payload: Optional[Dict[str, Any]],
) -> PredictiveStageController:
    if not isinstance(payload, dict):
        controller.clamp()
        return controller

    if "quick_cycle_fraction" in payload:
        controller.quick_cycle_fraction = float(payload["quick_cycle_fraction"])
    if "mid_cycle_fraction" in payload:
        controller.mid_cycle_fraction = float(payload["mid_cycle_fraction"])
    if "quick_keep_ratio" in payload:
        controller.quick_keep_ratio = float(payload["quick_keep_ratio"])
    if "mid_keep_ratio" in payload:
        controller.mid_keep_ratio = float(payload["mid_keep_ratio"])
    if "key_variant_count" in payload:
        controller.key_variant_count = int(payload["key_variant_count"])

    controller.clamp()
    return controller


class SafeGFSLEvolver(GFSLEvolver):
    """GFSLEvolver variant that cannot deadlock on diversity saturation."""

    def __init__(
        self,
        population_size: int = 50,
        supervised_guide=None,
        *,
        parent_pool_ratio: float = 0.60,
        stagnation_patience: int = 4,
        mutation_floor: float = 0.12,
        mutation_ceiling: float = 0.55,
        mutation_step: float = 0.05,
    ):
        super().__init__(population_size=population_size, supervised_guide=supervised_guide)
        self.parent_pool_ratio = max(0.25, min(1.0, float(parent_pool_ratio)))
        self.stagnation_patience = max(1, int(stagnation_patience))
        self.mutation_floor = max(0.01, min(1.0, float(mutation_floor)))
        self.mutation_ceiling = max(self.mutation_floor, min(1.0, float(mutation_ceiling)))
        self.mutation_step = max(0.005, min(0.5, float(mutation_step)))
        self.best_fitness_tracker = -float("inf")
        self.stagnation_count = 0
        self.best_signature_tracker = ""
        self.signature_stagnation_count = 0

    def initialize_population(
        self,
        genome_type: str = "algorithm",
        initial_instructions: int = 10,
    ) -> None:
        """Create an initial diverse population with a faster random builder."""
        self.population = []
        self.diversity_cache = set()
        attempts = 0
        max_attempts = max(self.population_size * 8, self.population_size)

        while len(self.population) < self.population_size and attempts < max_attempts:
            attempts += 1
            genome = GFSLGenome(genome_type)
            target_count = random.randint(1, max(1, int(initial_instructions)))
            for _ in range(target_count):
                if not _append_random_instruction_fast(genome, max_attempts=6):
                    break
            genome.rebuild_validator_state()
            _invalidate_genome_caches(genome)
            signature = _evaluation_signature(genome)
            if signature in self.diversity_cache:
                continue
            self.diversity_cache.add(signature)
            self.population.append(genome)

        while len(self.population) < self.population_size:
            genome = GFSLGenome(genome_type)
            if not _append_random_instruction_fast(genome, max_attempts=10):
                try:
                    genome.add_instruction_interactive(max_attempts=8)
                except Exception:
                    pass
            genome.rebuild_validator_state()
            _invalidate_genome_caches(genome)
            self.population.append(genome)

    def _stagnation_pressure(self) -> float:
        fitness_pressure = float(self.stagnation_count) / float(
            max(1, self.stagnation_patience)
        )
        signature_pressure = float(self.signature_stagnation_count) / float(
            max(1, self.stagnation_patience * 2)
        )
        return clamp(max(fitness_pressure, signature_pressure), 0.0, 1.0)

    def _mutate_slot_fast(
        self,
        genome: GFSLGenome,
        *,
        diversification: float,
    ) -> bool:
        if not genome.instructions:
            return False
        instr_idx = random.randrange(len(genome.instructions))
        base_instruction = genome.instructions[instr_idx]
        slot_count = len(base_instruction.slots)
        if slot_count <= 0:
            return False

        max_trials = max(3, min(10, slot_count * 2))
        for _ in range(max_trials):
            candidate = base_instruction.copy()
            slot_idx = random.randrange(slot_count)
            valid_options = genome.validator.get_valid_options(candidate, slot_idx)
            if not valid_options:
                continue

            current_val = candidate.slots[slot_idx]
            alternatives = [opt for opt in valid_options if opt != current_val]
            if alternatives:
                candidate.slots[slot_idx] = random.choice(alternatives)
                changed = True
            else:
                changed = False
                if random.random() >= diversification:
                    continue

            valid_suffix = True
            for next_slot in range(slot_idx + 1, slot_count):
                next_valid = genome.validator.get_valid_options(candidate, next_slot)
                if not next_valid:
                    valid_suffix = False
                    break
                next_current = candidate.slots[next_slot]
                if next_current not in next_valid:
                    candidate.slots[next_slot] = random.choice(next_valid)
                    changed = True
                    continue
                if random.random() < diversification and len(next_valid) > 1:
                    next_alternatives = [opt for opt in next_valid if opt != next_current]
                    if next_alternatives:
                        candidate.slots[next_slot] = random.choice(next_alternatives)
                        changed = True

            if not valid_suffix or not changed:
                continue
            genome.instructions[instr_idx] = candidate
            return True
        return False

    def _pick_mutation_operation(self, *, instruction_count: int, pressure: float) -> str:
        operations: List[str] = ["slot", "add", "swap"]
        slot_weight = max(0.10, 0.46 - (0.16 * pressure))
        add_weight = 0.24 + (0.22 * pressure)
        swap_weight = 0.10 + (0.10 * pressure)
        weights: List[float] = [slot_weight, add_weight, swap_weight]
        if instruction_count > 1:
            operations.append("remove")
            weights.append(0.14 + (0.06 * pressure))
        return random.choices(operations, weights=weights, k=1)[0]

    def mutate(self, genome: GFSLGenome) -> GFSLGenome:
        """Faster, higher-entropy mutation operator for large experiment sweeps."""
        mutated = copy.deepcopy(genome)
        if not mutated.instructions:
            _append_random_instruction_fast(mutated, max_attempts=12)

        pressure = self._stagnation_pressure()
        diversification = clamp(0.20 + (0.55 * pressure), 0.20, 0.85)
        passes = 1
        if random.random() < (0.35 + (0.45 * pressure)):
            passes += 1
        if pressure > 0.65 and random.random() < 0.80:
            passes += 1

        changed = False
        validator_synced = True
        for _ in range(passes):
            op = self._pick_mutation_operation(
                instruction_count=len(mutated.instructions),
                pressure=pressure,
            )
            if op in {"slot", "add"} and not validator_synced:
                mutated.rebuild_validator_state()
                validator_synced = True

            if op == "slot":
                changed = self._mutate_slot_fast(
                    mutated,
                    diversification=diversification,
                ) or changed
            elif op == "add":
                changed = (
                    _append_random_instruction_fast(
                        mutated,
                        max_attempts=max(6, 10 + int(6 * pressure)),
                    )
                    or changed
                )
            elif op == "swap" and len(mutated.instructions) > 1:
                a, b = random.sample(range(len(mutated.instructions)), 2)
                mutated.instructions[a], mutated.instructions[b] = (
                    mutated.instructions[b],
                    mutated.instructions[a],
                )
                if len(mutated.instruction_activity) > max(a, b):
                    mutated.instruction_activity[a], mutated.instruction_activity[b] = (
                        mutated.instruction_activity[b],
                        mutated.instruction_activity[a],
                    )
                changed = True
                validator_synced = False
            elif op == "remove" and len(mutated.instructions) > 1:
                idx = random.randrange(len(mutated.instructions))
                mutated.instructions.pop(idx)
                if idx < len(mutated.instruction_activity):
                    mutated.instruction_activity.pop(idx)
                changed = True
                validator_synced = False

        if not changed:
            if validator_synced:
                changed = self._mutate_slot_fast(
                    mutated,
                    diversification=max(diversification, 0.45),
                )
            if not changed:
                changed = _append_random_instruction_fast(
                    mutated,
                    max_attempts=14,
                )

        mutated.rebuild_validator_state()
        _invalidate_genome_caches(mutated)
        mutated.fitness = None
        mutated.generation = genome.generation
        return mutated

    def evolve(
        self,
        generations: int,
        evaluator,
        progress_callback=None,
        batch_evaluator=None,
        should_stop=None,
    ):
        self._early_stop_generation = None  # type: ignore[attr-defined]
        self._early_stop_reason = None  # type: ignore[attr-defined]
        for gen in range(generations):
            self.generation = gen

            if batch_evaluator is not None:
                try:
                    batch_evaluator(self.population)
                except Exception:
                    for genome in self.population:
                        if genome.fitness is None:
                            try:
                                genome.fitness = evaluator(genome)
                            except Exception:
                                genome.fitness = -float("inf")
            else:
                for genome in self.population:
                    if genome.fitness is None:
                        try:
                            genome.fitness = evaluator(genome)
                        except Exception:
                            genome.fitness = -float("inf")

            self.population.sort(
                key=lambda g: g.fitness or -float("inf"), reverse=True
            )
            current_best = float(self.population[0].fitness or -float("inf")) if self.population else -float("inf")
            current_signature = _evaluation_signature(self.population[0]) if self.population else ""
            if current_best > (self.best_fitness_tracker + 1e-12):
                self.best_fitness_tracker = current_best
                self.stagnation_count = 0
                self.mutation_rate = max(
                    self.mutation_floor,
                    self.mutation_rate - (0.5 * self.mutation_step),
                )
            else:
                self.stagnation_count += 1
                if self.stagnation_count >= self.stagnation_patience:
                    self.mutation_rate = min(
                        self.mutation_ceiling,
                        self.mutation_rate + self.mutation_step,
                    )
                    self.stagnation_count = 0

            if current_signature == self.best_signature_tracker:
                self.signature_stagnation_count += 1
            else:
                self.best_signature_tracker = current_signature
                self.signature_stagnation_count = 0

            stagnation_pressure = self._stagnation_pressure()
            if self.supervised_guide:
                self.supervised_guide.observe_population(self.population)

            if progress_callback and self.population:
                best = self.population[0]
                progress_callback(gen, best, best.fitness or -float("inf"))
            if should_stop is not None and self.population:
                stop = False
                stop_reason = "adaptive-stagnation"
                try:
                    decision = should_stop(
                        gen,
                        self.population[0],
                        float(self.population[0].fitness or -float("inf")),
                        self.population,
                    )
                    if isinstance(decision, tuple):
                        stop = bool(decision[0]) if len(decision) > 0 else False
                        if len(decision) > 1 and str(decision[1]).strip():
                            stop_reason = str(decision[1]).strip()
                    elif isinstance(decision, str):
                        stop = bool(decision)
                        if decision.strip():
                            stop_reason = decision.strip()
                    else:
                        stop = bool(decision)
                except Exception:
                    stop = False
                if stop:
                    self._early_stop_generation = int(gen)  # type: ignore[attr-defined]
                    self._early_stop_reason = str(stop_reason)  # type: ignore[attr-defined]
                    print(
                        "[pcpl-evolvo][evolver] early-stop gen={gen:03d} reason={reason}".format(
                            gen=gen,
                            reason=stop_reason,
                        )
                    )
                    break

            elite_size = max(1, int(self.population_size * self.elite_ratio))
            new_population = self.population[:elite_size]

            seen = {_evaluation_signature(genome) for genome in new_population}
            attempts = 0
            max_attempts = max(self.population_size * 90, 240)
            parent_pool_size = max(2, int(len(self.population) * self.parent_pool_ratio))
            parent_pool = self.population[: parent_pool_size]
            local_refine_chance = 0.28 + (0.47 * stagnation_pressure)

            def tournament_from(pool: Sequence[GFSLGenome], size: int = 3) -> GFSLGenome:
                tournament = random.sample(list(pool), min(size, len(pool)))
                return max(tournament, key=lambda g: g.fitness or -float("inf"))

            while len(new_population) < self.population_size and attempts < max_attempts:
                attempts += 1
                if stagnation_pressure > 0.0 and random.random() < local_refine_chance:
                    # Focused local refinement around high-performing parents.
                    parent = parent_pool[attempts % len(parent_pool)]
                    child = copy.deepcopy(parent)
                    child = self.mutate(child)
                else:
                    parent1 = tournament_from(parent_pool)
                    parent2 = tournament_from(parent_pool)

                    if random.random() < self.crossover_rate:
                        child = self.crossover(parent1, parent2)
                    else:
                        child = copy.deepcopy(random.choice([parent1, parent2]))

                child.fitness = None
                child.generation = gen + 1

                force_mutation = stagnation_pressure >= 0.55 and random.random() < 0.70
                if force_mutation or random.random() < self.mutation_rate:
                    if self.supervised_guide:
                        child = self.supervised_guide.propose_mutation(self, child)
                    else:
                        child = self.mutate(child)
                    child.fitness = None
                    child.generation = gen + 1

                signature = _evaluation_signature(child)
                if signature in seen:
                    continue
                seen.add(signature)
                new_population.append(child)

            # Inject random immigrants when the top signature is stuck for too long.
            if self.signature_stagnation_count >= max(1, self.stagnation_patience):
                immigrant_ratio = 0.15 + (0.20 * stagnation_pressure)
                immigrant_target = max(1, int(round(self.population_size * immigrant_ratio)))
                injected = 0
                inject_attempts = 0
                while (
                    len(new_population) < self.population_size
                    and injected < immigrant_target
                    and inject_attempts < (immigrant_target * 12)
                ):
                    inject_attempts += 1
                    immigrant = _make_random_genome(
                        max(4, int(self.population_size * 0.7))
                    )
                    immigrant.fitness = None
                    immigrant.generation = gen + 1
                    signature = _evaluation_signature(immigrant)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    new_population.append(immigrant)
                    injected += 1

            while len(new_population) < self.population_size:
                fallback = copy.deepcopy(random.choice(self.population[:elite_size]))
                fallback.fitness = None
                fallback.generation = gen + 1
                fallback_added = False
                for _ in range(16):
                    signature = _evaluation_signature(fallback)
                    if signature not in seen:
                        seen.add(signature)
                        new_population.append(fallback)
                        fallback_added = True
                        break
                    fallback = self.mutate(copy.deepcopy(fallback))
                    fallback.fitness = None
                    fallback.generation = gen + 1
                if fallback_added:
                    continue
                immigrant = _make_random_genome(
                    max(4, int(self.population_size * 0.6))
                )
                immigrant.fitness = None
                immigrant.generation = gen + 1
                signature = _evaluation_signature(immigrant)
                if signature in seen:
                    continue
                seen.add(signature)
                new_population.append(immigrant)

            self.population = new_population[: self.population_size]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_gpu_backend(preferred_device: str = "auto") -> Tuple[str, bool, bool]:
    torch_available = False
    backend = "none"
    try:
        import torch  # type: ignore

        torch_available = True
        if preferred_device != "auto":
            preferred = preferred_device.lower()
            if preferred == "cuda" and torch.cuda.is_available():
                backend = "cuda"
            elif preferred == "mps":
                mps_ok = bool(
                    hasattr(torch.backends, "mps")
                    and torch.backends.mps.is_available()
                )
                if mps_ok:
                    backend = "mps"
            elif preferred == "cpu":
                backend = "none"
        else:
            if torch.cuda.is_available():
                backend = "cuda"
            elif (
                hasattr(torch.backends, "mps")
                and torch.backends.mps.is_available()
            ):
                backend = "mps"
    except Exception:
        torch_available = False
        backend = "none"

    return backend, backend != "none", torch_available


def _resolve_resource_plan(config: ExperimentConfig, max_population: int) -> ResourcePlan:
    cpu_count = max(1, int(os.cpu_count() or 1))
    workers = int(config.parallel_workers)
    if workers <= 0:
        workers = cpu_count
    workers = max(1, min(workers, max(1, max_population)))

    requested_backend = str(config.parallel_backend).lower()
    if requested_backend not in {"auto", "process", "thread", "off"}:
        requested_backend = "auto"
    if requested_backend == "off":
        resolved_backend = "off"
        workers = 1
    elif requested_backend == "auto":
        resolved_backend = "process" if workers > 1 else "off"
    else:
        resolved_backend = requested_backend if workers > 1 else "off"

    if platform.system().lower().startswith("win") and resolved_backend == "process":
        # Keep behavior predictable on macOS/Linux request, but avoid fragile forks elsewhere.
        resolved_backend = "thread"

    gpu_backend, gpu_available, torch_available = _detect_gpu_backend(config.preferred_device)
    return ResourcePlan(
        cpu_count=cpu_count,
        parallel_workers=workers,
        parallel_backend=resolved_backend,
        gpu_backend=gpu_backend,
        gpu_available=gpu_available,
        torch_available=torch_available,
        platform=platform.platform(),
    )


def _process_pool_context_name() -> Optional[str]:
    if os.name == "nt":
        return None
    if platform.system().lower() == "darwin":
        # Avoid unsafe fork interactions with MPS/objc runtime on macOS.
        return "spawn"
    return "fork"


def _metrics_from_rows(rows: Sequence[Dict[str, Any]]) -> List[ScenarioMetrics]:
    return [ScenarioMetrics(**row) for row in rows]


def _mix_seed(seed: int, label: str) -> int:
    payload = f"{int(seed)}:{label}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _variant_seed(
    base_seed: int,
    variant_index: int,
    *,
    complexity: str = "mid",
) -> Tuple[int, str]:
    level = str(complexity).strip().lower()
    if variant_index <= 0:
        if level == "quick":
            return int((base_seed % 8192) + 17), "quick-base"
        if level == "hard":
            return _mix_seed(base_seed, "hard-base"), "hard-base"
        return int(base_seed), "base"
    if variant_index == 1:
        if level == "quick":
            return int((base_seed % 2048) + 33), "quick-shared-low-entropy"
        if level == "hard":
            return _mix_seed(base_seed, "hard-rotating-derived"), "hard-rotating-derived"
        return int((base_seed % 4096) + 17), "shared-low-entropy"
    if variant_index == 2:
        if level == "quick":
            return int((base_seed % 16384) + 97), "quick-mid-entropy"
        if level == "hard":
            return _mix_seed(base_seed, "hard-lineage-derived"), "hard-lineage-derived"
        return _mix_seed(base_seed, "rotating-derived"), "rotating-derived"
    if level == "hard":
        return _mix_seed(base_seed, f"hard-lineage-xor:{variant_index}"), f"hard-lineage-{variant_index}"
    if level == "quick":
        return int((base_seed % 65536) + (17 * variant_index)), f"quick-lineage-{variant_index}"
    return _mix_seed(base_seed, f"lineage-xor:{variant_index}"), f"lineage-{variant_index}"


def _stage_complexity_profile(
    scenario: ScenarioConfig,
    *,
    complexity: str,
    variant_index: int,
) -> Dict[str, Any]:
    level = str(complexity).strip().lower()
    if level == "quick":
        return {
            "prime_mode": "fixed",
            "prime_bits": max(12, int(scenario.prime_bits) - 2),
            "modulus_bits": max(47, int(scenario.modulus_bits) - 4),
            "compound_mode": "semiprime" if (variant_index % 2 == 0) else "prime-power",
            "compound_count": max(2, int(math.ceil(float(scenario.compound_count) * 0.55))),
            "compound_primes": max(2, min(3, int(scenario.compound_primes))),
            "compound_offset": 0,
            "compound_prime_bits": 0,
            "compound_pool_size": max(12, int(math.ceil(float(scenario.compound_pool_size) * 0.65))),
            "attack_token_bits": max(10, int(scenario.attack_token_bits) - 4),
        }
    if level == "hard":
        return {
            "prime_mode": "generated",
            "prime_bits": max(int(scenario.prime_bits), 20) + min(2, variant_index // 2),
            "modulus_bits": max(int(scenario.modulus_bits), 53) + min(2, variant_index // 3),
            "compound_mode": "blend",
            "compound_count": max(int(scenario.compound_count) + 2, int(math.ceil(float(scenario.compound_count) * 1.25))),
            "compound_primes": max(3, int(scenario.compound_primes) + 1),
            "compound_offset": max(int(scenario.compound_offset), 5 + variant_index),
            "compound_prime_bits": max(int(scenario.compound_prime_bits), 11),
            "compound_pool_size": max(int(scenario.compound_pool_size), 24 + (2 * variant_index)),
            "attack_token_bits": min(24, int(scenario.attack_token_bits) + 4),
        }
    return {
        "prime_mode": str(scenario.prime_mode),
        "prime_bits": int(scenario.prime_bits),
        "modulus_bits": int(scenario.modulus_bits),
        "compound_mode": "blend" if (variant_index > 0 and str(scenario.compound_mode) != "blend") else str(scenario.compound_mode),
        "compound_count": max(3, int(math.ceil(float(scenario.compound_count) * 0.85))),
        "compound_primes": max(2, int(scenario.compound_primes)),
        "compound_offset": max(0, int(scenario.compound_offset)),
        "compound_prime_bits": max(0, int(scenario.compound_prime_bits)),
        "compound_pool_size": max(14, int(math.ceil(float(scenario.compound_pool_size) * 0.85))),
        "attack_token_bits": int(scenario.attack_token_bits),
    }


def _build_stage_scenarios(
    scenarios: Sequence[ScenarioConfig],
    *,
    cycle_fraction: float,
    key_variant_count: int,
    complexity: str,
    device_mhz: float,
    provider_mhz: float,
    max_test_time_seconds: float,
) -> List[ScenarioConfig]:
    stage_scenarios: List[ScenarioConfig] = []
    fraction = max(0.05, min(1.0, float(cycle_fraction)))
    variants = max(1, int(key_variant_count))
    for scenario in scenarios:
        stage_cycles = max(6, int(round(float(scenario.cycles) * fraction)))
        for idx in range(variants):
            var_seed, label = _variant_seed(
                scenario.seed,
                idx,
                complexity=complexity,
            )
            complexity_cfg = _stage_complexity_profile(
                scenario,
                complexity=complexity,
                variant_index=idx,
            )
            stage_scenarios.append(
                replace(
                    scenario,
                    name=(
                        f"{scenario.name}:{complexity}:{label}:"
                        f"f{int(round(fraction * 100.0))}"
                    ),
                    seed=var_seed,
                    cycles=stage_cycles,
                    prime_mode=str(complexity_cfg["prime_mode"]),
                    prime_bits=int(complexity_cfg["prime_bits"]),
                    modulus_bits=int(complexity_cfg["modulus_bits"]),
                    compound_mode=str(complexity_cfg["compound_mode"]),
                    compound_count=int(complexity_cfg["compound_count"]),
                    compound_primes=int(complexity_cfg["compound_primes"]),
                    compound_offset=int(complexity_cfg["compound_offset"]),
                    compound_prime_bits=int(complexity_cfg["compound_prime_bits"]),
                    compound_pool_size=int(complexity_cfg["compound_pool_size"]),
                    attack_token_bits=int(complexity_cfg["attack_token_bits"]),
                    device_mhz=float(device_mhz),
                    provider_mhz=float(provider_mhz),
                    max_test_time_seconds=float(max_test_time_seconds),
                )
            )
    return stage_scenarios


def _subset_stage_scenarios(
    scenarios: Sequence[ScenarioConfig],
    *,
    stage: str,
) -> List[ScenarioConfig]:
    scenario_list = list(scenarios)
    total = len(scenario_list)
    if total <= 1 or stage == "full":
        return scenario_list

    if stage == "quick":
        keep = int(math.ceil(total * 0.34))
    else:
        keep = int(math.ceil(total * 0.67))
    keep = max(1, min(total, keep))
    if keep >= total:
        return scenario_list

    indices: List[int] = []
    if keep == 1:
        indices = [0]
    else:
        for pos in range(keep):
            idx = int(round((pos * (total - 1)) / float(keep - 1)))
            if idx not in indices:
                indices.append(idx)
    while len(indices) < keep:
        next_idx = len(indices)
        if next_idx >= total:
            break
        if next_idx not in indices:
            indices.append(next_idx)
    return [scenario_list[idx] for idx in indices[:keep]]


def _predictive_cut_score(score: float, stage: str, penalty: float) -> float:
    if stage == "quick":
        return (score * 0.88) - penalty
    if stage == "mid":
        return (score * 0.94) - (0.5 * penalty)
    return score


def _stage_keep_count(total: int, ratio: float, *, min_keep: int) -> int:
    count = max(0, int(total))
    if count <= 0:
        return 0
    if count == 1:
        return 1
    keep = max(int(min_keep), int(math.ceil(float(count) * float(ratio))))
    keep = min(count, keep)
    if keep >= count:
        keep = count - 1
    return max(1, keep)


def _quick_eval_limit(
    *,
    total: int,
    workers: int,
    profile: str,
) -> int:
    if total <= 2:
        return max(1, total)
    eff_workers = max(1, int(workers))
    pressure = float(total) / float(eff_workers)
    if pressure <= 1.5:
        return total

    # Reduce first-stage load when there are too many pending genomes per worker.
    frac = 0.88 - (0.18 * (pressure - 1.5))
    if str(profile).lower() == "full":
        frac -= 0.05
    frac = clamp(frac, 0.42, 0.88)
    target = int(math.ceil(float(total) * frac))
    min_budget = max(4, int(math.ceil(eff_workers * 1.80)))
    return max(min_budget, min(total, target))


def _select_quick_pending(
    pending: Sequence[Tuple[int, GFSLGenome]],
    *,
    workers: int,
    profile: str,
    archive_signatures: set[str],
) -> Tuple[List[Tuple[int, GFSLGenome]], List[Tuple[int, GFSLGenome]]]:
    total = len(pending)
    limit = _quick_eval_limit(total=total, workers=workers, profile=profile)
    if total <= limit:
        return list(pending), []

    ranked: List[Tuple[float, int, GFSLGenome]] = []
    for idx, genome in pending:
        signature = _evaluation_signature(genome)
        novelty = 1.0 if signature not in archive_signatures else 0.0
        # Stable jitter from signature + fresh jitter to avoid deterministic loops.
        stable_jitter = (int(signature[:8], 16) % 1009) / 1009.0
        jitter = (0.25 * stable_jitter) + (0.10 * random.random())
        ranked.append((novelty + jitter, idx, genome))

    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = [(idx, genome) for _, idx, genome in ranked[:limit]]
    skipped = [(idx, genome) for _, idx, genome in ranked[limit:]]
    return selected, skipped


def _throttle_quick_pending_from_previous_stats(
    *,
    quick_pending: Sequence[Tuple[int, GFSLGenome]],
    quick_skipped: Sequence[Tuple[int, GFSLGenome]],
    previous_stats: Optional[Dict[str, float]],
    workers: int,
) -> Tuple[List[Tuple[int, GFSLGenome]], List[Tuple[int, GFSLGenome]], float]:
    selected = list(quick_pending)
    skipped = list(quick_skipped)
    if not selected:
        return selected, skipped, 1.0
    if not isinstance(previous_stats, dict) or not previous_stats:
        return selected, skipped, 1.0

    prev_total_eval = (
        float(previous_stats.get("quick_eval", 0.0))
        + float(previous_stats.get("mid_eval", 0.0))
        + float(previous_stats.get("full_eval", 0.0))
        + float(previous_stats.get("probe_samples", 0.0))
    )
    prev_unique = float(previous_stats.get("eval_unique", 0.0))
    prev_cache_dup = float(previous_stats.get("cache_hits", 0.0)) + float(
        previous_stats.get("dup_reuse", 0.0)
    )
    prev_probe = float(previous_stats.get("probe_win_rate", 0.0))

    reuse_ratio = prev_cache_dup / max(1.0, prev_unique)
    uniqueness_ratio = prev_unique / max(1.0, prev_total_eval)
    if prev_probe > 0.08 or reuse_ratio < 0.75:
        return selected, skipped, 1.0

    throttle = 0.74
    if reuse_ratio >= 1.00 and uniqueness_ratio <= 0.55:
        throttle = 0.62
    if reuse_ratio >= 1.30 and uniqueness_ratio <= 0.45:
        throttle = 0.50

    min_keep = max(4, min(len(selected), int(math.ceil(max(1, int(workers)) * 1.20))))
    target = max(min_keep, int(math.ceil(float(len(selected)) * throttle)))
    if target >= len(selected):
        return selected, skipped, 1.0

    skipped.extend(selected[target:])
    throttled = selected[:target]
    applied_ratio = float(len(throttled)) / float(max(1, len(selected)))
    return throttled, skipped, applied_ratio


def _scenario_fingerprint(scenarios: Sequence[ScenarioConfig]) -> str:
    chunks: List[str] = []
    for scenario in scenarios:
        chunks.append(
            "{name}:{seed}:{cycles}:{budget}:{abs_ms}:{dev:.3f}:{prov:.3f}:{max_s:.3f}".format(
                name=scenario.name,
                seed=int(scenario.seed),
                cycles=int(scenario.cycles),
                budget=float(scenario.cycle_budget_ms),
                abs_ms=float(scenario.absolute_time_ms),
                dev=float(scenario.device_mhz),
                prov=float(scenario.provider_mhz),
                max_s=float(scenario.max_test_time_seconds),
            )
        )
    payload = "|".join(chunks).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=12).hexdigest()


def _evaluate_pending_dedup_cache_parallel(
    *,
    pending: Sequence[Tuple[int, GFSLGenome]],
    backend: str,
    workers: int,
    executor: Optional[concurrent.futures.Executor],
    worker_fn,
    build_task,
    attr_name: str,
    cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]",
    cache_key_fn,
    max_cache_entries: int,
) -> Dict[str, float]:
    stats = {
        "total": 0.0,
        "unique_eval": 0.0,
        "cache_hits": 0.0,
        "dup_reuse": 0.0,
    }
    if not pending:
        return stats

    groups: Dict[str, List[GFSLGenome]] = {}
    for _, genome in pending:
        key = str(cache_key_fn(genome))
        groups.setdefault(key, []).append(genome)
    stats["total"] = float(len(pending))

    unique_pending: List[Tuple[int, GFSLGenome]] = []
    unique_keys: List[str] = []
    for key, genomes in groups.items():
        cached = cache.get(key)
        if cached is not None:
            score, rows = cached
            cache.move_to_end(key)
            for genome in genomes:
                setattr(genome, attr_name, _metrics_from_rows(rows))
                genome.fitness = float(score)
            stats["cache_hits"] += float(len(genomes))
            continue

        unique_pending.append((0, genomes[0]))
        unique_keys.append(key)
        if len(genomes) > 1:
            stats["dup_reuse"] += float(len(genomes) - 1)

    if unique_pending:
        _evaluate_pending_parallel(
            pending=unique_pending,
            backend=backend,
            workers=workers,
            executor=executor,
            worker_fn=worker_fn,
            build_task=build_task,
            attr_name=attr_name,
        )
        stats["unique_eval"] = float(len(unique_pending))

        for (_, genome), key in zip(unique_pending, unique_keys):
            score = float(genome.fitness or -float("inf"))
            metrics = getattr(genome, attr_name, [])
            rows = _metrics_rows(metrics)
            cache[key] = (score, rows)
            while len(cache) > max_cache_entries:
                cache.popitem(last=False)

            for dup in groups[key][1:]:
                setattr(dup, attr_name, _metrics_from_rows(rows))
                dup.fitness = score

    return stats


def _rank_with_novelty(
    genomes: Sequence[GFSLGenome],
    *,
    archive_signatures: set[str],
    novelty_bonus: float,
    signature_fn=None,
) -> List[Tuple[float, GFSLGenome, str]]:
    sig_fn = _evaluation_signature if signature_fn is None else signature_fn
    ranked: List[Tuple[float, GFSLGenome, str]] = []
    local_seen: set[str] = set()
    for genome in genomes:
        signature = sig_fn(genome)
        base = float(genome.fitness or -float("inf"))
        novel = signature not in archive_signatures and signature not in local_seen
        local_seen.add(signature)
        ranked.append((base + (novelty_bonus if novel else 0.0), genome, signature))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _mark_duplicate_genomes(
    genomes: Sequence[GFSLGenome],
    *,
    stage: str,
    penalty: float,
    signature_fn=None,
) -> None:
    sig_fn = _evaluation_signature if signature_fn is None else signature_fn
    seen: Dict[str, GFSLGenome] = {}
    ordered = sorted(genomes, key=lambda g: float(g.fitness or -float("inf")), reverse=True)
    for genome in ordered:
        signature = sig_fn(genome)
        if signature not in seen:
            seen[signature] = genome
            continue
        genome.fitness = _predictive_cut_score(float(genome.fitness or -float("inf")), stage, penalty + 0.015)


def _runtime_window_stats(
    *,
    generation_log: Sequence[Dict[str, Any]],
    total_generations: int,
    score_key: str,
    target_generation_seconds: float,
) -> Optional[Dict[str, float]]:
    if not generation_log:
        return None

    total_gens = max(1, int(total_generations))
    min_gens = min(total_gens, max(6, int(math.ceil(total_gens * 0.22))))
    if len(generation_log) < min_gens:
        return None

    window = min(
        len(generation_log),
        max(4, int(math.ceil(total_gens * 0.14))),
    )
    recent = list(generation_log[-window:])
    if len(recent) < window:
        return None

    scores = [float(row.get(score_key, -float("inf"))) for row in recent]
    if not scores:
        return None

    stage_eval_total = sum(
        float(row.get("quick_eval", 0.0))
        + float(row.get("mid_eval", 0.0))
        + float(row.get("full_eval", 0.0))
        + float(row.get("probe_samples", 0.0))
        for row in recent
    )
    eval_unique_total = sum(float(row.get("eval_unique", 0.0)) for row in recent)
    cache_dup_total = sum(
        float(row.get("cache_hits", 0.0)) + float(row.get("dup_reuse", 0.0))
        for row in recent
    )
    probe_win_rate = sum(float(row.get("probe_win_rate", 0.0)) for row in recent) / float(
        max(1, len(recent))
    )
    target_secs = max(0.25, float(target_generation_seconds))
    batch_seconds = [float(row.get("batch_seconds", 0.0)) for row in recent]
    slow_batches = sum(1 for seconds in batch_seconds if seconds > target_secs)

    return {
        "window": float(window),
        "score_gain": float(max(scores) - min(scores)),
        "probe_win_rate": float(probe_win_rate),
        "target_seconds": float(target_secs),
        "avg_batch_seconds": float(sum(batch_seconds) / max(1, len(batch_seconds))),
        "slow_batches": float(slow_batches),
        "stage_eval_total": float(stage_eval_total),
        "eval_unique_total": float(eval_unique_total),
        "cache_dup_total": float(cache_dup_total),
        "reuse_ratio": float(cache_dup_total / max(1.0, eval_unique_total)),
        "uniqueness_ratio": float(eval_unique_total / max(1.0, stage_eval_total)),
    }


def _should_stop_by_runtime_stats(
    *,
    generation_log: Sequence[Dict[str, Any]],
    population: Sequence[GFSLGenome],
    total_generations: int,
    score_key: str,
    min_gain: float,
    target_generation_seconds: float,
) -> Tuple[bool, str]:
    stats = _runtime_window_stats(
        generation_log=generation_log,
        total_generations=total_generations,
        score_key=score_key,
        target_generation_seconds=target_generation_seconds,
    )
    if stats is None:
        return False, ""

    score_gain = float(stats["score_gain"])
    probe_win_rate = float(stats["probe_win_rate"])
    reuse_ratio = float(stats["reuse_ratio"])
    uniqueness_ratio = float(stats["uniqueness_ratio"])
    avg_batch_seconds = float(stats["avg_batch_seconds"])
    target_secs = float(stats["target_seconds"])
    slow_batches = float(stats["slow_batches"])
    plateau_floor = max(10, int(math.ceil(float(total_generations) * 0.35)))

    top_n = max(4, min(len(population), int(math.ceil(len(population) * 0.20))))
    top_slice = list(population[:top_n])
    if not top_slice:
        return False, ""
    top_unique_ratio = float(
        len({_evaluation_signature(genome) for genome in top_slice})
    ) / float(max(1, len(top_slice)))

    identical_floor = max(8, int(math.ceil(float(total_generations) * 0.20)))
    if len(generation_log) >= identical_floor:
        ident_window = min(
            len(generation_log),
            max(5, int(math.ceil(float(total_generations) * 0.12))),
        )
        recent_ident = list(generation_log[-ident_window:])
        fingerprints = {
            (
                round(float(row.get(score_key, 0.0)), 8),
                round(float(row.get("principle", row.get("lane_success", 0.0))), 8),
                round(float(row.get("security", row.get("token_success", 0.0))), 8),
                round(float(row.get("cost", row.get("attacker_adv", 0.0))), 8),
                round(float(row.get("attacker_adv", 0.0)), 8),
                str(row.get("stage_eval", "")),
                str(row.get("stage_keep", "")),
                round(float(row.get("quick_fraction", 0.0)), 4),
                round(float(row.get("mid_fraction", 0.0)), 4),
                round(float(row.get("quick_keep", 0.0)), 4),
                round(float(row.get("mid_keep", 0.0)), 4),
                int(row.get("key_variants", 0)),
                str(row.get("best_signature", "")),
            )
            for row in recent_ident
        }
        if len(fingerprints) == 1 and probe_win_rate <= 0.08:
            return True, "identical-generations"

    signature_floor = max(10, int(math.ceil(float(total_generations) * 0.25)))
    if len(generation_log) >= signature_floor:
        sig_window = min(
            len(generation_log),
            max(6, int(math.ceil(float(total_generations) * 0.18))),
        )
        recent_sig = list(generation_log[-sig_window:])
        score_levels = {
            round(float(row.get(score_key, 0.0)), 8)
            for row in recent_sig
        }
        signatures = {
            str(row.get("best_signature", ""))
            for row in recent_sig
            if str(row.get("best_signature", ""))
        }
        if len(score_levels) == 1 and len(signatures) == 1 and probe_win_rate <= 0.08:
            return True, "same-signature-stall"

    if (
        score_gain <= float(min_gain)
        and probe_win_rate <= 0.05
        and reuse_ratio >= 0.90
        and uniqueness_ratio <= 0.55
        and top_unique_ratio <= 0.68
    ):
        return True, "plateau-reuse"

    if (
        score_gain <= (0.45 * float(min_gain))
        and probe_win_rate <= 0.03
        and reuse_ratio >= 1.10
        and len(generation_log) >= plateau_floor
    ):
        return True, "deep-plateau-reuse"

    if (
        score_gain <= (0.50 * float(min_gain))
        and slow_batches >= max(2.0, float(stats["window"]) - 1.0)
        and reuse_ratio >= 0.75
        and avg_batch_seconds > (0.95 * target_secs)
        and len(generation_log) >= plateau_floor
    ):
        return True, "runtime-budget-pressure"

    return False, ""


def _adaptive_attacker_config_from_defender_log(
    *,
    base_config: ExperimentConfig,
    defender_log: Sequence[Dict[str, Any]],
) -> Tuple[ExperimentConfig, Dict[str, Any]]:
    stats = _runtime_window_stats(
        generation_log=defender_log,
        total_generations=int(base_config.generations),
        score_key="best_score",
        target_generation_seconds=float(base_config.target_generation_seconds),
    )
    if stats is None:
        return base_config, {}

    plateau = (
        float(stats["score_gain"]) <= 0.00050
        and float(stats["probe_win_rate"]) <= 0.05
    )
    repetition = (
        float(stats["reuse_ratio"]) >= 0.85
        and float(stats["uniqueness_ratio"]) <= 0.60
    )
    if not (plateau and repetition):
        return base_config, {}

    new_population = max(10, int(math.ceil(float(base_config.attacker_population_size) * 0.65)))
    new_generations = max(6, int(math.ceil(float(base_config.attacker_generations) * 0.62)))
    if (
        new_population >= int(base_config.attacker_population_size)
        and new_generations >= int(base_config.attacker_generations)
    ):
        return base_config, {}

    adjusted = replace(
        base_config,
        attacker_population_size=min(int(base_config.attacker_population_size), int(new_population)),
        attacker_generations=min(int(base_config.attacker_generations), int(new_generations)),
    )
    return adjusted, {
        "active": True,
        "reason": "defender-plateau-repetition",
        "reuse_ratio": float(stats["reuse_ratio"]),
        "uniqueness_ratio": float(stats["uniqueness_ratio"]),
        "score_gain": float(stats["score_gain"]),
        "probe_win_rate": float(stats["probe_win_rate"]),
        "population": int(adjusted.attacker_population_size),
        "generations": int(adjusted.attacker_generations),
    }


def _build_supervised_guide_if_available(
    config: ExperimentConfig,
    plan: ResourcePlan,
):
    if not config.use_supervised_guide or not plan.torch_available:
        return None
    try:
        from evolvo.supervised import GFSLSupervisedGuide
    except Exception:
        return None

    if config.preferred_device.lower() != "auto":
        device = config.preferred_device.lower()
    elif plan.gpu_backend != "none":
        device = plan.gpu_backend
    else:
        device = "cpu"
    try:
        return GFSLSupervisedGuide(device=device)
    except Exception:
        return None


def _create_shared_executor(plan: ResourcePlan) -> Optional[concurrent.futures.Executor]:
    if plan.parallel_backend == "off" or plan.parallel_workers <= 1:
        return None
    if plan.parallel_backend == "process":
        kwargs: Dict[str, Any] = {}
        ctx_name = _process_pool_context_name()
        if ctx_name is not None:
            try:
                kwargs["mp_context"] = multiprocessing.get_context(ctx_name)
            except Exception:
                pass
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=plan.parallel_workers,
            **kwargs,
        )
    return concurrent.futures.ThreadPoolExecutor(max_workers=plan.parallel_workers)


def _shutdown_shared_executor(executor: Optional[concurrent.futures.Executor]) -> None:
    if executor is None:
        return
    try:
        executor.shutdown(wait=True, cancel_futures=True)
    except Exception:
        pass


def _defender_eval_worker(task: Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome]]) -> Tuple[float, List[Dict[str, Any]]]:
    genome, scenarios, attacker = task
    ensure_genome_io(genome)
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
    return float(score), _metrics_rows(metrics)


def _attacker_eval_worker(task: Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig]]) -> Tuple[float, List[Dict[str, Any]]]:
    attacker, defender, scenarios = task
    ensure_attacker_genome_io(attacker)
    ensure_genome_io(defender)
    _, metrics = evaluate_across_scenarios(scenarios, defender, attacker=attacker)
    attack_adv = _mean_metric(metrics, "attacker_advantage_score")
    lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
    token_success = _mean_metric(metrics, "attacker_token_success_rate")
    effective = len(attacker.extract_effective_algorithm())
    if effective == 0:
        score = attack_adv - 0.08
    else:
        complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
        score = attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty
    return float(score), _metrics_rows(metrics)


def _defender_eval_worker_batch(
    task: Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    genomes, scenarios, attacker = task
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    results: List[Tuple[float, List[Dict[str, Any]]]] = []
    for genome in genomes:
        ensure_genome_io(genome)
        score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
        results.append((float(score), _metrics_rows(metrics)))
    return results


def _attacker_eval_worker_batch(
    task: Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    attackers, defender, scenarios = task
    ensure_genome_io(defender)
    results: List[Tuple[float, List[Dict[str, Any]]]] = []
    for attacker in attackers:
        ensure_attacker_genome_io(attacker)
        _, metrics = evaluate_across_scenarios(scenarios, defender, attacker=attacker)
        attack_adv = _mean_metric(metrics, "attacker_advantage_score")
        lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
        token_success = _mean_metric(metrics, "attacker_token_success_rate")
        effective = len(attacker.extract_effective_algorithm())
        if effective == 0:
            score = attack_adv - 0.08
        else:
            complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
            score = attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty
        results.append((float(score), _metrics_rows(metrics)))
    return results


def _evaluate_pending_parallel(
    *,
    pending: Sequence[Tuple[int, GFSLGenome]],
    backend: str,
    workers: int,
    executor: Optional[concurrent.futures.Executor],
    worker_fn,
    build_task,
    attr_name: str,
) -> None:
    if not pending:
        return

    if backend == "off" or workers <= 1:
        for _, genome in pending:
            score, rows = worker_fn(build_task(genome))
            setattr(genome, attr_name, _metrics_from_rows(rows))
            genome.fitness = float(score)
        return

    tasks = [build_task(genome) for _, genome in pending]
    map_chunk_size = max(1, len(tasks) // max(1, workers * 2))

    def run_with_executor(exec_obj: concurrent.futures.Executor, fn, task_list):
        if isinstance(exec_obj, concurrent.futures.ProcessPoolExecutor):
            return list(exec_obj.map(fn, task_list, chunksize=map_chunk_size))
        return list(exec_obj.map(fn, task_list))

    def assign(results: Sequence[Tuple[float, List[Dict[str, Any]]]]) -> None:
        for (_, genome), (score, rows) in zip(pending, results):
            setattr(genome, attr_name, _metrics_from_rows(rows))
            genome.fitness = float(score)

    # Process backend benefits from batch-chunked tasks to reduce pickle/IPC overhead.
    use_batch_chunks = (
        backend == "process"
        and workers > 1
        and len(tasks) >= max(6, workers)
        and worker_fn in {_defender_eval_worker, _attacker_eval_worker}
    )
    if use_batch_chunks:
        batch_worker = _defender_eval_worker_batch if worker_fn is _defender_eval_worker else _attacker_eval_worker_batch
        batch_size = max(4, int(math.ceil(len(tasks) / float(max(1, workers)))))
        batch_size = min(32, batch_size)
        task_chunks = [tasks[pos : pos + batch_size] for pos in range(0, len(tasks), batch_size)]
        if worker_fn is _defender_eval_worker:
            batch_tasks = [
                ([item[0] for item in chunk], chunk[0][1], chunk[0][2])
                for chunk in task_chunks
            ]
        else:
            batch_tasks = [
                ([item[0] for item in chunk], chunk[0][1], chunk[0][2])
                for chunk in task_chunks
            ]

        if executor is not None:
            chunk_results = run_with_executor(executor, batch_worker, batch_tasks)
            flat_results = [item for chunk in chunk_results for item in chunk]
            assign(flat_results)
            return

        kwargs: Dict[str, Any] = {}
        ctx_name = _process_pool_context_name()
        if ctx_name is not None:
            try:
                kwargs["mp_context"] = multiprocessing.get_context(ctx_name)
            except Exception:
                pass
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, **kwargs) as local_exec:
            chunk_results = run_with_executor(local_exec, batch_worker, batch_tasks)
        flat_results = [item for chunk in chunk_results for item in chunk]
        assign(flat_results)
        return

    def run_standard(exec_obj: concurrent.futures.Executor):
        return run_with_executor(exec_obj, worker_fn, tasks)

    if executor is not None:
        results = run_standard(executor)
        assign(results)
        return

    if backend == "process":
        kwargs: Dict[str, Any] = {}
        ctx_name = _process_pool_context_name()
        if ctx_name is not None:
            try:
                kwargs["mp_context"] = multiprocessing.get_context(ctx_name)
            except Exception:
                pass
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, **kwargs) as local_exec:
            results = run_standard(local_exec)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as local_exec:
            results = run_standard(local_exec)

    assign(results)


def _mean_metric(metrics: Sequence[ScenarioMetrics], attr: str) -> float:
    if not metrics:
        return 0.0
    return sum(float(getattr(item, attr)) for item in metrics) / float(len(metrics))


def _make_random_genome(initial_instructions: int, *, slot_count: Optional[int] = None) -> GFSLGenome:
    genome = GFSLGenome("algorithm", slot_count=slot_count)
    for _ in range(random.randint(1, max(1, int(initial_instructions)))):
        if _append_random_instruction_fast(genome, max_attempts=6):
            continue
        try:
            genome.add_instruction_interactive(max_attempts=8)
        except RuntimeError:
            break
    genome.rebuild_validator_state()
    _invalidate_genome_caches(genome)
    return genome


def _metrics_rows(metrics: Sequence[ScenarioMetrics]) -> List[Dict[str, Any]]:
    return [metric.to_dict() for metric in metrics]


def _scenario_table(metrics: Sequence[ScenarioMetrics]) -> str:
    lines = []
    lines.append("| scenario | total | principle | sync | security | cost | op-cost | attacker-adv |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        lines.append(
            "| {name} | {total:.4f} | {principle:.4f} | {sync:.4f} | {security:.4f} | {cost:.4f} | {op_cost:.4f} | {attack_adv:.4f} |".format(
                name=metric.scenario,
                total=metric.total_score,
                principle=metric.principle_score,
                sync=metric.sync_score,
                security=metric.security_score,
                cost=metric.cost_score,
                op_cost=metric.operation_cost_score,
                attack_adv=metric.attacker_advantage_score,
            )
        )
    return "\n".join(lines)


def _prune_to_effective(genome: GFSLGenome) -> GFSLGenome:
    cloned = copy.deepcopy(genome)
    effective = set(cloned.extract_effective_algorithm())
    if not effective:
        cloned.rebuild_validator_state()
        return cloned

    new_instructions = []
    new_activity = []
    for idx, instruction in enumerate(cloned.instructions):
        if idx in effective:
            new_instructions.append(instruction.copy())
            if idx < len(cloned.instruction_activity):
                new_activity.append(copy.deepcopy(cloned.instruction_activity[idx]))

    cloned.instructions = new_instructions
    cloned.instruction_activity = new_activity
    cloned.rebuild_validator_state()
    return cloned


def _canonical_signature(genome: GFSLGenome) -> str:
    pruned = _prune_to_effective(genome)
    fixed_indices = pruned.extract_operation_indices(order="fixed")
    parts = [pruned.instructions[idx].get_signature() for idx in fixed_indices]
    outputs = sorted((int(cat), int(dtype), int(idx)) for cat, dtype, idx in pruned.outputs)
    payload = "|".join(parts) + "|outs=" + json.dumps(outputs, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def _evaluation_signature(genome: GFSLGenome) -> str:
    """Fast fingerprint used in hot evaluation paths.

    This intentionally avoids expensive canonical pruning/deep-copy so staged
    dedup and novelty checks stay cheap during generation loops.
    """
    base_sig = str(genome.get_signature())
    outputs_key = tuple(
        sorted((int(cat), int(dtype), int(idx)) for cat, dtype, idx in genome.outputs)
    )
    cache_key = (base_sig, outputs_key)
    cached_key = getattr(genome, "_pcpl_eval_sig_key", None)
    if cached_key == cache_key:
        cached = getattr(genome, "_pcpl_eval_sig", None)
        if isinstance(cached, str):
            return cached

    if outputs_key:
        outs_blob = ";".join(
            f"{cat}:{dtype}:{idx}" for cat, dtype, idx in outputs_key
        )
    else:
        outs_blob = ""
    payload = f"{base_sig}|outs={outs_blob}"
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
    genome._pcpl_eval_sig_key = cache_key  # type: ignore[attr-defined]
    genome._pcpl_eval_sig = digest  # type: ignore[attr-defined]
    return digest


def _serialize_genome(genome: GFSLGenome, *, role: str) -> Dict[str, Any]:
    pruned = _prune_to_effective(genome)
    return {
        "role": role,
        "genome_type": pruned.genome_type,
        "slot_count": int(pruned.validator.slot_count),
        "outputs": [
            [int(cat), int(dtype), int(idx)] for cat, dtype, idx in pruned.outputs
        ],
        "instructions": [
            {
                "slots": [int(slot) for slot in instruction.slots],
                "weight": instruction.weight,
            }
            for instruction in pruned.instructions
        ],
        "signature": pruned.get_signature(),
        "canonical_signature": _canonical_signature(pruned),
        "effective_size": len(pruned.extract_effective_algorithm()),
    }


def _deserialize_genome(payload: Dict[str, Any]) -> GFSLGenome:
    slot_count = int(payload.get("slot_count", 7))
    genome = GFSLGenome(payload.get("genome_type", "algorithm"), slot_count=slot_count)
    instructions = payload.get("instructions", [])
    genome.instructions = []
    for item in instructions:
        slots = [int(slot) for slot in item.get("slots", [])]
        if not slots:
            continue
        instruction = GFSLInstruction(
            slots=slots,
            slot_count=len(slots),
            weight=item.get("weight"),
        )
        genome.instructions.append(instruction)

    outputs = payload.get("outputs", [])
    genome.outputs = [
        (int(cat), int(dtype), int(idx)) for cat, dtype, idx in outputs
    ]
    genome.rebuild_validator_state()
    return genome


def _load_archive(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "defender_elites": [],
            "attacker_elites": [],
            "rounds": [],
            "predictive_profile": {},
            "updated_at": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "defender_elites": [],
            "attacker_elites": [],
            "rounds": [],
            "predictive_profile": {},
            "updated_at": None,
        }
    payload.setdefault("defender_elites", [])
    payload.setdefault("attacker_elites", [])
    payload.setdefault("rounds", [])
    payload.setdefault("predictive_profile", {})
    payload.setdefault("updated_at", None)
    return payload


def _save_archive(path: Path, archive: Dict[str, Any]) -> None:
    archive["updated_at"] = _utc_now_iso()
    path.write_text(json.dumps(archive, indent=2), encoding="utf-8")


def _insert_elite(
    entries: List[Dict[str, Any]],
    record: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    genome_payload = record.get("genome", {})
    if int(genome_payload.get("effective_size", 0)) <= 0:
        return entries

    canonical = str(record["canonical_signature"])
    by_signature: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        key = str(entry["canonical_signature"])
        existing = by_signature.get(key)
        if existing is None or float(entry["score"]) > float(existing["score"]):
            by_signature[key] = entry

    existing = by_signature.get(canonical)
    if existing is None or float(record["score"]) > float(existing["score"]):
        by_signature[canonical] = record

    ordered = sorted(
        by_signature.values(),
        key=lambda item: float(item["score"]),
        reverse=True,
    )
    return ordered[: max(1, int(limit))]


def _seed_population_from_archive(
    *,
    evolver: SafeGFSLEvolver,
    archive_elites: Sequence[Dict[str, Any]],
    io_initializer,
    population_size: int,
    initial_instructions: int,
    elite_pool: int,
) -> None:
    evolver.initialize_population("algorithm", initial_instructions=initial_instructions)
    seeded: List[GFSLGenome] = []
    seen: set[str] = set()

    def push(genome: GFSLGenome) -> bool:
        io_initializer(genome)
        signature = _evaluation_signature(genome)
        if signature in seen:
            return False
        seen.add(signature)
        seeded.append(genome)
        return True

    for entry in archive_elites[: max(1, elite_pool)]:
        try:
            genome = _deserialize_genome(entry["genome"])
            push(genome)
        except Exception:
            continue

    # Add random mutations from elites so each round explores around known tops.
    elite_mutation_parents = seeded[:]
    for parent in elite_mutation_parents:
        if len(seeded) >= population_size:
            break
        try:
            mutated = evolver.mutate(copy.deepcopy(parent))
            push(mutated)
        except Exception:
            continue

    for genome in evolver.population:
        if len(seeded) >= population_size:
            break
        push(genome)

    while len(seeded) < population_size:
        fallback = copy.deepcopy(random.choice(evolver.population))
        if push(fallback):
            continue
        added = False
        for _ in range(12):
            try:
                mutated = evolver.mutate(copy.deepcopy(fallback))
                if push(mutated):
                    added = True
                    break
            except Exception:
                continue
        if added:
            continue
        try:
            random_genome = _make_random_genome(max(4, initial_instructions))
            if not push(random_genome):
                io_initializer(random_genome)
                seeded.append(random_genome)
        except Exception:
            fallback_any = copy.deepcopy(random.choice(evolver.population))
            io_initializer(fallback_any)
            seeded.append(fallback_any)

    evolver.population = seeded[:population_size]


def _build_round_report(
    *,
    config: ExperimentConfig,
    round_index: int,
    scenarios: Sequence[ScenarioConfig],
    defender_score: float,
    defender_signature: str,
    defender_metrics: Sequence[ScenarioMetrics],
    attacker_score: float,
    attacker_signature: str,
    defender_log: Sequence[Dict[str, Any]],
    attacker_log: Sequence[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append("# PCPL Evolvo Continuous Round")
    lines.append("")
    lines.append(f"- round: `{round_index}`")
    lines.append(f"- profile: `{config.profile}`")
    lines.append(f"- defender score: `{defender_score:.6f}`")
    lines.append(f"- defender signature: `{defender_signature}`")
    lines.append(f"- attacker score: `{attacker_score:.6f}`")
    lines.append(f"- attacker signature: `{attacker_signature}`")
    lines.append("")
    lines.append("## Scenarios")
    for scenario in scenarios:
        lines.append(
            "- `{name}`: x={x}, cycles={cycles}, budget_ms={budget}, abs_ref_ms={abs_ms}, dev_mhz={dev_mhz}, prov_mhz={prov_mhz}, max_test_s={max_s}".format(
                name=scenario.name,
                x=scenario.x,
                cycles=scenario.cycles,
                budget=scenario.cycle_budget_ms,
                abs_ms=scenario.absolute_time_ms,
                dev_mhz=scenario.device_mhz,
                prov_mhz=scenario.provider_mhz,
                max_s=scenario.max_test_time_seconds,
            )
        )
    lines.append("")
    lines.append("## Defender Metrics")
    lines.append("")
    lines.append(_scenario_table(defender_metrics))
    lines.append("")
    lines.append("## Defender Evolution")
    lines.append("")
    lines.append("| gen | best | principle | security | cost | sync-loss | attacker-adv | q frac/keep | m frac/keep | key var | probe | q-thr | eval q>m>f | keep q>m | probes | qskip | uniq | cache | dup | t(s) |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in defender_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {security:.4f} | {cost:.4f} | {sync_loss:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {qthr:.2f} | {stage_eval} | {stage_keep} | {probe_n} | {qskip} | {uniq} | {cache} | {dup} | {secs:.2f} |".format(
                generation=row["generation"],
                best_score=row["best_score"],
                principle=row["principle"],
                security=row["security"],
                cost=row["cost"],
                sync_loss=row["sync_loss"],
                attacker_adv=row["attacker_adv"],
                qf=float(row.get("quick_fraction", 0.0)),
                qk=float(row.get("quick_keep", 0.0)),
                mf=float(row.get("mid_fraction", 0.0)),
                mk=float(row.get("mid_keep", 0.0)),
                kv=int(row.get("key_variants", 0)),
                probe=float(row.get("probe_win_rate", 0.0)),
                qthr=float(row.get("quick_throttle", 1.0)),
                stage_eval=str(row.get("stage_eval", "0>0>0")),
                stage_keep=str(row.get("stage_keep", "0>0")),
                probe_n=int(row.get("probe_samples", 0)),
                qskip=int(row.get("quick_skipped", 0)),
                uniq=int(row.get("eval_unique", 0)),
                cache=int(row.get("cache_hits", 0)),
                dup=int(row.get("dup_reuse", 0)),
                secs=float(row.get("batch_seconds", 0.0)),
            )
        )
    lines.append("")
    lines.append("## Attacker Evolution")
    lines.append("")
    lines.append("| gen | attack_score | lane_success | token_success | attacker_adv | q frac/keep | m frac/keep | key var | probe | q-thr | eval q>m>f | keep q>m | probes | qskip | uniq | cache | dup | t(s) |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in attacker_log:
        lines.append(
            "| {generation} | {attack_score:.4f} | {lane_success:.4f} | {token_success:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {qthr:.2f} | {stage_eval} | {stage_keep} | {probe_n} | {qskip} | {uniq} | {cache} | {dup} | {secs:.2f} |".format(
                generation=row["generation"],
                attack_score=row["attack_score"],
                lane_success=row["lane_success"],
                token_success=row["token_success"],
                attacker_adv=row["attacker_adv"],
                qf=float(row.get("quick_fraction", 0.0)),
                qk=float(row.get("quick_keep", 0.0)),
                mf=float(row.get("mid_fraction", 0.0)),
                mk=float(row.get("mid_keep", 0.0)),
                kv=int(row.get("key_variants", 0)),
                probe=float(row.get("probe_win_rate", 0.0)),
                qthr=float(row.get("quick_throttle", 1.0)),
                stage_eval=str(row.get("stage_eval", "0>0>0")),
                stage_keep=str(row.get("stage_keep", "0>0")),
                probe_n=int(row.get("probe_samples", 0)),
                qskip=int(row.get("quick_skipped", 0)),
                uniq=int(row.get("eval_unique", 0)),
                cache=int(row.get("cache_hits", 0)),
                dup=int(row.get("dup_reuse", 0)),
                secs=float(row.get("batch_seconds", 0.0)),
            )
        )
    return "\n".join(lines) + "\n"


def _baseline_rows(scenarios: Sequence[ScenarioConfig]) -> List[Dict[str, Any]]:
    baselines = [
        (
            "reference-full",
            PolicyDecision(
                active_ratio=1.0,
                kernel=0,
                stride_seed=0,
                state_mix=0.5,
                exponent_mix=0.5,
                hash_rounds=2,
                bouquet_spread=0.5,
                state_churn=0.5,
                lane_salt=0,
                token_scramble=0.25,
                phase_jitter=0.25,
            ),
        ),
        (
            "balanced",
            PolicyDecision(
                active_ratio=0.65,
                kernel=1,
                stride_seed=19,
                state_mix=0.5,
                exponent_mix=0.5,
                hash_rounds=2,
                bouquet_spread=0.6,
                state_churn=0.45,
                lane_salt=23,
                token_scramble=0.20,
                phase_jitter=0.20,
            ),
        ),
        (
            "minimal-cost",
            PolicyDecision(
                active_ratio=0.25,
                kernel=2,
                stride_seed=31,
                state_mix=0.45,
                exponent_mix=0.35,
                hash_rounds=1,
                bouquet_spread=0.3,
                state_churn=0.25,
                lane_salt=7,
                token_scramble=0.10,
                phase_jitter=0.10,
            ),
        ),
    ]
    rows: List[Dict[str, Any]] = []
    for name, policy in baselines:
        score, metrics = evaluate_across_scenarios(scenarios, None, fixed_decision=policy)
        rows.append({
            "name": name,
            "mean_score": score,
            "metrics": _metrics_rows(metrics),
        })
    return rows


def _leaderboard_markdown(entries: Sequence[Dict[str, Any]], *, title: str) -> str:
    lines = [f"# {title}", "", "| rank | score | round | signature | effective |", "| ---: | ---: | ---: | --- | ---: |"]
    for idx, entry in enumerate(entries, start=1):
        genome = entry.get("genome", {})
        lines.append(
            "| {rank} | {score:.6f} | {round_idx} | `{sig}` | {effective} |".format(
                rank=idx,
                score=float(entry.get("score", 0.0)),
                round_idx=int(entry.get("round", -1)),
                sig=str(entry.get("signature", "")),
                effective=int(genome.get("effective_size", 0)),
            )
        )
    return "\n".join(lines) + "\n"


def _mean_metrics_row(metrics_rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not metrics_rows:
        return {}
    keys = [k for k, v in metrics_rows[0].items() if isinstance(v, (float, int))]
    means: Dict[str, float] = {}
    for key in keys:
        values = [float(row.get(key, 0.0)) for row in metrics_rows]
        means[key] = sum(values) / float(max(1, len(values)))
    return means


def _write_view_outputs(
    *,
    out_dir: Path,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    archive: Dict[str, Any],
    baseline_rows: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    views_dir = out_dir / "views"
    best_dir = out_dir / "best"
    leaderboards_dir = out_dir / "leaderboards"
    summaries_dir = out_dir / "summaries"
    for path in (views_dir, best_dir, leaderboards_dir, summaries_dir):
        path.mkdir(parents=True, exist_ok=True)

    defender_top = list(archive.get("defender_elites", []))
    attacker_top = list(archive.get("attacker_elites", []))

    defender_top10 = defender_top[:10]
    attacker_top10 = attacker_top[:10]
    (leaderboards_dir / "defender-top10.json").write_text(
        json.dumps(defender_top10, indent=2),
        encoding="utf-8",
    )
    (leaderboards_dir / "attacker-top10.json").write_text(
        json.dumps(attacker_top10, indent=2),
        encoding="utf-8",
    )
    (leaderboards_dir / "defender-top10.md").write_text(
        _leaderboard_markdown(defender_top10, title="Defender Top 10"),
        encoding="utf-8",
    )
    (leaderboards_dir / "attacker-top10.md").write_text(
        _leaderboard_markdown(attacker_top10, title="Attacker Top 10"),
        encoding="utf-8",
    )

    best_defender = defender_top10[:1]
    best_attacker = attacker_top10[:1]
    best_paths: Dict[str, str] = {}
    if best_defender:
        defender_entry = best_defender[0]
        defender_genome = _deserialize_genome(defender_entry["genome"])
        defender_path = best_dir / "best-defender-genome.txt"
        defender_path.write_text(
            "\n".join(defender_genome.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (best_dir / "best-defender.json").write_text(
            json.dumps(defender_entry, indent=2),
            encoding="utf-8",
        )
        best_paths["best_defender_genome"] = str(defender_path)

    if best_attacker:
        attacker_entry = best_attacker[0]
        attacker_genome = _deserialize_genome(attacker_entry["genome"])
        attacker_path = best_dir / "best-attacker-genome.txt"
        attacker_path.write_text(
            "\n".join(attacker_genome.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (best_dir / "best-attacker.json").write_text(
            json.dumps(attacker_entry, indent=2),
            encoding="utf-8",
        )
        best_paths["best_attacker_genome"] = str(attacker_path)

    conclusion_lines: List[str] = []
    conclusion_lines.append("# PCPL Evolvo Conclusions")
    conclusion_lines.append("")
    conclusion_lines.append(f"- profile: `{config.profile}`")
    conclusion_lines.append(f"- rounds completed: `{len(archive.get('rounds', []))}`")
    conclusion_lines.append(f"- backend: `{resource_plan.parallel_backend}`")
    conclusion_lines.append(f"- workers: `{resource_plan.parallel_workers}`")
    conclusion_lines.append(f"- gpu backend: `{resource_plan.gpu_backend}`")
    conclusion_lines.append("")
    if best_defender:
        defender_mean = _mean_metrics_row(best_defender[0].get("metrics", []))
        conclusion_lines.append("## Best Defender Summary")
        conclusion_lines.append(
            "- score={score:.6f}, principle={principle:.4f}, security={security:.4f}, sync={sync:.4f}, cost={cost:.4f}".format(
                score=float(best_defender[0].get("score", 0.0)),
                principle=float(defender_mean.get("principle_score", 0.0)),
                security=float(defender_mean.get("security_score", 0.0)),
                sync=float(defender_mean.get("sync_score", 0.0)),
                cost=float(defender_mean.get("cost_score", 0.0)),
            )
        )
        conclusion_lines.append(
            "- brute_force_resistance={bf:.4f}, reverse_hack_resistance={rh:.4f}, sync_loss={sl:.4f}, projected_sync_loss_10s={psl:.4f}, horizon_sync={hs:.4f}".format(
                bf=float(defender_mean.get("brute_force_resistance_score", 0.0)),
                rh=float(defender_mean.get("reverse_hack_resistance_score", 0.0)),
                sl=float(defender_mean.get("sync_loss_rate", 0.0)),
                psl=float(defender_mean.get("projected_sync_loss_rate", 0.0)),
                hs=float(defender_mean.get("horizon_sync_score", 0.0)),
            )
        )
        conclusion_lines.append("")
    if best_attacker:
        attacker_mean = _mean_metrics_row(best_attacker[0].get("metrics", []))
        conclusion_lines.append("## Strongest Attacker Summary")
        conclusion_lines.append(
            "- score={score:.6f}, lane_success={lane:.4f}, token_success={token:.4f}, attacker_adv={adv:.4f}".format(
                score=float(best_attacker[0].get("score", 0.0)),
                lane=float(attacker_mean.get("attacker_lane_success_rate", 0.0)),
                token=float(attacker_mean.get("attacker_token_success_rate", 0.0)),
                adv=float(attacker_mean.get("attacker_advantage_score", 0.0)),
            )
        )
        conclusion_lines.append("")
    conclusion_lines.append("## Baseline Means")
    conclusion_lines.append("")
    for row in baseline_rows:
        conclusion_lines.append(
            "- `{name}`: {score:.4f}".format(
                name=str(row["name"]),
                score=float(row["mean_score"]),
            )
        )
    conclusion_path = summaries_dir / "conclusions.md"
    conclusion_path.write_text("\n".join(conclusion_lines) + "\n", encoding="utf-8")

    index_lines: List[str] = []
    index_lines.append("# PCPL Evolvo Run Index")
    index_lines.append("")
    index_lines.append("## Quick Links")
    index_lines.append(f"- conclusions: `{conclusion_path}`")
    index_lines.append(f"- defender leaderboard: `{leaderboards_dir / 'defender-top10.md'}`")
    index_lines.append(f"- attacker leaderboard: `{leaderboards_dir / 'attacker-top10.md'}`")
    if "best_defender_genome" in best_paths:
        index_lines.append(f"- best defender genome: `{best_paths['best_defender_genome']}`")
    if "best_attacker_genome" in best_paths:
        index_lines.append(f"- best attacker genome: `{best_paths['best_attacker_genome']}`")
    index_lines.append("")
    index_lines.append("## Output Layout")
    index_lines.append("- `rounds/`: per-round artifacts and reports")
    index_lines.append("- `best/`: best genome snapshots + metadata")
    index_lines.append("- `leaderboards/`: top ranked defenders/attackers")
    index_lines.append("- `summaries/`: final conclusions and compact overview")
    index_path = views_dir / "index.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    return {
        "index_path": str(index_path),
        "conclusion_path": str(conclusion_path),
        "defender_leaderboard_path": str(leaderboards_dir / "defender-top10.md"),
        "attacker_leaderboard_path": str(leaderboards_dir / "attacker-top10.md"),
        **best_paths,
    }


def _run_defender_round(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    shared_executor: Optional[concurrent.futures.Executor],
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    attacker: Optional[GFSLGenome],
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(config, resource_plan)
    evolver = SafeGFSLEvolver(
        population_size=config.population_size,
        supervised_guide=guide,
        parent_pool_ratio=config.parent_pool_ratio,
        stagnation_patience=config.stagnation_patience,
        mutation_floor=config.mutation_floor,
        mutation_ceiling=config.mutation_ceiling,
        mutation_step=config.mutation_step,
    )
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=archive.get("defender_elites", []),
        io_initializer=ensure_genome_io,
        population_size=config.population_size,
        initial_instructions=config.initial_instructions,
        elite_pool=config.elite_pool,
    )

    generation_log: List[Dict[str, Any]] = []
    archive_signatures = {
        str(entry.get("signature") or entry.get("canonical_signature"))
        for entry in archive.get("defender_elites", [])
        if (entry.get("signature") or entry.get("canonical_signature"))
    }
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    controller = PredictiveStageController.from_config(config)
    controller = _seed_controller_from_payload(
        controller,
        archive.get("predictive_profile", {}).get("defender"),
    )
    stage_stats: Dict[str, float] = {}
    stage_cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
    cache_limit = max(500, int(config.max_eval_cache_entries))
    attacker_signature = _evaluation_signature(attacker) if attacker is not None else "none"

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(1, controller.key_variant_count))
            complexity = "quick"
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(1, controller.key_variant_count))
            complexity = "mid"
        else:
            frac = min(1.0, max(0.50, controller.mid_cycle_fraction + 0.08))
            key_variants = max(1, controller.key_variant_count)
            complexity = "hard"
        stage_scenarios = _build_stage_scenarios(
            scenarios,
            cycle_fraction=frac,
            key_variant_count=key_variants,
            complexity=complexity,
            device_mhz=config.device_mhz,
            provider_mhz=config.provider_mhz,
            max_test_time_seconds=config.max_test_time_seconds,
        )
        return _subset_stage_scenarios(stage_scenarios, stage=stage)

    def fitness(genome: GFSLGenome) -> float:
        ensure_genome_io(genome)
        full_scenarios = make_scenarios("full")
        score, metrics = evaluate_across_scenarios(full_scenarios, genome, attacker=attacker)
        genome._pcpl_metrics = metrics  # type: ignore[attr-defined]
        return score

    def progress(gen: int, best: GFSLGenome, best_fitness: float) -> None:
        metrics = getattr(best, "_pcpl_metrics", None)
        if metrics is None:
            full_scenarios = make_scenarios("full")
            _, metrics = evaluate_across_scenarios(full_scenarios, best, attacker=attacker)
            best._pcpl_metrics = metrics

        row = {
            "generation": int(gen),
            "best_score": float(best_fitness),
            "best_signature": _evaluation_signature(best),
            "principle": _mean_metric(metrics, "principle_score"),
            "security": _mean_metric(metrics, "security_score"),
            "cost": _mean_metric(metrics, "cost_score"),
            "sync_loss": _mean_metric(metrics, "sync_loss_rate"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
            "quick_fraction": float(controller.quick_cycle_fraction),
            "mid_fraction": float(controller.mid_cycle_fraction),
            "quick_keep": float(controller.quick_keep_ratio),
            "mid_keep": float(controller.mid_keep_ratio),
            "key_variants": int(controller.key_variant_count),
            "probe_win_rate": float(stage_stats.get("probe_win_rate", 0.0)),
            "quick_throttle": float(stage_stats.get("quick_throttle", 1.0)),
            "stage_eval": "{q}>{m}>{f}".format(
                q=int(stage_stats.get("quick_eval", 0.0)),
                m=int(stage_stats.get("mid_eval", 0.0)),
                f=int(stage_stats.get("full_eval", 0.0)),
            ),
            "stage_keep": "{q}>{m}".format(
                q=int(stage_stats.get("quick_kept", 0.0)),
                m=int(stage_stats.get("mid_kept", 0.0)),
            ),
            "quick_eval": int(stage_stats.get("quick_eval", 0.0)),
            "mid_eval": int(stage_stats.get("mid_eval", 0.0)),
            "full_eval": int(stage_stats.get("full_eval", 0.0)),
            "quick_kept_count": int(stage_stats.get("quick_kept", 0.0)),
            "mid_kept_count": int(stage_stats.get("mid_kept", 0.0)),
            "probe_samples": int(stage_stats.get("probe_samples", 0.0)),
            "quick_skipped": int(stage_stats.get("quick_skipped", 0.0)),
            "eval_unique": int(stage_stats.get("eval_unique", 0.0)),
            "cache_hits": int(stage_stats.get("cache_hits", 0.0)),
            "dup_reuse": int(stage_stats.get("dup_reuse", 0.0)),
            "batch_seconds": float(stage_stats.get("batch_seconds", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][defender] gen={gen:03d} score={score:.5f} sec={security:.4f} cost={cost:.4f} attack_adv={attack_adv:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} t={secs:.2f}s".format(
                gen=gen,
                score=best_fitness,
                security=row["security"],
                cost=row["cost"],
                attack_adv=row["attacker_adv"],
                qf=row["quick_fraction"],
                qk=row["quick_keep"],
                mf=row["mid_fraction"],
                mk=row["mid_keep"],
                kv=row["key_variants"],
                probe=row["probe_win_rate"],
                qt=row["quick_throttle"],
                stage_eval=row["stage_eval"],
                stage_keep=row["stage_keep"],
                probe_n=row["probe_samples"],
                qskip=row["quick_skipped"],
                uniq=row["eval_unique"],
                cache=row["cache_hits"],
                dup=row["dup_reuse"],
                secs=row["batch_seconds"],
            )
        )

    def should_stop(
        gen: int,
        best: GFSLGenome,
        best_fitness: float,
        population: Sequence[GFSLGenome],
    ) -> Tuple[bool, str]:
        _ = (gen, best, best_fitness)
        return _should_stop_by_runtime_stats(
            generation_log=generation_log,
            population=population,
            total_generations=int(config.generations),
            score_key="best_score",
            min_gain=0.00035,
            target_generation_seconds=float(config.target_generation_seconds),
        )

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, genome)
            for idx, genome in enumerate(population)
            if genome.fitness is None
        ]
        if not pending:
            return

        eval_started = time.perf_counter()
        local_stage: Dict[str, float] = {
            "population": float(len(pending)),
            "quick_eval": float(len(pending)),
            "mid_eval": 0.0,
            "full_eval": 0.0,
            "quick_kept": 0.0,
            "mid_kept": 0.0,
            "probe_samples": 0.0,
            "probe_wins": 0.0,
            "novelty_quick": 0.0,
            "novelty_mid": 0.0,
            "eval_unique": 0.0,
            "cache_hits": 0.0,
            "dup_reuse": 0.0,
            "quick_throttle": 1.0,
            "target_batch_seconds": float(config.target_generation_seconds),
            "batch_seconds": 0.0,
        }

        if config.statistical_predictive and config.auto_statistical_tuning:
            # Preemptively tighten staged load when pending genomes outnumber workers.
            pressure = float(len(pending)) / float(max(1, resource_plan.parallel_workers))
            if pressure > 2.0:
                factor = min(2.0, pressure - 2.0)
                controller.quick_keep_ratio -= 0.06 * factor
                controller.mid_keep_ratio -= 0.05 * factor
                controller.quick_cycle_fraction -= 0.035 * factor
                controller.mid_cycle_fraction -= 0.028 * factor
                if pressure > 3.0 and controller.key_variant_count > 1:
                    controller.key_variant_count -= 1
                controller.clamp()

        if not config.statistical_predictive:
            full_scenarios = make_scenarios("full")
            full_fp = _scenario_fingerprint(full_scenarios)
            eval_stats = _evaluate_pending_dedup_cache_parallel(
                pending=pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    attacker,
                ),
                attr_name="_pcpl_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "def:full:{sf}:{att}:{sig}".format(
                    sf=sf,
                    att=attacker_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
            )
            local_stage["full_eval"] = float(eval_stats["total"])
            local_stage["quick_kept"] = float(eval_stats["total"])
            local_stage["mid_kept"] = float(eval_stats["total"])
            local_stage["eval_unique"] += float(eval_stats["unique_eval"])
            local_stage["cache_hits"] += float(eval_stats["cache_hits"])
            local_stage["dup_reuse"] += float(eval_stats["dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            stage_stats.clear()
            stage_stats.update(local_stage)
            return

        quick_scenarios = make_scenarios("quick")
        mid_scenarios = make_scenarios("mid")
        full_scenarios = make_scenarios("full")

        quick_pending, quick_skipped = _select_quick_pending(
            pending,
            workers=resource_plan.parallel_workers,
            profile=config.profile,
            archive_signatures=archive_signatures,
        )
        quick_pending, quick_skipped, quick_throttle = _throttle_quick_pending_from_previous_stats(
            quick_pending=quick_pending,
            quick_skipped=quick_skipped,
            previous_stats=stage_stats if stage_stats else None,
            workers=resource_plan.parallel_workers,
        )
        local_stage["quick_throttle"] = float(quick_throttle)
        local_stage["quick_eval"] = float(len(quick_pending))
        local_stage["quick_skipped"] = float(len(quick_skipped))
        for _, skipped_genome in quick_skipped:
            skipped_sig = _evaluation_signature(skipped_genome)
            novelty_offset = 0.015 if skipped_sig not in archive_signatures else 0.0
            skipped_genome.fitness = _predictive_cut_score(
                -0.20 + novelty_offset,
                "quick",
                float(config.predictive_penalty) + 0.01,
            )

        # Stage 1: fast statistical screen.
        quick_fp = _scenario_fingerprint(quick_scenarios)
        quick_stats = _evaluate_pending_dedup_cache_parallel(
            pending=quick_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                quick_scenarios,
                attacker,
            ),
            attr_name="_pcpl_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=quick_fp: "def:quick:{sf}:{att}:{sig}".format(
                sf=sf,
                att=attacker_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["eval_unique"] += float(quick_stats["unique_eval"])
        local_stage["cache_hits"] += float(quick_stats["cache_hits"])
        local_stage["dup_reuse"] += float(quick_stats["dup_reuse"])
        pending_genomes = [genome for _, genome in pending]
        _mark_duplicate_genomes(
            pending_genomes,
            stage="quick",
            penalty=config.predictive_penalty,
        )
        ranked_quick = _rank_with_novelty(
            pending_genomes,
            archive_signatures=archive_signatures,
            novelty_bonus=config.novelty_bonus,
        )
        keep_quick_n = _stage_keep_count(
            len(ranked_quick),
            float(controller.quick_keep_ratio),
            min_keep=2,
        )
        keep_quick_ids = {
            id(item[1]) for item in ranked_quick[: min(len(ranked_quick), keep_quick_n)]
        }
        local_stage["quick_kept"] = float(len(keep_quick_ids))
        local_stage["novelty_quick"] = float(
            len([1 for _, _, sig in ranked_quick[: keep_quick_n] if sig not in archive_signatures])
        ) / float(max(1, keep_quick_n))
        for _, genome, _ in ranked_quick:
            if id(genome) in keep_quick_ids:
                genome.fitness = None
            else:
                genome.fitness = _predictive_cut_score(
                    float(genome.fitness or -float("inf")),
                    "quick",
                    float(config.predictive_penalty),
                )

        mid_pending = [
            (idx, genome)
            for idx, genome in pending
            if id(genome) in keep_quick_ids
        ]
        local_stage["mid_eval"] = float(len(mid_pending))
        if not mid_pending:
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        # Stage 2: medium-depth check.
        mid_fp = _scenario_fingerprint(mid_scenarios)
        mid_stats = _evaluate_pending_dedup_cache_parallel(
            pending=mid_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                mid_scenarios,
                attacker,
            ),
            attr_name="_pcpl_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=mid_fp: "def:mid:{sf}:{att}:{sig}".format(
                sf=sf,
                att=attacker_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["mid_eval"] = float(mid_stats["total"])
        local_stage["eval_unique"] += float(mid_stats["unique_eval"])
        local_stage["cache_hits"] += float(mid_stats["cache_hits"])
        local_stage["dup_reuse"] += float(mid_stats["dup_reuse"])
        mid_genomes = [genome for _, genome in mid_pending]
        _mark_duplicate_genomes(
            mid_genomes,
            stage="mid",
            penalty=config.predictive_penalty,
        )
        ranked_mid = _rank_with_novelty(
            mid_genomes,
            archive_signatures=archive_signatures,
            novelty_bonus=max(0.0, 0.5 * config.novelty_bonus),
        )
        keep_mid_n = _stage_keep_count(
            len(ranked_mid),
            float(controller.mid_keep_ratio),
            min_keep=1,
        )
        keep_mid_ids = {
            id(item[1]) for item in ranked_mid[: min(len(ranked_mid), keep_mid_n)]
        }
        local_stage["mid_kept"] = float(len(keep_mid_ids))
        local_stage["novelty_mid"] = float(
            len([1 for _, _, sig in ranked_mid[: keep_mid_n] if sig not in archive_signatures])
        ) / float(max(1, keep_mid_n))
        for _, genome, _ in ranked_mid:
            if id(genome) in keep_mid_ids:
                genome.fitness = None
            else:
                genome.fitness = _predictive_cut_score(
                    float(genome.fitness or -float("inf")),
                    "mid",
                    float(config.predictive_penalty),
                )

        full_pending = [
            (idx, genome)
            for idx, genome in mid_pending
            if id(genome) in keep_mid_ids
        ]
        local_stage["full_eval"] = float(len(full_pending))
        if not full_pending:
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        # Stage 3: full-depth validation on finalists.
        full_fp = _scenario_fingerprint(full_scenarios)
        full_stats = _evaluate_pending_dedup_cache_parallel(
            pending=full_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
            ),
            attr_name="_pcpl_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=full_fp: "def:full:{sf}:{att}:{sig}".format(
                sf=sf,
                att=attacker_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["full_eval"] = float(full_stats["total"])
        local_stage["eval_unique"] += float(full_stats["unique_eval"])
        local_stage["cache_hits"] += float(full_stats["cache_hits"])
        local_stage["dup_reuse"] += float(full_stats["dup_reuse"])

        # Probe a small random sample from cut genomes to estimate false negatives.
        survivor_ids = {id(genome) for _, genome in full_pending}
        cut_candidates = [
            (idx, genome)
            for idx, genome in pending
            if id(genome) not in survivor_ids
        ]
        probe_n = min(
            max(0, int(math.ceil(len(cut_candidates) * 0.10))),
            2,
        )
        probe_pending: List[Tuple[int, GFSLGenome]] = []
        if probe_n > 0 and cut_candidates:
            probe_pending = random.sample(cut_candidates, min(probe_n, len(cut_candidates)))
            probe_stats = _evaluate_pending_dedup_cache_parallel(
                pending=probe_pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    attacker,
                ),
                attr_name="_pcpl_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "def:full:{sf}:{att}:{sig}".format(
                    sf=sf,
                    att=attacker_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
            )
            local_stage["eval_unique"] += float(probe_stats["unique_eval"])
            local_stage["cache_hits"] += float(probe_stats["cache_hits"])
            local_stage["dup_reuse"] += float(probe_stats["dup_reuse"])
            cutoff = min(float(genome.fitness or -float("inf")) for _, genome in full_pending)
            probe_wins = 0
            for _, probe_genome in probe_pending:
                probe_score = float(probe_genome.fitness or -float("inf"))
                if probe_score > cutoff:
                    probe_wins += 1
                else:
                    probe_genome.fitness = _predictive_cut_score(
                        probe_score,
                        "mid",
                        float(config.predictive_penalty),
                    )
            local_stage["probe_samples"] = float(len(probe_pending))
            local_stage["probe_wins"] = float(probe_wins)

        local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
        controller.apply_feedback(local_stage)
        stage_stats.clear()
        stage_stats.update({
            **local_stage,
            "probe_win_rate": float(local_stage.get("probe_wins", 0.0))
            / float(max(1.0, local_stage.get("probe_samples", 0.0))),
        })

    evolver.evolve(
        config.generations,
        fitness,
        progress_callback=progress,
        batch_evaluator=batch_eval,
        should_stop=should_stop,
    )
    evolver._predictive_controller_state = controller.to_dict()  # type: ignore[attr-defined]
    return evolver, generation_log


def _run_attacker_round(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    shared_executor: Optional[concurrent.futures.Executor],
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    defender: GFSLGenome,
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(config, resource_plan)
    evolver = SafeGFSLEvolver(
        population_size=config.attacker_population_size,
        supervised_guide=guide,
        parent_pool_ratio=config.parent_pool_ratio,
        stagnation_patience=config.stagnation_patience,
        mutation_floor=config.mutation_floor,
        mutation_ceiling=config.mutation_ceiling,
        mutation_step=config.mutation_step,
    )
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=archive.get("attacker_elites", []),
        io_initializer=ensure_attacker_genome_io,
        population_size=config.attacker_population_size,
        initial_instructions=max(4, config.initial_instructions // 2),
        elite_pool=max(4, config.elite_pool // 2),
    )

    generation_log: List[Dict[str, Any]] = []
    archive_signatures = {
        str(entry.get("signature") or entry.get("canonical_signature"))
        for entry in archive.get("attacker_elites", [])
        if (entry.get("signature") or entry.get("canonical_signature"))
    }
    ensure_genome_io(defender)
    controller = PredictiveStageController.from_config(config)
    controller = _seed_controller_from_payload(
        controller,
        archive.get("predictive_profile", {}).get("attacker"),
    )
    stage_stats: Dict[str, float] = {}
    stage_cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
    cache_limit = max(500, int(config.max_eval_cache_entries))
    defender_signature = _evaluation_signature(defender)

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(1, controller.key_variant_count))
            complexity = "quick"
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(1, controller.key_variant_count))
            complexity = "mid"
        else:
            frac = min(1.0, max(0.50, controller.mid_cycle_fraction + 0.08))
            key_variants = max(1, controller.key_variant_count)
            complexity = "hard"
        stage_scenarios = _build_stage_scenarios(
            scenarios,
            cycle_fraction=frac,
            key_variant_count=key_variants,
            complexity=complexity,
            device_mhz=config.device_mhz,
            provider_mhz=config.provider_mhz,
            max_test_time_seconds=config.max_test_time_seconds,
        )
        return _subset_stage_scenarios(stage_scenarios, stage=stage)

    def fitness(attacker: GFSLGenome) -> float:
        ensure_attacker_genome_io(attacker)
        full_scenarios = make_scenarios("full")
        _, metrics = evaluate_across_scenarios(full_scenarios, defender, attacker=attacker)
        attacker._attack_metrics = metrics  # type: ignore[attr-defined]
        attack_adv = _mean_metric(metrics, "attacker_advantage_score")
        lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
        token_success = _mean_metric(metrics, "attacker_token_success_rate")
        effective = len(attacker.extract_effective_algorithm())
        if effective == 0:
            return attack_adv - 0.08
        complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
        return attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty

    def progress(gen: int, best: GFSLGenome, best_fitness: float) -> None:
        metrics = getattr(best, "_attack_metrics", None)
        if metrics is None:
            full_scenarios = make_scenarios("full")
            _, metrics = evaluate_across_scenarios(full_scenarios, defender, attacker=best)
            best._attack_metrics = metrics
        row = {
            "generation": int(gen),
            "attack_score": float(best_fitness),
            "best_signature": _evaluation_signature(best),
            "lane_success": _mean_metric(metrics, "attacker_lane_success_rate"),
            "token_success": _mean_metric(metrics, "attacker_token_success_rate"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
            "quick_fraction": float(controller.quick_cycle_fraction),
            "mid_fraction": float(controller.mid_cycle_fraction),
            "quick_keep": float(controller.quick_keep_ratio),
            "mid_keep": float(controller.mid_keep_ratio),
            "key_variants": int(controller.key_variant_count),
            "probe_win_rate": float(stage_stats.get("probe_win_rate", 0.0)),
            "quick_throttle": float(stage_stats.get("quick_throttle", 1.0)),
            "stage_eval": "{q}>{m}>{f}".format(
                q=int(stage_stats.get("quick_eval", 0.0)),
                m=int(stage_stats.get("mid_eval", 0.0)),
                f=int(stage_stats.get("full_eval", 0.0)),
            ),
            "stage_keep": "{q}>{m}".format(
                q=int(stage_stats.get("quick_kept", 0.0)),
                m=int(stage_stats.get("mid_kept", 0.0)),
            ),
            "quick_eval": int(stage_stats.get("quick_eval", 0.0)),
            "mid_eval": int(stage_stats.get("mid_eval", 0.0)),
            "full_eval": int(stage_stats.get("full_eval", 0.0)),
            "quick_kept_count": int(stage_stats.get("quick_kept", 0.0)),
            "mid_kept_count": int(stage_stats.get("mid_kept", 0.0)),
            "probe_samples": int(stage_stats.get("probe_samples", 0.0)),
            "quick_skipped": int(stage_stats.get("quick_skipped", 0.0)),
            "eval_unique": int(stage_stats.get("eval_unique", 0.0)),
            "cache_hits": int(stage_stats.get("cache_hits", 0.0)),
            "dup_reuse": int(stage_stats.get("dup_reuse", 0.0)),
            "batch_seconds": float(stage_stats.get("batch_seconds", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][attacker] gen={gen:03d} score={score:.5f} lane={lane:.4f} token={token:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} t={secs:.2f}s".format(
                gen=gen,
                score=best_fitness,
                lane=row["lane_success"],
                token=row["token_success"],
                qf=row["quick_fraction"],
                qk=row["quick_keep"],
                mf=row["mid_fraction"],
                mk=row["mid_keep"],
                kv=row["key_variants"],
                probe=row["probe_win_rate"],
                qt=row["quick_throttle"],
                stage_eval=row["stage_eval"],
                stage_keep=row["stage_keep"],
                probe_n=row["probe_samples"],
                qskip=row["quick_skipped"],
                uniq=row["eval_unique"],
                cache=row["cache_hits"],
                dup=row["dup_reuse"],
                secs=row["batch_seconds"],
            )
        )

    def should_stop(
        gen: int,
        best: GFSLGenome,
        best_fitness: float,
        population: Sequence[GFSLGenome],
    ) -> Tuple[bool, str]:
        _ = (gen, best, best_fitness)
        return _should_stop_by_runtime_stats(
            generation_log=generation_log,
            population=population,
            total_generations=int(config.attacker_generations),
            score_key="attack_score",
            min_gain=0.00045,
            target_generation_seconds=float(config.target_generation_seconds),
        )

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, attacker_genome)
            for idx, attacker_genome in enumerate(population)
            if attacker_genome.fitness is None
        ]
        if not pending:
            return

        eval_started = time.perf_counter()
        local_stage: Dict[str, float] = {
            "population": float(len(pending)),
            "quick_eval": float(len(pending)),
            "mid_eval": 0.0,
            "full_eval": 0.0,
            "quick_kept": 0.0,
            "mid_kept": 0.0,
            "probe_samples": 0.0,
            "probe_wins": 0.0,
            "novelty_quick": 0.0,
            "novelty_mid": 0.0,
            "eval_unique": 0.0,
            "cache_hits": 0.0,
            "dup_reuse": 0.0,
            "quick_throttle": 1.0,
            "target_batch_seconds": float(config.target_generation_seconds),
            "batch_seconds": 0.0,
        }

        if config.statistical_predictive and config.auto_statistical_tuning:
            pressure = float(len(pending)) / float(max(1, resource_plan.parallel_workers))
            if pressure > 2.0:
                factor = min(2.0, pressure - 2.0)
                controller.quick_keep_ratio -= 0.06 * factor
                controller.mid_keep_ratio -= 0.05 * factor
                controller.quick_cycle_fraction -= 0.035 * factor
                controller.mid_cycle_fraction -= 0.028 * factor
                if pressure > 3.0 and controller.key_variant_count > 1:
                    controller.key_variant_count -= 1
                controller.clamp()

        if not config.statistical_predictive:
            full_scenarios = make_scenarios("full")
            full_fp = _scenario_fingerprint(full_scenarios)
            eval_stats = _evaluate_pending_dedup_cache_parallel(
                pending=pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    defender,
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "atk:full:{sf}:{def_sig}:{sig}".format(
                    sf=sf,
                    def_sig=defender_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
            )
            local_stage["full_eval"] = float(eval_stats["total"])
            local_stage["quick_kept"] = float(eval_stats["total"])
            local_stage["mid_kept"] = float(eval_stats["total"])
            local_stage["eval_unique"] += float(eval_stats["unique_eval"])
            local_stage["cache_hits"] += float(eval_stats["cache_hits"])
            local_stage["dup_reuse"] += float(eval_stats["dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            stage_stats.clear()
            stage_stats.update(local_stage)
            return

        quick_scenarios = make_scenarios("quick")
        mid_scenarios = make_scenarios("mid")
        full_scenarios = make_scenarios("full")

        quick_pending, quick_skipped = _select_quick_pending(
            pending,
            workers=resource_plan.parallel_workers,
            profile=config.profile,
            archive_signatures=archive_signatures,
        )
        quick_pending, quick_skipped, quick_throttle = _throttle_quick_pending_from_previous_stats(
            quick_pending=quick_pending,
            quick_skipped=quick_skipped,
            previous_stats=stage_stats if stage_stats else None,
            workers=resource_plan.parallel_workers,
        )
        local_stage["quick_throttle"] = float(quick_throttle)
        local_stage["quick_eval"] = float(len(quick_pending))
        local_stage["quick_skipped"] = float(len(quick_skipped))
        for _, skipped_genome in quick_skipped:
            skipped_sig = _evaluation_signature(skipped_genome)
            novelty_offset = 0.015 if skipped_sig not in archive_signatures else 0.0
            skipped_genome.fitness = _predictive_cut_score(
                -0.22 + novelty_offset,
                "quick",
                float(config.predictive_penalty) + 0.01,
            )

        quick_fp = _scenario_fingerprint(quick_scenarios)
        quick_stats = _evaluate_pending_dedup_cache_parallel(
            pending=quick_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                quick_scenarios,
            ),
            attr_name="_attack_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=quick_fp: "atk:quick:{sf}:{def_sig}:{sig}".format(
                sf=sf,
                def_sig=defender_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["eval_unique"] += float(quick_stats["unique_eval"])
        local_stage["cache_hits"] += float(quick_stats["cache_hits"])
        local_stage["dup_reuse"] += float(quick_stats["dup_reuse"])
        pending_genomes = [genome for _, genome in pending]
        _mark_duplicate_genomes(
            pending_genomes,
            stage="quick",
            penalty=config.predictive_penalty,
        )
        ranked_quick = _rank_with_novelty(
            pending_genomes,
            archive_signatures=archive_signatures,
            novelty_bonus=config.novelty_bonus,
        )
        keep_quick_n = _stage_keep_count(
            len(ranked_quick),
            float(controller.quick_keep_ratio),
            min_keep=2,
        )
        keep_quick_ids = {
            id(item[1]) for item in ranked_quick[: min(len(ranked_quick), keep_quick_n)]
        }
        local_stage["quick_kept"] = float(len(keep_quick_ids))
        local_stage["novelty_quick"] = float(
            len([1 for _, _, sig in ranked_quick[: keep_quick_n] if sig not in archive_signatures])
        ) / float(max(1, keep_quick_n))
        for _, genome, _ in ranked_quick:
            if id(genome) in keep_quick_ids:
                genome.fitness = None
            else:
                genome.fitness = _predictive_cut_score(
                    float(genome.fitness or -float("inf")),
                    "quick",
                    float(config.predictive_penalty),
                )

        mid_pending = [
            (idx, attacker_genome)
            for idx, attacker_genome in pending
            if id(attacker_genome) in keep_quick_ids
        ]
        local_stage["mid_eval"] = float(len(mid_pending))
        if not mid_pending:
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        mid_fp = _scenario_fingerprint(mid_scenarios)
        mid_stats = _evaluate_pending_dedup_cache_parallel(
            pending=mid_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                mid_scenarios,
            ),
            attr_name="_attack_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=mid_fp: "atk:mid:{sf}:{def_sig}:{sig}".format(
                sf=sf,
                def_sig=defender_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["mid_eval"] = float(mid_stats["total"])
        local_stage["eval_unique"] += float(mid_stats["unique_eval"])
        local_stage["cache_hits"] += float(mid_stats["cache_hits"])
        local_stage["dup_reuse"] += float(mid_stats["dup_reuse"])
        mid_genomes = [genome for _, genome in mid_pending]
        _mark_duplicate_genomes(
            mid_genomes,
            stage="mid",
            penalty=config.predictive_penalty,
        )
        ranked_mid = _rank_with_novelty(
            mid_genomes,
            archive_signatures=archive_signatures,
            novelty_bonus=max(0.0, 0.5 * config.novelty_bonus),
        )
        keep_mid_n = _stage_keep_count(
            len(ranked_mid),
            float(controller.mid_keep_ratio),
            min_keep=1,
        )
        keep_mid_ids = {
            id(item[1]) for item in ranked_mid[: min(len(ranked_mid), keep_mid_n)]
        }
        local_stage["mid_kept"] = float(len(keep_mid_ids))
        local_stage["novelty_mid"] = float(
            len([1 for _, _, sig in ranked_mid[: keep_mid_n] if sig not in archive_signatures])
        ) / float(max(1, keep_mid_n))
        for _, genome, _ in ranked_mid:
            if id(genome) in keep_mid_ids:
                genome.fitness = None
            else:
                genome.fitness = _predictive_cut_score(
                    float(genome.fitness or -float("inf")),
                    "mid",
                    float(config.predictive_penalty),
                )

        full_pending = [
            (idx, attacker_genome)
            for idx, attacker_genome in mid_pending
            if id(attacker_genome) in keep_mid_ids
        ]
        local_stage["full_eval"] = float(len(full_pending))
        if not full_pending:
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        full_fp = _scenario_fingerprint(full_scenarios)
        full_stats = _evaluate_pending_dedup_cache_parallel(
            pending=full_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
            ),
            attr_name="_attack_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=full_fp: "atk:full:{sf}:{def_sig}:{sig}".format(
                sf=sf,
                def_sig=defender_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
        )
        local_stage["full_eval"] = float(full_stats["total"])
        local_stage["eval_unique"] += float(full_stats["unique_eval"])
        local_stage["cache_hits"] += float(full_stats["cache_hits"])
        local_stage["dup_reuse"] += float(full_stats["dup_reuse"])

        survivor_ids = {id(genome) for _, genome in full_pending}
        cut_candidates = [
            (idx, genome)
            for idx, genome in pending
            if id(genome) not in survivor_ids
        ]
        probe_n = min(
            max(0, int(math.ceil(len(cut_candidates) * 0.10))),
            2,
        )
        probe_pending: List[Tuple[int, GFSLGenome]] = []
        if probe_n > 0 and cut_candidates:
            probe_pending = random.sample(cut_candidates, min(probe_n, len(cut_candidates)))
            probe_stats = _evaluate_pending_dedup_cache_parallel(
                pending=probe_pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    defender,
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "atk:full:{sf}:{def_sig}:{sig}".format(
                    sf=sf,
                    def_sig=defender_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
            )
            local_stage["eval_unique"] += float(probe_stats["unique_eval"])
            local_stage["cache_hits"] += float(probe_stats["cache_hits"])
            local_stage["dup_reuse"] += float(probe_stats["dup_reuse"])
            cutoff = min(float(genome.fitness or -float("inf")) for _, genome in full_pending)
            probe_wins = 0
            for _, probe_genome in probe_pending:
                probe_score = float(probe_genome.fitness or -float("inf"))
                if probe_score > cutoff:
                    probe_wins += 1
                else:
                    probe_genome.fitness = _predictive_cut_score(
                        probe_score,
                        "mid",
                        float(config.predictive_penalty),
                    )
            local_stage["probe_samples"] = float(len(probe_pending))
            local_stage["probe_wins"] = float(probe_wins)

        local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
        controller.apply_feedback(local_stage)
        stage_stats.clear()
        stage_stats.update({
            **local_stage,
            "probe_win_rate": float(local_stage.get("probe_wins", 0.0))
            / float(max(1.0, local_stage.get("probe_samples", 0.0))),
        })

    evolver.evolve(
        config.attacker_generations,
        fitness,
        progress_callback=progress,
        batch_evaluator=batch_eval,
        should_stop=should_stop,
    )
    evolver._predictive_controller_state = controller.to_dict()  # type: ignore[attr-defined]
    return evolver, generation_log


def run_continuous_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Run persistent defender/attacker co-evolution for one or more rounds."""
    raw_scenarios = list(scenarios) if scenarios is not None else default_scenarios(config.profile)
    scenario_list = [
        replace(
            scenario,
            device_mhz=float(config.device_mhz),
            provider_mhz=float(config.provider_mhz),
            max_test_time_seconds=float(config.max_test_time_seconds),
        )
        for scenario in raw_scenarios
    ]
    resource_plan = _resolve_resource_plan(
        config,
        max(config.population_size, config.attacker_population_size),
    )

    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)

    archive_path = out_dir / "archive.json"
    archive = _load_archive(archive_path) if config.resume else _load_archive(Path("/dev/null"))
    baseline_rows = _baseline_rows(scenario_list)

    print(
        "[pcpl-evolvo] resources cpu={cpu} backend={backend} workers={workers} gpu={gpu} torch={torch}".format(
            cpu=resource_plan.cpu_count,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            gpu=resource_plan.gpu_backend,
            torch=resource_plan.torch_available,
        )
    )
    shared_executor = _create_shared_executor(resource_plan)

    try:
        random.seed(config.seed)

        last_defender_score = -float("inf")
        last_attacker_score = -float("inf")
        last_defender_signature = ""
        last_attacker_signature = ""
        last_round_dir = None

        current_attacker: Optional[GFSLGenome] = None
        if archive.get("attacker_elites"):
            try:
                current_attacker = _deserialize_genome(archive["attacker_elites"][0]["genome"])
                ensure_attacker_genome_io(current_attacker)
            except Exception:
                current_attacker = None

        start_round = len(archive.get("rounds", []))
        for offset in range(max(1, config.rounds)):
            round_index = start_round + offset
            round_dir = rounds_dir / f"round-{round_index:04d}"
            round_dir.mkdir(parents=True, exist_ok=True)

            # Defender evolution under current strongest attacker.
            defender_evolver, defender_log = _run_defender_round(
                config=config,
                resource_plan=resource_plan,
                shared_executor=shared_executor,
                scenarios=scenario_list,
                archive=archive,
                attacker=current_attacker,
            )

            # Preliminary best defender from the round.
            preliminary_defender = defender_evolver.population[0]

            attacker_round_config, attacker_budget_meta = _adaptive_attacker_config_from_defender_log(
                base_config=config,
                defender_log=defender_log,
            )
            if attacker_budget_meta.get("active"):
                print(
                    "[pcpl-evolvo] adaptive attacker budget: pop={pop} gen={gen} reason={reason} reuse={reuse:.2f} uniq={uniq:.2f} gain={gain:.6f}".format(
                        pop=int(attacker_budget_meta.get("population", config.attacker_population_size)),
                        gen=int(attacker_budget_meta.get("generations", config.attacker_generations)),
                        reason=str(attacker_budget_meta.get("reason", "adaptive")),
                        reuse=float(attacker_budget_meta.get("reuse_ratio", 0.0)),
                        uniq=float(attacker_budget_meta.get("uniqueness_ratio", 0.0)),
                        gain=float(attacker_budget_meta.get("score_gain", 0.0)),
                    )
                )

            # Attacker co-evolution against this defender.
            attacker_evolver, attacker_log = _run_attacker_round(
                config=attacker_round_config,
                resource_plan=resource_plan,
                shared_executor=shared_executor,
                scenarios=scenario_list,
                archive=archive,
                defender=preliminary_defender,
            )
            best_attacker = attacker_evolver.population[0]
            ensure_attacker_genome_io(best_attacker)

            defender_profile = getattr(defender_evolver, "_predictive_controller_state", {})
            if not isinstance(defender_profile, dict):
                defender_profile = {}
            attacker_profile = getattr(attacker_evolver, "_predictive_controller_state", {})
            if not isinstance(attacker_profile, dict):
                attacker_profile = {}
            predictive_profile = archive.setdefault("predictive_profile", {})
            if defender_profile:
                predictive_profile["defender"] = defender_profile
            if attacker_profile:
                predictive_profile["attacker"] = attacker_profile
            selection_key_variants = max(
                1,
                int(defender_profile.get("key_variant_count", config.key_variant_count)),
                int(attacker_profile.get("key_variant_count", config.key_variant_count)),
            )
            selection_scenarios = _build_stage_scenarios(
                scenario_list,
                cycle_fraction=1.0,
                key_variant_count=selection_key_variants,
                complexity="hard",
                device_mhz=config.device_mhz,
                provider_mhz=config.provider_mhz,
                max_test_time_seconds=config.max_test_time_seconds,
            )

            # Select robust defender among top candidates against the new attacker.
            top_candidates = [
                genome for genome in defender_evolver.population
                if len(genome.extract_effective_algorithm()) > 0
            ]
            if not top_candidates:
                top_candidates = defender_evolver.population[:]
            top_candidates = top_candidates[: min(5, len(top_candidates))]
            selected_defender = top_candidates[0]
            selected_metrics: List[ScenarioMetrics] = []
            selected_score = -float("inf")
            for candidate in top_candidates:
                ensure_genome_io(candidate)
                score, metrics = evaluate_across_scenarios(
                    selection_scenarios,
                    candidate,
                    attacker=best_attacker,
                )
                if score > selected_score:
                    selected_score = score
                    selected_metrics = metrics
                    selected_defender = candidate

            attacker_score, attacker_metrics = evaluate_across_scenarios(
                selection_scenarios,
                selected_defender,
                attacker=best_attacker,
            )
            attack_adv = _mean_metric(attacker_metrics, "attacker_advantage_score")

            defender_signature = selected_defender.get_signature()
            attacker_signature = best_attacker.get_signature()

            defender_record = {
                "role": "defender",
                "round": round_index,
                "timestamp": _utc_now_iso(),
                "score": float(selected_score),
                "signature": defender_signature,
                "canonical_signature": _canonical_signature(selected_defender),
                "metrics": _metrics_rows(selected_metrics),
                "genome": _serialize_genome(selected_defender, role="defender"),
            }
            attacker_record = {
                "role": "attacker",
                "round": round_index,
                "timestamp": _utc_now_iso(),
                "score": float(attack_adv),
                "signature": attacker_signature,
                "canonical_signature": _canonical_signature(best_attacker),
                "metrics": _metrics_rows(attacker_metrics),
                "genome": _serialize_genome(best_attacker, role="attacker"),
            }

            archive["defender_elites"] = _insert_elite(
                archive.get("defender_elites", []),
                defender_record,
                limit=config.archive_limit,
            )
            archive["attacker_elites"] = _insert_elite(
                archive.get("attacker_elites", []),
                attacker_record,
                limit=config.archive_limit,
            )

            round_summary = {
                "round": round_index,
                "timestamp": _utc_now_iso(),
                "defender_score": float(selected_score),
                "defender_signature": defender_signature,
                "attacker_score": float(attack_adv),
                "attacker_signature": attacker_signature,
                "defender_log": defender_log,
                "attacker_log": attacker_log,
                "metrics": _metrics_rows(selected_metrics),
                "predictive_profile": {
                    "defender": defender_profile,
                    "attacker": attacker_profile,
                    "selection_key_variants": selection_key_variants,
                },
                "adaptive_attacker_budget": attacker_budget_meta,
            }
            archive.setdefault("rounds", []).append(round_summary)

            # Round artifacts.
            (round_dir / "defender-genome.txt").write_text(
                "\n".join(selected_defender.to_human_readable()) + "\n",
                encoding="utf-8",
            )
            (round_dir / "attacker-genome.txt").write_text(
                "\n".join(best_attacker.to_human_readable()) + "\n",
                encoding="utf-8",
            )
            (round_dir / "round-results.json").write_text(
                json.dumps(round_summary, indent=2),
                encoding="utf-8",
            )
            round_report = _build_round_report(
                config=config,
                round_index=round_index,
                scenarios=scenario_list,
                defender_score=selected_score,
                defender_signature=defender_signature,
                defender_metrics=selected_metrics,
                attacker_score=attack_adv,
                attacker_signature=attacker_signature,
                defender_log=defender_log,
                attacker_log=attacker_log,
            )
            (round_dir / "round-report.md").write_text(round_report, encoding="utf-8")

            _save_archive(archive_path, archive)

            current_attacker = best_attacker
            last_defender_score = selected_score
            last_attacker_score = attack_adv
            last_defender_signature = defender_signature
            last_attacker_signature = attacker_signature
            last_round_dir = round_dir

            print(
                "[pcpl-evolvo] round={round:04d} defender={def_score:.6f} attacker={atk_score:.6f}".format(
                    round=round_index,
                    def_score=selected_score,
                    atk_score=attack_adv,
                )
            )

        # Build global summary and report.
        view_paths = _write_view_outputs(
            out_dir=out_dir,
            config=config,
            resource_plan=resource_plan,
            archive=archive,
            baseline_rows=baseline_rows,
        )
        predictive_profile = archive.get("predictive_profile", {})
        if not isinstance(predictive_profile, dict):
            predictive_profile = {}
        defender_profile = predictive_profile.get("defender", {})
        if not isinstance(defender_profile, dict):
            defender_profile = {}
        attacker_profile = predictive_profile.get("attacker", {})
        if not isinstance(attacker_profile, dict):
            attacker_profile = {}

        final_summary = {
            "config": {
                **asdict(config),
                "out_dir": str(out_dir),
            },
            "resources": resource_plan.to_dict(),
            "baselines": baseline_rows,
            "rounds_completed": len(archive.get("rounds", [])),
            "best_defender": archive.get("defender_elites", [])[:1],
            "best_attacker": archive.get("attacker_elites", [])[:1],
            "predictive_profile": predictive_profile,
            "last_round": {
                "score": last_defender_score,
                "signature": last_defender_signature,
                "attacker_score": last_attacker_score,
                "attacker_signature": last_attacker_signature,
                "round_dir": str(last_round_dir) if last_round_dir else None,
            },
            "views": view_paths,
        }

        results_json = out_dir / "results.json"
        results_json.write_text(json.dumps(final_summary, indent=2), encoding="utf-8")

        report_lines: List[str] = []
        report_lines.append("# PCPL Evolvo Continuous Report")
        report_lines.append("")
        report_lines.append(f"- profile: `{config.profile}`")
        report_lines.append(f"- rounds completed: `{len(archive.get('rounds', []))}`")
        report_lines.append(f"- best defender score: `{last_defender_score:.6f}`")
        report_lines.append(f"- best attacker score: `{last_attacker_score:.6f}`")
        report_lines.append(
            "- resources: backend={backend} workers={workers} gpu={gpu}".format(
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                gpu=resource_plan.gpu_backend,
            )
        )
        report_lines.append(
            "- statistical seeds: enabled={enabled} quick={quick:.2f} mid={mid:.2f} keep={keep_q:.2f}/{keep_m:.2f} key_variants={key_vars}".format(
                enabled=config.statistical_predictive,
                quick=float(config.quick_cycle_fraction),
                mid=float(config.mid_cycle_fraction),
                keep_q=float(config.quick_keep_ratio),
                keep_m=float(config.mid_keep_ratio),
                key_vars=int(config.key_variant_count),
            )
        )
        report_lines.append(
            "- auto statistical tuning: `{enabled}`".format(
                enabled=(
                    "enabled"
                    if bool(config.statistical_predictive and config.auto_statistical_tuning)
                    else "disabled"
                )
            )
        )
        if bool(config.statistical_predictive):
            report_lines.append(
                "- tuned defender: quick={quick:.2f} mid={mid:.2f} keep={keep_q:.2f}/{keep_m:.2f} key_variants={key_vars}".format(
                    quick=float(defender_profile.get("quick_cycle_fraction", config.quick_cycle_fraction)),
                    mid=float(defender_profile.get("mid_cycle_fraction", config.mid_cycle_fraction)),
                    keep_q=float(defender_profile.get("quick_keep_ratio", config.quick_keep_ratio)),
                    keep_m=float(defender_profile.get("mid_keep_ratio", config.mid_keep_ratio)),
                    key_vars=int(defender_profile.get("key_variant_count", config.key_variant_count)),
                )
            )
            report_lines.append(
                "- tuned attacker: quick={quick:.2f} mid={mid:.2f} keep={keep_q:.2f}/{keep_m:.2f} key_variants={key_vars}".format(
                    quick=float(attacker_profile.get("quick_cycle_fraction", config.quick_cycle_fraction)),
                    mid=float(attacker_profile.get("mid_cycle_fraction", config.mid_cycle_fraction)),
                    keep_q=float(attacker_profile.get("quick_keep_ratio", config.quick_keep_ratio)),
                    keep_m=float(attacker_profile.get("mid_keep_ratio", config.mid_keep_ratio)),
                    key_vars=int(attacker_profile.get("key_variant_count", config.key_variant_count)),
                )
            )
        report_lines.append(
            "- timing-horizon: max_test_s={max_s:.1f} device_mhz={dev:.1f} provider_mhz={prov:.1f}".format(
                max_s=float(config.max_test_time_seconds),
                dev=float(config.device_mhz),
                prov=float(config.provider_mhz),
            )
        )
        report_lines.append(
            "- runtime-governor: target_gen_s={target:.2f} eval_cache={cache}".format(
                target=float(config.target_generation_seconds),
                cache=int(config.max_eval_cache_entries),
            )
        )
        report_lines.append("")
        report_lines.append("## Baselines")
        report_lines.append("")
        report_lines.append("| policy | mean score |")
        report_lines.append("| --- | ---: |")
        for row in baseline_rows:
            report_lines.append(f"| {row['name']} | {row['mean_score']:.4f} |")
        report_lines.append("")
        report_lines.append("## Latest Round")
        report_lines.append("")
        if last_round_dir:
            report_lines.append(f"- round dir: `{last_round_dir}`")
        report_lines.append(f"- defender signature: `{last_defender_signature}`")
        report_lines.append(f"- attacker signature: `{last_attacker_signature}`")

        report_path = out_dir / "report.md"
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

        summary = {
            "out_dir": str(out_dir),
            "results_json": str(results_json),
            "report_path": str(report_path),
            "archive_path": str(archive_path),
            "best_score": last_defender_score,
            "best_signature": last_defender_signature,
            "best_attacker_score": last_attacker_score,
            "best_attacker_signature": last_attacker_signature,
            "rounds_completed": len(archive.get("rounds", [])),
            "resource_plan": resource_plan.to_dict(),
            "predictive_profile": predictive_profile,
            **view_paths,
        }
        return summary
    finally:
        _shutdown_shared_executor(shared_executor)


def run_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Backward-compatible entry point; now supports continuous rounds."""
    return run_continuous_experiment(config, scenarios=scenarios)
