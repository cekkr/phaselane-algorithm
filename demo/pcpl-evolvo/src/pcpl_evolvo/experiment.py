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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable
from .simulation import (
    PolicyDecision,
    ScenarioConfig,
    ScenarioMetrics,
    build_reference_defender_genome,
    default_scenarios,
    ensure_attacker_genome_io,
    ensure_genome_io,
    evaluate_across_scenarios,
    reference_pcpl_policy,
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
    supervised_end_round_only: bool = True
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
            # Full-profile runs should preserve high parallel load and diversity by default.
            controller.quick_cycle_fraction = min(controller.quick_cycle_fraction, 0.08)
            controller.mid_cycle_fraction = min(controller.mid_cycle_fraction, 0.30)
            controller.quick_keep_ratio = max(controller.quick_keep_ratio, 0.52)
            controller.mid_keep_ratio = max(controller.mid_keep_ratio, 0.28)
            controller.key_variant_count = max(controller.key_variant_count, 4)
        controller.clamp()
        return controller

    def clamp(self) -> None:
        # Allow very small quick fractions when cycles are already very large.
        self.quick_cycle_fraction = clamp(self.quick_cycle_fraction, 0.05, 0.65)
        self.mid_cycle_fraction = clamp(
            max(self.mid_cycle_fraction, self.quick_cycle_fraction + 0.12),
            0.18,
            0.95,
        )
        self.quick_keep_ratio = clamp(self.quick_keep_ratio, 0.24, 0.92)
        self.mid_keep_ratio = clamp(self.mid_keep_ratio, 0.12, 0.80)
        self.mid_keep_ratio = min(self.mid_keep_ratio, max(0.12, self.quick_keep_ratio - 0.03))
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
        workers = max(1.0, float(stats.get("workers", 1.0)))
        full_eval = max(0.0, float(stats.get("full_eval", 0.0)))
        eval_unique = max(0.0, float(stats.get("eval_unique", 0.0)))

        over_budget = batch_seconds > target_seconds
        underutilized = (
            batch_seconds > 0.0
            and batch_seconds < (0.72 * target_seconds)
            and (
                full_eval < (0.90 * workers)
                or eval_unique < (1.20 * workers)
            )
        )

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

        if underutilized:
            # Keep CPU lanes fed when batches complete too quickly with too few finalists.
            self.quick_keep_ratio += 0.11
            self.mid_keep_ratio += 0.10
            self.quick_cycle_fraction += 0.04
            self.mid_cycle_fraction += 0.05
            self.key_variant_count += 1

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
            self.quick_cycle_fraction += 0.02
            self.mid_cycle_fraction += 0.02

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


@dataclass
class TorchRuntimeTuner:
    """Torch-backed micro-predictor to keep staged workload near runtime target."""

    enabled: bool
    max_history: int = 128
    history_x: List[List[float]] = field(default_factory=list)
    history_y: List[float] = field(default_factory=list)
    last_prediction_ratio: float = 1.0
    last_mode: str = "neutral"

    def _feature_vector(
        self,
        *,
        quick_cycle_fraction: float,
        mid_cycle_fraction: float,
        quick_keep_ratio: float,
        mid_keep_ratio: float,
        key_variant_count: int,
        population: float,
        quick_eval: float,
        mid_eval: float,
        full_eval: float,
        reuse_ratio: float,
        probe_win_rate: float,
        workers: float,
    ) -> List[float]:
        worker = max(1.0, float(workers))
        return [
            clamp(float(quick_cycle_fraction), 0.0, 1.0),
            clamp(float(mid_cycle_fraction), 0.0, 1.0),
            clamp(float(quick_keep_ratio), 0.0, 1.0),
            clamp(float(mid_keep_ratio), 0.0, 1.0),
            clamp(float(key_variant_count) / 6.0, 0.0, 2.0),
            clamp(float(population) / (worker * 6.0), 0.0, 2.5),
            clamp(float(quick_eval) / (worker * 3.0), 0.0, 2.5),
            clamp(float(mid_eval) / (worker * 2.0), 0.0, 2.5),
            clamp(float(full_eval) / worker, 0.0, 2.5),
            clamp(float(reuse_ratio), 0.0, 3.0),
            clamp(float(probe_win_rate), 0.0, 1.0),
        ]

    def observe(self, *, controller_state: Dict[str, Any], stats: Dict[str, float]) -> None:
        if not self.enabled:
            return
        target = max(0.25, float(stats.get("target_batch_seconds", 1.0)))
        ratio = clamp(float(stats.get("batch_seconds", 0.0)) / target, 0.05, 4.0)
        eval_unique = max(1.0, float(stats.get("eval_unique", 0.0)))
        cache_dup = float(stats.get("cache_hits", 0.0)) + float(stats.get("dup_reuse", 0.0))
        reuse_ratio = cache_dup / eval_unique
        features = self._feature_vector(
            quick_cycle_fraction=float(controller_state.get("quick_cycle_fraction", 0.10)),
            mid_cycle_fraction=float(controller_state.get("mid_cycle_fraction", 0.35)),
            quick_keep_ratio=float(controller_state.get("quick_keep_ratio", 0.42)),
            mid_keep_ratio=float(controller_state.get("mid_keep_ratio", 0.16)),
            key_variant_count=int(controller_state.get("key_variant_count", 2)),
            population=float(stats.get("population", 1.0)),
            quick_eval=float(stats.get("quick_eval", 0.0)),
            mid_eval=float(stats.get("mid_eval", 0.0)),
            full_eval=float(stats.get("full_eval", 0.0)),
            reuse_ratio=float(reuse_ratio),
            probe_win_rate=float(stats.get("probe_win_rate", 0.0)),
            workers=float(stats.get("workers", 1.0)),
        )
        self.history_x.append(features)
        self.history_y.append(float(ratio))
        while len(self.history_x) > self.max_history:
            self.history_x.pop(0)
            self.history_y.pop(0)

    def _fit_coefficients(self):
        if not self.enabled:
            return None
        if len(self.history_x) < 10:
            return None
        try:
            import torch  # type: ignore

            x = torch.tensor(self.history_x, dtype=torch.float32)
            y = torch.tensor(self.history_y, dtype=torch.float32).unsqueeze(1)
            ones = torch.ones((x.shape[0], 1), dtype=torch.float32)
            design = torch.cat((x, ones), dim=1)
            ridge = 0.06 * torch.eye(design.shape[1], dtype=torch.float32)
            ridge[-1, -1] = 0.0
            coeff = torch.linalg.solve(design.T @ design + ridge, design.T @ y)
            return coeff
        except Exception:
            return None

    def _predict_ratio(self, coeff, features: Sequence[float]) -> float:
        try:
            import torch  # type: ignore

            row = torch.tensor([*features, 1.0], dtype=torch.float32).unsqueeze(0)
            prediction = float((row @ coeff).squeeze().item())
            return clamp(prediction, 0.05, 4.0)
        except Exception:
            return 1.0

    def tune(self, controller: PredictiveStageController, *, stats: Dict[str, float]) -> None:
        coeff = self._fit_coefficients()
        if coeff is None:
            return

        workers = max(1.0, float(stats.get("workers", 1.0)))
        target = max(0.25, float(stats.get("target_batch_seconds", 1.0)))
        batch_seconds = max(0.0, float(stats.get("batch_seconds", 0.0)))
        full_eval = max(0.0, float(stats.get("full_eval", 0.0)))
        eval_unique = max(0.0, float(stats.get("eval_unique", 0.0)))
        underutilized = (
            batch_seconds < (0.72 * target)
            and (
                full_eval < (0.90 * workers)
                or eval_unique < (1.20 * workers)
            )
        )
        overbudget = batch_seconds > (1.08 * target)

        population = float(stats.get("population", 1.0))
        quick_eval = float(stats.get("quick_eval", 0.0))
        mid_eval = float(stats.get("mid_eval", 0.0))
        eval_unique_safe = max(1.0, eval_unique)
        cache_dup = float(stats.get("cache_hits", 0.0)) + float(stats.get("dup_reuse", 0.0))
        reuse_ratio = cache_dup / eval_unique_safe
        probe_win_rate = float(stats.get("probe_win_rate", 0.0))

        base_qf = float(controller.quick_cycle_fraction)
        base_mf = float(controller.mid_cycle_fraction)
        base_qk = float(controller.quick_keep_ratio)
        base_mk = float(controller.mid_keep_ratio)
        base_kv = int(controller.key_variant_count)
        base_load = base_qf + base_mf + base_qk + base_mk + (float(base_kv) / 6.0)

        candidates = [
            (0.00, 0.00, 0.00, 0.00, 0, "neutral"),
            (0.03, 0.04, 0.05, 0.04, 1, "up"),
            (0.02, 0.03, 0.03, 0.03, 0, "up-soft"),
            (-0.02, -0.03, -0.04, -0.03, -1, "down"),
            (-0.01, -0.02, -0.02, -0.02, 0, "down-soft"),
        ]

        def clamp_candidate(qf: float, mf: float, qk: float, mk: float, kv: int) -> Tuple[float, float, float, float, int]:
            qf = clamp(qf, 0.05, 0.65)
            mf = clamp(max(mf, qf + 0.12), 0.18, 0.95)
            qk = clamp(qk, 0.24, 0.92)
            mk = clamp(mk, 0.12, 0.80)
            mk = min(mk, max(0.12, qk - 0.03))
            kv = max(1, min(6, int(kv)))
            return float(qf), float(mf), float(qk), float(mk), int(kv)

        best = None
        best_score = float("inf")
        for dqf, dmf, dqk, dmk, dkv, mode in candidates:
            qf, mf, qk, mk, kv = clamp_candidate(
                base_qf + dqf,
                base_mf + dmf,
                base_qk + dqk,
                base_mk + dmk,
                base_kv + dkv,
            )
            qf_scale = qf / max(1e-6, base_qf)
            qk_scale = qk / max(1e-6, base_qk)
            mf_scale = mf / max(1e-6, base_mf)
            mk_scale = mk / max(1e-6, base_mk)
            kv_scale = float(kv) / float(max(1, base_kv))
            est_quick = quick_eval * qf_scale * (0.60 + (0.40 * kv_scale))
            est_mid = mid_eval * qk_scale * mf_scale
            est_full = full_eval * mk_scale * (0.75 + (0.25 * kv_scale))

            features = self._feature_vector(
                quick_cycle_fraction=qf,
                mid_cycle_fraction=mf,
                quick_keep_ratio=qk,
                mid_keep_ratio=mk,
                key_variant_count=kv,
                population=population,
                quick_eval=est_quick,
                mid_eval=est_mid,
                full_eval=est_full,
                reuse_ratio=reuse_ratio,
                probe_win_rate=probe_win_rate,
                workers=workers,
            )
            pred_ratio = self._predict_ratio(coeff, features)
            load = qf + mf + qk + mk + (float(kv) / 6.0)
            load_gain = load - base_load
            objective = abs(pred_ratio - 1.0)
            if underutilized:
                objective += 0.38 * max(0.0, -load_gain)
                objective -= 0.22 * max(0.0, load_gain)
            if overbudget:
                objective += 0.22 * max(0.0, load_gain)
                objective -= 0.08 * max(0.0, -load_gain)
            if objective < best_score:
                best_score = objective
                best = (qf, mf, qk, mk, kv, pred_ratio, mode)

        if best is None:
            return
        qf, mf, qk, mk, kv, pred_ratio, mode = best
        blend = 0.55
        controller.quick_cycle_fraction = base_qf + (blend * (qf - base_qf))
        controller.mid_cycle_fraction = base_mf + (blend * (mf - base_mf))
        controller.quick_keep_ratio = base_qk + (blend * (qk - base_qk))
        controller.mid_keep_ratio = base_mk + (blend * (mk - base_mk))
        controller.key_variant_count = int(round(base_kv + (blend * float(kv - base_kv))))
        controller.clamp()
        self.last_prediction_ratio = float(pred_ratio)
        self.last_mode = str(mode)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "samples": int(len(self.history_x)),
            "last_prediction_ratio": float(self.last_prediction_ratio),
            "last_mode": str(self.last_mode),
        }


