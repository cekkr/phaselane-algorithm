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
    quick_cycle_fraction: float = 0.20
    mid_cycle_fraction: float = 0.55
    quick_keep_ratio: float = 0.65
    mid_keep_ratio: float = 0.35
    key_variant_count: int = 3
    novelty_bonus: float = 0.03
    predictive_penalty: float = 0.05
    auto_statistical_tuning: bool = True
    device_mhz: float = 100.0
    provider_mhz: float = 300.0
    max_test_time_seconds: float = 10.0


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
        return PredictiveStageController(
            quick_cycle_fraction=float(config.quick_cycle_fraction),
            mid_cycle_fraction=float(config.mid_cycle_fraction),
            quick_keep_ratio=float(config.quick_keep_ratio),
            mid_keep_ratio=float(config.mid_keep_ratio),
            key_variant_count=int(config.key_variant_count),
            auto_tune=bool(config.auto_statistical_tuning),
        )

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

        # Enforce staged selectivity: if a stage keeps almost everyone, tighten it.
        if population >= 3.0 and quick_rate > 0.90:
            self.quick_keep_ratio -= 0.07
            self.quick_cycle_fraction -= 0.02
        if quick_kept >= 2.0 and mid_over_quick > 0.88:
            self.mid_keep_ratio -= 0.05
            self.mid_cycle_fraction -= 0.015

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

    def evolve(
        self,
        generations: int,
        evaluator,
        progress_callback=None,
        batch_evaluator=None,
    ):
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

            if self.supervised_guide:
                self.supervised_guide.observe_population(self.population)

            if progress_callback and self.population:
                best = self.population[0]
                progress_callback(gen, best, best.fitness or -float("inf"))

            elite_size = max(1, int(self.population_size * self.elite_ratio))
            new_population = self.population[:elite_size]

            seen = {_canonical_signature(genome) for genome in new_population}
            attempts = 0
            max_attempts = max(self.population_size * 80, 200)
            parent_pool_size = max(2, int(len(self.population) * self.parent_pool_ratio))
            parent_pool = self.population[: parent_pool_size]

            def tournament_from(pool: Sequence[GFSLGenome], size: int = 3) -> GFSLGenome:
                tournament = random.sample(list(pool), min(size, len(pool)))
                return max(tournament, key=lambda g: g.fitness or -float("inf"))

            while len(new_population) < self.population_size and attempts < max_attempts:
                attempts += 1
                if self.stagnation_count > 0 and random.random() < 0.45:
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

                if random.random() < self.mutation_rate and not (
                    self.stagnation_count > 0 and random.random() < 0.45
                ):
                    if self.supervised_guide:
                        child = self.supervised_guide.propose_mutation(self, child)
                    else:
                        child = self.mutate(child)
                    child.fitness = None
                    child.generation = gen + 1

                signature = _canonical_signature(child)
                if signature in seen:
                    continue
                seen.add(signature)
                new_population.append(child)

            while len(new_population) < self.population_size:
                fallback = copy.deepcopy(random.choice(self.population[:elite_size]))
                fallback.fitness = None
                fallback.generation = gen + 1
                new_population.append(fallback)

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


def _metrics_from_rows(rows: Sequence[Dict[str, Any]]) -> List[ScenarioMetrics]:
    return [ScenarioMetrics(**row) for row in rows]


def _mix_seed(seed: int, label: str) -> int:
    payload = f"{int(seed)}:{label}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _variant_seed(base_seed: int, variant_index: int) -> Tuple[int, str]:
    if variant_index <= 0:
        return int(base_seed), "base"
    if variant_index == 1:
        # Deliberately low-entropy shared seed to test weak/shared provisioning.
        return int((base_seed % 4096) + 17), "shared-low-entropy"
    if variant_index == 2:
        return _mix_seed(base_seed, "rotating-derived"), "rotating-derived"
    return _mix_seed(base_seed, f"lineage-xor:{variant_index}"), f"lineage-{variant_index}"


