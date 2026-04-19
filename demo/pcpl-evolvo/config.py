"""Central runtime defaults and evolution presets for pcpl-evolvo.

Edit this file to tune behavior without passing dozens of CLI arguments.
CLI options still override these defaults when explicitly provided.
"""

from __future__ import annotations

from typing import Any, Dict, List

from settings import ACCELERATION_DEFAULTS


DEFAULT_PROFILE = "full"
DEFAULT_MODE = "paper"


BASE_DEFAULTS: Dict[str, Any] = {
    "seed": 94960397,
    "population_size": 96,
    "generations": 56,
    "initial_instructions": 16,
    "rounds": 10,
    "attacker_population_size": 64,
    "attacker_generations": 18,
    "elite_pool": 48,
    "archive_limit": 256,
    "continuous_max_iterations": 0,
    "workers": 0,
    "parallel_backend": "process",
    "round_parallelism": 0,
    "max_cpu_utilization": 0.75,
    "max_gpu_utilization": 0.75,
    "round_state_sync": "batch-start",
    "executor_backend": str(ACCELERATION_DEFAULTS.get("executor_backend", "auto")),
    "kompute_runtime_mode": str(
        ACCELERATION_DEFAULTS.get("kompute_runtime_mode", "native")
    ),
    "kompute_warn_on_fallback": bool(
        ACCELERATION_DEFAULTS.get("kompute_warn_on_fallback", True)
    ),
    "kompute_fail_hard": bool(
        ACCELERATION_DEFAULTS.get("kompute_fail_hard", False)
    ),
    "kompute_keep_vram_state": bool(
        ACCELERATION_DEFAULTS.get("kompute_keep_vram_state", True)
    ),
    "kompute_min_native_stage_count": int(
        ACCELERATION_DEFAULTS.get("kompute_min_native_stage_count", 1)
    ),
    "kompute_min_native_stage_share": float(
        ACCELERATION_DEFAULTS.get("kompute_min_native_stage_share", 0.0)
    ),
    "kompute_max_unsupported_count": int(
        ACCELERATION_DEFAULTS.get("kompute_max_unsupported_count", -1)
    ),
    "kompute_max_unsupported_share": float(
        ACCELERATION_DEFAULTS.get("kompute_max_unsupported_share", 1.0)
    ),
    "kompute_force_cpu_on_partial_coverage": bool(
        ACCELERATION_DEFAULTS.get("kompute_force_cpu_on_partial_coverage", False)
    ),
    "kompute_native_enable_decimal": bool(
        ACCELERATION_DEFAULTS.get("kompute_native_enable_decimal", True)
    ),
    "kompute_native_enable_boolean_compare": bool(
        ACCELERATION_DEFAULTS.get("kompute_native_enable_boolean_compare", True)
    ),
    "kompute_native_enable_boolean_logic": bool(
        ACCELERATION_DEFAULTS.get("kompute_native_enable_boolean_logic", True)
    ),
    "kompute_native_enable_list_query": bool(
        ACCELERATION_DEFAULTS.get("kompute_native_enable_list_query", True)
    ),
    "kompute_allow_process_pool": bool(
        ACCELERATION_DEFAULTS.get("kompute_allow_process_pool", False)
    ),
    "preferred_device": "auto",
    "parent_pool_ratio": 0.50,
    "stagnation_patience": 2,
    "mutation_floor": 0.20,
    "mutation_ceiling": 0.90,
    "mutation_step": 0.12,
    "quick_cycle_fraction": 0.07,
    "mid_cycle_fraction": 0.24,
    "quick_keep_ratio": 0.52,
    "mid_keep_ratio": 0.30,
    "key_variants": 5,
    "novelty_bonus": 0.12,
    "predictive_penalty": 0.08,
    "sync_loss_gate_percentile": 0.60,
    "sync_loss_gate_penalty": 0.10,
    "sync_loss_gate_flat_boost": 0.06,
    "anti_neutrality_window": 10,
    "anti_neutrality_penalty": 0.030,
    "anti_neutrality_bonus": 0.015,
    "attacker_panel_size": 3,
    "attacker_panel_penalty": 0.16,
    "target_generation_seconds": 1.8,
    "max_eval_cache_entries": 32000,
    "device_mhz": 100.0,
    "provider_mhz": 300.0,
    "max_test_seconds": 10.0,
    "use_supervised_guide": True,
    "supervised_end_round_only": True,
    "supervised_hidden_layers": [],
    "supervised_epochs": 0,
    "supervised_candidate_pool": 0,
    "supervised_capacity_auto_tune": True,
    "statistical_predictive": True,
    "auto_statistical_tuning": True,
    "resume": True,
}


