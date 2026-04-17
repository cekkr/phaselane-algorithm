#!/usr/bin/env python3
"""Run PCPL evolvo experiments (single-run or continuous resumable mode)."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import itertools
import json
import multiprocessing
import os
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import sys

from config import (
    DEFAULT_MODE,
    DEFAULT_PROFILE,
    available_modes,
    mode_summary,
    resolve_defaults,
)

PROJECT_DIR = Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
try:
    # Ensure child-process progress logs flush line-by-line in continuous runs.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

from pcpl_evolvo.experiment import ExperimentConfig, run_experiment

DEFAULT_FITNESS_SCHEMA_VERSION = "auto"


def _safe_int(value: int, minimum: int = 1) -> int:
    return max(minimum, int(value))


def _param_choices(base: int, *, minimum: int = 1, high_factor: float = 1.5) -> List[int]:
    base = _safe_int(base, minimum=minimum)
    high = _safe_int(round(base * high_factor), minimum=minimum)
    high = max(high, base + 1)
    if high == base:
        return [base]
    return [base, high]


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _parse_hidden_layers_spec(value: Any) -> List[int]:
    if value is None:
        return []
    raw_tokens: List[Any]
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"auto", "default", "none", "off"}:
            return []
        raw_tokens = [chunk.strip() for chunk in text.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_tokens = list(value)
    else:
        raw_tokens = [value]

    parsed: List[int] = []
    for token in raw_tokens:
        if token is None:
            continue
        token_str = str(token).strip()
        if not token_str:
            continue
        try:
            width = int(token_str)
        except ValueError as exc:
            raise ValueError(
                f"Invalid hidden layer width `{token}` in supervised-hidden-layers."
            ) from exc
        if width <= 0:
            raise ValueError("supervised-hidden-layers values must be > 0")
        parsed.append(width)
    return parsed[:5]


def _continuous_strategy_profiles(args: argparse.Namespace) -> List[Dict[str, Any]]:
    mode = str(getattr(args, "mode", "")).lower()
    base = {
        "strategy": "base",
        "parent_pool_ratio": float(args.parent_pool_ratio),
        "stagnation_patience": int(args.stagnation_patience),
        "mutation_floor": float(args.mutation_floor),
        "mutation_ceiling": float(args.mutation_ceiling),
        "mutation_step": float(args.mutation_step),
        "quick_cycle_fraction": float(args.quick_cycle_fraction),
        "mid_cycle_fraction": float(args.mid_cycle_fraction),
        "quick_keep_ratio": float(args.quick_keep_ratio),
        "mid_keep_ratio": float(args.mid_keep_ratio),
        "key_variants": int(args.key_variants),
        "novelty_bonus": float(args.novelty_bonus),
        "predictive_penalty": float(args.predictive_penalty),
        "sync_loss_gate_percentile": float(args.sync_loss_gate_percentile),
        "sync_loss_gate_penalty": float(args.sync_loss_gate_penalty),
        "sync_loss_gate_flat_boost": float(args.sync_loss_gate_flat_boost),
        "anti_neutrality_window": int(args.anti_neutrality_window),
        "anti_neutrality_penalty": float(args.anti_neutrality_penalty),
        "anti_neutrality_bonus": float(args.anti_neutrality_bonus),
        "attacker_panel_size": int(args.attacker_panel_size),
        "attacker_panel_penalty": float(args.attacker_panel_penalty),
        "target_generation_seconds": float(args.target_generation_seconds),
        "max_eval_cache_entries": int(args.max_eval_cache_entries),
    }
    if mode != "paper":
        return [base]

    dynamic = {
        "strategy": "dynamic",
        "parent_pool_ratio": _clamp_float(base["parent_pool_ratio"], 0.35, 0.65),
        "stagnation_patience": max(1, base["stagnation_patience"]),
        "mutation_floor": _clamp_float(max(0.18, base["mutation_floor"]), 0.10, 0.90),
        "mutation_ceiling": _clamp_float(max(base["mutation_ceiling"], base["mutation_floor"] + 0.18), 0.55, 0.99),
        "mutation_step": _clamp_float(max(0.10, base["mutation_step"]), 0.03, 0.30),
        "quick_cycle_fraction": _clamp_float(base["quick_cycle_fraction"] + 0.01, 0.05, 0.35),
        "mid_cycle_fraction": _clamp_float(base["mid_cycle_fraction"] + 0.03, 0.18, 0.75),
        "quick_keep_ratio": _clamp_float(base["quick_keep_ratio"] + 0.03, 0.20, 0.82),
        "mid_keep_ratio": _clamp_float(base["mid_keep_ratio"] + 0.02, 0.08, 0.50),
        "key_variants": max(3, base["key_variants"]),
        "novelty_bonus": _clamp_float(max(0.10, base["novelty_bonus"]), 0.03, 0.30),
        "predictive_penalty": _clamp_float(max(0.06, base["predictive_penalty"]), 0.03, 0.22),
        "sync_loss_gate_percentile": _clamp_float(min(base["sync_loss_gate_percentile"], 0.62), 0.30, 0.90),
        "sync_loss_gate_penalty": _clamp_float(max(0.08, base["sync_loss_gate_penalty"]), 0.02, 0.35),
        "sync_loss_gate_flat_boost": _clamp_float(max(0.05, base["sync_loss_gate_flat_boost"]), 0.00, 0.30),
        "anti_neutrality_window": max(6, base["anti_neutrality_window"]),
        "anti_neutrality_penalty": _clamp_float(max(0.02, base["anti_neutrality_penalty"]), 0.00, 0.20),
        "anti_neutrality_bonus": _clamp_float(max(0.01, base["anti_neutrality_bonus"]), 0.00, 0.15),
        "attacker_panel_size": max(2, base["attacker_panel_size"]),
        "attacker_panel_penalty": _clamp_float(max(0.10, base["attacker_panel_penalty"]), 0.00, 0.40),
        "target_generation_seconds": _clamp_float(base["target_generation_seconds"] * 0.95, 0.70, 4.0),
        "max_eval_cache_entries": max(15000, int(round(base["max_eval_cache_entries"] * 1.10))),
    }
    explorer = {
        "strategy": "explorer",
        "parent_pool_ratio": _clamp_float(min(base["parent_pool_ratio"], 0.36), 0.25, 0.55),
        "stagnation_patience": 1,
        "mutation_floor": _clamp_float(max(0.26, base["mutation_floor"]), 0.18, 0.95),
        "mutation_ceiling": _clamp_float(max(0.94, base["mutation_ceiling"]), 0.70, 0.99),
        "mutation_step": _clamp_float(max(0.14, base["mutation_step"]), 0.05, 0.35),
        "quick_cycle_fraction": _clamp_float(min(base["quick_cycle_fraction"], 0.10), 0.05, 0.30),
        "mid_cycle_fraction": _clamp_float(min(base["mid_cycle_fraction"], 0.36), 0.18, 0.70),
        "quick_keep_ratio": _clamp_float(min(base["quick_keep_ratio"], 0.42), 0.20, 0.75),
        "mid_keep_ratio": _clamp_float(min(base["mid_keep_ratio"], 0.16), 0.08, 0.40),
        "key_variants": max(4, base["key_variants"]),
        "novelty_bonus": _clamp_float(max(0.14, base["novelty_bonus"]), 0.05, 0.35),
        "predictive_penalty": _clamp_float(max(0.10, base["predictive_penalty"]), 0.04, 0.25),
        "sync_loss_gate_percentile": _clamp_float(min(base["sync_loss_gate_percentile"], 0.58), 0.30, 0.90),
        "sync_loss_gate_penalty": _clamp_float(max(0.10, base["sync_loss_gate_penalty"]), 0.02, 0.35),
        "sync_loss_gate_flat_boost": _clamp_float(max(0.06, base["sync_loss_gate_flat_boost"]), 0.00, 0.30),
        "anti_neutrality_window": max(6, base["anti_neutrality_window"] - 1),
        "anti_neutrality_penalty": _clamp_float(max(0.03, base["anti_neutrality_penalty"]), 0.00, 0.20),
        "anti_neutrality_bonus": _clamp_float(max(0.01, base["anti_neutrality_bonus"]), 0.00, 0.15),
        "attacker_panel_size": max(3, base["attacker_panel_size"]),
        "attacker_panel_penalty": _clamp_float(max(0.12, base["attacker_panel_penalty"]), 0.00, 0.40),
        "target_generation_seconds": _clamp_float(base["target_generation_seconds"] * 0.88, 0.60, 4.0),
        "max_eval_cache_entries": max(15000, int(round(base["max_eval_cache_entries"] * 1.18))),
    }
    return [dynamic, explorer]


def _combo_label(combo: Dict[str, Any]) -> str:
    strategy = str(combo.get("strategy", "")).strip()
    strategy_suffix = f"-s{strategy}" if strategy else ""
    return (
        f"p{combo['population_size']}-g{combo['generations']}"
        f"-i{combo['initial_instructions']}"
        f"-ap{combo['attacker_population_size']}"
        f"-ag{combo['attacker_generations']}"
        f"-e{combo['elite_pool']}"
        f"{strategy_suffix}"
    )


def _build_continuous_grid(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Generate a finite exhaustive parameter grid for continuous sweeps."""
    populations = _param_choices(args.population_size, minimum=4, high_factor=1.5)
    generations = _param_choices(args.generations, minimum=1, high_factor=1.6)
    instructions = _param_choices(args.initial_instructions, minimum=3, high_factor=1.5)
    attacker_populations = _param_choices(
        args.attacker_population_size,
        minimum=3,
        high_factor=1.5,
    )
    attacker_generations = _param_choices(
        args.attacker_generations,
        minimum=1,
        high_factor=1.6,
    )
    elites = _param_choices(args.elite_pool, minimum=4, high_factor=1.4)
    strategies = _continuous_strategy_profiles(args)

    combos: List[Dict[str, Any]] = []
    for values in itertools.product(
        populations,
        generations,
        instructions,
        attacker_populations,
        attacker_generations,
        elites,
    ):
        pop, gen, instr, apop, agen, elite = values
        for strategy in strategies:
            combos.append(
                {
                    "population_size": pop,
                    "generations": gen,
                    "initial_instructions": instr,
                    "attacker_population_size": apop,
                    "attacker_generations": agen,
                    "elite_pool": min(pop, elite),
                    "archive_limit": max(args.archive_limit, min(pop * 6, args.archive_limit * 2)),
                    "strategy": str(strategy.get("strategy", "base")),
                    "parent_pool_ratio": float(strategy["parent_pool_ratio"]),
                    "stagnation_patience": int(strategy["stagnation_patience"]),
                    "mutation_floor": float(strategy["mutation_floor"]),
                    "mutation_ceiling": float(strategy["mutation_ceiling"]),
                    "mutation_step": float(strategy["mutation_step"]),
                    "quick_cycle_fraction": float(strategy["quick_cycle_fraction"]),
                    "mid_cycle_fraction": float(strategy["mid_cycle_fraction"]),
                    "quick_keep_ratio": float(strategy["quick_keep_ratio"]),
                    "mid_keep_ratio": float(strategy["mid_keep_ratio"]),
                    "key_variants": int(strategy["key_variants"]),
                    "novelty_bonus": float(strategy["novelty_bonus"]),
                    "predictive_penalty": float(strategy["predictive_penalty"]),
                    "sync_loss_gate_percentile": float(strategy["sync_loss_gate_percentile"]),
                    "sync_loss_gate_penalty": float(strategy["sync_loss_gate_penalty"]),
                    "sync_loss_gate_flat_boost": float(strategy["sync_loss_gate_flat_boost"]),
                    "anti_neutrality_window": int(strategy["anti_neutrality_window"]),
                    "anti_neutrality_penalty": float(strategy["anti_neutrality_penalty"]),
                    "anti_neutrality_bonus": float(strategy["anti_neutrality_bonus"]),
                    "attacker_panel_size": int(strategy["attacker_panel_size"]),
                    "attacker_panel_penalty": float(strategy["attacker_panel_penalty"]),
                    "target_generation_seconds": float(strategy["target_generation_seconds"]),
                    "max_eval_cache_entries": int(strategy["max_eval_cache_entries"]),
                }
            )

    dedup: Dict[str, Dict[str, Any]] = {}
    for combo in combos:
        dedup[_combo_label(combo)] = combo
    return list(dedup.values())


