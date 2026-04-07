"""Central runtime defaults and evolution presets for pcpl-evolvo.

Edit this file to tune behavior without passing dozens of CLI arguments.
CLI options still override these defaults when explicitly provided.
"""

from __future__ import annotations

from typing import Any, Dict, List


DEFAULT_MODE = "dynamic"


BASE_DEFAULTS: Dict[str, Any] = {
    "seed": 1337,
    "population_size": 18,
    "generations": 16,
    "initial_instructions": 12,
    "rounds": 1,
    "attacker_population_size": 12,
    "attacker_generations": 6,
    "elite_pool": 12,
    "archive_limit": 64,
    "continuous_max_iterations": 0,
    "workers": 0,
    "parallel_backend": "auto",
    "preferred_device": "auto",
    "parent_pool_ratio": 0.60,
    "stagnation_patience": 4,
    "mutation_floor": 0.12,
    "mutation_ceiling": 0.55,
    "mutation_step": 0.05,
    "quick_cycle_fraction": 0.14,
    "mid_cycle_fraction": 0.50,
    "quick_keep_ratio": 0.55,
    "mid_keep_ratio": 0.30,
    "key_variants": 2,
    "novelty_bonus": 0.03,
    "predictive_penalty": 0.05,
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
        "generations": 12,
        "attacker_generations": 5,
    },
    "full": {},
}


PROFILE_MODE_OVERRIDES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "fast": {
        "dynamic": {
            "generations": 14,
            "attacker_generations": 6,
        },
        "explorer": {
            "generations": 16,
            "attacker_generations": 7,
        },
    },
    "full": {
        "dynamic": {
            "generations": 16,
            "attacker_generations": 8,
        },
        "explorer": {
            "generations": 18,
            "attacker_generations": 10,
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
    if mode == "dynamic":
        return "Aggressive anti-stagnation and novelty pressure."
    if mode == "explorer":
        return "Maximum exploration to escape stable local optima."
    return "Custom mode."