def _build_torch_runtime_tuner(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
) -> TorchRuntimeTuner:
    enabled = bool(
        config.statistical_predictive
        and config.auto_statistical_tuning
        and resource_plan.torch_available
        and resource_plan.parallel_workers > 1
    )
    return TorchRuntimeTuner(enabled=enabled)


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
            # Under stagnation we bias towards diversity, not local micro-tweaks.
            local_refine_chance = clamp(0.40 - (0.28 * stagnation_pressure), 0.08, 0.40)
            guide_sizes = [
                max(1, len(genome.extract_effective_algorithm()))
                for genome in parent_pool[: max(4, min(len(parent_pool), elite_size + 2))]
            ]
            if guide_sizes:
                guide_mean = sum(guide_sizes) / float(len(guide_sizes))
            else:
                guide_mean = 8.0
            immigrant_instruction_budget = max(
                4,
                min(24, int(round((guide_mean * 1.55) + 2.0))),
            )
            fallback_instruction_budget = max(
                4,
                min(20, int(round((guide_mean * 1.20) + 1.0))),
            )

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
                immigrant_ratio = 0.22 + (0.33 * stagnation_pressure)
                immigrant_target = max(1, int(round(self.population_size * immigrant_ratio)))
                injected = 0
                inject_attempts = 0
                while (
                    len(new_population) < self.population_size
                    and injected < immigrant_target
                    and inject_attempts < (immigrant_target * 12)
                ):
                    inject_attempts += 1
                    immigrant = _make_random_genome(immigrant_instruction_budget)
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
                immigrant = _make_random_genome(fallback_instruction_budget)
                immigrant.fitness = None
                immigrant.generation = gen + 1
                signature = _evaluation_signature(immigrant)
                if signature in seen:
                    continue
                seen.add(signature)
                new_population.append(immigrant)

            self.population = new_population[: self.population_size]


def _runtime_underutilization_boost(
    *,
    controller: PredictiveStageController,
    evolver: SafeGFSLEvolver,
    stage_stats: Dict[str, float],
) -> float:
    """Raise exploration and mutation when effective parallel utilization collapses."""
    workers = max(1.0, float(stage_stats.get("workers", 1.0)))
    target = max(0.25, float(stage_stats.get("target_batch_seconds", 1.0)))
    batch_seconds = max(0.0, float(stage_stats.get("batch_seconds", 0.0)))
    quick_eval = max(0.0, float(stage_stats.get("quick_eval", 0.0)))
    full_eval = max(0.0, float(stage_stats.get("full_eval", 0.0)))
    eval_unique = max(0.0, float(stage_stats.get("eval_unique", 0.0)))
    cache_dup = float(stage_stats.get("cache_hits", 0.0)) + float(stage_stats.get("dup_reuse", 0.0))
    reuse_ratio = cache_dup / max(1.0, eval_unique)

    underutilized = (
        batch_seconds > 0.0
        and batch_seconds < (0.74 * target)
        and (
            full_eval < (0.90 * workers)
            or eval_unique < (1.20 * workers)
            or quick_eval < (1.35 * workers)
        )
    )
    repetitive = reuse_ratio >= 1.0 and eval_unique < (1.25 * workers)
    if not underutilized and not repetitive:
        return 0.0

    full_gap = max(0.0, ((0.95 * workers) - full_eval) / max(1.0, 0.95 * workers))
    unique_gap = max(0.0, ((1.20 * workers) - eval_unique) / max(1.0, 1.20 * workers))
    quick_gap = max(0.0, ((1.35 * workers) - quick_eval) / max(1.0, 1.35 * workers))
    boost = clamp(
        0.22 + (0.78 * max(full_gap, unique_gap, quick_gap)) + (0.20 if repetitive else 0.0),
        0.12,
        1.0,
    )

    controller.quick_keep_ratio += 0.08 * boost
    controller.mid_keep_ratio += 0.07 * boost
    controller.quick_cycle_fraction += 0.03 * boost
    controller.mid_cycle_fraction += 0.04 * boost
    if boost >= 0.30:
        controller.key_variant_count += 1
    controller.clamp()

    mutation_bump = max(0.02, (0.80 * float(evolver.mutation_step))) + (0.04 * boost)
    evolver.mutation_rate = min(
        float(evolver.mutation_ceiling),
        max(float(evolver.mutation_floor), float(evolver.mutation_rate) + mutation_bump),
    )
    if boost >= 0.65:
        evolver.stagnation_count = max(
            int(evolver.stagnation_count),
            int(evolver.stagnation_patience),
        )
        evolver.signature_stagnation_count = max(
            int(evolver.signature_stagnation_count),
            int(evolver.stagnation_patience),
        )
    return float(boost)


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
    workers = max(1, min(workers, cpu_count))

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


def _full_stage_fraction(mid_cycle_fraction: float) -> float:
    """Full stage can stay below 100% because robust full checks happen at round selection."""
    return clamp(float(mid_cycle_fraction) + 0.10, 0.35, 0.70)


