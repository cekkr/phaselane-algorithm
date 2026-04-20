"""Acceleration settings for pcpl-evolvo experiments.

Edit these flags to tune CPU/Kompute/Torch flow without changing core code.
`run_experiments.py` and `pcpl_evolvo.experiment` consume these defaults.
"""

from __future__ import annotations

# GFSL execution backend used by scenario evaluation:
# auto | cpu | kompute | kompute-sim
EXECUTOR_BACKEND = "auto"

# Kompute runtime mode:
# native | simulated | auto
KOMPUTE_RUNTIME_MODE = "native"

# Kompute runtime controls.
KOMPUTE_WARN_ON_FALLBACK = True
KOMPUTE_FAIL_HARD = False
KOMPUTE_KEEP_VRAM_STATE = True

# Coverage policy to avoid expensive hybrid CPU<->GPU exchange when native coverage is weak.
# A value of 0 disables the minimum gate.
KOMPUTE_MIN_NATIVE_STAGE_COUNT = 1
KOMPUTE_MIN_NATIVE_STAGE_SHARE = 0.0

# A value of -1 disables unsupported-count limit.
KOMPUTE_MAX_UNSUPPORTED_COUNT = -1
KOMPUTE_MAX_UNSUPPORTED_SHARE = 1.0

# If True, any partial coverage forces CPU execution for that genome.
KOMPUTE_FORCE_CPU_ON_PARTIAL_COVERAGE = False

# Native shader family gates (useful for bottleneck isolation / mixed CPU-GPU policy tuning).
KOMPUTE_NATIVE_ENABLE_DECIMAL = True
KOMPUTE_NATIVE_ENABLE_BOOLEAN_COMPARE = True
KOMPUTE_NATIVE_ENABLE_BOOLEAN_LOGIC = True
KOMPUTE_NATIVE_ENABLE_LIST_QUERY = True

# If False, process-pool parallelism is automatically downgraded to threads in Kompute mode.
KOMPUTE_ALLOW_PROCESS_POOL = True


ACCELERATION_DEFAULTS = {
    "executor_backend": str(EXECUTOR_BACKEND).strip().lower(),
    "kompute_runtime_mode": str(KOMPUTE_RUNTIME_MODE).strip().lower(),
    "kompute_warn_on_fallback": bool(KOMPUTE_WARN_ON_FALLBACK),
    "kompute_fail_hard": bool(KOMPUTE_FAIL_HARD),
    "kompute_keep_vram_state": bool(KOMPUTE_KEEP_VRAM_STATE),
    "kompute_min_native_stage_count": int(KOMPUTE_MIN_NATIVE_STAGE_COUNT),
    "kompute_min_native_stage_share": float(KOMPUTE_MIN_NATIVE_STAGE_SHARE),
    "kompute_max_unsupported_count": int(KOMPUTE_MAX_UNSUPPORTED_COUNT),
    "kompute_max_unsupported_share": float(KOMPUTE_MAX_UNSUPPORTED_SHARE),
    "kompute_force_cpu_on_partial_coverage": bool(KOMPUTE_FORCE_CPU_ON_PARTIAL_COVERAGE),
    "kompute_native_enable_decimal": bool(KOMPUTE_NATIVE_ENABLE_DECIMAL),
    "kompute_native_enable_boolean_compare": bool(KOMPUTE_NATIVE_ENABLE_BOOLEAN_COMPARE),
    "kompute_native_enable_boolean_logic": bool(KOMPUTE_NATIVE_ENABLE_BOOLEAN_LOGIC),
    "kompute_native_enable_list_query": bool(KOMPUTE_NATIVE_ENABLE_LIST_QUERY),
    "kompute_allow_process_pool": bool(KOMPUTE_ALLOW_PROCESS_POOL),
}


__all__ = [
    "EXECUTOR_BACKEND",
    "KOMPUTE_RUNTIME_MODE",
    "KOMPUTE_WARN_ON_FALLBACK",
    "KOMPUTE_FAIL_HARD",
    "KOMPUTE_KEEP_VRAM_STATE",
    "KOMPUTE_MIN_NATIVE_STAGE_COUNT",
    "KOMPUTE_MIN_NATIVE_STAGE_SHARE",
    "KOMPUTE_MAX_UNSUPPORTED_COUNT",
    "KOMPUTE_MAX_UNSUPPORTED_SHARE",
    "KOMPUTE_FORCE_CPU_ON_PARTIAL_COVERAGE",
    "KOMPUTE_NATIVE_ENABLE_DECIMAL",
    "KOMPUTE_NATIVE_ENABLE_BOOLEAN_COMPARE",
    "KOMPUTE_NATIVE_ENABLE_BOOLEAN_LOGIC",
    "KOMPUTE_NATIVE_ENABLE_LIST_QUERY",
    "KOMPUTE_ALLOW_PROCESS_POOL",
    "ACCELERATION_DEFAULTS",
]