def _build_experiment_config(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    seed: int,
    population_size: int,
    generations: int,
    initial_instructions: int,
    rounds: int,
    attacker_population_size: int,
    attacker_generations: int,
    elite_pool: int,
    archive_limit: int,
    resume: bool,
    workers: int,
    parent_pool_ratio: Optional[float] = None,
    stagnation_patience: Optional[int] = None,
    mutation_floor: Optional[float] = None,
    mutation_ceiling: Optional[float] = None,
    mutation_step: Optional[float] = None,
    quick_cycle_fraction: Optional[float] = None,
    mid_cycle_fraction: Optional[float] = None,
    quick_keep_ratio: Optional[float] = None,
    mid_keep_ratio: Optional[float] = None,
    key_variants: Optional[int] = None,
    novelty_bonus: Optional[float] = None,
    predictive_penalty: Optional[float] = None,
    sync_loss_gate_percentile: Optional[float] = None,
    sync_loss_gate_penalty: Optional[float] = None,
    sync_loss_gate_flat_boost: Optional[float] = None,
    anti_neutrality_window: Optional[int] = None,
    anti_neutrality_penalty: Optional[float] = None,
    anti_neutrality_bonus: Optional[float] = None,
    attacker_panel_size: Optional[int] = None,
    attacker_panel_penalty: Optional[float] = None,
    target_generation_seconds: Optional[float] = None,
    max_eval_cache_entries: Optional[int] = None,
) -> ExperimentConfig:
    return ExperimentConfig(
        out_dir=out_dir,
        profile=args.profile,
        seed=seed,
        population_size=population_size,
        generations=generations,
        initial_instructions=initial_instructions,
        rounds=rounds,
        attacker_population_size=attacker_population_size,
        attacker_generations=attacker_generations,
        elite_pool=elite_pool,
        archive_limit=archive_limit,
        resume=resume,
        parallel_workers=workers,
        parallel_backend=args.parallel_backend,
        executor_backend=args.executor_backend,
        use_supervised_guide=bool(args.use_supervised_guide),
        supervised_end_round_only=bool(args.supervised_end_round_only),
        preferred_device=args.device,
        supervised_hidden_layers=tuple(int(width) for width in args.supervised_hidden_layers),
        supervised_epochs=int(args.supervised_epochs),
        supervised_candidate_pool=int(args.supervised_candidate_pool),
        supervised_capacity_auto_tune=bool(args.supervised_capacity_auto_tune),
        parent_pool_ratio=args.parent_pool_ratio if parent_pool_ratio is None else float(parent_pool_ratio),
        stagnation_patience=args.stagnation_patience if stagnation_patience is None else int(stagnation_patience),
        mutation_floor=args.mutation_floor if mutation_floor is None else float(mutation_floor),
        mutation_ceiling=args.mutation_ceiling if mutation_ceiling is None else float(mutation_ceiling),
        mutation_step=args.mutation_step if mutation_step is None else float(mutation_step),
        statistical_predictive=bool(args.statistical_predictive),
        quick_cycle_fraction=args.quick_cycle_fraction if quick_cycle_fraction is None else float(quick_cycle_fraction),
        mid_cycle_fraction=args.mid_cycle_fraction if mid_cycle_fraction is None else float(mid_cycle_fraction),
        quick_keep_ratio=args.quick_keep_ratio if quick_keep_ratio is None else float(quick_keep_ratio),
        mid_keep_ratio=args.mid_keep_ratio if mid_keep_ratio is None else float(mid_keep_ratio),
        key_variant_count=args.key_variants if key_variants is None else int(key_variants),
        novelty_bonus=args.novelty_bonus if novelty_bonus is None else float(novelty_bonus),
        predictive_penalty=args.predictive_penalty if predictive_penalty is None else float(predictive_penalty),
        sync_loss_gate_percentile=(
            args.sync_loss_gate_percentile
            if sync_loss_gate_percentile is None
            else float(sync_loss_gate_percentile)
        ),
        sync_loss_gate_penalty=(
            args.sync_loss_gate_penalty
            if sync_loss_gate_penalty is None
            else float(sync_loss_gate_penalty)
        ),
        sync_loss_gate_flat_boost=(
            args.sync_loss_gate_flat_boost
            if sync_loss_gate_flat_boost is None
            else float(sync_loss_gate_flat_boost)
        ),
        anti_neutrality_window=(
            args.anti_neutrality_window
            if anti_neutrality_window is None
            else int(anti_neutrality_window)
        ),
        anti_neutrality_penalty=(
            args.anti_neutrality_penalty
            if anti_neutrality_penalty is None
            else float(anti_neutrality_penalty)
        ),
        anti_neutrality_bonus=(
            args.anti_neutrality_bonus
            if anti_neutrality_bonus is None
            else float(anti_neutrality_bonus)
        ),
        attacker_panel_size=(
            args.attacker_panel_size
            if attacker_panel_size is None
            else int(attacker_panel_size)
        ),
        attacker_panel_penalty=(
            args.attacker_panel_penalty
            if attacker_panel_penalty is None
            else float(attacker_panel_penalty)
        ),
        auto_statistical_tuning=bool(args.auto_statistical_tuning),
        target_generation_seconds=(
            args.target_generation_seconds
            if target_generation_seconds is None
            else float(target_generation_seconds)
        ),
        max_eval_cache_entries=(
            args.max_eval_cache_entries
            if max_eval_cache_entries is None
            else int(max_eval_cache_entries)
        ),
        device_mhz=args.device_mhz,
        provider_mhz=args.provider_mhz,
        max_test_time_seconds=args.max_test_seconds,
    )