def _enforce_parallel_load_floor(
    *,
    controller: PredictiveStageController,
    population: int,
    workers: int,
) -> bool:
    """Ensure staged keep ratios can feed most workers with unique finalists."""
    total = max(0, int(population))
    lanes = max(1, int(workers))
    if total <= 1 or lanes <= 1:
        return False

    desired_full = max(2.0, min(float(total), float(lanes) * 0.95))
    expected_full = (
        float(total)
        * float(controller.quick_keep_ratio)
        * float(controller.mid_keep_ratio)
    )
    if expected_full >= desired_full:
        return False

    target_product = desired_full / max(1.0, float(total))
    current_product = max(
        1e-6,
        float(controller.quick_keep_ratio) * float(controller.mid_keep_ratio),
    )
    scale = min(2.6, max(1.0, math.sqrt(target_product / current_product)))
    if scale <= 1.01:
        return False

    controller.quick_keep_ratio *= scale
    controller.mid_keep_ratio *= scale
    controller.quick_cycle_fraction += 0.01 * min(1.0, scale - 1.0)
    controller.mid_cycle_fraction += 0.015 * min(1.0, scale - 1.0)
    controller.key_variant_count += 1
    controller.clamp()
    return True


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
    prev_batch_seconds = float(previous_stats.get("batch_seconds", 0.0))
    prev_target_seconds = max(0.25, float(previous_stats.get("target_batch_seconds", 1.0)))
    prev_quick_eval = float(previous_stats.get("quick_eval", 0.0))

    reuse_ratio = prev_cache_dup / max(1.0, prev_unique)
    uniqueness_ratio = prev_unique / max(1.0, prev_total_eval)
    underutilized = (
        prev_batch_seconds > 0.0
        and prev_batch_seconds < (0.72 * prev_target_seconds)
        and prev_quick_eval < (1.20 * float(max(1, int(workers))))
    )
    if underutilized:
        return selected, skipped, 1.0
    if prev_probe > 0.08 or reuse_ratio < 0.75:
        return selected, skipped, 1.0

    throttle = 0.74
    if reuse_ratio >= 1.00 and uniqueness_ratio <= 0.55:
        throttle = 0.62
    if reuse_ratio >= 1.30 and uniqueness_ratio <= 0.45:
        throttle = 0.50

    min_keep = max(4, min(len(selected), int(math.ceil(max(1, int(workers)) * 1.50))))
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
    store_metrics: bool = True,
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
                if store_metrics:
                    if rows:
                        setattr(genome, attr_name, _metrics_from_rows(rows))
                    elif hasattr(genome, attr_name):
                        delattr(genome, attr_name)
                elif hasattr(genome, attr_name):
                    delattr(genome, attr_name)
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
            store_metrics=store_metrics,
        )
        stats["unique_eval"] = float(len(unique_pending))

        for (_, genome), key in zip(unique_pending, unique_keys):
            score = float(genome.fitness or -float("inf"))
            rows: List[Dict[str, Any]] = []
            if store_metrics:
                metrics = getattr(genome, attr_name, [])
                rows = _metrics_rows(metrics)
            cache[key] = (score, rows)
            while len(cache) > max_cache_entries:
                cache.popitem(last=False)

            for dup in groups[key][1:]:
                if store_metrics:
                    if rows:
                        setattr(dup, attr_name, _metrics_from_rows(rows))
                    elif hasattr(dup, attr_name):
                        delattr(dup, attr_name)
                elif hasattr(dup, attr_name):
                    delattr(dup, attr_name)
                dup.fitness = score

    return stats


def _replace_genome_contents(target: GFSLGenome, source: GFSLGenome) -> None:
    target.genome_type = str(source.genome_type)
    target.instructions = [instruction.copy() for instruction in source.instructions]
    target.outputs = [
        (int(cat), int(dtype), int(idx))
        for cat, dtype, idx in source.outputs
    ]
    target.validator = copy.deepcopy(source.validator)
    target.instruction_activity = copy.deepcopy(source.instruction_activity)
    target.fitness = None
    _invalidate_genome_caches(target)
    for attr in ("_pcpl_metrics", "_attack_metrics"):
        if hasattr(target, attr):
            delattr(target, attr)
    if hasattr(source, "_pcpl_scaffold_injected"):
        target._pcpl_scaffold_injected = bool(getattr(source, "_pcpl_scaffold_injected"))  # type: ignore[attr-defined]
    elif hasattr(target, "_pcpl_scaffold_injected"):
        delattr(target, "_pcpl_scaffold_injected")


def _rebalance_pending_parallelism(
    *,
    pending: Sequence[Tuple[int, GFSLGenome]],
    workers: int,
    io_initializer,
    mutate_fn,
    random_genome_fn=None,
) -> int:
    """Mutate/replace duplicate genomes so unique eval tasks can saturate worker pool."""
    total = len(pending)
    if total <= 1 or workers <= 1:
        return 0
    target_ratio = 1.35
    if total >= int(math.ceil(float(workers) * 2.0)):
        target_ratio = 1.60
    desired_unique = min(total, max(2, int(math.ceil(float(workers) * target_ratio))))

    by_sig: Dict[str, List[GFSLGenome]] = {}
    for _, genome in pending:
        signature = _evaluation_signature(genome)
        by_sig.setdefault(signature, []).append(genome)

    seen = set(by_sig.keys())
    if len(seen) >= desired_unique:
        return 0

    duplicates: List[GFSLGenome] = []
    for genomes in by_sig.values():
        if len(genomes) > 1:
            duplicates.extend(genomes[1:])
    if not duplicates:
        return 0
    random.shuffle(duplicates)

    changed = 0
    for genome in duplicates:
        if len(seen) >= desired_unique:
            break
        accepted = False
        for _ in range(10):
            try:
                candidate = mutate_fn(genome)
                io_initializer(candidate)
                signature = _evaluation_signature(candidate)
                if signature in seen:
                    continue
                _replace_genome_contents(genome, candidate)
                seen.add(signature)
                changed += 1
                accepted = True
                break
            except Exception:
                continue
        if accepted:
            continue
        if random_genome_fn is None:
            continue
        for _ in range(4):
            try:
                candidate = random_genome_fn()
                io_initializer(candidate)
                signature = _evaluation_signature(candidate)
                if signature in seen:
                    continue
                _replace_genome_contents(genome, candidate)
                seen.add(signature)
                changed += 1
                break
            except Exception:
                continue
    return changed


def _idle_random_trial_budget(
    *,
    workers: int,
    pending_count: int,
    full_eval: float,
    probe_samples: float,
    elapsed_seconds: float,
    target_seconds: float,
) -> int:
    lanes = max(1, int(workers))
    if lanes <= 1:
        return 0
    if pending_count <= 0:
        return 0
    _ = (elapsed_seconds, target_seconds)

    current_load = max(0.0, float(full_eval) + float(probe_samples))
    desired_load = max(float(lanes) * 1.25, min(float(pending_count), float(lanes) * 2.0))
    gap = int(math.ceil(desired_load - current_load))
    if gap <= 0:
        return 0

    if pending_count < lanes:
        cap = max(2, int(math.ceil(float(lanes) * 1.25)))
    else:
        cap = max(2, lanes)
    cap = min(cap, max(2, pending_count * 2))
    return max(0, min(gap, cap))