def _build_stage_scenarios(
    scenarios: Sequence[ScenarioConfig],
    *,
    cycle_fraction: float,
    key_variant_count: int,
    device_mhz: float,
    provider_mhz: float,
    max_test_time_seconds: float,
) -> List[ScenarioConfig]:
    stage_scenarios: List[ScenarioConfig] = []
    fraction = max(0.05, min(1.0, float(cycle_fraction)))
    variants = max(1, int(key_variant_count))
    for scenario in scenarios:
        stage_cycles = max(10, int(round(float(scenario.cycles) * fraction)))
        for idx in range(variants):
            var_seed, label = _variant_seed(scenario.seed, idx)
            stage_scenarios.append(
                replace(
                    scenario,
                    name=f"{scenario.name}:{label}:f{int(round(fraction * 100.0))}",
                    seed=var_seed,
                    cycles=stage_cycles,
                    device_mhz=float(device_mhz),
                    provider_mhz=float(provider_mhz),
                    max_test_time_seconds=float(max_test_time_seconds),
                )
            )
    return stage_scenarios


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


def _rank_with_novelty(
    genomes: Sequence[GFSLGenome],
    *,
    archive_signatures: set[str],
    novelty_bonus: float,
) -> List[Tuple[float, GFSLGenome, str]]:
    ranked: List[Tuple[float, GFSLGenome, str]] = []
    local_seen: set[str] = set()
    for genome in genomes:
        signature = _canonical_signature(genome)
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
) -> None:
    seen: Dict[str, GFSLGenome] = {}
    ordered = sorted(genomes, key=lambda g: float(g.fitness or -float("inf")), reverse=True)
    for genome in ordered:
        signature = _canonical_signature(genome)
        if signature not in seen:
            seen[signature] = genome
            continue
        genome.fitness = _predictive_cut_score(float(genome.fitness or -float("inf")), stage, penalty + 0.015)


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
        if os.name != "nt":
            try:
                kwargs["mp_context"] = multiprocessing.get_context("fork")
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
    chunk_size = max(1, len(tasks) // max(1, workers * 2))

    def run_with_executor(exec_obj: concurrent.futures.Executor):
        if isinstance(exec_obj, concurrent.futures.ProcessPoolExecutor):
            return list(exec_obj.map(worker_fn, tasks, chunksize=chunk_size))
        return list(exec_obj.map(worker_fn, tasks))

    if executor is not None:
        results = run_with_executor(executor)
        for (_, genome), (score, rows) in zip(pending, results):
            setattr(genome, attr_name, _metrics_from_rows(rows))
            genome.fitness = float(score)
        return

    if backend == "process":
        kwargs: Dict[str, Any] = {}
        if os.name != "nt":
            try:
                kwargs["mp_context"] = multiprocessing.get_context("fork")
            except Exception:
                pass
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, **kwargs) as local_exec:
            results = run_with_executor(local_exec)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as local_exec:
            results = run_with_executor(local_exec)

    for (_, genome), (score, rows) in zip(pending, results):
        setattr(genome, attr_name, _metrics_from_rows(rows))
        genome.fitness = float(score)


def _mean_metric(metrics: Sequence[ScenarioMetrics], attr: str) -> float:
    if not metrics:
        return 0.0
    return sum(float(getattr(item, attr)) for item in metrics) / float(len(metrics))


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
        signature = _canonical_signature(genome)
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
        io_initializer(fallback)
        seeded.append(fallback)

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
    lines.append("| gen | best | principle | security | cost | sync-loss | attacker-adv | q frac/keep | m frac/keep | key var | probe | eval q>m>f | keep q>m | probes |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |")
    for row in defender_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {security:.4f} | {cost:.4f} | {sync_loss:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {stage_eval} | {stage_keep} | {probe_n} |".format(
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
                stage_eval=str(row.get("stage_eval", "0>0>0")),
                stage_keep=str(row.get("stage_keep", "0>0")),
                probe_n=int(row.get("probe_samples", 0)),
            )
        )
    lines.append("")
    lines.append("## Attacker Evolution")
    lines.append("")
    lines.append("| gen | attack_score | lane_success | token_success | attacker_adv | q frac/keep | m frac/keep | key var | probe | eval q>m>f | keep q>m | probes |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |")
    for row in attacker_log:
        lines.append(
            "| {generation} | {attack_score:.4f} | {lane_success:.4f} | {token_success:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {stage_eval} | {stage_keep} | {probe_n} |".format(
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
                stage_eval=str(row.get("stage_eval", "0>0>0")),
                stage_keep=str(row.get("stage_keep", "0>0")),
                probe_n=int(row.get("probe_samples", 0)),
            )
        )
    return "\n".join(lines) + "\n"