def _resolve_continuous_lane_plan(
    *,
    grid_size: int,
    workers_arg: int,
    parallel_backend: str,
) -> Dict[str, int]:
    cpu_count = max(1, int(os.cpu_count() or 1))
    total_workers = max(1, int(workers_arg)) if int(workers_arg) > 0 else cpu_count
    backend = str(parallel_backend).lower()
    if backend not in {"auto", "process", "thread", "off"}:
        backend = "auto"

    if backend == "off" or grid_size <= 1 or total_workers <= 1:
        return {
            "cpu_count": cpu_count,
            "total_workers": total_workers,
            "lanes": 1,
            "workers_per_lane": max(1, total_workers),
        }

    # Keep parallel lanes wide enough to saturate CPUs inside each experiment lane.
    if backend == "process":
        # Prefer wider process lanes to avoid tiny 4-worker islands on high-core hosts.
        if total_workers >= 24:
            min_workers_per_lane = 8
            max_lanes = 3
        elif total_workers >= 12:
            min_workers_per_lane = 6
            max_lanes = 3
        elif total_workers >= 8:
            min_workers_per_lane = 4
            max_lanes = 2
        else:
            min_workers_per_lane = 2
            max_lanes = 2
    else:
        min_workers_per_lane = 3 if total_workers >= 6 else 2
        max_lanes = 12
    lanes = max(1, total_workers // max(1, min_workers_per_lane))
    lanes = min(lanes, grid_size, max_lanes)
    lanes = max(1, lanes)
    while lanes > 1 and (total_workers // lanes) < min_workers_per_lane:
        lanes -= 1
    workers_per_lane = max(1, total_workers // lanes)
    return {
        "cpu_count": cpu_count,
        "total_workers": total_workers,
        "lanes": lanes,
        "workers_per_lane": workers_per_lane,
    }


def _outer_mp_context() -> Optional[str]:
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return "spawn"
    return "fork"


def _resolve_runtime_config(args: argparse.Namespace) -> Dict[str, Any]:
    defaults = resolve_defaults(profile=str(args.profile), mode=str(args.mode))
    key_map = {
        "seed": "seed",
        "population_size": "population_size",
        "generations": "generations",
        "initial_instructions": "initial_instructions",
        "rounds": "rounds",
        "attacker_population_size": "attacker_population_size",
        "attacker_generations": "attacker_generations",
        "elite_pool": "elite_pool",
        "archive_limit": "archive_limit",
        "continuous_max_iterations": "continuous_max_iterations",
        "workers": "workers",
        "parallel_backend": "parallel_backend",
        "executor_backend": "executor_backend",
        "device": "preferred_device",
        "supervised_hidden_layers": "supervised_hidden_layers",
        "supervised_epochs": "supervised_epochs",
        "supervised_candidate_pool": "supervised_candidate_pool",
        "parent_pool_ratio": "parent_pool_ratio",
        "stagnation_patience": "stagnation_patience",
        "mutation_floor": "mutation_floor",
        "mutation_ceiling": "mutation_ceiling",
        "mutation_step": "mutation_step",
        "quick_cycle_fraction": "quick_cycle_fraction",
        "mid_cycle_fraction": "mid_cycle_fraction",
        "quick_keep_ratio": "quick_keep_ratio",
        "mid_keep_ratio": "mid_keep_ratio",
        "key_variants": "key_variants",
        "novelty_bonus": "novelty_bonus",
        "predictive_penalty": "predictive_penalty",
        "sync_loss_gate_percentile": "sync_loss_gate_percentile",
        "sync_loss_gate_penalty": "sync_loss_gate_penalty",
        "sync_loss_gate_flat_boost": "sync_loss_gate_flat_boost",
        "anti_neutrality_window": "anti_neutrality_window",
        "anti_neutrality_penalty": "anti_neutrality_penalty",
        "anti_neutrality_bonus": "anti_neutrality_bonus",
        "attacker_panel_size": "attacker_panel_size",
        "attacker_panel_penalty": "attacker_panel_penalty",
        "target_generation_seconds": "target_generation_seconds",
        "max_eval_cache_entries": "max_eval_cache_entries",
        "device_mhz": "device_mhz",
        "provider_mhz": "provider_mhz",
        "max_test_seconds": "max_test_seconds",
    }

    resolved: Dict[str, Any] = {}
    for arg_key, cfg_key in key_map.items():
        value = getattr(args, arg_key)
        resolved[arg_key] = defaults[cfg_key] if value is None else value

    resolved["use_supervised_guide"] = bool(defaults["use_supervised_guide"]) and not bool(
        args.no_supervised_guide
    )
    if args.supervised_end_round_only is None:
        resolved["supervised_end_round_only"] = bool(
            defaults.get("supervised_end_round_only", False)
        )
    else:
        resolved["supervised_end_round_only"] = bool(args.supervised_end_round_only)
    if args.supervised_capacity_auto_tune is None:
        resolved["supervised_capacity_auto_tune"] = bool(
            defaults.get("supervised_capacity_auto_tune", True)
        )
    else:
        resolved["supervised_capacity_auto_tune"] = bool(args.supervised_capacity_auto_tune)
    if not resolved["use_supervised_guide"]:
        resolved["supervised_end_round_only"] = False
    resolved["supervised_hidden_layers"] = _parse_hidden_layers_spec(
        resolved.get("supervised_hidden_layers")
    )
    backend = str(resolved.get("executor_backend", "auto")).strip().lower()
    if backend not in {"auto", "cpu", "kompute", "kompute-sim"}:
        backend = "auto"
    resolved["executor_backend"] = backend
    resolved["supervised_epochs"] = max(0, int(resolved.get("supervised_epochs", 0)))
    resolved["supervised_candidate_pool"] = max(0, int(resolved.get("supervised_candidate_pool", 0)))
    resolved["sync_loss_gate_percentile"] = _clamp_float(
        float(resolved.get("sync_loss_gate_percentile", 0.60)),
        0.0,
        1.0,
    )
    resolved["sync_loss_gate_penalty"] = max(
        0.0,
        float(resolved.get("sync_loss_gate_penalty", 0.10)),
    )
    resolved["sync_loss_gate_flat_boost"] = max(
        0.0,
        float(resolved.get("sync_loss_gate_flat_boost", 0.06)),
    )
    resolved["anti_neutrality_window"] = max(
        1,
        int(resolved.get("anti_neutrality_window", 10)),
    )
    resolved["anti_neutrality_penalty"] = max(
        0.0,
        float(resolved.get("anti_neutrality_penalty", 0.03)),
    )
    resolved["anti_neutrality_bonus"] = max(
        0.0,
        float(resolved.get("anti_neutrality_bonus", 0.015)),
    )
    resolved["attacker_panel_size"] = max(
        1,
        int(resolved.get("attacker_panel_size", 3)),
    )
    resolved["attacker_panel_penalty"] = max(
        0.0,
        float(resolved.get("attacker_panel_penalty", 0.16)),
    )
    resolved["statistical_predictive"] = bool(defaults["statistical_predictive"]) and not bool(
        args.no_statistical_predictive
    )
    resolved["auto_statistical_tuning"] = bool(defaults["auto_statistical_tuning"]) and not bool(
        args.no_auto_statistical_tuning
    )
    resolved["resume"] = bool(defaults["resume"]) and not bool(args.no_resume)
    resolved["mode"] = str(args.mode)
    resolved["profile"] = str(args.profile)
    resolved["mode_summary"] = mode_summary(str(args.mode))
    return resolved


def _apply_runtime_config(args: argparse.Namespace, resolved: Dict[str, Any]) -> None:
    for key in (
        "seed",
        "population_size",
        "generations",
        "initial_instructions",
        "rounds",
        "attacker_population_size",
        "attacker_generations",
        "elite_pool",
        "archive_limit",
        "continuous_max_iterations",
        "workers",
        "parallel_backend",
        "executor_backend",
        "device",
        "supervised_hidden_layers",
        "supervised_epochs",
        "supervised_candidate_pool",
        "parent_pool_ratio",
        "stagnation_patience",
        "mutation_floor",
        "mutation_ceiling",
        "mutation_step",
        "quick_cycle_fraction",
        "mid_cycle_fraction",
        "quick_keep_ratio",
        "mid_keep_ratio",
        "key_variants",
        "novelty_bonus",
        "predictive_penalty",
        "sync_loss_gate_percentile",
        "sync_loss_gate_penalty",
        "sync_loss_gate_flat_boost",
        "anti_neutrality_window",
        "anti_neutrality_penalty",
        "anti_neutrality_bonus",
        "attacker_panel_size",
        "attacker_panel_penalty",
        "target_generation_seconds",
        "max_eval_cache_entries",
        "device_mhz",
        "provider_mhz",
        "max_test_seconds",
    ):
        setattr(args, key, resolved[key])
    setattr(args, "use_supervised_guide", bool(resolved["use_supervised_guide"]))
    setattr(
        args,
        "supervised_end_round_only",
        bool(resolved["supervised_end_round_only"]),
    )
    setattr(
        args,
        "supervised_capacity_auto_tune",
        bool(resolved["supervised_capacity_auto_tune"]),
    )
    setattr(args, "statistical_predictive", bool(resolved["statistical_predictive"]))
    setattr(args, "auto_statistical_tuning", bool(resolved["auto_statistical_tuning"]))
    setattr(args, "resume", bool(resolved["resume"]))


def _print_effective_config(resolved: Dict[str, Any]) -> None:
    print(
        "[pcpl-evolvo] config profile={profile} mode={mode} ({summary})".format(
            profile=resolved["profile"],
            mode=resolved["mode"],
            summary=resolved["mode_summary"],
        )
    )
    print(
        "[pcpl-evolvo] evolve pop={pop} gen={gen} rounds={rounds} init={init} atk_pop={apop} atk_gen={agen} elite={elite}".format(
            pop=resolved["population_size"],
            gen=resolved["generations"],
            rounds=resolved["rounds"],
            init=resolved["initial_instructions"],
            apop=resolved["attacker_population_size"],
            agen=resolved["attacker_generations"],
            elite=resolved["elite_pool"],
        )
    )
    print(
        "[pcpl-evolvo] dynamics parent_pool={pp:.2f} stagnation={stag} mutation={mf:.2f}..{mc:.2f} step={ms:.2f}".format(
            pp=float(resolved["parent_pool_ratio"]),
            stag=int(resolved["stagnation_patience"]),
            mf=float(resolved["mutation_floor"]),
            mc=float(resolved["mutation_ceiling"]),
            ms=float(resolved["mutation_step"]),
        )
    )
    print(
        "[pcpl-evolvo] executor backend={backend}".format(
            backend=str(resolved["executor_backend"]),
        )
    )
    print(
        "[pcpl-evolvo] staged quick={qf:.2f}/{qk:.2f} mid={mf:.2f}/{mk:.2f} key_variants={kv} novelty={nov:.3f} penalty={pen:.3f}".format(
            qf=float(resolved["quick_cycle_fraction"]),
            qk=float(resolved["quick_keep_ratio"]),
            mf=float(resolved["mid_cycle_fraction"]),
            mk=float(resolved["mid_keep_ratio"]),
            kv=int(resolved["key_variants"]),
            nov=float(resolved["novelty_bonus"]),
            pen=float(resolved["predictive_penalty"]),
        )
    )
    print(
        "[pcpl-evolvo] sync-gate pct={pct:.2f} penalty={pen:.3f}+{flat:.3f} anti-neutrality={aw}({ap:.3f}/{ab:.3f}) attacker-panel={panel}@{panel_pen:.3f}".format(
            pct=float(resolved["sync_loss_gate_percentile"]),
            pen=float(resolved["sync_loss_gate_penalty"]),
            flat=float(resolved["sync_loss_gate_flat_boost"]),
            aw=int(resolved["anti_neutrality_window"]),
            ap=float(resolved["anti_neutrality_penalty"]),
            ab=float(resolved["anti_neutrality_bonus"]),
            panel=int(resolved["attacker_panel_size"]),
            panel_pen=float(resolved["attacker_panel_penalty"]),
        )
    )
    supervised_mode = "disabled"
    if bool(resolved["use_supervised_guide"]):
        supervised_mode = (
            "end-round-only"
            if bool(resolved["supervised_end_round_only"])
            else "per-generation"
        )
    print(f"[pcpl-evolvo] supervised guide: {supervised_mode}")
    print(
        "[pcpl-evolvo] supervised tuning layers={layers} epochs={epochs} candidate_pool={pool} capacity_auto_tune={capacity}".format(
            layers=(
                ",".join(str(int(width)) for width in resolved.get("supervised_hidden_layers", []))
                or "auto"
            ),
            epochs=(
                int(resolved.get("supervised_epochs", 0))
                if int(resolved.get("supervised_epochs", 0)) > 0
                else "auto"
            ),
            pool=(
                int(resolved.get("supervised_candidate_pool", 0))
                if int(resolved.get("supervised_candidate_pool", 0)) > 0
                else "auto"
            ),
            capacity=(
                "enabled"
                if bool(resolved.get("supervised_capacity_auto_tune", True))
                else "disabled"
            ),
        )
    )
    print(
        "[pcpl-evolvo] runtime target_gen_s={target:.2f} eval_cache={cache}".format(
            target=float(resolved["target_generation_seconds"]),
            cache=int(resolved["max_eval_cache_entries"]),
        )
    )
    if "fitness_schema_version" in resolved:
        print(
            "[pcpl-evolvo] conclusions fitness_schema={schema} analysis_tag={tag} replicates={reps} replicate_ref={rep_ref}".format(
                schema=str(resolved.get("fitness_schema_version", "")),
                tag=str(resolved.get("analysis_tag", "")),
                reps=int(resolved.get("replicates", 1)),
                rep_ref=str(resolved.get("replicate_reference", "best")),
            )
        )


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _normalize_json(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


def _git_revision() -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_DIR.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return str(output) if output else "unknown"
    except Exception:
        return "unknown"


def _score_schema_fingerprint() -> str:
    import hashlib

    hasher = hashlib.blake2b(digest_size=8)
    schema_sources = [
        SRC_DIR / "pcpl_evolvo" / "simulation.py",
        SRC_DIR / "pcpl_evolvo" / "experiment.py",
    ]
    for path in schema_sources:
        hasher.update(str(path.name).encode("utf-8"))
        if path.exists():
            hasher.update(path.read_bytes())
        else:
            hasher.update(b"<missing>")
    return hasher.hexdigest()


def _resolve_fitness_schema_version(
    *,
    requested: str,
    mode: str,
    profile: str,
) -> str:
    value = str(requested or "").strip()
    if value and value.lower() != "auto":
        return value
    fp = _score_schema_fingerprint()
    return f"auto-{mode}-{profile}-{fp}"


def _run_metadata_payload(
    *,
    config: ExperimentConfig,
    summary: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "git_revision": _git_revision(),
        "experiment_config": _normalize_json(dataclasses.asdict(config)),
        "summary": _normalize_json(summary),
    }
    if meta:
        payload["launcher_meta"] = _normalize_json(meta)
    return payload


def _run_once(
    config: ExperimentConfig,
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary = run_experiment(config)
    out_dir = Path(summary["out_dir"])
    summary_path = out_dir / "summary.json"
    _write_json(summary_path, summary)
    run_meta_path = out_dir / "run-metadata.json"
    _write_json(
        run_meta_path,
        _run_metadata_payload(
            config=config,
            summary=summary,
            meta=meta,
        ),
    )
    summary["run_metadata_path"] = str(run_meta_path)
    return summary


def _select_replicate_reference_index(
    summaries: List[Dict[str, Any]],
    *,
    policy: str,
) -> int:
    if not summaries:
        return 0
    normalized = str(policy or "best").strip().lower()
    if normalized == "first":
        return 0
    indexed = list(enumerate(summaries))
    if normalized == "median":
        ordered = sorted(indexed, key=lambda item: float(item[1].get("best_score", float("-inf"))))
        return int(ordered[len(ordered) // 2][0])
    best = max(indexed, key=lambda item: float(item[1].get("best_score", float("-inf"))))
    return int(best[0])


def _print_summary(summary: Dict[str, Any]) -> None:
    print("[pcpl-evolvo] completed")
    print(f"[pcpl-evolvo] out_dir={summary['out_dir']}")
    print(f"[pcpl-evolvo] best_score={summary['best_score']:.6f}")
    print(f"[pcpl-evolvo] best_attacker_score={summary['best_attacker_score']:.6f}")
    if summary.get("reference_score") is not None:
        ref = float(summary["reference_score"])
        print(f"[pcpl-evolvo] reference_score={ref:.6f}")
        print(f"[pcpl-evolvo] delta_vs_reference={(float(summary['best_score']) - ref):+.6f}")
    print(f"[pcpl-evolvo] rounds_completed={summary['rounds_completed']}")
    print(f"[pcpl-evolvo] results={summary['results_json']}")
    print(f"[pcpl-evolvo] report={summary['report_path']}")
    print(f"[pcpl-evolvo] archive={summary['archive_path']}")
    if "resource_plan" in summary:
        plan = summary["resource_plan"]
        print(
            "[pcpl-evolvo] resources backend={backend} workers={workers} gpu={gpu} exec={exec_backend}".format(
                backend=plan.get("parallel_backend"),
                workers=plan.get("parallel_workers"),
                gpu=plan.get("gpu_backend"),
                exec_backend=plan.get("executor_backend", "cpu"),
            )
        )
    if "index_path" in summary:
        print(f"[pcpl-evolvo] index={summary['index_path']}")
    if "conclusion_path" in summary:
        print(f"[pcpl-evolvo] conclusions={summary['conclusion_path']}")
    if "run_metadata_path" in summary:
        print(f"[pcpl-evolvo] run_metadata={summary['run_metadata_path']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PCPL continuous empirical/evolutionary runner (Evolvo-backed)."
    )
    parser.add_argument(
        "--mode",
        choices=available_modes(),
        default=DEFAULT_MODE,
        help=(
            "High-level evolution mode loaded from config.py. "
            "Use this instead of tuning many low-level flags."
        ),
    )
    parser.add_argument(
        "--list-modes",
        action="store_true",
        help="Print available modes from config.py and exit.",
    )
    parser.add_argument(
        "--print-effective-config",
        action="store_true",
        help="Print resolved config (mode + profile + CLI overrides) before run.",
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default=DEFAULT_PROFILE,
        help=(
            "Scenario profile loaded from config.py default. "
            "full is preferred for robust conclusions."
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed.")
    parser.add_argument(
        "--population-size",
        type=int,
        default=None,
        help="Defender population size.",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=None,
        help="Defender generations per round.",
    )
    parser.add_argument(
        "--initial-instructions",
        type=int,
        default=None,
        help="Max random seed instruction count.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        help="Continuous rounds to run in this invocation.",
    )
    parser.add_argument(
        "--attacker-population-size",
        type=int,
        default=None,
        help="Attacker population size per round.",
    )
    parser.add_argument(
        "--attacker-generations",
        type=int,
        default=None,
        help="Attacker generations per round.",
    )
    parser.add_argument(
        "--elite-pool",
        type=int,
        default=None,
        help="Number of top archived genomes used to seed each new round.",
    )
    parser.add_argument(
        "--archive-limit",
        type=int,
        default=None,
        help="Max defender/attacker elites kept in archive.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not load previous archive state from --out-dir.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help=(
            "Output directory. Default: "
            "demo/pcpl-evolvo/runs/<timestamp>-<profile>"
        ),
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Run forever (until Ctrl+C), sweeping all generated parameter "
            "combinations and continuously saving archives/stats."
        ),
    )
    parser.add_argument(
        "--continuous-max-iterations",
        type=int,
        default=None,
        help=(
            "Optional cap for continuous mode iterations; 0 means infinite "
            "(until user stop)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Parallel fitness workers. 0 means auto (use all available CPU cores)."
        ),
    )
    parser.add_argument(
        "--parallel-backend",
        choices=("auto", "process", "thread", "off"),
        default=None,
        help="Parallel backend for fitness evaluation.",
    )
    parser.add_argument(
        "--executor-backend",
        choices=("auto", "cpu", "kompute", "kompute-sim"),
        default=None,
        help=(
            "Execution backend for GFSL program evaluation. "
            "`kompute-sim` runs Kompute compatibility/planning checks plus simulated execution."
        ),
    )
    parser.add_argument(
        "--no-supervised-guide",
        action="store_true",
        help="Disable optional supervised guide acceleration.",
    )
    supervised_group = parser.add_mutually_exclusive_group()
    supervised_group.add_argument(
        "--supervised-end-round-only",
        dest="supervised_end_round_only",
        action="store_true",
        default=None,
        help="Train supervised guide only once per round (after all generations).",
    )
    supervised_group.add_argument(
        "--no-supervised-end-round-only",
        dest="supervised_end_round_only",
        action="store_false",
        help="Use supervised guide each generation (higher overhead).",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "rocm", "mps"),
        default=None,
        help="Preferred compute device for supervised guide.",
    )
    parser.add_argument(
        "--supervised-hidden-layers",
        type=str,
        default=None,
        help=(
            "Comma-separated hidden layer widths for supervised model "
            "(example: 384,256,160,96). Use 'auto' to keep profile defaults."
        ),
    )
    parser.add_argument(
        "--supervised-epochs",
        type=int,
        default=None,
        help="Override supervised training epochs per update (0/omit keeps auto defaults).",
    )
    parser.add_argument(
        "--supervised-candidate-pool",
        type=int,
        default=None,
        help="Override supervised mutation candidate pool size (0/omit keeps auto defaults).",
    )
    supervised_capacity_group = parser.add_mutually_exclusive_group()
    supervised_capacity_group.add_argument(
        "--supervised-capacity-auto-tune",
        dest="supervised_capacity_auto_tune",
        action="store_true",
        default=None,
        help="Enable adaptive supervised model capacity control (recommended).",
    )
    supervised_capacity_group.add_argument(
        "--no-supervised-capacity-auto-tune",
        dest="supervised_capacity_auto_tune",
        action="store_false",
        help="Disable adaptive supervised model capacity control.",
    )
    parser.add_argument(
        "--parent-pool-ratio",
        type=float,
        default=None,
        help="Fraction of top genomes used as parent pool (less random dispersivity).",
    )
    parser.add_argument(
        "--stagnation-patience",
        type=int,
        default=None,
        help="Generations without improvement before increasing mutation pressure.",
    )
    parser.add_argument(
        "--mutation-floor",
        type=float,
        default=None,
        help="Minimum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-ceiling",
        type=float,
        default=None,
        help="Maximum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-step",
        type=float,
        default=None,
        help="Adaptive mutation step when stagnating/improving.",
    )
    parser.add_argument(
        "--no-statistical-predictive",
        action="store_true",
        help="Disable staged statistical evaluation and run full evaluation on all genomes.",
    )
    parser.add_argument(
        "--quick-cycle-fraction",
        type=float,
        default=None,
        help="Initial fraction of cycles used by quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-cycle-fraction",
        type=float,
        default=None,
        help="Initial fraction of cycles used by medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--quick-keep-ratio",
        type=float,
        default=None,
        help="Initial fraction of genomes kept after quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-keep-ratio",
        type=float,
        default=None,
        help="Initial fraction of genomes kept after medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--key-variants",
        type=int,
        default=None,
        help="Initial key generation/sharing variants per scenario (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--no-auto-statistical-tuning",
        action="store_true",
        help="Keep staged fractions/ratios fixed to CLI values (disable real-time auto-tuning).",
    )
    parser.add_argument(
        "--novelty-bonus",
        type=float,
        default=None,
        help="Fitness bonus for novel non-duplicate genomes during staged ranking.",
    )
    parser.add_argument(
        "--predictive-penalty",
        type=float,
        default=None,
        help="Penalty applied when a genome is cut by predictive stages.",
    )
    parser.add_argument(
        "--sync-loss-gate-percentile",
        type=float,
        default=None,
        help="Defender full-stage percentile threshold for projected_sync_loss_rate gate.",
    )
    parser.add_argument(
        "--sync-loss-gate-penalty",
        type=float,
        default=None,
        help="Base fitness penalty for defenders above sync-loss gate threshold.",
    )
    parser.add_argument(
        "--sync-loss-gate-flat-boost",
        type=float,
        default=None,
        help="Extra sync-loss gate penalty added when recent generations are flat.",
    )
    parser.add_argument(
        "--anti-neutrality-window",
        type=int,
        default=None,
        help="Defender generations window used to detect repeated phenotype fingerprints.",
    )
    parser.add_argument(
        "--anti-neutrality-penalty",
        type=float,
        default=None,
        help="Penalty for defender candidates repeating recent phenotype fingerprint.",
    )
    parser.add_argument(
        "--anti-neutrality-bonus",
        type=float,
        default=None,
        help="Bonus for defender candidates improving sync/horizon while escaping repeats.",
    )
    parser.add_argument(
        "--attacker-panel-size",
        type=int,
        default=None,
        help="Round-selection attacker panel size used for defender robust ranking.",
    )
    parser.add_argument(
        "--attacker-panel-penalty",
        type=float,
        default=None,
        help="Penalty multiplier for worst attacker advantage in defender panel ranking.",
    )
    parser.add_argument(
        "--target-generation-seconds",
        type=float,
        default=None,
        help="Target max wall-time per generation batch used by auto-tuning/early-stop.",
    )
    parser.add_argument(
        "--max-eval-cache-entries",
        type=int,
        default=None,
        help="Per-round dedup cache capacity for reuse of evaluated genome signatures.",
    )
    parser.add_argument(
        "--device-mhz",
        type=float,
        default=None,
        help="Simulated consumer device frequency in MHz.",
    )
    parser.add_argument(
        "--provider-mhz",
        type=float,
        default=None,
        help="Simulated provider frequency in MHz.",
    )
    parser.add_argument(
        "--max-test-seconds",
        type=float,
        default=None,
        help="Long-horizon timing projection target (seconds).",
    )
    parser.add_argument(
        "--analysis-tag",
        type=str,
        default="",
        help="Optional label stored in run metadata for later conclusion aggregation.",
    )
    parser.add_argument(
        "--fitness-schema-version",
        type=str,
        default=DEFAULT_FITNESS_SCHEMA_VERSION,
        help=(
            "Fitness/scoring schema label stored with each run. "
            "Use 'auto' (default) to derive a deterministic schema reference from current scoring sources."
        ),
    )
    parser.add_argument(
        "--replicates",
        type=int,
        default=1,
        help=(
            "Number of independent single-run replications (same config, different seeds, separate out dirs)."
        ),
    )
    parser.add_argument(
        "--replicate-seed-step",
        type=int,
        default=7919,
        help="Seed increment between replicates when --replicates > 1.",
    )
    parser.add_argument(
        "--replicate-reference",
        choices=("best", "median", "first"),
        default="best",
        help=(
            "Canonical replicate selector for multi-run campaigns: "
            "best (max score), median (middle score), or first (rep-000)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rounds_explicit = args.rounds is not None
    if args.list_modes:
        print("[pcpl-evolvo] available modes:")
        for name in available_modes():
            print(f"- {name}: {mode_summary(name)}")
        return

    resolved = _resolve_runtime_config(args)
    _apply_runtime_config(args, resolved)
    args.fitness_schema_version = _resolve_fitness_schema_version(
        requested=str(args.fitness_schema_version),
        mode=str(args.mode),
        profile=str(args.profile),
    )
    if int(args.replicate_seed_step) <= 0:
        raise ValueError("--replicate-seed-step must be > 0")
    if int(args.supervised_epochs) < 0:
        raise ValueError("--supervised-epochs must be >= 0")
    if int(args.supervised_candidate_pool) < 0:
        raise ValueError("--supervised-candidate-pool must be >= 0")
    if not (0.0 <= float(args.sync_loss_gate_percentile) <= 1.0):
        raise ValueError("--sync-loss-gate-percentile must be in [0, 1]")
    if float(args.sync_loss_gate_penalty) < 0.0:
        raise ValueError("--sync-loss-gate-penalty must be >= 0")
    if float(args.sync_loss_gate_flat_boost) < 0.0:
        raise ValueError("--sync-loss-gate-flat-boost must be >= 0")
    if int(args.anti_neutrality_window) < 1:
        raise ValueError("--anti-neutrality-window must be >= 1")
    if float(args.anti_neutrality_penalty) < 0.0:
        raise ValueError("--anti-neutrality-penalty must be >= 0")
    if float(args.anti_neutrality_bonus) < 0.0:
        raise ValueError("--anti-neutrality-bonus must be >= 0")
    if int(args.attacker_panel_size) < 1:
        raise ValueError("--attacker-panel-size must be >= 1")
    if float(args.attacker_panel_penalty) < 0.0:
        raise ValueError("--attacker-panel-penalty must be >= 0")
    resolved["fitness_schema_version"] = str(args.fitness_schema_version)
    resolved["analysis_tag"] = str(args.analysis_tag or "")
    resolved["replicates"] = int(args.replicates)
    resolved["replicate_reference"] = str(args.replicate_reference)
    if args.continuous and str(args.mode).lower() == "paper" and not rounds_explicit:
        # In continuous paper sweeps, prioritize cadence across many combos/strategies.
        args.rounds = 1
        resolved["rounds"] = 1
        print(
            "[pcpl-evolvo] paper continuous default: using rounds=1 per iteration for faster signal cadence (set --rounds to override)."
        )
    _print_effective_config(resolved)
    if args.print_effective_config:
        return

    if args.continuous and int(args.replicates) != 1:
        print("[pcpl-evolvo] warning: --replicates is ignored in --continuous mode")

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = (PROJECT_DIR / "runs" / f"{stamp}-{args.profile}").resolve()

    if not args.continuous:
        if int(args.replicates) < 1:
            raise ValueError("--replicates must be >= 1")

        base_meta = {
            "analysis_tag": str(args.analysis_tag or ""),
            "fitness_schema_version": str(args.fitness_schema_version),
            "mode": str(args.mode),
            "profile": str(args.profile),
            "replicate_reference_policy": str(args.replicate_reference),
            "resolved_config": resolved,
            "continuous": False,
            "launcher": "run_experiments.py",
        }

        if int(args.replicates) == 1:
            config = _build_experiment_config(
                args,
                out_dir=out_dir,
                seed=args.seed,
                population_size=args.population_size,
                generations=args.generations,
                initial_instructions=args.initial_instructions,
                rounds=args.rounds,
                attacker_population_size=args.attacker_population_size,
                attacker_generations=args.attacker_generations,
                elite_pool=args.elite_pool,
                archive_limit=args.archive_limit,
                resume=bool(args.resume),
                workers=args.workers,
            )

            summary = _run_once(config, meta=base_meta)
            _print_summary(summary)
            return

        out_dir.mkdir(parents=True, exist_ok=True)
        replicate_summaries: List[Dict[str, Any]] = []
        base_seed = int(args.seed)
        seed_step = int(args.replicate_seed_step)
        print(
            "[pcpl-evolvo] replicate campaign: n={n} base_seed={seed} step={step} root={root}".format(
                n=int(args.replicates),
                seed=base_seed,
                step=seed_step,
                root=out_dir,
            )
        )
        for rep in range(int(args.replicates)):
            rep_seed = base_seed + (rep * seed_step)
            rep_out_dir = (out_dir / f"rep-{rep:03d}").resolve()
            config = _build_experiment_config(
                args,
                out_dir=rep_out_dir,
                seed=rep_seed,
                population_size=args.population_size,
                generations=args.generations,
                initial_instructions=args.initial_instructions,
                rounds=args.rounds,
                attacker_population_size=args.attacker_population_size,
                attacker_generations=args.attacker_generations,
                elite_pool=args.elite_pool,
                archive_limit=args.archive_limit,
                resume=False,
                workers=args.workers,
            )
            rep_meta = dict(base_meta)
            rep_meta["replicate_index"] = rep
            rep_meta["replicate_seed"] = rep_seed
            rep_meta["replicate_count"] = int(args.replicates)
            summary = _run_once(config, meta=rep_meta)
            _print_summary(summary)
            replicate_summaries.append(summary)

        best_scores = [float(item["best_score"]) for item in replicate_summaries]
        attacker_scores = [float(item["best_attacker_score"]) for item in replicate_summaries]
        round_counts = [int(item.get("rounds_completed", 0)) for item in replicate_summaries]
        ref_idx = _select_replicate_reference_index(
            replicate_summaries,
            policy=str(args.replicate_reference),
        )
        ref_summary = replicate_summaries[ref_idx]
        campaign_summary: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "analysis_tag": str(args.analysis_tag or ""),
            "fitness_schema_version": str(args.fitness_schema_version),
            "mode": str(args.mode),
            "profile": str(args.profile),
            "replicate_count": int(args.replicates),
            "base_seed": base_seed,
            "seed_step": seed_step,
            "reference_policy": str(args.replicate_reference),
            "reference_replicate_index": int(ref_idx),
            "reference_out_dir": str(ref_summary.get("out_dir", "")),
            "reference_best_score": float(ref_summary.get("best_score", float("-inf"))),
            "best_score_mean": sum(best_scores) / float(max(1, len(best_scores))),
            "best_score_min": min(best_scores) if best_scores else None,
            "best_score_max": max(best_scores) if best_scores else None,
            "best_attacker_score_mean": sum(attacker_scores) / float(max(1, len(attacker_scores))),
            "rounds_completed_mean": sum(round_counts) / float(max(1, len(round_counts))),
            "runs": [
                {
                    "out_dir": item.get("out_dir"),
                    "best_score": item.get("best_score"),
                    "best_attacker_score": item.get("best_attacker_score"),
                    "rounds_completed": item.get("rounds_completed"),
                    "archive_path": item.get("archive_path"),
                    "report_path": item.get("report_path"),
                    "run_metadata_path": item.get("run_metadata_path"),
                }
                for item in replicate_summaries
            ],
        }
        campaign_path = out_dir / "replicates-summary.json"
        _write_json(campaign_path, campaign_summary)
        reference_path = out_dir / "reference-run.json"
        _write_json(
            reference_path,
            {
                "timestamp": datetime.now().isoformat(),
                "analysis_tag": str(args.analysis_tag or ""),
                "fitness_schema_version": str(args.fitness_schema_version),
                "policy": str(args.replicate_reference),
                "replicate_index": int(ref_idx),
                "summary": ref_summary,
            },
        )
        print(f"[pcpl-evolvo] replicates_summary={campaign_path}")
        print(f"[pcpl-evolvo] replicate_reference={reference_path}")
        return

    if args.no_resume:
        print("[pcpl-evolvo] warning: --no-resume ignored in --continuous mode")

    out_dir.mkdir(parents=True, exist_ok=True)
    runs_root = out_dir / "continuous-runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    grid = _build_continuous_grid(args)
    if not grid:
        raise RuntimeError("Continuous grid is empty")
    strategy_counts: Dict[str, int] = {}
    for combo in grid:
        strategy = str(combo.get("strategy", "base"))
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    state_path = out_dir / "continuous-state.json"
    leaderboard_path = out_dir / "continuous-leaderboard.json"
    log_path = out_dir / "continuous.log"

    rng = random.Random(args.seed)
    order = list(range(len(grid)))
    rng.shuffle(order)

    leaderboard: Dict[str, Dict[str, Any]] = {}
    total_iterations = 0
    total_sweeps = 0
    lane_plan = _resolve_continuous_lane_plan(
        grid_size=len(grid),
        workers_arg=int(args.workers),
        parallel_backend=str(args.parallel_backend),
    )
    lane_count = int(lane_plan["lanes"])
    workers_per_lane = int(lane_plan["workers_per_lane"])

    print(
        "[pcpl-evolvo] continuous mode: combos={count} rounds-per-iteration={rounds} output={out} lanes={lanes} workers-per-lane={lane_workers} total-workers={total_workers}".format(
            count=len(grid),
            rounds=max(1, args.rounds),
            out=out_dir,
            lanes=lane_count,
            lane_workers=workers_per_lane,
            total_workers=int(lane_plan["total_workers"]),
        )
    )
    if len(strategy_counts) > 1:
        details = ", ".join(
            f"{name}={count}" for name, count in sorted(strategy_counts.items())
        )
        print(f"[pcpl-evolvo] continuous strategies: {details}")

    outer_kwargs: Dict[str, Any] = {}
    mp_ctx_name = _outer_mp_context()
    if mp_ctx_name is not None:
        try:
            outer_kwargs["mp_context"] = multiprocessing.get_context(mp_ctx_name)
        except Exception:
            pass

    try:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=lane_count,
            **outer_kwargs,
        ) as combo_pool:
            while True:
                stop_requested = False
                next_slot = 0
                pending: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
                while next_slot < len(order) or pending:
                    while next_slot < len(order) and len(pending) < lane_count:
                        if (
                            args.continuous_max_iterations > 0
                            and (total_iterations + len(pending))
                            >= args.continuous_max_iterations
                        ):
                            break
                        combo_idx = order[next_slot]
                        combo = grid[combo_idx]
                        combo_name = _combo_label(combo)
                        combo_dir = runs_root / combo_name
                        combo_dir.mkdir(parents=True, exist_ok=True)

                        iteration_index = total_iterations + len(pending)
                        run_seed = args.seed + (iteration_index * 7_919) + next_slot
                        config = _build_experiment_config(
                            args,
                            out_dir=combo_dir,
                            seed=run_seed,
                            population_size=combo["population_size"],
                            generations=combo["generations"],
                            initial_instructions=combo["initial_instructions"],
                            rounds=max(1, args.rounds),
                            attacker_population_size=combo["attacker_population_size"],
                            attacker_generations=combo["attacker_generations"],
                            elite_pool=combo["elite_pool"],
                            archive_limit=combo["archive_limit"],
                            resume=True,
                            workers=workers_per_lane,
                            parent_pool_ratio=combo.get("parent_pool_ratio"),
                            stagnation_patience=combo.get("stagnation_patience"),
                            mutation_floor=combo.get("mutation_floor"),
                            mutation_ceiling=combo.get("mutation_ceiling"),
                            mutation_step=combo.get("mutation_step"),
                            quick_cycle_fraction=combo.get("quick_cycle_fraction"),
                            mid_cycle_fraction=combo.get("mid_cycle_fraction"),
                            quick_keep_ratio=combo.get("quick_keep_ratio"),
                            mid_keep_ratio=combo.get("mid_keep_ratio"),
                            key_variants=combo.get("key_variants"),
                            novelty_bonus=combo.get("novelty_bonus"),
                            predictive_penalty=combo.get("predictive_penalty"),
                            sync_loss_gate_percentile=combo.get("sync_loss_gate_percentile"),
                            sync_loss_gate_penalty=combo.get("sync_loss_gate_penalty"),
                            sync_loss_gate_flat_boost=combo.get("sync_loss_gate_flat_boost"),
                            anti_neutrality_window=combo.get("anti_neutrality_window"),
                            anti_neutrality_penalty=combo.get("anti_neutrality_penalty"),
                            anti_neutrality_bonus=combo.get("anti_neutrality_bonus"),
                            attacker_panel_size=combo.get("attacker_panel_size"),
                            attacker_panel_penalty=combo.get("attacker_panel_penalty"),
                            target_generation_seconds=combo.get("target_generation_seconds"),
                            max_eval_cache_entries=combo.get("max_eval_cache_entries"),
                        )

                        print(
                            "[pcpl-evolvo] launch iter={iter} sweep={sweep} combo={combo} strategy={strategy} seed={seed} lane={lane}/{lanes} lane-workers={lane_workers} target_gen_s={target:.2f} cache={cache}".format(
                                iter=iteration_index,
                                sweep=total_sweeps,
                                combo=combo_name,
                                strategy=str(combo.get("strategy", "base")),
                                seed=run_seed,
                                lane=len(pending) + 1,
                                lanes=lane_count,
                                lane_workers=workers_per_lane,
                                target=float(combo.get("target_generation_seconds", args.target_generation_seconds)),
                                cache=int(combo.get("max_eval_cache_entries", args.max_eval_cache_entries)),
                            )
                        )
                        combo_meta = {
                            "analysis_tag": str(args.analysis_tag or ""),
                            "fitness_schema_version": str(args.fitness_schema_version),
                            "mode": str(args.mode),
                            "profile": str(args.profile),
                            "replicate_reference_policy": "single",
                            "strategy": str(combo.get("strategy", "base")),
                            "combo_label": combo_name,
                            "continuous": True,
                            "launcher": "run_experiments.py",
                        }
                        future = combo_pool.submit(_run_once, config, meta=combo_meta)
                        pending[future] = {
                            "combo": combo,
                            "combo_name": combo_name,
                            "combo_dir": str(combo_dir),
                        }
                        next_slot += 1

                    if (
                        args.continuous_max_iterations > 0
                        and total_iterations >= args.continuous_max_iterations
                        and not pending
                    ):
                        stop_requested = True
                        break

                    if not pending:
                        break

                    done, _ = concurrent.futures.wait(
                        list(pending.keys()),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        meta = pending.pop(future)
                        combo = meta["combo"]
                        combo_name = meta["combo_name"]
                        success = True
                        try:
                            summary = future.result()
                        except Exception as exc:
                            success = False
                            summary = {
                                "out_dir": meta["combo_dir"],
                                "error": str(exc),
                                "best_score": float("-inf"),
                                "best_attacker_score": float("-inf"),
                                "rounds_completed": 0,
                            }
                            print(
                                "[pcpl-evolvo] combo failed combo={combo} error={error}".format(
                                    combo=combo_name,
                                    error=exc,
                                )
                            )

                        if success:
                            leaderboard[combo_name] = {
                                "combo": combo,
                                "strategy": str(combo.get("strategy", "base")),
                                "best_score": summary["best_score"],
                                "best_signature": summary["best_signature"],
                                "best_attacker_score": summary["best_attacker_score"],
                                "best_attacker_signature": summary["best_attacker_signature"],
                                "rounds_completed": summary["rounds_completed"],
                                "archive_path": summary["archive_path"],
                                "updated_at": datetime.now().isoformat(),
                            }

                        top = sorted(
                            leaderboard.values(),
                            key=lambda item: float(item["best_score"]),
                            reverse=True,
                        )[:20]
                        state_payload = {
                            "continuous": True,
                            "profile": args.profile,
                            "mode": str(args.mode),
                            "analysis_tag": str(args.analysis_tag or ""),
                            "fitness_schema_version": str(args.fitness_schema_version),
                            "iterations_completed": total_iterations + 1,
                            "sweeps_completed": total_sweeps,
                            "grid_size": len(grid),
                            "latest_combo": combo_name,
                            "latest_summary": summary,
                            "updated_at": datetime.now().isoformat(),
                        }
                        _write_json(state_path, state_payload)
                        _write_json(
                            leaderboard_path,
                            {
                                "updated_at": datetime.now().isoformat(),
                                "analysis_tag": str(args.analysis_tag or ""),
                                "fitness_schema_version": str(args.fitness_schema_version),
                                "leaders": top,
                            },
                        )

                        with log_path.open("a", encoding="utf-8") as handle:
                            if success:
                                handle.write(
                                    "{ts} iter={iter} sweep={sweep} combo={combo} score={score:.6f} attacker={attack:.6f} rounds={rounds}\n".format(
                                        ts=datetime.now().isoformat(),
                                        iter=total_iterations,
                                        sweep=total_sweeps,
                                        combo=combo_name,
                                        score=float(summary["best_score"]),
                                        attack=float(summary["best_attacker_score"]),
                                        rounds=int(summary["rounds_completed"]),
                                    )
                                )
                            else:
                                handle.write(
                                    "{ts} iter={iter} sweep={sweep} combo={combo} status=error\n".format(
                                        ts=datetime.now().isoformat(),
                                        iter=total_iterations,
                                        sweep=total_sweeps,
                                        combo=combo_name,
                                    )
                                )

                        if success:
                            _print_summary(summary)
                        total_iterations += 1

                        if (
                            args.continuous_max_iterations > 0
                            and total_iterations >= args.continuous_max_iterations
                        ):
                            print(
                                "[pcpl-evolvo] continuous stop: reached --continuous-max-iterations="
                                f"{args.continuous_max_iterations}"
                            )
                            stop_requested = True
                            break

                    if stop_requested:
                        for pending_future in list(pending.keys()):
                            pending_future.cancel()
                        break

                if stop_requested:
                    return

                total_sweeps += 1
                rng.shuffle(order)
                print(
                    "[pcpl-evolvo] sweep complete: sweeps={sweeps} iterations={iters}".format(
                        sweeps=total_sweeps,
                        iters=total_iterations,
                    )
                )
    except KeyboardInterrupt:
        print("[pcpl-evolvo] continuous mode stopped by user (Ctrl+C)")
        print(f"[pcpl-evolvo] state={state_path}")
        print(f"[pcpl-evolvo] leaderboard={leaderboard_path}")
        print(f"[pcpl-evolvo] log={log_path}")


if __name__ == "__main__":
    main()
