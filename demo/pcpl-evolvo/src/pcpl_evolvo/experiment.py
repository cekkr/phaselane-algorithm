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
import signal
import statistics
import threading
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

from evolvo import GFSLGenome, GFSLInstruction, GFSLEvolver, resolve_torch_accelerator


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
    executor_backend: str = "auto"  # auto|cpu|kompute|kompute-sim
    kompute_runtime_mode: str = "native"  # native|simulated|auto
    kompute_warn_on_fallback: bool = True
    kompute_fail_hard: bool = False
    kompute_keep_vram_state: bool = True
    kompute_min_native_stage_count: int = 1
    kompute_min_native_stage_share: float = 0.0
    kompute_max_unsupported_share: float = 1.0
    kompute_max_unsupported_count: int = -1
    kompute_force_cpu_on_partial_coverage: bool = False
    kompute_native_enable_decimal: bool = True
    kompute_native_enable_boolean_compare: bool = True
    kompute_native_enable_boolean_logic: bool = True
    kompute_native_enable_list_query: bool = True
    kompute_allow_process_pool: bool = False
    use_supervised_guide: bool = True
    supervised_end_round_only: bool = True
    preferred_device: str = "auto"  # auto|cpu|cuda|rocm|mps
    supervised_hidden_layers: Tuple[int, ...] = ()
    supervised_epochs: int = 0
    supervised_candidate_pool: int = 0
    supervised_capacity_auto_tune: bool = True
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
    sync_loss_gate_percentile: float = 0.60
    sync_loss_gate_penalty: float = 0.10
    sync_loss_gate_flat_boost: float = 0.06
    anti_neutrality_window: int = 10
    anti_neutrality_penalty: float = 0.030
    anti_neutrality_bonus: float = 0.015
    attacker_panel_size: int = 3
    attacker_panel_penalty: float = 0.16
    # Debug-only evaluator observability controls (disabled by default).
    debug_eval_timeout_seconds: float = 0.0
    debug_eval_log_interval_seconds: float = 0.0


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


_EVAL_EXECUTOR_KWARGS: Dict[str, Any] = {"compute_backend": "auto"}
_DEBUG_EVAL_TIMEOUT_SECONDS: float = 0.0
_DEBUG_EVAL_LOG_INTERVAL_SECONDS: float = 0.0


def _normalize_executor_backend(backend: str) -> str:
    backend_norm = str(backend).strip().lower()
    if backend_norm not in {"auto", "cpu", "kompute", "kompute-sim"}:
        return "auto"
    return backend_norm


