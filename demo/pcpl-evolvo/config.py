"""Central runtime defaults and evolution presets for pcpl-evolvo.

Edit this file to tune behavior without passing dozens of CLI arguments.
CLI options still override these defaults when explicitly provided.
"""

from __future__ import annotations

from typing import Any, Dict, List


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
    "target_generation_seconds": 1.8,
    "max_eval_cache_entries": 32000,
    "device_mhz": 100.0,
    "provider_mhz": 300.0,
    "max_test_seconds": 10.0,
    "use_supervised_guide": True,
    "supervised_end_round_only": True,
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
        "stagnation_patience": 1,
        "mutation_floor": 0.26,
        "mutation_ceiling": 0.99,
        "mutation_step": 0.18,
        "quick_cycle_fraction": 0.05,
        "mid_cycle_fraction": 0.22,
        "quick_keep_ratio": 0.58,
        "mid_keep_ratio": 0.32,
        "key_variants": 6,
        "novelty_bonus": 0.18,
        "predictive_penalty": 0.07,
        "target_generation_seconds": 1.25,
        "max_eval_cache_entries": 65000,
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