MODE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "balanced": {},
    # Paper-grade default: high parallel throughput with dynamic/explorer-compatible settings.
    "paper": {
        "population_size": 152,
        "generations": 56,
        "rounds": 32,
        "attacker_population_size": 104,
        "attacker_generations": 18,
        "elite_pool": 76,
        "archive_limit": 640,
        "parent_pool_ratio": 0.34,
        "stagnation_patience": 2,
        "mutation_floor": 0.28,
        "mutation_ceiling": 0.99,
        "mutation_step": 0.18,
        "quick_cycle_fraction": 0.07,
        "mid_cycle_fraction": 0.26,
        "quick_keep_ratio": 0.50,
        "mid_keep_ratio": 0.28,
        "key_variants": 6,
        "novelty_bonus": 0.20,
        "predictive_penalty": 0.09,
        "sync_loss_gate_percentile": 0.58,
        "sync_loss_gate_penalty": 0.12,
        "sync_loss_gate_flat_boost": 0.08,
        "anti_neutrality_window": 10,
        "anti_neutrality_penalty": 0.032,
        "anti_neutrality_bonus": 0.017,
        "attacker_panel_size": 4,
        "attacker_panel_penalty": 0.18,
        "target_generation_seconds": 2.4,
        "max_eval_cache_entries": 56000,
        "supervised_end_round_only": True,
    },
    # Default mode for publishing empirical conclusions.
    "conclusion": {
        "population_size": 128,
        "generations": 64,
        "rounds": 20,
        "attacker_population_size": 96,
        "attacker_generations": 22,
        "elite_pool": 64,
        "archive_limit": 512,
        "parent_pool_ratio": 0.36,
        "stagnation_patience": 1,
        "mutation_floor": 0.24,
        "mutation_ceiling": 0.97,
        "mutation_step": 0.14,
        "quick_cycle_fraction": 0.06,
        "mid_cycle_fraction": 0.24,
        "quick_keep_ratio": 0.54,
        "mid_keep_ratio": 0.30,
        "key_variants": 5,
        "novelty_bonus": 0.14,
        "predictive_penalty": 0.07,
        "sync_loss_gate_percentile": 0.60,
        "sync_loss_gate_penalty": 0.10,
        "sync_loss_gate_flat_boost": 0.07,
        "anti_neutrality_window": 10,
        "anti_neutrality_penalty": 0.030,
        "anti_neutrality_bonus": 0.015,
        "attacker_panel_size": 3,
        "attacker_panel_penalty": 0.16,
        "target_generation_seconds": 1.4,
        "max_eval_cache_entries": 50000,
        "supervised_end_round_only": True,
    },
    # More aggressive adaptation and novelty pressure; designed to avoid score plateaus.
    "dynamic": {
        "parent_pool_ratio": 0.40,
        "stagnation_patience": 1,
        "mutation_floor": 0.24,
        "mutation_ceiling": 0.94,
        "mutation_step": 0.16,
        "quick_cycle_fraction": 0.06,
        "mid_cycle_fraction": 0.26,
        "quick_keep_ratio": 0.56,
        "mid_keep_ratio": 0.33,
        "key_variants": 5,
        "novelty_bonus": 0.16,
        "predictive_penalty": 0.08,
        "sync_loss_gate_percentile": 0.56,
        "sync_loss_gate_penalty": 0.12,
        "sync_loss_gate_flat_boost": 0.08,
        "anti_neutrality_window": 9,
        "anti_neutrality_penalty": 0.034,
        "anti_neutrality_bonus": 0.018,
        "attacker_panel_size": 4,
        "attacker_panel_penalty": 0.19,
        "target_generation_seconds": 1.3,
        "max_eval_cache_entries": 42000,
        "initial_instructions": 14,
        "attacker_generations": 14,
        "supervised_end_round_only": True,
    },
    # High-entropy exploration mode for escaping local optima quickly.
    "explorer": {
        "parent_pool_ratio": 0.30,
        "stagnation_patience": 1,
        "mutation_floor": 0.30,
        "mutation_ceiling": 0.99,
        "mutation_step": 0.20,
        "quick_cycle_fraction": 0.05,
        "mid_cycle_fraction": 0.22,
        "quick_keep_ratio": 0.60,
        "mid_keep_ratio": 0.34,
        "key_variants": 6,
        "novelty_bonus": 0.20,
        "predictive_penalty": 0.10,
        "sync_loss_gate_percentile": 0.54,
        "sync_loss_gate_penalty": 0.13,
        "sync_loss_gate_flat_boost": 0.09,
        "anti_neutrality_window": 8,
        "anti_neutrality_penalty": 0.036,
        "anti_neutrality_bonus": 0.020,
        "attacker_panel_size": 4,
        "attacker_panel_penalty": 0.20,
        "target_generation_seconds": 1.2,
        "max_eval_cache_entries": 46000,
        "initial_instructions": 16,
        "attacker_generations": 16,
        "attacker_population_size": 80,
        "supervised_end_round_only": True,
    },
}