def _sanitize_eval_executor_kwargs(kwargs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(kwargs or {})
    backend = _normalize_executor_backend(str(raw.get("compute_backend", "auto")))
    sanitized: Dict[str, Any] = {"compute_backend": backend}
    mode = raw.get("kompute_runtime_mode")
    if mode is not None:
        mode_norm = str(mode).strip().lower()
        if mode_norm in {"native", "simulated", "auto"}:
            sanitized["kompute_runtime_mode"] = mode_norm
    for key in (
        "kompute_warn_on_fallback",
        "kompute_fail_hard",
        "kompute_keep_vram_state",
        "kompute_force_cpu_on_partial_coverage",
        "kompute_native_enable_decimal",
        "kompute_native_enable_boolean_compare",
        "kompute_native_enable_boolean_logic",
        "kompute_native_enable_list_query",
    ):
        if key in raw:
            sanitized[key] = bool(raw[key])
    if "kompute_min_native_stage_count" in raw:
        try:
            sanitized["kompute_min_native_stage_count"] = max(
                0,
                int(raw["kompute_min_native_stage_count"]),
            )
        except Exception:
            pass
    if "kompute_max_unsupported_count" in raw:
        try:
            sanitized["kompute_max_unsupported_count"] = max(
                -1,
                int(raw["kompute_max_unsupported_count"]),
            )
        except Exception:
            pass
    if "kompute_min_native_stage_share" in raw:
        try:
            value = float(raw["kompute_min_native_stage_share"])
            sanitized["kompute_min_native_stage_share"] = max(0.0, min(1.0, value))
        except Exception:
            pass
    if "kompute_max_unsupported_share" in raw:
        try:
            value = float(raw["kompute_max_unsupported_share"])
            sanitized["kompute_max_unsupported_share"] = max(0.0, min(1.0, value))
        except Exception:
            pass
    return sanitized


def _set_eval_executor_kwargs(kwargs: Optional[Dict[str, Any]]) -> None:
    global _EVAL_EXECUTOR_KWARGS
    _EVAL_EXECUTOR_KWARGS = _sanitize_eval_executor_kwargs(kwargs)


def _get_eval_executor_kwargs() -> Dict[str, Any]:
    return dict(_EVAL_EXECUTOR_KWARGS)


def _set_debug_eval_runtime_settings(*, timeout_seconds: float, log_interval_seconds: float) -> None:
    global _DEBUG_EVAL_TIMEOUT_SECONDS, _DEBUG_EVAL_LOG_INTERVAL_SECONDS
    _DEBUG_EVAL_TIMEOUT_SECONDS = max(0.0, float(timeout_seconds))
    _DEBUG_EVAL_LOG_INTERVAL_SECONDS = max(0.0, float(log_interval_seconds))


def _build_eval_executor_kwargs(config: ExperimentConfig) -> Dict[str, Any]:
    backend = _normalize_executor_backend(config.executor_backend)
    runtime_mode = str(config.kompute_runtime_mode).strip().lower()
    if runtime_mode not in {"native", "simulated", "auto"}:
        runtime_mode = "native"
    kwargs: Dict[str, Any] = {
        "compute_backend": backend,
        "kompute_runtime_mode": runtime_mode,
        "kompute_warn_on_fallback": bool(config.kompute_warn_on_fallback),
        "kompute_fail_hard": bool(config.kompute_fail_hard),
        "kompute_keep_vram_state": bool(config.kompute_keep_vram_state),
        "kompute_min_native_stage_count": max(
            0,
            int(config.kompute_min_native_stage_count),
        ),
        "kompute_min_native_stage_share": max(
            0.0,
            min(1.0, float(config.kompute_min_native_stage_share)),
        ),
        "kompute_max_unsupported_share": max(
            0.0,
            min(1.0, float(config.kompute_max_unsupported_share)),
        ),
        "kompute_max_unsupported_count": max(
            -1,
            int(config.kompute_max_unsupported_count),
        ),
        "kompute_force_cpu_on_partial_coverage": bool(
            config.kompute_force_cpu_on_partial_coverage
        ),
        "kompute_native_enable_decimal": bool(config.kompute_native_enable_decimal),
        "kompute_native_enable_boolean_compare": bool(
            config.kompute_native_enable_boolean_compare
        ),
        "kompute_native_enable_boolean_logic": bool(
            config.kompute_native_enable_boolean_logic
        ),
        "kompute_native_enable_list_query": bool(
            config.kompute_native_enable_list_query
        ),
    }
    if backend == "kompute-sim":
        kwargs["kompute_runtime_mode"] = "simulated"
    return kwargs


MAX_DEFENDER_TOTAL_INSTRUCTIONS = 128
MAX_DEFENDER_EFFECTIVE_INSTRUCTIONS = 72
MAX_ATTACKER_TOTAL_INSTRUCTIONS = 96
MAX_ATTACKER_EFFECTIVE_INSTRUCTIONS = 56
DEFENDER_EVAL_TIMEOUT_SECONDS = 14.0
ATTACKER_EVAL_TIMEOUT_SECONDS = 10.0
QUICK_KEEP_PARALLEL_FLOOR_MULTIPLIER = 1.85
MID_KEEP_PARALLEL_FLOOR_MULTIPLIER = 1.55
DEFENDER_STAGE_TIMEOUT_QUICK_SECONDS = 4.0
DEFENDER_STAGE_TIMEOUT_MID_SECONDS = 8.0
DEFENDER_STAGE_TIMEOUT_FULL_SECONDS = 12.0
ATTACKER_STAGE_TIMEOUT_QUICK_SECONDS = 3.0
ATTACKER_STAGE_TIMEOUT_MID_SECONDS = 6.0
ATTACKER_STAGE_TIMEOUT_FULL_SECONDS = 9.0
PROCESS_EVAL_TIMEOUT_GRACE_SECONDS = 2.0
PROCESS_EVAL_WATCHDOG_POLL_SECONDS = 0.25
RUNTIME_OVERBUDGET_TOLERANCE = 1.12
KEY_VARIANT_FLOOR = 2
TARGET_CALIBRATION_MIN_OBSERVATIONS = 3
TARGET_CALIBRATION_MAX_OBSERVATIONS = 6
TARGET_CALIBRATION_MEDIAN_SCALE = 0.85
TARGET_CALIBRATION_MAX_MULTIPLIER = 16.0
SYNC_LOSS_GATE_MIN_PERCENTILE = 0.30
SYNC_LOSS_GATE_MAX_PERCENTILE = 0.90
ANTI_NEUTRALITY_MIN_WINDOW = 4


def _complexity_limits(role: str) -> Tuple[int, int]:
    role_norm = str(role).strip().lower()
    if role_norm == "attacker":
        return (
            int(MAX_ATTACKER_TOTAL_INSTRUCTIONS),
            int(MAX_ATTACKER_EFFECTIVE_INSTRUCTIONS),
        )
    return (
        int(MAX_DEFENDER_TOTAL_INSTRUCTIONS),
        int(MAX_DEFENDER_EFFECTIVE_INSTRUCTIONS),
    )


def _complexity_cut_score(*, role: str, total: int, effective: int) -> float:
    total_limit, effective_limit = _complexity_limits(role)
    total_excess = max(0, int(total) - int(total_limit))
    effective_excess = max(0, int(effective) - int(effective_limit))
    excess = float(total_excess + (2 * effective_excess))
    base = -1.20 if str(role).lower() == "defender" else -0.45
    return float(base - min(2.0, 0.015 * excess))


def _cached_effective_instruction_count(genome: GFSLGenome) -> Optional[int]:
    cached = getattr(genome, "_effective_instructions", None)
    if cached is None:
        return None
    if isinstance(cached, int):
        return max(0, int(cached))
    try:
        return max(0, int(len(cached)))  # type: ignore[arg-type]
    except Exception:
        return None


def _estimated_effective_instruction_count(genome: GFSLGenome) -> int:
    cached = _cached_effective_instruction_count(genome)
    if cached is not None:
        return int(cached)
    total = max(0, int(len(genome.instructions)))
    if total == 0:
        return 0
    return int(max(1, min(total, math.ceil(float(total) * 0.62))))


def _is_genome_over_complexity_budget(genome: GFSLGenome, *, role: str) -> Tuple[bool, float]:
    total_limit, effective_limit = _complexity_limits(role)
    total = len(genome.instructions)
    estimated_effective = _estimated_effective_instruction_count(genome)
    if total <= int(total_limit):
        if estimated_effective <= int(effective_limit):
            return False, 0.0
        return True, _complexity_cut_score(
            role=role,
            total=total,
            effective=estimated_effective,
        )
    return True, _complexity_cut_score(
        role=role,
        total=total,
        effective=estimated_effective,
    )


def _timeout_cut_score(*, role: str) -> float:
    return -3.00 if str(role).lower() == "defender" else -1.50


def _normalize_eval_stage(stage: Optional[str]) -> str:
    stage_norm = str(stage or "").strip().lower()
    if stage_norm in {"quick", "mid", "full"}:
        return stage_norm
    return "full"


def _stage_eval_timeout_seconds(*, role: str, stage: Optional[str]) -> float:
    role_norm = str(role).strip().lower()
    stage_norm = _normalize_eval_stage(stage)
    if role_norm == "attacker":
        if stage_norm == "quick":
            return float(ATTACKER_STAGE_TIMEOUT_QUICK_SECONDS)
        if stage_norm == "mid":
            return float(ATTACKER_STAGE_TIMEOUT_MID_SECONDS)
        return float(ATTACKER_STAGE_TIMEOUT_FULL_SECONDS)
    if stage_norm == "quick":
        return float(DEFENDER_STAGE_TIMEOUT_QUICK_SECONDS)
    if stage_norm == "mid":
        return float(DEFENDER_STAGE_TIMEOUT_MID_SECONDS)
    return float(DEFENDER_STAGE_TIMEOUT_FULL_SECONDS)


def _task_eval_stage(task: Any) -> str:
    if not isinstance(task, tuple):
        return "full"
    if len(task) >= 5 and isinstance(task[4], str):
        return _normalize_eval_stage(task[4])
    if len(task) >= 4 and isinstance(task[3], str):
        return _normalize_eval_stage(task[3])
    return "full"


def _task_timeout_seconds(task: Any, *, worker_fn) -> float:
    stage = _task_eval_stage(task)
    if worker_fn is _defender_eval_worker:
        return _stage_eval_timeout_seconds(role="defender", stage=stage)
    if worker_fn is _attacker_eval_worker:
        return _stage_eval_timeout_seconds(role="attacker", stage=stage)
    return max(float(DEFENDER_EVAL_TIMEOUT_SECONDS), float(ATTACKER_EVAL_TIMEOUT_SECONDS))


def _timeout_fallback_result(task: Any, *, worker_fn) -> Tuple[float, List[Dict[str, Any]]]:
    _ = task
    if worker_fn is _defender_eval_worker:
        return _timeout_cut_score(role="defender"), []
    if worker_fn is _attacker_eval_worker:
        return _timeout_cut_score(role="attacker"), []
    return -1e9, []


def _can_use_eval_timeout() -> bool:
    if os.name == "nt":
        return False
    if threading.current_thread() is not threading.main_thread():
        return False
    return bool(hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer"))


def _evaluate_with_timeout(
    scenarios: Sequence[ScenarioConfig],
    genome: GFSLGenome,
    *,
    attacker: Optional[GFSLGenome],
    timeout_seconds: float,
) -> Tuple[float, Sequence[Any]]:
    runtime_executor_kwargs = _get_eval_executor_kwargs()
    timeout = max(0.0, float(timeout_seconds))
    if timeout <= 0.0 or not _can_use_eval_timeout():
        return evaluate_across_scenarios(
            scenarios,
            genome,
            attacker=attacker,
            executor_kwargs=runtime_executor_kwargs,
        )

    def _raise_timeout(signum, frame):  # type: ignore[no-untyped-def]
        _ = (signum, frame)
        raise TimeoutError("evaluation-timeout")

    previous_handler = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        return evaluate_across_scenarios(
            scenarios,
            genome,
            attacker=attacker,
            executor_kwargs=runtime_executor_kwargs,
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def _evaluate_across_scenarios_runtime(
    scenarios: Sequence[ScenarioConfig],
    genome: Optional[GFSLGenome],
    *,
    fixed_decision: Optional[PolicyDecision] = None,
    attacker: Optional[GFSLGenome] = None,
) -> Tuple[float, List[ScenarioMetrics]]:
    return evaluate_across_scenarios(
        scenarios,
        genome,
        fixed_decision=fixed_decision,
        attacker=attacker,
        executor_kwargs=_get_eval_executor_kwargs(),
    )


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
        self.key_variant_count = max(int(KEY_VARIANT_FLOOR), min(6, int(self.key_variant_count)))

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

        over_budget = batch_seconds > (float(RUNTIME_OVERBUDGET_TOLERANCE) * target_seconds)
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
            if self.key_variant_count > int(KEY_VARIANT_FLOOR):
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
            (-0.02, -0.03, -0.04, -0.03, 0, "down"),
            (-0.01, -0.02, -0.02, -0.02, 0, "down-soft"),
        ]

        def clamp_candidate(qf: float, mf: float, qk: float, mk: float, kv: int) -> Tuple[float, float, float, float, int]:
            qf = clamp(qf, 0.05, 0.65)
            mf = clamp(max(mf, qf + 0.12), 0.18, 0.95)
            qk = clamp(qk, 0.24, 0.92)
            mk = clamp(mk, 0.12, 0.80)
            mk = min(mk, max(0.12, qk - 0.03))
            kv = max(int(KEY_VARIANT_FLOOR), min(6, int(kv)))
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
            kv_scale = float(kv) / float(max(int(KEY_VARIANT_FLOOR), base_kv))
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


def _runtime_underutilization_boost(
    *,
    controller: PredictiveStageController,
    evolver: GFSLEvolver,
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
    try:
        backend, _device, accelerator_available, torch_available = resolve_torch_accelerator(
            preferred_device
        )
    except Exception:
        return "none", False, False

    if not accelerator_available:
        return "none", False, torch_available
    return backend, True, torch_available


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

    executor_backend = _normalize_executor_backend(config.executor_backend)
    allow_kompute_process_pool = bool(config.kompute_allow_process_pool) or (
        str(os.environ.get("EVOLVO_KOMPUTE_ALLOW_PROCESS_POOL", "")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if (
        executor_backend in {"kompute", "kompute-sim"}
        and resolved_backend == "process"
        and not allow_kompute_process_pool
    ):
        # Vulkan drivers can become unstable under heavy process forking; prefer threads here.
        resolved_backend = "thread"
        workers = max(1, min(workers, 8))

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


def _process_pool_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    ctx_name = _process_pool_context_name()
    if ctx_name is not None:
        try:
            kwargs["mp_context"] = multiprocessing.get_context(ctx_name)
        except Exception:
            pass
    return kwargs


def _process_pool_initializer(eval_executor_kwargs: Dict[str, Any]) -> None:
    _set_eval_executor_kwargs(eval_executor_kwargs)


def _create_process_pool_executor(
    max_workers: int,
    *,
    eval_executor_kwargs: Optional[Dict[str, Any]] = None,
) -> concurrent.futures.ProcessPoolExecutor:
    kwargs = _process_pool_kwargs()
    init_kwargs = _sanitize_eval_executor_kwargs(
        eval_executor_kwargs
        if eval_executor_kwargs is not None
        else _get_eval_executor_kwargs()
    )
    kwargs["initializer"] = _process_pool_initializer
    kwargs["initargs"] = (init_kwargs,)
    return concurrent.futures.ProcessPoolExecutor(max_workers=max_workers, **kwargs)


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
    variants = max(int(KEY_VARIANT_FLOOR), int(key_variant_count))
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


def _parallel_keep_floor(
    *,
    total: int,
    workers: int,
    multiplier: float,
    minimum: int,
) -> int:
    count = max(0, int(total))
    if count <= 0:
        return 0
    lanes = max(1, int(workers))
    target = max(int(minimum), int(math.ceil(float(lanes) * float(multiplier))))
    return max(1, min(count, target))


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

    desired_full = max(2.0, min(float(total), float(lanes) * 1.10))
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
    desired_load = max(float(lanes) * 1.60, min(float(pending_count), float(lanes) * 2.60))
    gap = int(math.ceil(desired_load - current_load))
    if gap <= 0:
        return 0

    if pending_count < lanes:
        cap = max(2, int(math.ceil(float(lanes) * 1.50)))
    else:
        cap = max(2, int(math.ceil(float(lanes) * 1.75)))
    cap = min(cap, max(2, pending_count * 3))
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


def _quantile(values: Sequence[float], q: float) -> float:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return 0.0
    if len(finite) == 1:
        return float(finite[0])
    qq = clamp(float(q), 0.0, 1.0)
    pos = qq * float(len(finite) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(finite[lo])
    blend = pos - float(lo)
    return float((1.0 - blend) * float(finite[lo]) + blend * float(finite[hi]))


def _recent_flatness_ratio(
    generation_log: Sequence[Dict[str, Any]],
    *,
    score_key: str,
    window: int,
) -> float:
    if not generation_log:
        return 0.0
    size = max(3, int(window))
    recent = list(generation_log[-size:])
    scores = [
        float(row.get(score_key, -float("inf")))
        for row in recent
        if math.isfinite(float(row.get(score_key, -float("inf"))))
    ]
    if len(scores) < 2:
        return 0.0
    gain = max(scores) - min(scores)
    levels = len({round(score, 8) for score in scores})
    level_ratio = float(levels) / float(max(1, len(scores)))
    gain_component = clamp(1.0 - (gain / 0.00090), 0.0, 1.0)
    diversity_component = clamp(1.0 - level_ratio, 0.0, 1.0)
    return clamp((0.60 * gain_component) + (0.40 * diversity_component), 0.0, 1.0)


def _mean_metric_from_rows(
    metrics_rows: Sequence[Dict[str, Any]],
    key: str,
    default: float = 0.0,
) -> float:
    if not metrics_rows:
        return float(default)
    values = [float(row.get(key, default)) for row in metrics_rows]
    if not values:
        return float(default)
    return float(sum(values) / float(max(1, len(values))))


def _defender_row_fingerprint(row: Dict[str, Any]) -> Optional[Tuple[float, ...]]:
    required = (
        "sync_score",
        "horizon_sync",
        "projected_sync_loss",
        "attacker_adv",
        "stability_score",
    )
    if any(key not in row for key in required):
        return None
    return (
        round(float(row.get("sync_score", 0.0)), 3),
        round(float(row.get("horizon_sync", 0.0)), 3),
        round(float(row.get("projected_sync_loss", 0.0)), 3),
        round(float(row.get("attacker_adv", 0.0)), 3),
        round(float(row.get("stability_score", 0.0)), 3),
    )


def _defender_metrics_fingerprint(metrics_rows: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, ...]]:
    if not metrics_rows:
        return None
    return (
        round(_mean_metric_from_rows(metrics_rows, "sync_score"), 3),
        round(_mean_metric_from_rows(metrics_rows, "horizon_sync_score"), 3),
        round(_mean_metric_from_rows(metrics_rows, "projected_sync_loss_rate"), 3),
        round(_mean_metric_from_rows(metrics_rows, "attacker_advantage_score"), 3),
        round(_mean_metric_from_rows(metrics_rows, "stability_score"), 3),
    )


def _apply_defender_sync_loss_gate(
    *,
    full_pending: Sequence[Tuple[int, GFSLGenome]],
    generation_log: Sequence[Dict[str, Any]],
    config: ExperimentConfig,
) -> Dict[str, float]:
    if not full_pending:
        return {
            "sync_gate_penalized": 0.0,
            "sync_gate_threshold": 0.0,
            "sync_gate_percentile": 0.0,
            "sync_gate_flatness": 0.0,
        }

    losses: List[float] = []
    by_genome: Dict[int, float] = {}
    for _, genome in full_pending:
        metrics = getattr(genome, "_pcpl_metrics", [])
        rows = _metrics_rows(metrics)
        if not rows:
            continue
        loss = _mean_metric_from_rows(rows, "projected_sync_loss_rate", default=1.0)
        by_genome[id(genome)] = float(loss)
        losses.append(float(loss))

    if not losses:
        return {
            "sync_gate_penalized": 0.0,
            "sync_gate_threshold": 0.0,
            "sync_gate_percentile": 0.0,
            "sync_gate_flatness": 0.0,
        }

    flatness = _recent_flatness_ratio(
        generation_log,
        score_key="best_score",
        window=max(6, int(config.anti_neutrality_window)),
    )
    base_percentile = clamp(
        float(config.sync_loss_gate_percentile),
        float(SYNC_LOSS_GATE_MIN_PERCENTILE),
        float(SYNC_LOSS_GATE_MAX_PERCENTILE),
    )
    dynamic_percentile = clamp(
        base_percentile - (0.18 * flatness),
        float(SYNC_LOSS_GATE_MIN_PERCENTILE),
        float(SYNC_LOSS_GATE_MAX_PERCENTILE),
    )
    threshold = _quantile(losses, dynamic_percentile)
    base_penalty = max(0.0, float(config.sync_loss_gate_penalty))
    flat_boost = max(0.0, float(config.sync_loss_gate_flat_boost))

    penalized = 0
    for _, genome in full_pending:
        loss = by_genome.get(id(genome))
        if loss is None:
            continue
        if loss <= (threshold + 1e-9):
            continue
        overflow = max(0.0, float(loss - threshold))
        penalty = base_penalty + (flat_boost * flatness) + min(0.20, 0.35 * overflow)
        genome.fitness = float(genome.fitness or -float("inf")) - float(penalty)
        penalized += 1

    return {
        "sync_gate_penalized": float(penalized),
        "sync_gate_threshold": float(threshold),
        "sync_gate_percentile": float(dynamic_percentile),
        "sync_gate_flatness": float(flatness),
    }


def _apply_defender_anti_neutrality(
    *,
    full_pending: Sequence[Tuple[int, GFSLGenome]],
    generation_log: Sequence[Dict[str, Any]],
    config: ExperimentConfig,
) -> Dict[str, float]:
    if not full_pending:
        return {
            "neutrality_penalized": 0.0,
            "neutrality_rewarded": 0.0,
        }

    window = max(int(ANTI_NEUTRALITY_MIN_WINDOW), int(config.anti_neutrality_window))
    recent = list(generation_log[-window:])
    recent_fingerprints = {
        fp
        for row in recent
        for fp in [_defender_row_fingerprint(row)]
        if fp is not None
    }
    recent_sync = [float(row.get("sync_score", 0.0)) for row in recent if "sync_score" in row]
    recent_horizon = [float(row.get("horizon_sync", 0.0)) for row in recent if "horizon_sync" in row]
    recent_loss = [float(row.get("projected_sync_loss", 1.0)) for row in recent if "projected_sync_loss" in row]
    recent_attack = [float(row.get("attacker_adv", 1.0)) for row in recent if "attacker_adv" in row]
    baseline_sync = float(sum(recent_sync) / float(max(1, len(recent_sync))))
    baseline_horizon = float(sum(recent_horizon) / float(max(1, len(recent_horizon))))
    baseline_loss = float(sum(recent_loss) / float(max(1, len(recent_loss))))
    baseline_attack = float(sum(recent_attack) / float(max(1, len(recent_attack))))

    penalty = max(0.0, float(config.anti_neutrality_penalty))
    bonus = max(0.0, float(config.anti_neutrality_bonus))
    penalized = 0
    rewarded = 0
    for _, genome in full_pending:
        metrics = getattr(genome, "_pcpl_metrics", [])
        rows = _metrics_rows(metrics)
        if not rows:
            continue
        fingerprint = _defender_metrics_fingerprint(rows)
        score = float(genome.fitness or -float("inf"))
        if fingerprint is not None and fingerprint in recent_fingerprints:
            genome.fitness = score - penalty
            penalized += 1
            continue

        sync_score = _mean_metric_from_rows(rows, "sync_score", default=0.0)
        horizon_score = _mean_metric_from_rows(rows, "horizon_sync_score", default=0.0)
        projected_loss = _mean_metric_from_rows(rows, "projected_sync_loss_rate", default=1.0)
        attacker_adv = _mean_metric_from_rows(rows, "attacker_advantage_score", default=1.0)
        moved = (
            (sync_score > (baseline_sync + 0.003))
            or (horizon_score > (baseline_horizon + 0.003))
            or (projected_loss < (baseline_loss - 0.004))
            or (attacker_adv < (baseline_attack - 0.003))
        )
        if moved and bonus > 0.0:
            genome.fitness = score + bonus
            rewarded += 1

    return {
        "neutrality_penalized": float(penalized),
        "neutrality_rewarded": float(rewarded),
    }


@dataclass
class RuntimeTargetCalibrator:
    """Auto-calibrate target generation seconds from first local batches."""

    base_target_seconds: float
    label: str = ""
    min_observations: int = TARGET_CALIBRATION_MIN_OBSERVATIONS
    max_observations: int = TARGET_CALIBRATION_MAX_OBSERVATIONS
    median_scale: float = TARGET_CALIBRATION_MEDIAN_SCALE
    max_multiplier: float = TARGET_CALIBRATION_MAX_MULTIPLIER
    target_seconds: float = field(init=False)
    observations: List[float] = field(default_factory=list)
    calibrated: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.target_seconds = max(0.25, float(self.base_target_seconds))

    def observe(self, batch_seconds: float) -> float:
        seconds = float(batch_seconds)
        if not math.isfinite(seconds) or seconds <= 0.0:
            return float(self.target_seconds)
        if self.calibrated:
            return float(self.target_seconds)

        self.observations.append(seconds)
        if len(self.observations) < max(1, int(self.min_observations)):
            return float(self.target_seconds)

        sample = self.observations[: max(1, int(self.max_observations))]
        median_seconds = float(statistics.median(sample))
        scaled = max(
            float(self.base_target_seconds),
            float(self.median_scale) * median_seconds,
        )
        upper = max(
            float(self.base_target_seconds) * float(self.max_multiplier),
            float(self.base_target_seconds) + 1.0,
        )
        previous = float(self.target_seconds)
        self.target_seconds = clamp(scaled, 0.25, upper)
        self.calibrated = True
        if abs(self.target_seconds - previous) >= 0.10:
            role = str(self.label).strip() or "evolution"
            print(
                "[pcpl-evolvo] runtime auto-calibration role={role} target_gen_s {prev:.2f}->{curr:.2f} sample_median={median:.2f}s n={count}".format(
                    role=role,
                    prev=previous,
                    curr=float(self.target_seconds),
                    median=median_seconds,
                    count=len(sample),
                )
            )
        return float(self.target_seconds)


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
    plateau_floor = max(8, int(math.ceil(float(total_generations) * 0.25)))

    top_n = max(4, min(len(population), int(math.ceil(len(population) * 0.20))))
    top_slice = list(population[:top_n])
    if not top_slice:
        return False, ""
    top_unique_ratio = float(
        len({_evaluation_signature(genome) for genome in top_slice})
    ) / float(max(1, len(top_slice)))

    identical_floor = max(6, int(math.ceil(float(total_generations) * 0.16)))
    if len(generation_log) >= identical_floor:
        ident_window = min(
            len(generation_log),
            max(4, int(math.ceil(float(total_generations) * 0.10))),
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

    signature_floor = max(8, int(math.ceil(float(total_generations) * 0.18)))
    if len(generation_log) >= signature_floor:
        sig_window = min(
            len(generation_log),
            max(5, int(math.ceil(float(total_generations) * 0.14))),
        )
        recent_sig = list(generation_log[-sig_window:])
        recent_stall_immigrants = sum(
            max(0.0, float(row.get("stall_immigrants", 0.0)))
            for row in recent_sig
        )
        score_levels = {
            round(float(row.get(score_key, 0.0)), 8)
            for row in recent_sig
        }
        signatures = {
            str(row.get("best_signature", ""))
            for row in recent_sig
            if str(row.get("best_signature", ""))
        }
        if (
            len(score_levels) == 1
            and len(signatures) == 1
            and probe_win_rate <= 0.08
            and recent_stall_immigrants <= 0.0
        ):
            return True, "same-signature-stall"

    if (
        score_gain <= float(min_gain)
        and probe_win_rate <= 0.08
        and reuse_ratio >= 0.82
        and uniqueness_ratio <= 0.62
        and top_unique_ratio <= 0.74
    ):
        return True, "plateau-reuse"

    if (
        score_gain <= (0.55 * float(min_gain))
        and probe_win_rate <= 0.06
        and reuse_ratio >= 0.95
        and len(generation_log) >= plateau_floor
    ):
        return True, "deep-plateau-reuse"

    no_improve_floor = max(8, int(math.ceil(float(total_generations) * 0.18)))
    if (
        score_gain <= (0.25 * float(min_gain))
        and probe_win_rate <= 0.10
        and reuse_ratio >= 0.92
        and top_unique_ratio <= 0.62
        and len(generation_log) >= no_improve_floor
    ):
        return True, "no-improvement-window"

    if (
        score_gain <= (0.65 * float(min_gain))
        and slow_batches >= max(2.0, float(stats["window"]) - 2.0)
        and reuse_ratio >= 0.65
        and avg_batch_seconds > (0.85 * target_secs)
        and len(generation_log) >= plateau_floor
    ):
        return True, "runtime-budget-pressure"

    flat_floor = max(12, int(math.ceil(float(total_generations) * 0.32)))
    if len(generation_log) >= flat_floor:
        flat_window = min(
            len(generation_log),
            max(8, int(math.ceil(float(total_generations) * 0.24))),
        )
        recent_flat = list(generation_log[-flat_window:])
        recent_scores = [float(row.get(score_key, -float("inf"))) for row in recent_flat]
        finite_scores = [score for score in recent_scores if math.isfinite(score)]
        if finite_scores:
            flat_gain = float(max(finite_scores) - min(finite_scores))
            score_levels = {round(score, 8) for score in finite_scores}
            if (
                flat_gain <= max(0.00020, 0.55 * float(min_gain))
                and len(score_levels) <= 3
                and probe_win_rate <= 0.16
                and avg_batch_seconds > (0.72 * target_secs)
                and slow_batches >= max(2.0, 0.45 * float(flat_window))
            ):
                return True, "flat-score-window"

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
    *,
    role: str,
):
    role_label = str(role).strip().lower() or "role"
    if not config.use_supervised_guide:
        print(f"[pcpl-evolvo] {role_label} supervised guide disabled by config")
        return None
    if not plan.torch_available:
        print(f"[pcpl-evolvo] {role_label} supervised guide unavailable: torch not available")
        return None
    try:
        from evolvo.supervised import GFSLSupervisedGuide
    except Exception as exc:
        print(f"[pcpl-evolvo] {role_label} supervised guide import failed: {exc}")
        return None

    if config.preferred_device.lower() != "auto":
        device = config.preferred_device.lower()
    elif plan.gpu_backend != "none":
        device = plan.gpu_backend
    else:
        device = "cpu"

    profile = str(config.profile).strip().lower()
    if profile == "full":
        hidden_layers = [384, 256, 160, 96]
        epochs = 5
        candidate_pool = 6
    else:
        hidden_layers = [256, 160, 96]
        epochs = 4
        candidate_pool = 5

    configured_layers = [int(width) for width in config.supervised_hidden_layers if int(width) > 0]
    if configured_layers:
        hidden_layers = configured_layers[:5]
    if int(config.supervised_epochs) > 0:
        epochs = int(config.supervised_epochs)
    if int(config.supervised_candidate_pool) > 0:
        candidate_pool = int(config.supervised_candidate_pool)

    min_buffer = max(24, min(192, int(round(float(config.population_size) * 0.55))))
    batch_size = max(16, min(96, int(round(float(min_buffer) * 0.75))))
    max_observations = max(16, min(64, int(round(float(config.population_size) * 0.60))))
    buffer_size = max(512, int(round(float(config.population_size) * 10.0)))

    def _create_guide(device_name: str):
        return GFSLSupervisedGuide(
            device=device_name,
            hidden_layers=hidden_layers,
            buffer_size=buffer_size,
            min_buffer=min_buffer,
            batch_size=batch_size,
            epochs=epochs,
            candidate_pool=candidate_pool,
            max_observations=max_observations,
            capacity_auto_tune=bool(config.supervised_capacity_auto_tune),
        )

    creation_error: Optional[str] = None
    guide = None
    active_device = str(device)
    try:
        guide = _create_guide(active_device)
    except Exception as exc:
        creation_error = str(exc)
        if active_device != "cpu":
            print(
                "[pcpl-evolvo] {role} supervised guide failed on `{device}` ({error}); retrying on cpu".format(
                    role=role_label,
                    device=active_device,
                    error=creation_error,
                )
            )
            active_device = "cpu"
            try:
                guide = _create_guide(active_device)
                creation_error = None
            except Exception as cpu_exc:
                creation_error = str(cpu_exc)
                guide = None
        if guide is None:
            print(
                "[pcpl-evolvo] {role} supervised guide disabled: {error}".format(
                    role=role_label,
                    error=creation_error or "unknown error",
                )
            )
            return None

    runtime = {}
    if hasattr(guide, "runtime_summary"):
        try:
            runtime = guide.runtime_summary()
        except Exception:
            runtime = {}
    probe_payload = runtime.get("probe", {}) if isinstance(runtime, dict) else {}
    if not isinstance(probe_payload, dict):
        probe_payload = {}
    probe_ok = bool(probe_payload.get("probe_ok", False))
    requested_device = str(runtime.get("requested_device", device)) if isinstance(runtime, dict) else str(device)
    resolved_backend = str(runtime.get("resolved_backend", "unknown")) if isinstance(runtime, dict) else "unknown"
    resolved_device = str(runtime.get("resolved_device", active_device)) if isinstance(runtime, dict) else str(active_device)
    model_device = str(runtime.get("model_device", resolved_device)) if isinstance(runtime, dict) else str(resolved_device)
    print(
        "[pcpl-evolvo] {role} supervised guide enabled mode={mode} request={req} backend={backend} resolved={resolved} model={model} probe={probe}".format(
            role=role_label,
            mode=(
                "end-round-only"
                if bool(config.supervised_end_round_only)
                else "per-generation"
            ),
            req=requested_device,
            backend=resolved_backend,
            resolved=resolved_device,
            model=model_device,
            probe=("ok" if probe_ok else "failed"),
        )
    )
    if not probe_ok and probe_payload.get("error"):
        print(
            "[pcpl-evolvo] {role} supervised probe error: {error}".format(
                role=role_label,
                error=str(probe_payload.get("error", "")),
            )
        )
    return guide


def _supervised_guide_state(
    guide: Any,
    *,
    role: str,
    config: ExperimentConfig,
    plan: ResourcePlan,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "role": str(role),
        "enabled": bool(guide is not None),
        "mode": (
            "end-round-only"
            if bool(config.supervised_end_round_only)
            else "per-generation"
        ),
        "requested_device": str(config.preferred_device),
        "resource_gpu_backend": str(plan.gpu_backend),
        "resource_gpu_available": bool(plan.gpu_available),
        "resource_torch_available": bool(plan.torch_available),
        "resource_executor_backend": _normalize_executor_backend(config.executor_backend),
    }
    if guide is None:
        return state
    if hasattr(guide, "runtime_summary"):
        try:
            runtime = guide.runtime_summary()
            if isinstance(runtime, dict):
                state["runtime"] = runtime
        except Exception:
            pass
    return state


def _create_shared_executor(
    plan: ResourcePlan,
    *,
    eval_executor_kwargs: Optional[Dict[str, Any]] = None,
) -> Optional[concurrent.futures.Executor]:
    if plan.parallel_backend == "off" or plan.parallel_workers <= 1:
        return None
    if plan.parallel_backend == "process":
        return _create_process_pool_executor(
            max_workers=plan.parallel_workers,
            eval_executor_kwargs=eval_executor_kwargs,
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
    task: Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome], bool, str]
    | Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome], bool]
    | Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome], str]
    | Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome]]
) -> Tuple[float, List[Dict[str, Any]]]:
    stage = "full"
    if len(task) == 3:
        genome, scenarios, attacker = task
        emit_rows = True
    elif len(task) == 4:
        genome, scenarios, attacker, fourth = task
        if isinstance(fourth, bool):
            emit_rows = bool(fourth)
        else:
            emit_rows = True
            stage = _normalize_eval_stage(str(fourth))
    else:
        genome, scenarios, attacker, emit_rows, stage_raw = task[0], task[1], task[2], task[3], task[4]
        stage = _normalize_eval_stage(str(stage_raw))
    ensure_genome_io(genome)
    over_budget, cut_score = _is_genome_over_complexity_budget(genome, role="defender")
    if over_budget:
        return float(cut_score), []
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    try:
        score, metrics = _evaluate_with_timeout(
            scenarios,
            genome,
            attacker=attacker,
            timeout_seconds=_stage_eval_timeout_seconds(role="defender", stage=stage),
        )
    except TimeoutError:
        return _timeout_cut_score(role="defender"), []
    if not bool(emit_rows):
        return float(score), []
    return float(score), _metrics_rows(metrics)