def _baseline_rows(scenarios: Sequence[ScenarioConfig]) -> List[Dict[str, Any]]:
    baselines = [
        ("reference-full", PolicyDecision(active_ratio=1.0, kernel=0, stride_seed=0, state_mix=0.5)),
        ("balanced", PolicyDecision(active_ratio=0.65, kernel=1, stride_seed=19, state_mix=0.5)),
        ("minimal-cost", PolicyDecision(active_ratio=0.25, kernel=2, stride_seed=31, state_mix=0.5)),
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
        str(entry.get("canonical_signature"))
        for entry in archive.get("defender_elites", [])
        if entry.get("canonical_signature")
    }
    controller = PredictiveStageController.from_config(config)
    controller = _seed_controller_from_payload(
        controller,
        archive.get("predictive_profile", {}).get("defender"),
    )
    stage_stats: Dict[str, float] = {}

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(1, controller.key_variant_count))
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(1, controller.key_variant_count))
        else:
            frac = 1.0
            key_variants = max(1, controller.key_variant_count)
        return _build_stage_scenarios(
            scenarios,
            cycle_fraction=frac,
            key_variant_count=key_variants,
            device_mhz=config.device_mhz,
            provider_mhz=config.provider_mhz,
            max_test_time_seconds=config.max_test_time_seconds,
        )

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
            "stage_eval": "{q}>{m}>{f}".format(
                q=int(stage_stats.get("quick_eval", 0.0)),
                m=int(stage_stats.get("mid_eval", 0.0)),
                f=int(stage_stats.get("full_eval", 0.0)),
            ),
            "stage_keep": "{q}>{m}".format(
                q=int(stage_stats.get("quick_kept", 0.0)),
                m=int(stage_stats.get("mid_kept", 0.0)),
            ),
            "probe_samples": int(stage_stats.get("probe_samples", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][defender] gen={gen:03d} score={score:.5f} sec={security:.4f} cost={cost:.4f} attack_adv={attack_adv:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n}".format(
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
                stage_eval=row["stage_eval"],
                stage_keep=row["stage_keep"],
                probe_n=row["probe_samples"],
            )
        )

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, genome)
            for idx, genome in enumerate(population)
            if genome.fitness is None
        ]
        if not pending:
            return

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
        }

        if not config.statistical_predictive:
            full_scenarios = make_scenarios("full")
            _evaluate_pending_parallel(
                pending=pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    copy.deepcopy(attacker) if attacker is not None else None,
                ),
                attr_name="_pcpl_metrics",
            )
            local_stage["full_eval"] = float(len(pending))
            local_stage["quick_kept"] = float(len(pending))
            local_stage["mid_kept"] = float(len(pending))
            stage_stats.clear()
            stage_stats.update(local_stage)
            return

        quick_scenarios = make_scenarios("quick")
        mid_scenarios = make_scenarios("mid")
        full_scenarios = make_scenarios("full")

        # Stage 1: fast statistical screen.
        _evaluate_pending_parallel(
            pending=pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                quick_scenarios,
                copy.deepcopy(attacker) if attacker is not None else None,
            ),
            attr_name="_pcpl_metrics",
        )
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
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        # Stage 2: medium-depth check.
        _evaluate_pending_parallel(
            pending=mid_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                mid_scenarios,
                copy.deepcopy(attacker) if attacker is not None else None,
            ),
            attr_name="_pcpl_metrics",
        )
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
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        # Stage 3: full-depth validation on finalists.
        _evaluate_pending_parallel(
            pending=full_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                full_scenarios,
                copy.deepcopy(attacker) if attacker is not None else None,
            ),
            attr_name="_pcpl_metrics",
        )

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
            _evaluate_pending_parallel(
                pending=probe_pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    copy.deepcopy(attacker) if attacker is not None else None,
                ),
                attr_name="_pcpl_metrics",
            )
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
        str(entry.get("canonical_signature"))
        for entry in archive.get("attacker_elites", [])
        if entry.get("canonical_signature")
    }
    controller = PredictiveStageController.from_config(config)
    controller = _seed_controller_from_payload(
        controller,
        archive.get("predictive_profile", {}).get("attacker"),
    )
    stage_stats: Dict[str, float] = {}

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(1, controller.key_variant_count))
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(1, controller.key_variant_count))
        else:
            frac = 1.0
            key_variants = max(1, controller.key_variant_count)
        return _build_stage_scenarios(
            scenarios,
            cycle_fraction=frac,
            key_variant_count=key_variants,
            device_mhz=config.device_mhz,
            provider_mhz=config.provider_mhz,
            max_test_time_seconds=config.max_test_time_seconds,
        )

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
            "lane_success": _mean_metric(metrics, "attacker_lane_success_rate"),
            "token_success": _mean_metric(metrics, "attacker_token_success_rate"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
            "quick_fraction": float(controller.quick_cycle_fraction),
            "mid_fraction": float(controller.mid_cycle_fraction),
            "quick_keep": float(controller.quick_keep_ratio),
            "mid_keep": float(controller.mid_keep_ratio),
            "key_variants": int(controller.key_variant_count),
            "probe_win_rate": float(stage_stats.get("probe_win_rate", 0.0)),
            "stage_eval": "{q}>{m}>{f}".format(
                q=int(stage_stats.get("quick_eval", 0.0)),
                m=int(stage_stats.get("mid_eval", 0.0)),
                f=int(stage_stats.get("full_eval", 0.0)),
            ),
            "stage_keep": "{q}>{m}".format(
                q=int(stage_stats.get("quick_kept", 0.0)),
                m=int(stage_stats.get("mid_kept", 0.0)),
            ),
            "probe_samples": int(stage_stats.get("probe_samples", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][attacker] gen={gen:03d} score={score:.5f} lane={lane:.4f} token={token:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n}".format(
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
                stage_eval=row["stage_eval"],
                stage_keep=row["stage_keep"],
                probe_n=row["probe_samples"],
            )
        )

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, attacker_genome)
            for idx, attacker_genome in enumerate(population)
            if attacker_genome.fitness is None
        ]
        if not pending:
            return

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
        }

        if not config.statistical_predictive:
            full_scenarios = make_scenarios("full")
            _evaluate_pending_parallel(
                pending=pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    copy.deepcopy(defender),
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
            )
            local_stage["full_eval"] = float(len(pending))
            local_stage["quick_kept"] = float(len(pending))
            local_stage["mid_kept"] = float(len(pending))
            stage_stats.clear()
            stage_stats.update(local_stage)
            return

        quick_scenarios = make_scenarios("quick")
        mid_scenarios = make_scenarios("mid")
        full_scenarios = make_scenarios("full")

        _evaluate_pending_parallel(
            pending=pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                copy.deepcopy(defender),
                quick_scenarios,
            ),
            attr_name="_attack_metrics",
        )
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
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        _evaluate_pending_parallel(
            pending=mid_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                copy.deepcopy(defender),
                mid_scenarios,
            ),
            attr_name="_attack_metrics",
        )
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
            controller.apply_feedback(local_stage)
            stage_stats.clear()
            stage_stats.update({
                **local_stage,
                "probe_win_rate": 0.0,
            })
            return

        _evaluate_pending_parallel(
            pending=full_pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                copy.deepcopy(defender),
                full_scenarios,
            ),
            attr_name="_attack_metrics",
        )

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
            _evaluate_pending_parallel(
                pending=probe_pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    copy.deepcopy(defender),
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
            )
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

            # Attacker co-evolution against this defender.
            attacker_evolver, attacker_log = _run_attacker_round(
                config=config,
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