def _evaluate_idle_random_trials(
    *,
    pending: Sequence[Tuple[int, GFSLGenome]],
    workers: int,
    full_eval: float,
    probe_samples: float,
    elapsed_seconds: float,
    target_seconds: float,
    backend: str,
    executor: Optional[concurrent.futures.Executor],
    worker_fn,
    build_task,
    attr_name: str,
    cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]",
    cache_key_fn,
    max_cache_entries: int,
    random_genome_fn: Callable[[], GFSLGenome],
) -> Dict[str, float]:
    stats = {
        "trial_count": 0.0,
        "trial_unique_eval": 0.0,
        "trial_cache_hits": 0.0,
        "trial_dup_reuse": 0.0,
        "trial_injected": 0.0,
    }
    budget = _idle_random_trial_budget(
        workers=workers,
        pending_count=len(pending),
        full_eval=full_eval,
        probe_samples=probe_samples,
        elapsed_seconds=elapsed_seconds,
        target_seconds=target_seconds,
    )
    if budget <= 0:
        return stats

    trial_pending: List[Tuple[int, GFSLGenome]] = []
    for idx in range(budget):
        try:
            trial_pending.append((idx, random_genome_fn()))
        except Exception:
            continue
    if not trial_pending:
        return stats

    eval_stats = _evaluate_pending_dedup_cache_parallel(
        pending=trial_pending,
        backend=backend,
        workers=workers,
        executor=executor,
        worker_fn=worker_fn,
        build_task=build_task,
        attr_name=attr_name,
        cache=cache,
        cache_key_fn=cache_key_fn,
        max_cache_entries=max_cache_entries,
        store_metrics=False,
    )
    stats["trial_count"] = float(len(trial_pending))
    stats["trial_unique_eval"] = float(eval_stats.get("unique_eval", 0.0))
    stats["trial_cache_hits"] = float(eval_stats.get("cache_hits", 0.0))
    stats["trial_dup_reuse"] = float(eval_stats.get("dup_reuse", 0.0))

    replace_targets = sorted(
        [genome for _, genome in pending],
        key=lambda genome: float(genome.fitness or -float("inf")),
    )
    challengers = sorted(
        [genome for _, genome in trial_pending],
        key=lambda genome: float(genome.fitness or -float("inf")),
        reverse=True,
    )
    if not replace_targets or not challengers:
        return stats

    seen_signatures = {_evaluation_signature(genome) for genome in replace_targets}
    replace_idx = 0
    injected = 0
    for challenger in challengers:
        if replace_idx >= len(replace_targets):
            break
        challenger_score = float(challenger.fitness or -float("inf"))
        target = replace_targets[replace_idx]
        target_score = float(target.fitness or -float("inf"))
        if challenger_score <= (target_score + 1e-9):
            break
        challenger_signature = _evaluation_signature(challenger)
        if challenger_signature in seen_signatures:
            continue

        _replace_genome_contents(target, challenger)
        target.fitness = challenger_score
        if hasattr(target, attr_name):
            delattr(target, attr_name)
        seen_signatures.add(challenger_signature)
        injected += 1
        replace_idx += 1

    stats["trial_injected"] = float(injected)
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


def _defender_eval_worker(
    task: Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome], bool]
    | Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome]]
) -> Tuple[float, List[Dict[str, Any]]]:
    if len(task) == 3:
        genome, scenarios, attacker = task
        emit_rows = True
    else:
        genome, scenarios, attacker, emit_rows = task
    ensure_genome_io(genome)
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
    if not bool(emit_rows):
        return float(score), []
    return float(score), _metrics_rows(metrics)


def _attacker_eval_worker(
    task: Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig], bool]
    | Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig]]
) -> Tuple[float, List[Dict[str, Any]]]:
    if len(task) == 3:
        attacker, defender, scenarios = task
        emit_rows = True
    else:
        attacker, defender, scenarios, emit_rows = task
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
    if not bool(emit_rows):
        return float(score), []
    return float(score), _metrics_rows(metrics)


def _defender_eval_worker_batch(
    task: Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome], bool]
    | Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    if len(task) == 3:
        genomes, scenarios, attacker = task
        emit_rows = True
    else:
        genomes, scenarios, attacker, emit_rows = task
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    results: List[Tuple[float, List[Dict[str, Any]]]] = []
    for genome in genomes:
        ensure_genome_io(genome)
        score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
        rows = _metrics_rows(metrics) if bool(emit_rows) else []
        results.append((float(score), rows))
    return results