def _attacker_eval_worker(
    task: Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig], bool, str]
    | Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig], bool]
    | Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig], str]
    | Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig]]
) -> Tuple[float, List[Dict[str, Any]]]:
    stage = "full"
    if len(task) == 3:
        attacker, defender, scenarios = task
        emit_rows = True
    elif len(task) == 4:
        attacker, defender, scenarios, fourth = task
        if isinstance(fourth, bool):
            emit_rows = bool(fourth)
        else:
            emit_rows = True
            stage = _normalize_eval_stage(str(fourth))
    else:
        attacker, defender, scenarios, emit_rows, stage_raw = (
            task[0],
            task[1],
            task[2],
            task[3],
            task[4],
        )
        stage = _normalize_eval_stage(str(stage_raw))
    ensure_attacker_genome_io(attacker)
    over_budget, cut_score = _is_genome_over_complexity_budget(attacker, role="attacker")
    if over_budget:
        return float(cut_score), []
    ensure_genome_io(defender)
    try:
        _, metrics = _evaluate_with_timeout(
            scenarios,
            defender,
            attacker=attacker,
            timeout_seconds=_stage_eval_timeout_seconds(role="attacker", stage=stage),
        )
    except TimeoutError:
        return _timeout_cut_score(role="attacker"), []
    attack_adv = _mean_metric(metrics, "attacker_advantage_score")
    lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
    token_success = _mean_metric(metrics, "attacker_token_success_rate")
    effective = _estimated_effective_instruction_count(attacker)
    if effective == 0:
        score = attack_adv - 0.08
    else:
        complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
        score = attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty
    if not bool(emit_rows):
        return float(score), []
    return float(score), _metrics_rows(metrics)