PROFILE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "fast": {
        "population_size": 64,
        "generations": 32,
        "rounds": 10,
        "attacker_population_size": 48,
        "attacker_generations": 12,
        "elite_pool": 32,
        "archive_limit": 192,
        "key_variants": 4,
        "target_generation_seconds": 1.2,
        "max_eval_cache_entries": 22000,
    },
    "full": {},
}


PROFILE_MODE_OVERRIDES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "fast": {
        "paper": {
            "population_size": 96,
            "generations": 34,
            "rounds": 18,
            "attacker_population_size": 72,
            "attacker_generations": 12,
            "elite_pool": 52,
            "archive_limit": 352,
        },
        "conclusion": {
            "population_size": 80,
            "generations": 36,
            "rounds": 14,
            "attacker_population_size": 56,
            "attacker_generations": 12,
            "elite_pool": 36,
            "archive_limit": 224,
        },
        "dynamic": {
            "population_size": 72,
            "generations": 32,
            "rounds": 12,
            "attacker_population_size": 48,
            "attacker_generations": 12,
        },
        "explorer": {
            "population_size": 80,
            "generations": 34,
            "rounds": 12,
            "attacker_population_size": 56,
            "attacker_generations": 14,
            "archive_limit": 256,
        },
    },
    "full": {
        "paper": {
            "population_size": 152,
            "generations": 52,
            "rounds": 36,
            "attacker_population_size": 104,
            "attacker_generations": 16,
            "elite_pool": 76,
            "archive_limit": 640,
        },
        "conclusion": {
            "population_size": 128,
            "generations": 64,
            "rounds": 22,
            "attacker_population_size": 96,
            "attacker_generations": 20,
            "elite_pool": 64,
            "archive_limit": 512,
        },
        "dynamic": {
            "population_size": 104,
            "generations": 46,
            "rounds": 16,
            "attacker_population_size": 72,
            "attacker_generations": 16,
            "elite_pool": 52,
        },
        "explorer": {
            "population_size": 112,
            "generations": 50,
            "rounds": 16,
            "attacker_population_size": 80,
            "attacker_generations": 18,
            "archive_limit": 480,
        },
    },
}


def available_modes() -> List[str]:
    return sorted(MODE_OVERRIDES.keys())


def resolve_defaults(*, profile: str, mode: str) -> Dict[str, Any]:
    if mode not in MODE_OVERRIDES:
        raise ValueError(f"Unknown mode: {mode}")
    if profile not in PROFILE_OVERRIDES:
        raise ValueError(f"Unknown profile: {profile}")

    resolved = dict(BASE_DEFAULTS)
    resolved.update(PROFILE_OVERRIDES.get(profile, {}))
    resolved.update(MODE_OVERRIDES.get(mode, {}))
    resolved.update(PROFILE_MODE_OVERRIDES.get(profile, {}).get(mode, {}))
    return resolved


def mode_summary(mode: str) -> str:
    if mode == "balanced":
        return "Balanced exploitation/exploration."
    if mode == "paper":
        return "Paper-grade default: shorter high-variance rounds, tighter fractions, and runtime auto-tuning."
    if mode == "conclusion":
        return "Strong default for full empirical conclusions and improvement discovery."
    if mode == "dynamic":
        return "Aggressive anti-stagnation and novelty pressure."
    if mode == "explorer":
        return "Maximum exploration to escape stable local optima."
    return "Custom mode."
