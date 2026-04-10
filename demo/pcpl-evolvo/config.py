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
    "generations": 72,
    "initial_instructions": 16,
    "rounds": 8,
    "attacker_population_size": 64,
    "attacker_generations": 24,
    "elite_pool": 48,
    "archive_limit": 256,
    "continuous_max_iterations": 0,
    "workers": 0,
    "parallel_backend": "process",
    "preferred_device": "auto",
    "parent_pool_ratio": 0.50,
    "stagnation_patience": 2,
    "mutation_floor": 0.18,
    "mutation_ceiling": 0.90,
    "mutation_step": 0.10,
    "quick_cycle_fraction": 0.10,
    "mid_cycle_fraction": 0.35,
    "quick_keep_ratio": 0.42,
    "mid_keep_ratio": 0.16,
    "key_variants": 4,
    "novelty_bonus": 0.10,
    "predictive_penalty": 0.08,
    "device_mhz": 100.0,
    "provider_mhz": 300.0,
    "max_test_seconds": 10.0,
    "use_supervised_guide": True,
    "statistical_predictive": True,
    "auto_statistical_tuning": True,
    "resume": True,
}


MODE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "balanced": {},
    # Paper-grade default: high parallel throughput with dynamic/explorer-compatible settings.
    "paper": {
        "population_size": 160,
        "generations": 120,
        "rounds": 16,
        "attacker_population_size": 112,
        "attacker_generations": 40,
        "elite_pool": 80,
        "archive_limit": 512,
        "parent_pool_ratio": 0.40,
        "stagnation_patience": 1,
        "mutation_floor": 0.22,
        "mutation_ceiling": 0.98,
        "mutation_step": 0.14,
        "quick_cycle_fraction": 0.10,
        "mid_cycle_fraction": 0.36,
        "quick_keep_ratio": 0.40,
        "mid_keep_ratio": 0.15,
        "key_variants": 5,
        "novelty_bonus": 0.14,
        "predictive_penalty": 0.09,
    },
    # Default mode for publishing empirical conclusions.
    "conclusion": {
        "population_size": 128,
        "generations": 96,
        "rounds": 12,
        "attacker_population_size": 96,
        "attacker_generations": 32,
        "elite_pool": 64,
        "archive_limit": 384,
        "parent_pool_ratio": 0.42,
        "stagnation_patience": 2,
        "mutation_floor": 0.20,
        "mutation_ceiling": 0.95,
        "mutation_step": 0.12,
        "quick_cycle_fraction": 0.10,
        "mid_cycle_fraction": 0.34,
        "quick_keep_ratio": 0.40,
        "mid_keep_ratio": 0.15,
        "key_variants": 4,
        "novelty_bonus": 0.12,
        "predictive_penalty": 0.07,
    },
    # More aggressive adaptation and novelty pressure; designed to avoid score plateaus.
    "dynamic": {
        "parent_pool_ratio": 0.45,
        "stagnation_patience": 2,
        "mutation_floor": 0.18,
        "mutation_ceiling": 0.82,
        "mutation_step": 0.09,
        "quick_cycle_fraction": 0.11,
        "mid_cycle_fraction": 0.40,
        "quick_keep_ratio": 0.46,
        "mid_keep_ratio": 0.18,
        "key_variants": 3,
        "novelty_bonus": 0.09,
        "predictive_penalty": 0.08,
        "initial_instructions": 14,
        "attacker_generations": 8,
    },
    # High-entropy exploration mode for escaping local optima quickly.
    "explorer": {
        "parent_pool_ratio": 0.35,
        "stagnation_patience": 1,
        "mutation_floor": 0.26,
        "mutation_ceiling": 0.95,
        "mutation_step": 0.13,
        "quick_cycle_fraction": 0.10,
        "mid_cycle_fraction": 0.35,
        "quick_keep_ratio": 0.42,
        "mid_keep_ratio": 0.16,
        "key_variants": 4,
        "novelty_bonus": 0.12,
        "predictive_penalty": 0.10,
        "initial_instructions": 16,
        "attacker_generations": 10,
        "attacker_population_size": 14,
    },
}


PROFILE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "fast": {
        "population_size": 64,
        "generations": 40,
        "rounds": 6,
        "attacker_population_size": 48,
        "attacker_generations": 16,
        "elite_pool": 32,
        "archive_limit": 192,
        "key_variants": 3,
    },
    "full": {},
}


PROFILE_MODE_OVERRIDES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "fast": {
        "paper": {
            "population_size": 96,
            "generations": 68,
            "rounds": 10,
            "attacker_population_size": 72,
            "attacker_generations": 24,
            "elite_pool": 52,
            "archive_limit": 352,
        },
        "conclusion": {
            "population_size": 72,
            "generations": 48,
            "rounds": 8,
            "attacker_population_size": 56,
            "attacker_generations": 18,
            "elite_pool": 36,
            "archive_limit": 224,
        },
        "dynamic": {
            "population_size": 64,
            "generations": 44,
            "rounds": 6,
            "attacker_population_size": 48,
            "attacker_generations": 18,
        },
        "explorer": {
            "population_size": 80,
            "generations": 56,
            "rounds": 6,
            "attacker_population_size": 56,
            "attacker_generations": 22,
            "archive_limit": 256,
        },
    },
    "full": {
        "paper": {
            "population_size": 160,
            "generations": 120,
            "rounds": 16,
            "attacker_population_size": 112,
            "attacker_generations": 40,
            "elite_pool": 80,
            "archive_limit": 512,
        },
        "conclusion": {
            "population_size": 128,
            "generations": 96,
            "rounds": 12,
            "attacker_population_size": 96,
            "attacker_generations": 32,
            "elite_pool": 64,
            "archive_limit": 384,
        },
        "dynamic": {
            "population_size": 104,
            "generations": 80,
            "rounds": 8,
            "attacker_population_size": 72,
            "attacker_generations": 26,
            "elite_pool": 52,
        },
        "explorer": {
            "population_size": 112,
            "generations": 84,
            "rounds": 8,
            "attacker_population_size": 80,
            "attacker_generations": 30,
            "archive_limit": 320,
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
        return "Paper-grade default: high parallel throughput with dynamic + explorer strategy sweeps."
    if mode == "conclusion":
        return "Strong default for full empirical conclusions and improvement discovery."
    if mode == "dynamic":
        return "Aggressive anti-stagnation and novelty pressure."
    if mode == "explorer":
        return "Maximum exploration to escape stable local optima."
    return "Custom mode."