def _defender_eval_worker_batch(
    task: Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome], bool, str]
    | Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome], bool]
    | Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome], str]
    | Tuple[List[GFSLGenome], Sequence[ScenarioConfig], Optional[GFSLGenome]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    stage = "full"
    if len(task) == 3:
        genomes, scenarios, attacker = task
        emit_rows = True
    elif len(task) == 4:
        genomes, scenarios, attacker, fourth = task
        if isinstance(fourth, bool):
            emit_rows = bool(fourth)
        else:
            emit_rows = True
            stage = _normalize_eval_stage(str(fourth))
    else:
        genomes, scenarios, attacker, emit_rows, stage_raw = (
            task[0],
            task[1],
            task[2],
            task[3],
            task[4],
        )
        stage = _normalize_eval_stage(str(stage_raw))
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    results: List[Tuple[float, List[Dict[str, Any]]]] = []
    for genome in genomes:
        ensure_genome_io(genome)
        over_budget, cut_score = _is_genome_over_complexity_budget(genome, role="defender")
        if over_budget:
            results.append((float(cut_score), []))
            continue
        try:
            score, metrics = _evaluate_with_timeout(
                scenarios,
                genome,
                attacker=attacker,
                timeout_seconds=_stage_eval_timeout_seconds(role="defender", stage=stage),
            )
        except TimeoutError:
            results.append((_timeout_cut_score(role="defender"), []))
            continue
        rows = _metrics_rows(metrics) if bool(emit_rows) else []
        results.append((float(score), rows))
    return results


def _attacker_eval_worker_batch(
    task: Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig], bool, str]
    | Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig], bool]
    | Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig], str]
    | Tuple[List[GFSLGenome], GFSLGenome, Sequence[ScenarioConfig]]
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    stage = "full"
    if len(task) == 3:
        attackers, defender, scenarios = task
        emit_rows = True
    elif len(task) == 4:
        attackers, defender, scenarios, fourth = task
        if isinstance(fourth, bool):
            emit_rows = bool(fourth)
        else:
            emit_rows = True
            stage = _normalize_eval_stage(str(fourth))
    else:
        attackers, defender, scenarios, emit_rows, stage_raw = (
            task[0],
            task[1],
            task[2],
            task[3],
            task[4],
        )
        stage = _normalize_eval_stage(str(stage_raw))
    ensure_genome_io(defender)
    results: List[Tuple[float, List[Dict[str, Any]]]] = []
    for attacker in attackers:
        ensure_attacker_genome_io(attacker)
        over_budget, cut_score = _is_genome_over_complexity_budget(attacker, role="attacker")
        if over_budget:
            results.append((float(cut_score), []))
            continue
        try:
            _, metrics = _evaluate_with_timeout(
                scenarios,
                defender,
                attacker=attacker,
                timeout_seconds=_stage_eval_timeout_seconds(role="attacker", stage=stage),
            )
        except TimeoutError:
            results.append((_timeout_cut_score(role="attacker"), []))
            continue
        attack_adv = _mean_metric(metrics, "attacker_advantage_score")
        lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
        token_success = _mean_metric(metrics, "attacker_token_success_rate")
        effective = _estimated_effective_instruction_count(attacker)
        if effective == 0:
            score = attack_adv - 0.08
        else:
            complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
            score = attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty
        rows = _metrics_rows(metrics) if bool(emit_rows) else []
        results.append((float(score), rows))
    return results


def _genome_parallel_weight(genome: GFSLGenome, *, role: str) -> float:
    total = len(genome.instructions)
    if total <= 0:
        return 1.0
    effective = _estimated_effective_instruction_count(genome)
    eff_scale = 1.90 if str(role).lower() == "attacker" else 1.70
    return float(max(1, total) + (eff_scale * float(max(0, effective))))


def _task_parallel_weight(task: Any, *, worker_fn) -> float:
    if not isinstance(task, tuple) or not task:
        return 1.0
    try:
        if worker_fn is _defender_eval_worker:
            genome = task[0]
            if isinstance(genome, GFSLGenome):
                return _genome_parallel_weight(genome, role="defender")
        if worker_fn is _attacker_eval_worker:
            attacker = task[0]
            if isinstance(attacker, GFSLGenome):
                return _genome_parallel_weight(attacker, role="attacker")
    except Exception:
        return 1.0
    return 1.0


def _balanced_task_chunks(
    tasks: Sequence[Any],
    *,
    worker_fn,
    batch_size: int,
) -> List[List[Any]]:
    if not tasks:
        return []
    limit = max(1, int(batch_size))
    chunk_count = int(math.ceil(float(len(tasks)) / float(limit)))
    chunk_count = max(1, min(chunk_count, len(tasks)))

    chunks: List[List[Any]] = [[] for _ in range(chunk_count)]
    loads: List[float] = [0.0 for _ in range(chunk_count)]
    ranked = [
        (
            _task_parallel_weight(task, worker_fn=worker_fn),
            int(idx),
            task,
        )
        for idx, task in enumerate(tasks)
    ]
    ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)

    for weight, _, task in ranked:
        chosen_idx: Optional[int] = None
        chosen_key: Optional[Tuple[float, int, int]] = None
        for idx in range(chunk_count):
            if len(chunks[idx]) >= limit:
                continue
            key = (loads[idx], len(chunks[idx]), idx)
            if chosen_key is None or key < chosen_key:
                chosen_idx = idx
                chosen_key = key
        if chosen_idx is None:
            chosen_idx = min(range(chunk_count), key=lambda idx: (loads[idx], len(chunks[idx]), idx))
        chunks[chosen_idx].append(task)
        loads[chosen_idx] += float(weight)

    return [chunk for chunk in chunks if chunk]