def _attacker_eval_worker_batch(
    task: Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig], bool]
    | Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    if len(task) == 3:
        attackers, defender, scenarios = task
        emit_rows = True
    else:
        attackers, defender, scenarios, emit_rows = task
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
        rows = _metrics_rows(metrics) if bool(emit_rows) else []
        results.append((float(score), rows))
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
    store_metrics: bool = True,
) -> None:
    if not pending:
        return

    if backend == "off" or workers <= 1:
        for _, genome in pending:
            score, rows = worker_fn(build_task(genome))
            if store_metrics:
                if rows:
                    setattr(genome, attr_name, _metrics_from_rows(rows))
                elif hasattr(genome, attr_name):
                    delattr(genome, attr_name)
            elif hasattr(genome, attr_name):
                delattr(genome, attr_name)
            genome.fitness = float(score)
        return

    tasks = [build_task(genome) for _, genome in pending]
    if worker_fn in {_defender_eval_worker, _attacker_eval_worker}:
        normalized_tasks = []
        for task in tasks:
            if not isinstance(task, tuple):
                normalized_tasks.append(task)
                continue
            if len(task) >= 4:
                normalized_tasks.append((task[0], task[1], task[2], bool(task[3]) and bool(store_metrics)))
            else:
                normalized_tasks.append((*task, bool(store_metrics)))
        tasks = normalized_tasks
    map_chunk_size = max(1, len(tasks) // max(1, workers * 2))

    def run_with_executor(
        exec_obj: concurrent.futures.Executor,
        fn,
        task_list,
        *,
        process_chunksize: Optional[int] = None,
    ):
        if isinstance(exec_obj, concurrent.futures.ProcessPoolExecutor):
            chunk_size = map_chunk_size if process_chunksize is None else int(process_chunksize)
            return list(exec_obj.map(fn, task_list, chunksize=max(1, chunk_size)))
        return list(exec_obj.map(fn, task_list))

    def assign(results: Sequence[Tuple[float, List[Dict[str, Any]]]]) -> None:
        for (_, genome), (score, rows) in zip(pending, results):
            if store_metrics:
                if rows:
                    setattr(genome, attr_name, _metrics_from_rows(rows))
                elif hasattr(genome, attr_name):
                    delattr(genome, attr_name)
            elif hasattr(genome, attr_name):
                delattr(genome, attr_name)
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
        if workers >= 24:
            min_batch = 1
        elif workers >= 12:
            min_batch = 2
        elif workers >= 6:
            min_batch = 3
        else:
            min_batch = 4
        batch_size = max(min_batch, int(math.ceil(len(tasks) / float(max(1, workers)))))
        batch_size = min(32, batch_size)
        task_chunks = [tasks[pos : pos + batch_size] for pos in range(0, len(tasks), batch_size)]
        if worker_fn is _defender_eval_worker:
            batch_tasks = [
                ([item[0] for item in chunk], chunk[0][1], chunk[0][2], bool(chunk[0][3]))
                for chunk in task_chunks
            ]
        else:
            batch_tasks = [
                ([item[0] for item in chunk], chunk[0][1], chunk[0][2], bool(chunk[0][3]))
                for chunk in task_chunks
            ]

        if executor is not None:
            chunk_results = run_with_executor(
                executor,
                batch_worker,
                batch_tasks,
                process_chunksize=1,
            )
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
            chunk_results = run_with_executor(
                local_exec,
                batch_worker,
                batch_tasks,
                process_chunksize=1,
            )
        flat_results = [item for chunk in chunk_results for item in chunk]
        assign(flat_results)
        return

    def run_standard(exec_obj: concurrent.futures.Executor):
        return run_with_executor(
            exec_obj,
            worker_fn,
            tasks,
            process_chunksize=map_chunk_size,
        )

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


def _mean_metric(metrics: Sequence[Any], attr: str) -> float:
    if not metrics:
        return 0.0
    total = 0.0
    for item in metrics:
        if isinstance(item, dict):
            total += float(item.get(attr, 0.0))
        else:
            total += float(getattr(item, attr, 0.0))
    return total / float(len(metrics))


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


def _metrics_rows(metrics: Sequence[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for metric in metrics:
        if isinstance(metric, dict):
            rows.append(dict(metric))
            continue
        to_dict = getattr(metric, "to_dict", None)
        if callable(to_dict):
            rows.append(dict(to_dict()))
    return rows


def _scenario_table(metrics: Sequence[Any]) -> str:
    lines = []
    lines.append("| scenario | total | principle | sync | security | cost | op-cost | attacker-adv |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        if isinstance(metric, dict):
            scenario = str(metric.get("scenario", "n/a"))
            total = float(metric.get("total_score", 0.0))
            principle = float(metric.get("principle_score", 0.0))
            sync = float(metric.get("sync_score", 0.0))
            security = float(metric.get("security_score", 0.0))
            cost = float(metric.get("cost_score", 0.0))
            op_cost = float(metric.get("operation_cost_score", 0.0))
            attack_adv = float(metric.get("attacker_advantage_score", 0.0))
        else:
            scenario = str(getattr(metric, "scenario", "n/a"))
            total = float(getattr(metric, "total_score", 0.0))
            principle = float(getattr(metric, "principle_score", 0.0))
            sync = float(getattr(metric, "sync_score", 0.0))
            security = float(getattr(metric, "security_score", 0.0))
            cost = float(getattr(metric, "cost_score", 0.0))
            op_cost = float(getattr(metric, "operation_cost_score", 0.0))
            attack_adv = float(getattr(metric, "attacker_advantage_score", 0.0))
        lines.append(
            "| {name} | {total:.4f} | {principle:.4f} | {sync:.4f} | {security:.4f} | {cost:.4f} | {op_cost:.4f} | {attack_adv:.4f} |".format(
                name=scenario,
                total=total,
                principle=principle,
                sync=sync,
                security=security,
                cost=cost,
                op_cost=op_cost,
                attack_adv=attack_adv,
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


def _compact_round_summary_for_archive(entry: Dict[str, Any]) -> Dict[str, Any]:
    reference_payload = entry.get("reference_anchor", {})
    if not isinstance(reference_payload, dict):
        reference_payload = {}
    predictor_payload = entry.get("predictive_profile", {})
    if not isinstance(predictor_payload, dict):
        predictor_payload = {}
    adaptive_payload = entry.get("adaptive_attacker_budget", {})
    if not isinstance(adaptive_payload, dict):
        adaptive_payload = {}

    compact: Dict[str, Any] = {
        "round": int(entry.get("round", -1)),
        "timestamp": str(entry.get("timestamp", _utc_now_iso())),
        "defender_score": float(entry.get("defender_score", 0.0)),
        "defender_signature": str(entry.get("defender_signature", "")),
        "attacker_score": float(entry.get("attacker_score", 0.0)),
        "attacker_signature": str(entry.get("attacker_signature", "")),
        "round_dir": entry.get("round_dir"),
        "defender_stop_reason": str(entry.get("defender_stop_reason", "")),
        "attacker_stop_reason": str(entry.get("attacker_stop_reason", "")),
        "predictive_profile": predictor_payload,
        "adaptive_attacker_budget": adaptive_payload,
        "reference_anchor": {
            "score": float(reference_payload.get("score", 0.0)),
            "signature": str(reference_payload.get("signature", "")),
            "canonical_signature": str(reference_payload.get("canonical_signature", "")),
            "score_delta": float(reference_payload.get("score_delta", 0.0)),
        },
    }
    return compact


def _compact_archive_payload(archive: Dict[str, Any]) -> Dict[str, Any]:
    rounds_payload = archive.get("rounds", [])
    if not isinstance(rounds_payload, list):
        archive["rounds"] = []
        return archive

    compacted: List[Dict[str, Any]] = []
    for entry in rounds_payload:
        if not isinstance(entry, dict):
            continue
        compacted.append(_compact_round_summary_for_archive(entry))
    archive["rounds"] = compacted
    return archive


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
    reference_anchor_factory: Optional[Callable[[], GFSLGenome]] = None,
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

    if reference_anchor_factory is not None:
        try:
            anchor = reference_anchor_factory()
            push(anchor)
        except Exception:
            pass

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
    reference_score: Optional[float] = None,
    reference_signature: str = "",
    reference_metrics: Optional[Sequence[ScenarioMetrics]] = None,
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
    if reference_score is not None:
        ref_metrics = list(reference_metrics or [])
        lines.append("## Reference Anchor Comparison")
        lines.append("")
        lines.append(f"- reference defender score: `{float(reference_score):.6f}`")
        if reference_signature:
            lines.append(f"- reference defender signature: `{reference_signature}`")
        lines.append(
            "- score delta vs reference: `{delta:+.6f}`".format(
                delta=float(defender_score) - float(reference_score),
            )
        )
        if ref_metrics:
            lines.append("")
            lines.append("| metric | current | reference | delta |")
            lines.append("| --- | ---: | ---: | ---: |")
            key_specs = [
                ("principle_score", "principle"),
                ("sync_score", "sync"),
                ("security_score", "security"),
                ("cost_score", "cost"),
                ("runtime_score", "runtime"),
                ("stability_score", "stability"),
                ("attacker_advantage_score", "attacker-adv"),
            ]
            for key, label in key_specs:
                current_value = _mean_metric(defender_metrics, key)
                ref_value = _mean_metric(ref_metrics, key)
                lines.append(
                    "| {label} | {current:.4f} | {reference:.4f} | {delta:+.4f} |".format(
                        label=label,
                        current=current_value,
                        reference=ref_value,
                        delta=(current_value - ref_value),
                    )
                )
        finding_rows = _pcpl_improvement_findings(
            metrics_rows=_metrics_rows(defender_metrics),
            reference_metrics_rows=_metrics_rows(ref_metrics) if ref_metrics else None,
        )
        if finding_rows:
            lines.append("")
            lines.append("### Priorities")
            for item in finding_rows[:5]:
                lines.append(
                    "- {label}: weakness={weakness:.4f}, current={current:.4f}, delta_vs_reference={delta:+.4f}. {recommendation}".format(
                        label=str(item["label"]),
                        weakness=float(item["weakness"]),
                        current=float(item["current"]),
                        delta=float(item["delta_vs_reference"]),
                        recommendation=str(item["recommendation"]),
                    )
                )
        lines.append("")

    lines.append("## Defender Evolution")
    lines.append("")
    lines.append("| gen | best | principle | security | cost | sync-loss | attacker-adv | q frac/keep | m frac/keep | key var | probe | q-thr | eval q>m>f | keep q>m | probes | qskip | uniq | cache | dup | reb | t(s) | stop |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in defender_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {security:.4f} | {cost:.4f} | {sync_loss:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {qthr:.2f} | {stage_eval} | {stage_keep} | {probe_n} | {qskip} | {uniq} | {cache} | {dup} | {reb} | {secs:.2f} | {stop_reason} |".format(
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
                reb=int(row.get("parallel_rebalanced", 0)),
                secs=float(row.get("batch_seconds", 0.0)),
                stop_reason=str(row.get("stop_reason", "")),
            )
        )
    lines.append("")
    lines.append("## Attacker Evolution")
    lines.append("")
    lines.append("| gen | attack_score | lane_success | token_success | attacker_adv | q frac/keep | m frac/keep | key var | probe | q-thr | eval q>m>f | keep q>m | probes | qskip | uniq | cache | dup | reb | t(s) | stop |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in attacker_log:
        lines.append(
            "| {generation} | {attack_score:.4f} | {lane_success:.4f} | {token_success:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {qthr:.2f} | {stage_eval} | {stage_keep} | {probe_n} | {qskip} | {uniq} | {cache} | {dup} | {reb} | {secs:.2f} | {stop_reason} |".format(
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
                reb=int(row.get("parallel_rebalanced", 0)),
                secs=float(row.get("batch_seconds", 0.0)),
                stop_reason=str(row.get("stop_reason", "")),
            )
        )
    return "\n".join(lines) + "\n"


def _baseline_rows(scenarios: Sequence[ScenarioConfig]) -> List[Dict[str, Any]]:
    baselines = [
        (
            "reference-full",
            reference_pcpl_policy(),
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


def _metric_mean_from_rows(metrics_rows: Sequence[Dict[str, Any]], key: str, default: float = 0.0) -> float:
    values = [float(row.get(key, default)) for row in metrics_rows if key in row]
    if not values:
        return float(default)
    return float(sum(values) / float(max(1, len(values))))


def _baseline_row_by_name(
    baseline_rows: Sequence[Dict[str, Any]],
    *,
    name: str,
) -> Optional[Dict[str, Any]]:
    for row in baseline_rows:
        if str(row.get("name", "")) == str(name):
            return row
    return None


def _pcpl_improvement_findings(
    *,
    metrics_rows: Sequence[Dict[str, Any]],
    reference_metrics_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not metrics_rows:
        return []

    reference_rows = list(reference_metrics_rows or [])
    specs = [
        (
            "principle_score",
            True,
            "Principle Coherence",
            "Tighten one-of-x/permutation constraints and add explicit invariant proofs in main-paper PCPL steps.",
        ),
        (
            "sync_score",
            True,
            "Sync Robustness",
            "Strengthen absolute-time drift compensation and resync trigger windows with bounded worst-case analysis.",
        ),
        (
            "security_score",
            True,
            "Security Envelope",
            "Increase unpredictability with stronger phase/hash mixing and document adversarial assumptions directly in the paper.",
        ),
        (
            "cost_score",
            True,
            "Cost Efficiency",
            "Introduce adaptive active-ratio/hash-round throttling in the PCPL formal definition to preserve low-cost regimes.",
        ),
        (
            "runtime_score",
            True,
            "Runtime Headroom",
            "Constrain heavy control branches and reserve complex transformations for high-risk phases only.",
        ),
        (
            "stability_score",
            True,
            "Controller Stability",
            "Reduce controller-fail paths by specifying safe fallback transitions and bounded state-churn rules.",
        ),
        (
            "attacker_advantage_score",
            False,
            "Attacker Advantage",
            "Harden lane and token unpredictability; prioritize anti-inference defenses where attacker advantage remains high.",
        ),
        (
            "replay_rate",
            False,
            "Replay Exposure",
            "Expand replay-window constraints and enforce stronger freshness coupling between phase and lane output.",
        ),
        (
            "cross_lane_collision_rate",
            False,
            "Cross-Lane Collisions",
            "Increase lane-decoupling entropy and formally bound collision probability in PCPL lane schedule definitions.",
        ),
        (
            "shared_device_match_rate",
            False,
            "Shared-Device Impersonation",
            "Add stronger seed-lineage separation to prevent converging behavior across same-device derivations.",
        ),
        (
            "projected_sync_loss_rate",
            False,
            "Long-Horizon Sync Loss",
            "Refine long-horizon sync model and derive explicit tolerance bounds for extended runtime windows.",
        ),
    ]

    findings: List[Dict[str, Any]] = []
    for key, higher_is_better, label, recommendation in specs:
        current = _metric_mean_from_rows(metrics_rows, key, default=0.0)
        reference = _metric_mean_from_rows(reference_rows, key, default=current)
        if higher_is_better:
            weakness = clamp(1.0 - current, 0.0, 1.0)
            delta_vs_reference = current - reference
        else:
            weakness = clamp(current, 0.0, 1.0)
            delta_vs_reference = reference - current
        findings.append(
            {
                "metric": key,
                "label": label,
                "higher_is_better": bool(higher_is_better),
                "current": float(current),
                "reference": float(reference),
                "delta_vs_reference": float(delta_vs_reference),
                "weakness": float(weakness),
                "recommendation": recommendation,
            }
        )

    findings.sort(
        key=lambda item: (
            float(item.get("weakness", 0.0)),
            -float(item.get("delta_vs_reference", 0.0)),
        ),
        reverse=True,
    )
    return findings


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
    reference_baseline = _baseline_row_by_name(baseline_rows, name="reference-full")
    reference_metrics_rows: Sequence[Dict[str, Any]] = []
    if reference_baseline is not None:
        reference_metrics_rows = list(reference_baseline.get("metrics", []))
    if best_defender:
        best_metrics_rows = list(best_defender[0].get("metrics", []))
        defender_mean = _mean_metrics_row(best_metrics_rows)
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
        if reference_baseline is not None:
            conclusion_lines.append(
                "- delta vs reference-full baseline: {delta:+.6f} (best={best:.6f}, baseline={baseline:.6f})".format(
                    delta=float(best_defender[0].get("score", 0.0)) - float(reference_baseline.get("mean_score", 0.0)),
                    best=float(best_defender[0].get("score", 0.0)),
                    baseline=float(reference_baseline.get("mean_score", 0.0)),
                )
            )
        improvement_findings = _pcpl_improvement_findings(
            metrics_rows=best_metrics_rows,
            reference_metrics_rows=reference_metrics_rows,
        )
        if improvement_findings:
            conclusion_lines.append("")
            conclusion_lines.append("## Paper Improvement Priorities")
            for item in improvement_findings[:6]:
                conclusion_lines.append(
                    "- {label}: weakness={weakness:.4f}, current={current:.4f}, delta_vs_reference={delta:+.4f}. {recommendation}".format(
                        label=str(item["label"]),
                        weakness=float(item["weakness"]),
                        current=float(item["current"]),
                        delta=float(item["delta_vs_reference"]),
                        recommendation=str(item["recommendation"]),
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
    evolver_guide = None if bool(config.supervised_end_round_only) else guide
    evolver = SafeGFSLEvolver(
        population_size=config.population_size,
        supervised_guide=evolver_guide,
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
        reference_anchor_factory=build_reference_defender_genome,
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
    torch_tuner = _build_torch_runtime_tuner(
        config=config,
        resource_plan=resource_plan,
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
            frac = _full_stage_fraction(controller.mid_cycle_fraction)
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
            "random_trials": int(stage_stats.get("random_trials", 0.0)),
            "random_injected": int(stage_stats.get("random_injected", 0.0)),
            "parallel_rebalanced": int(stage_stats.get("parallel_rebalanced", 0.0)),
            "underutilization_boost": float(stage_stats.get("underutilization_boost", 0.0)),
            "mutation_rate": float(stage_stats.get("mutation_rate", evolver.mutation_rate)),
            "batch_seconds": float(stage_stats.get("batch_seconds", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][defender] gen={gen:03d} score={score:.5f} sec={security:.4f} cost={cost:.4f} attack_adv={attack_adv:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} reb={reb} rnd={rt}/{ri} ub={ub:.2f} mut={mut:.2f} t={secs:.2f}s".format(
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
                reb=row["parallel_rebalanced"],
                rt=row["random_trials"],
                ri=row["random_injected"],
                ub=row["underutilization_boost"],
                mut=row["mutation_rate"],
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
            "workers": float(resource_plan.parallel_workers),
            "quick_eval": float(len(pending)),
            "mid_eval": 0.0,
            "full_eval": 0.0,
            "quick_kept": 0.0,
            "mid_kept": 0.0,
            "probe_samples": 0.0,
            "probe_wins": 0.0,
            "random_trials": 0.0,
            "random_injected": 0.0,
            "novelty_quick": 0.0,
            "novelty_mid": 0.0,
            "eval_unique": 0.0,
            "cache_hits": 0.0,
            "dup_reuse": 0.0,
            "parallel_rebalanced": 0.0,
            "underutilization_boost": 0.0,
            "mutation_rate": float(evolver.mutation_rate),
            "quick_throttle": 1.0,
            "target_batch_seconds": float(config.target_generation_seconds),
            "batch_seconds": 0.0,
        }
        batch_controller_state = controller.to_dict()
        rebalanced = _rebalance_pending_parallelism(
            pending=pending,
            workers=resource_plan.parallel_workers,
            io_initializer=ensure_genome_io,
            mutate_fn=lambda genome: evolver.mutate(genome),
            random_genome_fn=lambda: _make_random_genome(
                max(4, int(math.ceil(float(config.initial_instructions) * 0.80)))
            ),
        )
        if rebalanced > 0:
            local_stage["parallel_rebalanced"] = float(rebalanced)

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
            _enforce_parallel_load_floor(
                controller=controller,
                population=len(pending),
                workers=resource_plan.parallel_workers,
            )
            batch_controller_state = controller.to_dict()

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
                store_metrics=False,
            )
            local_stage["full_eval"] = float(eval_stats["total"])
            local_stage["quick_kept"] = float(eval_stats["total"])
            local_stage["mid_kept"] = float(eval_stats["total"])
            local_stage["eval_unique"] += float(eval_stats["unique_eval"])
            local_stage["cache_hits"] += float(eval_stats["cache_hits"])
            local_stage["dup_reuse"] += float(eval_stats["dup_reuse"])
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    attacker,
                ),
                attr_name="_pcpl_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "def:trial:{sf}:{att}:{sig}".format(
                    sf=sf,
                    att=attacker_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(config.initial_instructions) * 0.85)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
            store_metrics=False,
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
        quick_min_keep = max(
            2,
            min(
                len(ranked_quick),
                int(math.ceil(max(2, resource_plan.parallel_workers) * 0.85)),
            ),
        )
        keep_quick_n = _stage_keep_count(
            len(ranked_quick),
            float(controller.quick_keep_ratio),
            min_keep=quick_min_keep,
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
            full_fp = _scenario_fingerprint(full_scenarios)
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    attacker,
                ),
                attr_name="_pcpl_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "def:trial-early-mid:{sf}:{att}:{sig}".format(
                    sf=sf,
                    att=attacker_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(config.initial_instructions) * 0.85)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            controller.apply_feedback(local_stage)
            torch_tuner.tune(controller, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
            store_metrics=False,
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
        mid_min_keep = max(
            1,
            min(
                len(ranked_mid),
                int(math.ceil(max(1, resource_plan.parallel_workers) * 0.85)),
            ),
        )
        keep_mid_n = _stage_keep_count(
            len(ranked_mid),
            float(controller.mid_keep_ratio),
            min_keep=mid_min_keep,
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
            full_fp = _scenario_fingerprint(full_scenarios)
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    full_scenarios,
                    attacker,
                ),
                attr_name="_pcpl_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "def:trial-early-full:{sf}:{att}:{sig}".format(
                    sf=sf,
                    att=attacker_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(config.initial_instructions) * 0.85)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            controller.apply_feedback(local_stage)
            torch_tuner.tune(controller, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
                store_metrics=False,
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

        trial_stats = _evaluate_idle_random_trials(
            pending=pending,
            workers=resource_plan.parallel_workers,
            full_eval=local_stage["full_eval"],
            probe_samples=local_stage["probe_samples"],
            elapsed_seconds=float(time.perf_counter() - eval_started),
            target_seconds=float(config.target_generation_seconds),
            backend=resource_plan.parallel_backend,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
            ),
            attr_name="_pcpl_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=full_fp: "def:trial:{sf}:{att}:{sig}".format(
                sf=sf,
                att=attacker_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
            random_genome_fn=lambda: _make_random_genome(
                max(4, int(math.ceil(float(config.initial_instructions) * 0.85)))
            ),
        )
        local_stage["random_trials"] = float(trial_stats["trial_count"])
        local_stage["random_injected"] = float(trial_stats["trial_injected"])
        local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
        local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
        local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])

        local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
        torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
        controller.apply_feedback(local_stage)
        torch_tuner.tune(controller, stats=local_stage)
        local_stage["underutilization_boost"] = _runtime_underutilization_boost(
            controller=controller,
            evolver=evolver,
            stage_stats=local_stage,
        )
        local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
    if guide is not None and bool(config.supervised_end_round_only):
        try:
            guide.observe_population(evolver.population)
        except Exception:
            pass
    stop_reason = getattr(evolver, "_early_stop_reason", None)
    stop_generation = getattr(evolver, "_early_stop_generation", None)
    if generation_log:
        generation_log[-1]["stop_reason"] = str(stop_reason) if stop_reason else ""
        generation_log[-1]["stop_generation"] = int(stop_generation) if stop_generation is not None else -1
    controller_state = controller.to_dict()
    controller_state["torch_tuner"] = torch_tuner.to_dict()
    evolver._predictive_controller_state = controller_state  # type: ignore[attr-defined]
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
    evolver_guide = None if bool(config.supervised_end_round_only) else guide
    evolver = SafeGFSLEvolver(
        population_size=config.attacker_population_size,
        supervised_guide=evolver_guide,
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
        reference_anchor_factory=None,
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
    torch_tuner = _build_torch_runtime_tuner(
        config=config,
        resource_plan=resource_plan,
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
            frac = _full_stage_fraction(controller.mid_cycle_fraction)
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
            "random_trials": int(stage_stats.get("random_trials", 0.0)),
            "random_injected": int(stage_stats.get("random_injected", 0.0)),
            "parallel_rebalanced": int(stage_stats.get("parallel_rebalanced", 0.0)),
            "underutilization_boost": float(stage_stats.get("underutilization_boost", 0.0)),
            "mutation_rate": float(stage_stats.get("mutation_rate", evolver.mutation_rate)),
            "batch_seconds": float(stage_stats.get("batch_seconds", 0.0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][attacker] gen={gen:03d} score={score:.5f} lane={lane:.4f} token={token:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} reb={reb} rnd={rt}/{ri} ub={ub:.2f} mut={mut:.2f} t={secs:.2f}s".format(
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
                reb=row["parallel_rebalanced"],
                rt=row["random_trials"],
                ri=row["random_injected"],
                ub=row["underutilization_boost"],
                mut=row["mutation_rate"],
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
            "workers": float(resource_plan.parallel_workers),
            "quick_eval": float(len(pending)),
            "mid_eval": 0.0,
            "full_eval": 0.0,
            "quick_kept": 0.0,
            "mid_kept": 0.0,
            "probe_samples": 0.0,
            "probe_wins": 0.0,
            "random_trials": 0.0,
            "random_injected": 0.0,
            "novelty_quick": 0.0,
            "novelty_mid": 0.0,
            "eval_unique": 0.0,
            "cache_hits": 0.0,
            "dup_reuse": 0.0,
            "parallel_rebalanced": 0.0,
            "underutilization_boost": 0.0,
            "mutation_rate": float(evolver.mutation_rate),
            "quick_throttle": 1.0,
            "target_batch_seconds": float(config.target_generation_seconds),
            "batch_seconds": 0.0,
        }
        batch_controller_state = controller.to_dict()
        rebalanced = _rebalance_pending_parallelism(
            pending=pending,
            workers=resource_plan.parallel_workers,
            io_initializer=ensure_attacker_genome_io,
            mutate_fn=lambda genome: evolver.mutate(genome),
            random_genome_fn=lambda: _make_random_genome(
                max(4, int(math.ceil(float(max(4, config.initial_instructions // 2)) * 0.85)))
            ),
        )
        if rebalanced > 0:
            local_stage["parallel_rebalanced"] = float(rebalanced)

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
            _enforce_parallel_load_floor(
                controller=controller,
                population=len(pending),
                workers=resource_plan.parallel_workers,
            )
            batch_controller_state = controller.to_dict()

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
                store_metrics=False,
            )
            local_stage["full_eval"] = float(eval_stats["total"])
            local_stage["quick_kept"] = float(eval_stats["total"])
            local_stage["mid_kept"] = float(eval_stats["total"])
            local_stage["eval_unique"] += float(eval_stats["unique_eval"])
            local_stage["cache_hits"] += float(eval_stats["cache_hits"])
            local_stage["dup_reuse"] += float(eval_stats["dup_reuse"])
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    defender,
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "atk:trial:{sf}:{def_sig}:{sig}".format(
                    sf=sf,
                    def_sig=defender_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(max(4, config.initial_instructions // 2)) * 0.95)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
            store_metrics=False,
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
        quick_min_keep = max(
            2,
            min(
                len(ranked_quick),
                int(math.ceil(max(2, resource_plan.parallel_workers) * 0.85)),
            ),
        )
        keep_quick_n = _stage_keep_count(
            len(ranked_quick),
            float(controller.quick_keep_ratio),
            min_keep=quick_min_keep,
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
            full_fp = _scenario_fingerprint(full_scenarios)
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    defender,
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "atk:trial-early-mid:{sf}:{def_sig}:{sig}".format(
                    sf=sf,
                    def_sig=defender_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(max(4, config.initial_instructions // 2)) * 0.95)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            controller.apply_feedback(local_stage)
            torch_tuner.tune(controller, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
            store_metrics=False,
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
        mid_min_keep = max(
            1,
            min(
                len(ranked_mid),
                int(math.ceil(max(1, resource_plan.parallel_workers) * 0.85)),
            ),
        )
        keep_mid_n = _stage_keep_count(
            len(ranked_mid),
            float(controller.mid_keep_ratio),
            min_keep=mid_min_keep,
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
            full_fp = _scenario_fingerprint(full_scenarios)
            trial_stats = _evaluate_idle_random_trials(
                pending=pending,
                workers=resource_plan.parallel_workers,
                full_eval=local_stage["full_eval"],
                probe_samples=0.0,
                elapsed_seconds=float(time.perf_counter() - eval_started),
                target_seconds=float(config.target_generation_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                    attacker_genome,
                    defender,
                    full_scenarios,
                ),
                attr_name="_attack_metrics",
                cache=stage_cache,
                cache_key_fn=lambda g, sf=full_fp: "atk:trial-early-full:{sf}:{def_sig}:{sig}".format(
                    sf=sf,
                    def_sig=defender_signature,
                    sig=_evaluation_signature(g),
                ),
                max_cache_entries=cache_limit,
                random_genome_fn=lambda: _make_random_genome(
                    max(4, int(math.ceil(float(max(4, config.initial_instructions // 2)) * 0.95)))
                ),
            )
            local_stage["random_trials"] = float(trial_stats["trial_count"])
            local_stage["random_injected"] = float(trial_stats["trial_injected"])
            local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
            local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
            local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])
            local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
            torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
            controller.apply_feedback(local_stage)
            torch_tuner.tune(controller, stats=local_stage)
            local_stage["underutilization_boost"] = _runtime_underutilization_boost(
                controller=controller,
                evolver=evolver,
                stage_stats=local_stage,
            )
            local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
                store_metrics=False,
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

        trial_stats = _evaluate_idle_random_trials(
            pending=pending,
            workers=resource_plan.parallel_workers,
            full_eval=local_stage["full_eval"],
            probe_samples=local_stage["probe_samples"],
            elapsed_seconds=float(time.perf_counter() - eval_started),
            target_seconds=float(config.target_generation_seconds),
            backend=resource_plan.parallel_backend,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
            ),
            attr_name="_attack_metrics",
            cache=stage_cache,
            cache_key_fn=lambda g, sf=full_fp: "atk:trial:{sf}:{def_sig}:{sig}".format(
                sf=sf,
                def_sig=defender_signature,
                sig=_evaluation_signature(g),
            ),
            max_cache_entries=cache_limit,
            random_genome_fn=lambda: _make_random_genome(
                max(4, int(math.ceil(float(max(4, config.initial_instructions // 2)) * 0.95)))
            ),
        )
        local_stage["random_trials"] = float(trial_stats["trial_count"])
        local_stage["random_injected"] = float(trial_stats["trial_injected"])
        local_stage["eval_unique"] += float(trial_stats["trial_unique_eval"])
        local_stage["cache_hits"] += float(trial_stats["trial_cache_hits"])
        local_stage["dup_reuse"] += float(trial_stats["trial_dup_reuse"])

        local_stage["batch_seconds"] = float(time.perf_counter() - eval_started)
        torch_tuner.observe(controller_state=batch_controller_state, stats=local_stage)
        controller.apply_feedback(local_stage)
        torch_tuner.tune(controller, stats=local_stage)
        local_stage["underutilization_boost"] = _runtime_underutilization_boost(
            controller=controller,
            evolver=evolver,
            stage_stats=local_stage,
        )
        local_stage["mutation_rate"] = float(evolver.mutation_rate)
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
    if guide is not None and bool(config.supervised_end_round_only):
        try:
            guide.observe_population(evolver.population)
        except Exception:
            pass
    stop_reason = getattr(evolver, "_early_stop_reason", None)
    stop_generation = getattr(evolver, "_early_stop_generation", None)
    if generation_log:
        generation_log[-1]["stop_reason"] = str(stop_reason) if stop_reason else ""
        generation_log[-1]["stop_generation"] = int(stop_generation) if stop_generation is not None else -1
    controller_state = controller.to_dict()
    controller_state["torch_tuner"] = torch_tuner.to_dict()
    evolver._predictive_controller_state = controller_state  # type: ignore[attr-defined]
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
    archive = _compact_archive_payload(archive)
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
    shared_executor: Optional[concurrent.futures.Executor] = None

    try:
        random.seed(config.seed)

        last_defender_score = -float("inf")
        last_attacker_score = -float("inf")
        last_defender_signature = ""
        last_attacker_signature = ""
        last_reference_score = -float("inf")
        last_reference_signature = ""
        last_round_dir = None

        current_attacker: Optional[GFSLGenome] = None
        if archive.get("attacker_elites"):
            try:
                current_attacker = _deserialize_genome(archive["attacker_elites"][0]["genome"])
                ensure_attacker_genome_io(current_attacker)
            except Exception:
                current_attacker = None

        if resource_plan.parallel_backend != "off" and resource_plan.parallel_workers > 1:
            shared_executor = _create_shared_executor(resource_plan)

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
            candidate_pending = list(enumerate(top_candidates))
            for candidate in top_candidates:
                ensure_genome_io(candidate)
            _evaluate_pending_parallel(
                pending=candidate_pending,
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                    g,
                    selection_scenarios,
                    best_attacker,
                ),
                attr_name="_pcpl_metrics",
                store_metrics=False,
            )
            selected_defender = max(
                top_candidates,
                key=lambda genome: float(genome.fitness or -float("inf")),
            )
            selected_score = float(selected_defender.fitness or -float("inf"))
            _, selected_metrics = evaluate_across_scenarios(
                selection_scenarios,
                selected_defender,
                attacker=best_attacker,
            )

            reference_defender = build_reference_defender_genome()
            ensure_genome_io(reference_defender)
            reference_score, reference_metrics = evaluate_across_scenarios(
                selection_scenarios,
                reference_defender,
                attacker=best_attacker,
            )

            attack_adv = _mean_metric(selected_metrics, "attacker_advantage_score")

            defender_signature = selected_defender.get_signature()
            attacker_signature = best_attacker.get_signature()
            reference_signature = reference_defender.get_signature()

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
                "metrics": _metrics_rows(selected_metrics),
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
                "round_dir": str(round_dir),
                "defender_log": defender_log,
                "attacker_log": attacker_log,
                "defender_stop_reason": (
                    str(defender_log[-1].get("stop_reason", ""))
                    if defender_log
                    else ""
                ),
                "attacker_stop_reason": (
                    str(attacker_log[-1].get("stop_reason", ""))
                    if attacker_log
                    else ""
                ),
                "metrics": _metrics_rows(selected_metrics),
                "predictive_profile": {
                    "defender": defender_profile,
                    "attacker": attacker_profile,
                    "selection_key_variants": selection_key_variants,
                },
                "adaptive_attacker_budget": attacker_budget_meta,
                "reference_anchor": {
                    "score": float(reference_score),
                    "signature": reference_signature,
                    "canonical_signature": _canonical_signature(reference_defender),
                    "score_delta": float(selected_score - reference_score),
                    "metrics": _metrics_rows(reference_metrics),
                },
            }
            archive.setdefault("rounds", []).append(
                _compact_round_summary_for_archive(round_summary)
            )

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
                reference_score=reference_score,
                reference_signature=reference_signature,
                reference_metrics=reference_metrics,
            )
            (round_dir / "round-report.md").write_text(round_report, encoding="utf-8")

            _save_archive(archive_path, archive)

            current_attacker = best_attacker
            last_defender_score = selected_score
            last_attacker_score = attack_adv
            last_defender_signature = defender_signature
            last_attacker_signature = attacker_signature
            last_reference_score = reference_score
            last_reference_signature = reference_signature
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
                "reference_score": (
                    float(last_reference_score)
                    if math.isfinite(float(last_reference_score))
                    else None
                ),
                "reference_signature": (
                    str(last_reference_signature) if str(last_reference_signature) else None
                ),
                "score_delta_vs_reference": (
                    float(last_defender_score - last_reference_score)
                    if math.isfinite(float(last_reference_score))
                    else None
                ),
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
        report_lines.append(
            "- supervised guide mode: `{mode}`".format(
                mode=(
                    "disabled"
                    if not bool(config.use_supervised_guide)
                    else (
                        "end-round-only"
                        if bool(config.supervised_end_round_only)
                        else "per-generation"
                    )
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
            defender_torch = defender_profile.get("torch_tuner", {})
            attacker_torch = attacker_profile.get("torch_tuner", {})
            if isinstance(defender_torch, dict) or isinstance(attacker_torch, dict):
                if not isinstance(defender_torch, dict):
                    defender_torch = {}
                if not isinstance(attacker_torch, dict):
                    attacker_torch = {}
                report_lines.append(
                    "- torch runtime tuner: defender(enabled={de}, samples={ds}, mode={dm}, pred_ratio={dr:.2f}) attacker(enabled={ae}, samples={as_}, mode={am}, pred_ratio={ar:.2f})".format(
                        de=bool(defender_torch.get("enabled", False)),
                        ds=int(defender_torch.get("samples", 0)),
                        dm=str(defender_torch.get("last_mode", "n/a")),
                        dr=float(defender_torch.get("last_prediction_ratio", 1.0)),
                        ae=bool(attacker_torch.get("enabled", False)),
                        as_=int(attacker_torch.get("samples", 0)),
                        am=str(attacker_torch.get("last_mode", "n/a")),
                        ar=float(attacker_torch.get("last_prediction_ratio", 1.0)),
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
        if math.isfinite(float(last_reference_score)):
            report_lines.append(f"- reference signature: `{last_reference_signature}`")
            report_lines.append(
                "- defender delta vs reference: `{delta:+.6f}`".format(
                    delta=float(last_defender_score - last_reference_score),
                )
            )

        best_defender_entry = archive.get("defender_elites", [])[:1]
        reference_row = _baseline_row_by_name(baseline_rows, name="reference-full")
        latest_round_summary = archive.get("rounds", [])[-1] if archive.get("rounds") else {}
        latest_reference_metrics_rows: Optional[Sequence[Dict[str, Any]]] = None
        if isinstance(latest_round_summary, dict):
            anchor_payload = latest_round_summary.get("reference_anchor", {})
            if isinstance(anchor_payload, dict):
                metrics_rows = anchor_payload.get("metrics", [])
                if isinstance(metrics_rows, list) and metrics_rows:
                    latest_reference_metrics_rows = metrics_rows
        if best_defender_entry:
            report_lines.append("")
            report_lines.append("## Paper Priorities")
            findings = _pcpl_improvement_findings(
                metrics_rows=list(best_defender_entry[0].get("metrics", [])),
                reference_metrics_rows=(
                    latest_reference_metrics_rows
                    if latest_reference_metrics_rows is not None
                    else (
                        list(reference_row.get("metrics", []))
                        if isinstance(reference_row, dict)
                        else None
                    )
                ),
            )
            for item in findings[:5]:
                report_lines.append(
                    "- {label}: weakness={weakness:.4f}, current={current:.4f}, delta_vs_reference={delta:+.4f}. {recommendation}".format(
                        label=str(item["label"]),
                        weakness=float(item["weakness"]),
                        current=float(item["current"]),
                        delta=float(item["delta_vs_reference"]),
                        recommendation=str(item["recommendation"]),
                    )
                )

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
            "reference_score": (
                float(last_reference_score)
                if math.isfinite(float(last_reference_score))
                else None
            ),
            "reference_signature": (
                str(last_reference_signature) if str(last_reference_signature) else None
            ),
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