def _force_shutdown_process_executor(executor: concurrent.futures.ProcessPoolExecutor) -> None:
    processes = getattr(executor, "_processes", None)
    if isinstance(processes, dict):
        for process in list(processes.values()):
            if process is None:
                continue
            try:
                process.terminate()
            except Exception:
                pass
        time.sleep(0.05)
        for process in list(processes.values()):
            if process is None:
                continue
            try:
                if process.is_alive():
                    process.kill()
            except Exception:
                pass
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _run_process_tasks_with_watchdog(
    *,
    tasks: Sequence[Any],
    worker_fn,
    workers: int,
) -> List[Tuple[float, List[Dict[str, Any]]]]:
    if not tasks:
        return []

    worker_count = max(1, int(workers))
    timeouts = [
        max(
            0.5,
            float(_task_timeout_seconds(task, worker_fn=worker_fn))
            + float(PROCESS_EVAL_TIMEOUT_GRACE_SECONDS),
        )
        for task in tasks
    ]
    fallback_results = [
        _timeout_fallback_result(task, worker_fn=worker_fn)
        for task in tasks
    ]

    executor = _create_process_pool_executor(max_workers=worker_count)
    resolved: List[Optional[Tuple[float, List[Dict[str, Any]]]]] = [None for _ in tasks]
    pending: Dict[concurrent.futures.Future, Tuple[int, float, float]] = {}
    watchdog_abort = False
    next_idx = 0

    def _submit_next() -> bool:
        nonlocal next_idx
        if next_idx >= len(tasks):
            return False
        task_idx = int(next_idx)
        next_idx += 1
        future = executor.submit(worker_fn, tasks[task_idx])
        pending[future] = (task_idx, time.perf_counter(), timeouts[task_idx])
        return True

    try:
        while len(pending) < worker_count and _submit_next():
            pass

        while pending:
            done, _ = concurrent.futures.wait(
                tuple(pending.keys()),
                timeout=float(PROCESS_EVAL_WATCHDOG_POLL_SECONDS),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                idx, _, _ = pending.pop(future)
                try:
                    result = future.result()
                    if (
                        isinstance(result, tuple)
                        and len(result) == 2
                        and isinstance(result[1], list)
                    ):
                        resolved[idx] = (float(result[0]), result[1])
                    else:
                        resolved[idx] = fallback_results[idx]
                except Exception:
                    resolved[idx] = fallback_results[idx]
                while len(pending) < worker_count and _submit_next():
                    pass

            now = time.perf_counter()
            overdue = [
                (future, info[0])
                for future, info in pending.items()
                if (now - info[1]) > info[2]
            ]
            if overdue:
                watchdog_abort = True
                for future, idx in overdue:
                    pending.pop(future, None)
                    resolved[idx] = fallback_results[idx]
                for future, (idx, _, _) in list(pending.items()):
                    pending.pop(future, None)
                    resolved[idx] = fallback_results[idx]
                for idx in range(next_idx, len(tasks)):
                    resolved[idx] = fallback_results[idx]
                break
    finally:
        if watchdog_abort:
            _force_shutdown_process_executor(executor)
        else:
            try:
                executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                pass

    out: List[Tuple[float, List[Dict[str, Any]]]] = []
    for idx, result in enumerate(resolved):
        if result is None:
            out.append(fallback_results[idx])
        else:
            out.append(result)
    return out


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
                if isinstance(task[3], bool):
                    normalized = (task[0], task[1], task[2], bool(task[3]) and bool(store_metrics), *task[4:])
                else:
                    normalized = (task[0], task[1], task[2], bool(store_metrics), *task[3:])
                normalized_tasks.append(normalized)
            else:
                normalized_tasks.append((task[0], task[1], task[2], bool(store_metrics)))
        tasks = normalized_tasks
    if backend == "process" and workers > 1:
        # Keep process scheduling fine-grained enough to avoid long tail stalls.
        if len(tasks) <= max(16, workers * 12):
            map_chunk_size = 1
        else:
            map_chunk_size = max(1, len(tasks) // max(1, workers * 4))
    else:
        map_chunk_size = max(1, len(tasks) // max(1, workers * 2))

    debug_timeout_seconds = max(0.0, float(_DEBUG_EVAL_TIMEOUT_SECONDS))
    debug_log_interval_seconds = max(0.0, float(_DEBUG_EVAL_LOG_INTERVAL_SECONDS))
    debug_monitor_enabled = bool(
        debug_timeout_seconds > 0.0 or debug_log_interval_seconds > 0.0
    )
    stage_counts: Dict[str, int] = {}
    if worker_fn in {_defender_eval_worker, _attacker_eval_worker}:
        for task in tasks:
            stage_name = _task_eval_stage(task)
            stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
    stage_summary = (
        ",".join(f"{name}:{count}" for name, count in sorted(stage_counts.items()))
        if stage_counts
        else "n/a"
    )

    def run_with_debug_monitor(
        exec_obj: concurrent.futures.Executor,
        fn,
        task_list,
    ):
        total = len(task_list)
        if total <= 0:
            return []
        pending_futures: Dict[concurrent.futures.Future, Tuple[int, float]] = {}
        ordered_results: List[Optional[Tuple[float, List[Dict[str, Any]]]]] = [None] * total
        start_ts = time.perf_counter()
        last_completion_ts = start_ts
        next_log_ts = (
            start_ts + debug_log_interval_seconds
            if debug_log_interval_seconds > 0.0
            else float("inf")
        )
        next_timeout_ts = (
            start_ts + debug_timeout_seconds
            if debug_timeout_seconds > 0.0
            else float("inf")
        )
        worker_label = str(getattr(fn, "__name__", "worker"))
        backend_label = (
            "process"
            if isinstance(exec_obj, concurrent.futures.ProcessPoolExecutor)
            else "thread"
        )
        for task_idx, task in enumerate(task_list):
            future = exec_obj.submit(fn, task)
            pending_futures[future] = (int(task_idx), time.perf_counter())

        completed = 0
        while pending_futures:
            done, _ = concurrent.futures.wait(
                tuple(pending_futures.keys()),
                timeout=max(0.05, float(PROCESS_EVAL_WATCHDOG_POLL_SECONDS)),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            now = time.perf_counter()

            for future in done:
                idx, _submitted_ts = pending_futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    raise RuntimeError(
                        "parallel evaluator worker failed "
                        f"(worker={worker_label}, backend={backend_label}, task_index={idx})"
                    ) from exc
                ordered_results[idx] = result
                completed += 1
                last_completion_ts = now

            if debug_log_interval_seconds > 0.0 and now >= next_log_ts:
                oldest_age = (
                    max(
                        (now - submitted_ts)
                        for _future, (_idx, submitted_ts) in pending_futures.items()
                    )
                    if pending_futures
                    else 0.0
                )
                print(
                    "[pcpl-evolvo][debug][eval] worker={worker} backend={backend} stage={stage} done={done}/{total} in_flight={in_flight} elapsed={elapsed:.1f}s oldest={oldest:.1f}s".format(
                        worker=worker_label,
                        backend=backend_label,
                        stage=stage_summary,
                        done=int(completed),
                        total=int(total),
                        in_flight=int(len(pending_futures)),
                        elapsed=float(now - start_ts),
                        oldest=float(oldest_age),
                    )
                )
                next_log_ts = now + debug_log_interval_seconds

            if debug_timeout_seconds > 0.0 and now >= next_timeout_ts and pending_futures:
                no_completion = now - last_completion_ts
                if no_completion >= debug_timeout_seconds:
                    oldest_age = max(
                        (now - submitted_ts)
                        for _future, (_idx, submitted_ts) in pending_futures.items()
                    )
                    print(
                        "[pcpl-evolvo][debug][timeout] worker={worker} backend={backend} stage={stage} no_completion={no_completion:.1f}s threshold={threshold:.1f}s in_flight={in_flight} oldest={oldest:.1f}s done={done}/{total}".format(
                            worker=worker_label,
                            backend=backend_label,
                            stage=stage_summary,
                            no_completion=float(no_completion),
                            threshold=float(debug_timeout_seconds),
                            in_flight=int(len(pending_futures)),
                            oldest=float(oldest_age),
                            done=int(completed),
                            total=int(total),
                        )
                    )
                    next_timeout_ts = now + debug_timeout_seconds
                else:
                    next_timeout_ts = last_completion_ts + debug_timeout_seconds

        resolved: List[Tuple[float, List[Dict[str, Any]]]] = []
        for idx, result in enumerate(ordered_results):
            if result is None:
                raise RuntimeError(
                    f"parallel evaluator missing task result at index {idx} ({worker_label})"
                )
            resolved.append(result)
        return resolved

    def run_with_executor(
        exec_obj: concurrent.futures.Executor,
        fn,
        task_list,
        *,
        process_chunksize: Optional[int] = None,
    ):
        if debug_monitor_enabled:
            return run_with_debug_monitor(exec_obj, fn, task_list)
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

    if (
        backend == "process"
        and workers > 1
        and worker_fn in {_defender_eval_worker, _attacker_eval_worker}
    ):
        # Guard against rare evaluator hangs by hard-cutting overdue tasks and recycling the pool.
        results = _run_process_tasks_with_watchdog(
            tasks=tasks,
            worker_fn=worker_fn,
            workers=workers,
        )
        assign(results)
        return

    # Process backend benefits from batch-chunked tasks to reduce pickle/IPC overhead.
    use_batch_chunks = (
        backend == "process"
        and workers > 1
        and len(tasks) >= max(6, workers)
        and worker_fn in {_defender_eval_worker, _attacker_eval_worker}
    )
    if use_batch_chunks:
        batch_worker = _defender_eval_worker_batch if worker_fn is _defender_eval_worker else _attacker_eval_worker_batch
        # One genome per process task avoids chunk-level stragglers that can idle most workers.
        batch_size = 1
        task_chunks = _balanced_task_chunks(
            tasks,
            worker_fn=worker_fn,
            batch_size=batch_size,
        )
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

        with _create_process_pool_executor(max_workers=workers) as local_exec:
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
        with _create_process_pool_executor(max_workers=workers) as local_exec:
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
    lines.append(
        "| scenario | total | principle | sync | stability | phase-error | ctrl-flow | security | qft | linear-rank | compare-x | cost | op-cost | attacker-adv |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        if isinstance(metric, dict):
            scenario = str(metric.get("scenario", "n/a"))
            total = float(metric.get("total_score", 0.0))
            principle = float(metric.get("principle_score", 0.0))
            sync = float(metric.get("sync_score", 0.0))
            stability = float(metric.get("stability_score", 0.0))
            phase_error = float(metric.get("phase_error_control_score", 0.0))
            control_flow = float(metric.get("control_flow_score", 0.0))
            security = float(metric.get("security_score", 0.0))
            qft = float(metric.get("qft_score", 0.0))
            linear_rank = float(metric.get("linear_rank_score", 0.0))
            compare_x = float(metric.get("compare_x_score", 0.0))
            cost = float(metric.get("cost_score", 0.0))
            op_cost = float(metric.get("operation_cost_score", 0.0))
            attack_adv = float(metric.get("attacker_advantage_score", 0.0))
        else:
            scenario = str(getattr(metric, "scenario", "n/a"))
            total = float(getattr(metric, "total_score", 0.0))
            principle = float(getattr(metric, "principle_score", 0.0))
            sync = float(getattr(metric, "sync_score", 0.0))
            stability = float(getattr(metric, "stability_score", 0.0))
            phase_error = float(getattr(metric, "phase_error_control_score", 0.0))
            control_flow = float(getattr(metric, "control_flow_score", 0.0))
            security = float(getattr(metric, "security_score", 0.0))
            qft = float(getattr(metric, "qft_score", 0.0))
            linear_rank = float(getattr(metric, "linear_rank_score", 0.0))
            compare_x = float(getattr(metric, "compare_x_score", 0.0))
            cost = float(getattr(metric, "cost_score", 0.0))
            op_cost = float(getattr(metric, "operation_cost_score", 0.0))
            attack_adv = float(getattr(metric, "attacker_advantage_score", 0.0))
        lines.append(
            "| {name} | {total:.4f} | {principle:.4f} | {sync:.4f} | {stability:.4f} | {phase_error:.4f} | {control_flow:.4f} | {security:.4f} | {qft:.4f} | {linear_rank:.4f} | {compare_x:.4f} | {cost:.4f} | {op_cost:.4f} | {attack_adv:.4f} |".format(
                name=scenario,
                total=total,
                principle=principle,
                sync=sync,
                stability=stability,
                phase_error=phase_error,
                control_flow=control_flow,
                security=security,
                qft=qft,
                linear_rank=linear_rank,
                compare_x=compare_x,
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
            "defender_anti_attacker_elites": [],
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
            "defender_anti_attacker_elites": [],
            "attacker_elites": [],
            "rounds": [],
            "predictive_profile": {},
            "updated_at": None,
        }
    payload.setdefault("defender_elites", [])
    payload.setdefault("defender_anti_attacker_elites", [])
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
    panel_payload = entry.get("selection_panel", {})
    if not isinstance(panel_payload, dict):
        panel_payload = {}

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
        "selection_panel": {
            "attacker_panel_size": int(panel_payload.get("attacker_panel_size", 0)),
            "attacker_panel_penalty": float(panel_payload.get("attacker_panel_penalty", 0.0)),
            "defender_robust_score": float(panel_payload.get("defender_robust_score", 0.0)),
            "defender_panel_worst_score": float(panel_payload.get("defender_panel_worst_score", 0.0)),
            "defender_panel_worst_attacker_adv": float(panel_payload.get("defender_panel_worst_attacker_adv", 0.0)),
            "anti_attacker_slice": int(panel_payload.get("anti_attacker_slice", 0)),
        },
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
    evolver: GFSLEvolver,
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
                ("phase_error_control_score", "phase-error"),
                ("control_flow_score", "ctrl-flow"),
                ("qft_score", "qft"),
                ("linear_rank_score", "linear-rank"),
                ("compare_x_score", "compare-x"),
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
    lines.append("| gen | best | principle | sync | horizon | security | cost | sync-loss | attacker-adv | q frac/keep | m frac/keep | key var | probe | q-thr | eval q>m>f | keep q>m | probes | qskip | uniq | cache | dup | reb | gate | neu | t(s) | stop |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |")
    for row in defender_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {sync_score:.4f} | {horizon_sync:.4f} | {security:.4f} | {cost:.4f} | {sync_loss:.4f} | {attacker_adv:.4f} | {qf:.2f}/{qk:.2f} | {mf:.2f}/{mk:.2f} | {kv} | {probe:.2f} | {qthr:.2f} | {stage_eval} | {stage_keep} | {probe_n} | {qskip} | {uniq} | {cache} | {dup} | {reb} | {gate} | {neu} | {secs:.2f} | {stop_reason} |".format(
                generation=row["generation"],
                best_score=row["best_score"],
                principle=row["principle"],
                sync_score=float(row.get("sync_score", 0.0)),
                horizon_sync=float(row.get("horizon_sync", 0.0)),
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
                gate="{count}@{thr:.3f}/{pct:.2f}".format(
                    count=int(row.get("sync_gate_penalized", 0)),
                    thr=float(row.get("sync_gate_threshold", 0.0)),
                    pct=float(row.get("sync_gate_percentile", 0.0)),
                ),
                neu="{pen}/{rew}".format(
                    pen=int(row.get("neutrality_penalized", 0)),
                    rew=int(row.get("neutrality_rewarded", 0)),
                ),
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
        score, metrics = _evaluate_across_scenarios_runtime(
            scenarios,
            None,
            fixed_decision=policy,
        )
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
            "phase_error_control_score",
            True,
            "Phase-Error Control",
            "Add explicit phase-error corrective loops with bounded overshoot and monotonic recovery criteria.",
        ),
        (
            "control_flow_score",
            True,
            "Control-Flow Richness",
            "Increase effective compare/branch logic for drift-aware switching between steady and recovery paths.",
        ),
        (
            "qft_score",
            True,
            "QFT Period Margin",
            "Increase public period headroom and couple long-horizon timing assumptions with explicit QFT-visible bounds.",
        ),
        (
            "linear_rank_score",
            True,
            "Linear-Rank Difficulty",
            "Increase pre-hash exponent linear independence (mod 2 / mod 65537) and document expected rank floors.",
        ),
        (
            "compare_x_score",
            True,
            "Compare-X Scalability",
            "Validate stronger cross-x behavior by preserving one-of-x/per-block guarantees while x grows.",
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
    conclusion_lines.append(
        f"- executor backend: `{_normalize_executor_backend(config.executor_backend)}`"
    )
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
            "- score={score:.6f}, principle={principle:.4f}, security={security:.4f}, sync={sync:.4f}, stability={stability:.4f}, phase_error={phase_error:.4f}, ctrl_flow={ctrl_flow:.4f}, qft={qft:.4f}, linear_rank={linear_rank:.4f}, compare_x={compare_x:.4f}, cost={cost:.4f}".format(
                score=float(best_defender[0].get("score", 0.0)),
                principle=float(defender_mean.get("principle_score", 0.0)),
                security=float(defender_mean.get("security_score", 0.0)),
                sync=float(defender_mean.get("sync_score", 0.0)),
                stability=float(defender_mean.get("stability_score", 0.0)),
                phase_error=float(defender_mean.get("phase_error_control_score", 0.0)),
                ctrl_flow=float(defender_mean.get("control_flow_score", 0.0)),
                qft=float(defender_mean.get("qft_score", 0.0)),
                linear_rank=float(defender_mean.get("linear_rank_score", 0.0)),
                compare_x=float(defender_mean.get("compare_x_score", 0.0)),
                cost=float(defender_mean.get("cost_score", 0.0)),
            )
        )
        conclusion_lines.append(
            "- brute_force_resistance={bf:.4f}, reverse_hack_resistance={rh:.4f}, sync_loss={sl:.4f}, projected_sync_loss_10s={psl:.4f}, horizon_sync={hs:.4f}, qft_bits={qft_bits:.1f}, linear_rank_mod2={lr2:.4f}, linear_rank_mod65537={lrp:.4f}, compare_x_period={cxp:.4f}".format(
                bf=float(defender_mean.get("brute_force_resistance_score", 0.0)),
                rh=float(defender_mean.get("reverse_hack_resistance_score", 0.0)),
                sl=float(defender_mean.get("sync_loss_rate", 0.0)),
                psl=float(defender_mean.get("projected_sync_loss_rate", 0.0)),
                hs=float(defender_mean.get("horizon_sync_score", 0.0)),
                qft_bits=float(defender_mean.get("qft_period_bits", 0.0)),
                lr2=float(defender_mean.get("linear_rank_mod2_ratio", 0.0)),
                lrp=float(defender_mean.get("linear_rank_mod65537_ratio", 0.0)),
                cxp=float(defender_mean.get("compare_x_period_ratio", 0.0)),
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
) -> Tuple[GFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(
        config,
        resource_plan,
        role="defender",
    )
    evolver_guide = guide
    defender_instruction_cap = max(
        64,
        min(
            int(MAX_DEFENDER_TOTAL_INSTRUCTIONS),
            int(math.ceil(float(config.initial_instructions) * 6.0)),
        ),
    )
    defender_effective_cap = max(
        32,
        min(
            defender_instruction_cap,
            int(math.ceil(float(defender_instruction_cap) * 0.62)),
        ),
    )
    evolver = GFSLEvolver(
        population_size=config.population_size,
        supervised_guide=evolver_guide,
        guide_observe_each_generation=not bool(config.supervised_end_round_only),
        parent_pool_ratio=config.parent_pool_ratio,
        stagnation_patience=config.stagnation_patience,
        mutation_floor=config.mutation_floor,
        mutation_ceiling=config.mutation_ceiling,
        mutation_step=config.mutation_step,
        max_instruction_count=defender_instruction_cap,
        max_effective_instruction_count=defender_effective_cap,
    )
    defender_seed_elites = list(archive.get("defender_elites", []))
    anti_attacker_elites = list(archive.get("defender_anti_attacker_elites", []))
    if anti_attacker_elites:
        defender_seed_elites.extend(anti_attacker_elites)
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=defender_seed_elites,
        io_initializer=ensure_genome_io,
        reference_anchor_factory=build_reference_defender_genome,
        population_size=config.population_size,
        initial_instructions=config.initial_instructions,
        elite_pool=config.elite_pool,
    )
    for genome in evolver.population:
        evolver._enforce_complexity_budget(genome)
        _invalidate_genome_caches(genome)
    if guide is not None and bool(config.supervised_end_round_only):
        try:
            guide.observe_population(evolver.population)
        except Exception as exc:
            print(f"[pcpl-evolvo] defender supervised warmup failed: {exc}")

    generation_log: List[Dict[str, Any]] = []
    archive_signatures = {
        str(entry.get("signature") or entry.get("canonical_signature"))
        for entry in archive.get("defender_elites", [])
        if (entry.get("signature") or entry.get("canonical_signature"))
    }
    archive_signatures.update(
        {
            str(entry.get("signature") or entry.get("canonical_signature"))
            for entry in archive.get("defender_anti_attacker_elites", [])
            if (entry.get("signature") or entry.get("canonical_signature"))
        }
    )
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
    target_calibrator = RuntimeTargetCalibrator(
        base_target_seconds=float(config.target_generation_seconds),
        label="defender",
    )
    stage_stats: Dict[str, float] = {}
    stage_cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
    cache_limit = max(500, int(config.max_eval_cache_entries))
    attacker_signature = _evaluation_signature(attacker) if attacker is not None else "none"

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(int(KEY_VARIANT_FLOOR), controller.key_variant_count))
            complexity = "quick"
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(int(KEY_VARIANT_FLOOR), controller.key_variant_count))
            complexity = "mid"
        else:
            frac = _full_stage_fraction(controller.mid_cycle_fraction)
            key_variants = max(int(KEY_VARIANT_FLOOR), controller.key_variant_count)
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
        score, metrics = _evaluate_across_scenarios_runtime(
            full_scenarios,
            genome,
            attacker=attacker,
        )
        genome._pcpl_metrics = metrics  # type: ignore[attr-defined]
        return score

    def progress(gen: int, best: GFSLGenome, best_fitness: float) -> None:
        metrics = getattr(best, "_pcpl_metrics", None)
        if metrics is None:
            full_scenarios = make_scenarios("full")
            _, metrics = _evaluate_across_scenarios_runtime(
                full_scenarios,
                best,
                attacker=attacker,
            )
            best._pcpl_metrics = metrics
        observed_batch_seconds = float(stage_stats.get("batch_seconds", 0.0))
        if observed_batch_seconds > 0.0:
            stage_stats["target_batch_seconds"] = float(
                target_calibrator.observe(observed_batch_seconds)
            )

        row = {
            "generation": int(gen),
            "best_score": float(best_fitness),
            "best_signature": _evaluation_signature(best),
            "principle": _mean_metric(metrics, "principle_score"),
            "sync_score": _mean_metric(metrics, "sync_score"),
            "stability_score": _mean_metric(metrics, "stability_score"),
            "security": _mean_metric(metrics, "security_score"),
            "cost": _mean_metric(metrics, "cost_score"),
            "sync_loss": _mean_metric(metrics, "sync_loss_rate"),
            "projected_sync_loss": _mean_metric(metrics, "projected_sync_loss_rate"),
            "horizon_sync": _mean_metric(metrics, "horizon_sync_score"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
            "phase_error_control": _mean_metric(metrics, "phase_error_control_score"),
            "control_flow_score": _mean_metric(metrics, "control_flow_score"),
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
            "sync_gate_penalized": int(stage_stats.get("sync_gate_penalized", 0.0)),
            "sync_gate_threshold": float(stage_stats.get("sync_gate_threshold", 0.0)),
            "sync_gate_percentile": float(stage_stats.get("sync_gate_percentile", 0.0)),
            "neutrality_penalized": int(stage_stats.get("neutrality_penalized", 0.0)),
            "neutrality_rewarded": int(stage_stats.get("neutrality_rewarded", 0.0)),
            "mutation_rate": float(stage_stats.get("mutation_rate", evolver.mutation_rate)),
            "batch_seconds": float(stage_stats.get("batch_seconds", 0.0)),
            "target_batch_seconds": float(
                stage_stats.get("target_batch_seconds", target_calibrator.target_seconds)
            ),
            "stall_immigrants": int(getattr(evolver, "_last_stall_immigrants", 0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][defender] gen={gen:03d} score={score:.5f} sync={sync:.4f} hs={hs:.4f} sec={security:.4f} cost={cost:.4f} attack_adv={attack_adv:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} reb={reb} rnd={rt}/{ri} gate={gate}@{gthr:.3f}/{gp:.2f} neu={np}/{nr} imm={imm} ub={ub:.2f} mut={mut:.2f} t={secs:.2f}s".format(
                gen=gen,
                score=best_fitness,
                sync=row["sync_score"],
                hs=row["horizon_sync"],
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
                gate=row["sync_gate_penalized"],
                gthr=row["sync_gate_threshold"],
                gp=row["sync_gate_percentile"],
                np=row["neutrality_penalized"],
                nr=row["neutrality_rewarded"],
                imm=row["stall_immigrants"],
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
            min_gain=0.00050,
            target_generation_seconds=float(target_calibrator.target_seconds),
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
        target_seconds = float(target_calibrator.target_seconds)
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
            "sync_gate_penalized": 0.0,
            "sync_gate_threshold": 0.0,
            "sync_gate_percentile": 0.0,
            "sync_gate_flatness": 0.0,
            "neutrality_penalized": 0.0,
            "neutrality_rewarded": 0.0,
            "mutation_rate": float(evolver.mutation_rate),
            "quick_throttle": 1.0,
            "target_batch_seconds": float(target_seconds),
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
            prev_batch_seconds = float(stage_stats.get("batch_seconds", 0.0)) if stage_stats else 0.0
            prev_target_seconds = max(
                0.25,
                float(stage_stats.get("target_batch_seconds", target_seconds)) if stage_stats else float(target_seconds),
            )
            runtime_pressure = (
                prev_batch_seconds / prev_target_seconds
                if prev_batch_seconds > 0.0
                else 1.0
            )
            if pressure > 2.0 and runtime_pressure > float(RUNTIME_OVERBUDGET_TOLERANCE):
                factor = min(2.0, pressure - 2.0) * min(1.0, runtime_pressure - 1.0)
                controller.quick_keep_ratio -= 0.06 * factor
                controller.mid_keep_ratio -= 0.05 * factor
                controller.quick_cycle_fraction -= 0.035 * factor
                controller.mid_cycle_fraction -= 0.028 * factor
                if pressure > 3.0 and controller.key_variant_count > int(KEY_VARIANT_FLOOR):
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
                "full",
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "quick",
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
        quick_min_keep = _parallel_keep_floor(
            total=len(ranked_quick),
            workers=resource_plan.parallel_workers,
            multiplier=float(QUICK_KEEP_PARALLEL_FLOOR_MULTIPLIER),
            minimum=2,
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "mid",
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
        mid_min_keep = _parallel_keep_floor(
            total=len(ranked_mid),
            workers=resource_plan.parallel_workers,
            multiplier=float(MID_KEEP_PARALLEL_FLOOR_MULTIPLIER),
            minimum=1,
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_defender_eval_worker,
                build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "full",
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
        gate_stats = _apply_defender_sync_loss_gate(
            full_pending=full_pending,
            generation_log=generation_log,
            config=config,
        )
        local_stage.update(gate_stats)
        neutrality_stats = _apply_defender_anti_neutrality(
            full_pending=full_pending,
            generation_log=generation_log,
            config=config,
        )
        local_stage.update(neutrality_stats)

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
                "full",
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
            target_seconds=float(target_seconds),
            backend=resource_plan.parallel_backend,
            executor=shared_executor,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                full_scenarios,
                attacker,
                "full",
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
        local_stage["target_batch_seconds"] = float(
            target_calibrator.observe(local_stage["batch_seconds"])
        )
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
    stop_reason = getattr(evolver, "_early_stop_reason", None)
    stop_generation = getattr(evolver, "_early_stop_generation", None)
    if generation_log:
        generation_log[-1]["stop_reason"] = str(stop_reason) if stop_reason else ""
        generation_log[-1]["stop_generation"] = int(stop_generation) if stop_generation is not None else -1
    controller_state = controller.to_dict()
    controller_state["torch_tuner"] = torch_tuner.to_dict()
    evolver._predictive_controller_state = controller_state  # type: ignore[attr-defined]
    evolver._supervised_guide_state = _supervised_guide_state(  # type: ignore[attr-defined]
        guide,
        role="defender",
        config=config,
        plan=resource_plan,
    )
    return evolver, generation_log


def _run_attacker_round(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    shared_executor: Optional[concurrent.futures.Executor],
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    defender: GFSLGenome,
) -> Tuple[GFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(
        config,
        resource_plan,
        role="attacker",
    )
    evolver_guide = guide
    attacker_seed_budget = max(4, config.initial_instructions // 2)
    attacker_instruction_cap = max(
        40,
        min(
            int(MAX_ATTACKER_TOTAL_INSTRUCTIONS),
            int(math.ceil(float(attacker_seed_budget) * 6.0)),
        ),
    )
    attacker_effective_cap = max(
        24,
        min(
            attacker_instruction_cap,
            int(math.ceil(float(attacker_instruction_cap) * 0.60)),
        ),
    )
    evolver = GFSLEvolver(
        population_size=config.attacker_population_size,
        supervised_guide=evolver_guide,
        guide_observe_each_generation=not bool(config.supervised_end_round_only),
        parent_pool_ratio=config.parent_pool_ratio,
        stagnation_patience=config.stagnation_patience,
        mutation_floor=config.mutation_floor,
        mutation_ceiling=config.mutation_ceiling,
        mutation_step=config.mutation_step,
        max_instruction_count=attacker_instruction_cap,
        max_effective_instruction_count=attacker_effective_cap,
    )
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=archive.get("attacker_elites", []),
        io_initializer=ensure_attacker_genome_io,
        reference_anchor_factory=None,
        population_size=config.attacker_population_size,
        initial_instructions=attacker_seed_budget,
        elite_pool=max(4, config.elite_pool // 2),
    )
    for attacker_genome in evolver.population:
        evolver._enforce_complexity_budget(attacker_genome)
        _invalidate_genome_caches(attacker_genome)
    if guide is not None and bool(config.supervised_end_round_only):
        try:
            guide.observe_population(evolver.population)
        except Exception as exc:
            print(f"[pcpl-evolvo] attacker supervised warmup failed: {exc}")

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
    target_calibrator = RuntimeTargetCalibrator(
        base_target_seconds=float(config.target_generation_seconds),
        label="attacker",
    )
    stage_stats: Dict[str, float] = {}
    stage_cache: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
    cache_limit = max(500, int(config.max_eval_cache_entries))
    defender_signature = _evaluation_signature(defender)

    def make_scenarios(stage: str) -> List[ScenarioConfig]:
        if stage == "quick":
            frac = controller.quick_cycle_fraction
            key_variants = min(2, max(int(KEY_VARIANT_FLOOR), controller.key_variant_count))
            complexity = "quick"
        elif stage == "mid":
            frac = controller.mid_cycle_fraction
            key_variants = min(3, max(int(KEY_VARIANT_FLOOR), controller.key_variant_count))
            complexity = "mid"
        else:
            frac = _full_stage_fraction(controller.mid_cycle_fraction)
            key_variants = max(int(KEY_VARIANT_FLOOR), controller.key_variant_count)
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
        _, metrics = _evaluate_across_scenarios_runtime(
            full_scenarios,
            defender,
            attacker=attacker,
        )
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
            _, metrics = _evaluate_across_scenarios_runtime(
                full_scenarios,
                defender,
                attacker=best,
            )
            best._attack_metrics = metrics
        observed_batch_seconds = float(stage_stats.get("batch_seconds", 0.0))
        if observed_batch_seconds > 0.0:
            stage_stats["target_batch_seconds"] = float(
                target_calibrator.observe(observed_batch_seconds)
            )
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
            "target_batch_seconds": float(
                stage_stats.get("target_batch_seconds", target_calibrator.target_seconds)
            ),
            "stall_immigrants": int(getattr(evolver, "_last_stall_immigrants", 0)),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][attacker] gen={gen:03d} score={score:.5f} lane={lane:.4f} token={token:.4f} q={qf:.2f}/{qk:.2f} m={mf:.2f}/{mk:.2f} kv={kv} probe={probe:.2f} qt={qt:.2f} eval={stage_eval} keep={stage_keep} probes={probe_n} qskip={qskip} uniq={uniq} cache={cache} dup={dup} reb={reb} rnd={rt}/{ri} imm={imm} ub={ub:.2f} mut={mut:.2f} t={secs:.2f}s".format(
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
                imm=row["stall_immigrants"],
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
            min_gain=0.00060,
            target_generation_seconds=float(target_calibrator.target_seconds),
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
        target_seconds = float(target_calibrator.target_seconds)
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
            "target_batch_seconds": float(target_seconds),
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
            prev_batch_seconds = float(stage_stats.get("batch_seconds", 0.0)) if stage_stats else 0.0
            prev_target_seconds = max(
                0.25,
                float(stage_stats.get("target_batch_seconds", target_seconds)) if stage_stats else float(target_seconds),
            )
            runtime_pressure = (
                prev_batch_seconds / prev_target_seconds
                if prev_batch_seconds > 0.0
                else 1.0
            )
            if pressure > 2.0 and runtime_pressure > float(RUNTIME_OVERBUDGET_TOLERANCE):
                factor = min(2.0, pressure - 2.0) * min(1.0, runtime_pressure - 1.0)
                controller.quick_keep_ratio -= 0.06 * factor
                controller.mid_keep_ratio -= 0.05 * factor
                controller.quick_cycle_fraction -= 0.035 * factor
                controller.mid_cycle_fraction -= 0.028 * factor
                if pressure > 3.0 and controller.key_variant_count > int(KEY_VARIANT_FLOOR):
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
                "full",
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "quick",
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
        quick_min_keep = _parallel_keep_floor(
            total=len(ranked_quick),
            workers=resource_plan.parallel_workers,
            multiplier=float(QUICK_KEEP_PARALLEL_FLOOR_MULTIPLIER),
            minimum=2,
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "mid",
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
        mid_min_keep = _parallel_keep_floor(
            total=len(ranked_mid),
            workers=resource_plan.parallel_workers,
            multiplier=float(MID_KEEP_PARALLEL_FLOOR_MULTIPLIER),
            minimum=1,
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
                target_seconds=float(target_seconds),
                backend=resource_plan.parallel_backend,
                executor=shared_executor,
                worker_fn=_attacker_eval_worker,
                build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
                "full",
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
            local_stage["target_batch_seconds"] = float(
                target_calibrator.observe(local_stage["batch_seconds"])
            )
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
                "full",
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
                "full",
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
            target_seconds=float(target_seconds),
            backend=resource_plan.parallel_backend,
            executor=shared_executor,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                defender,
                full_scenarios,
                "full",
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
        local_stage["target_batch_seconds"] = float(
            target_calibrator.observe(local_stage["batch_seconds"])
        )
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
    stop_reason = getattr(evolver, "_early_stop_reason", None)
    stop_generation = getattr(evolver, "_early_stop_generation", None)
    if generation_log:
        generation_log[-1]["stop_reason"] = str(stop_reason) if stop_reason else ""
        generation_log[-1]["stop_generation"] = int(stop_generation) if stop_generation is not None else -1
    controller_state = controller.to_dict()
    controller_state["torch_tuner"] = torch_tuner.to_dict()
    evolver._predictive_controller_state = controller_state  # type: ignore[attr-defined]
    evolver._supervised_guide_state = _supervised_guide_state(  # type: ignore[attr-defined]
        guide,
        role="attacker",
        config=config,
        plan=resource_plan,
    )
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
    eval_executor_kwargs = _build_eval_executor_kwargs(config)
    _set_eval_executor_kwargs(eval_executor_kwargs)
    _set_debug_eval_runtime_settings(
        timeout_seconds=float(config.debug_eval_timeout_seconds),
        log_interval_seconds=float(config.debug_eval_log_interval_seconds),
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
        "[pcpl-evolvo] resources cpu={cpu} backend={backend} workers={workers} gpu={gpu} torch={torch} exec={exec_backend}".format(
            cpu=resource_plan.cpu_count,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            gpu=resource_plan.gpu_backend,
            torch=resource_plan.torch_available,
            exec_backend=str(eval_executor_kwargs.get("compute_backend", "cpu")),
        )
    )
    if (
        float(config.debug_eval_timeout_seconds) > 0.0
        or float(config.debug_eval_log_interval_seconds) > 0.0
    ):
        print(
            "[pcpl-evolvo] debug eval monitor enabled timeout_s={timeout:.1f} log_interval_s={interval:.1f}".format(
                timeout=float(config.debug_eval_timeout_seconds),
                interval=float(config.debug_eval_log_interval_seconds),
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
            shared_executor = _create_shared_executor(
                resource_plan,
                eval_executor_kwargs=eval_executor_kwargs,
            )

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
            defender_supervised = getattr(defender_evolver, "_supervised_guide_state", {})
            if not isinstance(defender_supervised, dict):
                defender_supervised = {}
            attacker_supervised = getattr(attacker_evolver, "_supervised_guide_state", {})
            if not isinstance(attacker_supervised, dict):
                attacker_supervised = {}
            predictive_profile = archive.setdefault("predictive_profile", {})
            if defender_profile:
                predictive_profile["defender"] = defender_profile
            if attacker_profile:
                predictive_profile["attacker"] = attacker_profile
            if defender_supervised:
                predictive_profile["defender_supervised"] = defender_supervised
            if attacker_supervised:
                predictive_profile["attacker_supervised"] = attacker_supervised
            selection_key_variants = max(
                int(KEY_VARIANT_FLOOR),
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
            if not top_candidates:
                raise RuntimeError("defender round produced empty candidate pool")
            top_candidates = top_candidates[: min(5, len(top_candidates))]
            for candidate in top_candidates:
                ensure_genome_io(candidate)

            attacker_panel: List[GFSLGenome] = []
            panel_seen: set[str] = set()

            ensure_attacker_genome_io(best_attacker)
            attacker_panel.append(best_attacker)
            panel_seen.add(_evaluation_signature(best_attacker))

            panel_size = max(1, int(config.attacker_panel_size))
            if panel_size > 1:
                for entry in archive.get("attacker_elites", []):
                    if len(attacker_panel) >= panel_size:
                        break
                    try:
                        payload = entry.get("genome", {})
                        if not isinstance(payload, dict):
                            continue
                        panel_attacker = _deserialize_genome(payload)
                        ensure_attacker_genome_io(panel_attacker)
                        signature = _evaluation_signature(panel_attacker)
                        if signature in panel_seen:
                            continue
                        attacker_panel.append(panel_attacker)
                        panel_seen.add(signature)
                    except Exception:
                        continue

            panel_scores: Dict[int, List[float]] = {id(candidate): [] for candidate in top_candidates}
            panel_advantages: Dict[int, List[float]] = {id(candidate): [] for candidate in top_candidates}
            panel_primary_metrics: Dict[int, List[Dict[str, Any]]] = {}

            for panel_index, panel_attacker in enumerate(attacker_panel):
                candidate_pending = list(enumerate(top_candidates))
                _evaluate_pending_parallel(
                    pending=candidate_pending,
                    backend=resource_plan.parallel_backend,
                    workers=resource_plan.parallel_workers,
                    executor=shared_executor,
                    worker_fn=_defender_eval_worker,
                    build_task=lambda g, atk=panel_attacker: (
                        g,
                        selection_scenarios,
                        atk,
                        "full",
                    ),
                    attr_name="_pcpl_metrics",
                    store_metrics=True,
                )
                for candidate in top_candidates:
                    score = float(candidate.fitness or -float("inf"))
                    metrics = getattr(candidate, "_pcpl_metrics", [])
                    panel_scores[id(candidate)].append(score)
                    panel_advantages[id(candidate)].append(
                        _mean_metric(metrics, "attacker_advantage_score")
                    )
                    if panel_index == 0:
                        panel_primary_metrics[id(candidate)] = _metrics_rows(metrics)

            panel_penalty = max(0.0, float(config.attacker_panel_penalty))
            ranked_candidates: List[Tuple[float, GFSLGenome, float, float, float]] = []
            for candidate in top_candidates:
                scores = panel_scores.get(id(candidate), [])
                advantages = panel_advantages.get(id(candidate), [])
                if not scores:
                    scores = [float(candidate.fitness or -float("inf"))]
                if not advantages:
                    advantages = [0.0]
                base_score = float(scores[0])
                worst_score = float(min(scores))
                mean_score = float(sum(scores) / float(max(1, len(scores))))
                worst_adv = float(max(advantages))
                robust_score = (
                    (0.42 * base_score)
                    + (0.38 * worst_score)
                    + (0.20 * mean_score)
                    - (panel_penalty * worst_adv)
                )
                ranked_candidates.append(
                    (robust_score, candidate, base_score, worst_score, worst_adv)
                )

            ranked_candidates.sort(key=lambda item: item[0], reverse=True)
            selected_robust_score, selected_defender, selected_score, selected_panel_worst_score, selected_panel_worst_adv = ranked_candidates[0]
            _, selected_metrics = _evaluate_across_scenarios_runtime(
                selection_scenarios,
                selected_defender,
                attacker=best_attacker,
            )

            reference_defender = build_reference_defender_genome()
            ensure_genome_io(reference_defender)
            reference_score, reference_metrics = _evaluate_across_scenarios_runtime(
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
                "robust_score": float(selected_robust_score),
                "panel_worst_score": float(selected_panel_worst_score),
                "panel_worst_attacker_adv": float(selected_panel_worst_adv),
                "panel_size": int(len(attacker_panel)),
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
            anti_limit = max(8, int(math.ceil(float(config.archive_limit) * 0.5)))
            anti_slice_keep = min(
                len(ranked_candidates),
                max(1, min(3, int(math.ceil(float(config.elite_pool) * 0.08)))),
            )
            anti_entries = archive.get("defender_anti_attacker_elites", [])
            if not isinstance(anti_entries, list):
                anti_entries = []
            for robust_score, candidate, base_score, worst_score, worst_adv in ranked_candidates[:anti_slice_keep]:
                anti_record = {
                    "role": "defender-anti-attacker",
                    "round": round_index,
                    "timestamp": _utc_now_iso(),
                    "score": float(robust_score),
                    "base_score": float(base_score),
                    "worst_score": float(worst_score),
                    "worst_attacker_adv": float(worst_adv),
                    "signature": candidate.get_signature(),
                    "canonical_signature": _canonical_signature(candidate),
                    "metrics": panel_primary_metrics.get(id(candidate), []),
                    "genome": _serialize_genome(candidate, role="defender"),
                }
                anti_entries = _insert_elite(
                    anti_entries,
                    anti_record,
                    limit=anti_limit,
                )
            archive["defender_anti_attacker_elites"] = anti_entries

            round_summary = {
                "round": round_index,
                "timestamp": _utc_now_iso(),
                "defender_score": float(selected_score),
                "defender_signature": defender_signature,
                "attacker_score": float(attack_adv),
                "attacker_signature": attacker_signature,
                "selection_panel": {
                    "attacker_panel_size": int(len(attacker_panel)),
                    "attacker_panel_penalty": float(panel_penalty),
                    "defender_robust_score": float(selected_robust_score),
                    "defender_panel_worst_score": float(selected_panel_worst_score),
                    "defender_panel_worst_attacker_adv": float(selected_panel_worst_adv),
                    "anti_attacker_slice": int(anti_slice_keep),
                },
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
                    "defender_supervised": defender_supervised,
                    "attacker_supervised": attacker_supervised,
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
        defender_supervised = predictive_profile.get("defender_supervised", {})
        if not isinstance(defender_supervised, dict):
            defender_supervised = {}
        attacker_supervised = predictive_profile.get("attacker_supervised", {})
        if not isinstance(attacker_supervised, dict):
            attacker_supervised = {}

        final_summary = {
            "config": {
                **asdict(config),
                "out_dir": str(out_dir),
            },
            "resources": {
                **resource_plan.to_dict(),
                "executor_backend": _normalize_executor_backend(config.executor_backend),
            },
            "baselines": baseline_rows,
            "rounds_completed": len(archive.get("rounds", [])),
            "best_defender": archive.get("defender_elites", [])[:1],
            "best_defender_anti_attacker": archive.get("defender_anti_attacker_elites", [])[:1],
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
            "- anti-attacker defender elites: `{count}`".format(
                count=len(archive.get("defender_anti_attacker_elites", [])),
            )
        )
        report_lines.append(
            "- resources: backend={backend} workers={workers} gpu={gpu} exec={exec_backend}".format(
                backend=resource_plan.parallel_backend,
                workers=resource_plan.parallel_workers,
                gpu=resource_plan.gpu_backend,
                exec_backend=_normalize_executor_backend(config.executor_backend),
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
        report_lines.append(
            "- supervised tuning: layers=`{layers}` epochs=`{epochs}` candidate_pool=`{pool}` capacity_auto_tune=`{capacity}`".format(
                layers=(
                    ",".join(str(int(width)) for width in config.supervised_hidden_layers)
                    if config.supervised_hidden_layers
                    else "auto"
                ),
                epochs=(int(config.supervised_epochs) if int(config.supervised_epochs) > 0 else "auto"),
                pool=(
                    int(config.supervised_candidate_pool)
                    if int(config.supervised_candidate_pool) > 0
                    else "auto"
                ),
                capacity=("enabled" if bool(config.supervised_capacity_auto_tune) else "disabled"),
            )
        )
        if defender_supervised or attacker_supervised:
            d_runtime = defender_supervised.get("runtime", {})
            a_runtime = attacker_supervised.get("runtime", {})
            if not isinstance(d_runtime, dict):
                d_runtime = {}
            if not isinstance(a_runtime, dict):
                a_runtime = {}
            d_probe = d_runtime.get("probe", {})
            a_probe = a_runtime.get("probe", {})
            if not isinstance(d_probe, dict):
                d_probe = {}
            if not isinstance(a_probe, dict):
                a_probe = {}
            report_lines.append(
                "- supervised runtime: defender(enabled={de}, backend={db}, device={dd}, model={dm}, probe={dp}, train_calls={dt}, predict_calls={dpc}) attacker(enabled={ae}, backend={ab}, device={ad}, model={am}, probe={ap}, train_calls={at}, predict_calls={apc})".format(
                    de=bool(defender_supervised.get("enabled", False)),
                    db=str(d_runtime.get("resolved_backend", "n/a")),
                    dd=str(d_runtime.get("resolved_device", "n/a")),
                    dm=str(d_runtime.get("model_device", "n/a")),
                    dp=(
                        "ok"
                        if bool(d_probe.get("probe_ok", False))
                        else ("failed" if d_probe else "n/a")
                    ),
                    dt=int(d_runtime.get("train_calls", 0)),
                    dpc=int(d_runtime.get("predict_calls", 0)),
                    ae=bool(attacker_supervised.get("enabled", False)),
                    ab=str(a_runtime.get("resolved_backend", "n/a")),
                    ad=str(a_runtime.get("resolved_device", "n/a")),
                    am=str(a_runtime.get("model_device", "n/a")),
                    ap=(
                        "ok"
                        if bool(a_probe.get("probe_ok", False))
                        else ("failed" if a_probe else "n/a")
                    ),
                    at=int(a_runtime.get("train_calls", 0)),
                    apc=int(a_runtime.get("predict_calls", 0)),
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
        report_lines.append(
            "- phase-sync-governor: sync_loss_gate={pct:.2f}/{pen:.3f}+{flat:.3f} anti_neutrality={window}({npen:.3f}/{nbon:.3f}) attacker_panel={panel}@{panel_pen:.3f}".format(
                pct=float(config.sync_loss_gate_percentile),
                pen=float(config.sync_loss_gate_penalty),
                flat=float(config.sync_loss_gate_flat_boost),
                window=int(config.anti_neutrality_window),
                npen=float(config.anti_neutrality_penalty),
                nbon=float(config.anti_neutrality_bonus),
                panel=int(config.attacker_panel_size),
                panel_pen=float(config.attacker_panel_penalty),
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
            "resource_plan": {
                **resource_plan.to_dict(),
                "executor_backend": _normalize_executor_backend(config.executor_backend),
            },
            "predictive_profile": predictive_profile,
            **view_paths,
        }
        return summary
    finally:
        _set_debug_eval_runtime_settings(
            timeout_seconds=0.0,
            log_interval_seconds=0.0,
        )
        _shutdown_shared_executor(shared_executor)


def run_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Backward-compatible entry point; now supports continuous rounds."""
    return run_continuous_experiment(config, scenarios=scenarios)
