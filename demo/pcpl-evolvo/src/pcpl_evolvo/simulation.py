"""PCPL simulation environment, scoring and adversarial benchmarks."""

from __future__ import annotations

import copy
import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable, load_reference_pcpl

ensure_evolvo_importable()

from evolvo import (
    Category,
    DataType,
    GFSLExpressionBuilder,
    GFSLExecutor,
    GFSLGenome,
    GFSLInstruction,
    Operation,
    custom_operations,
    pack_type_index,
    register_custom_operation,
)


DEFENDER_OUTPUT_ACTIVE_IDX = 20
DEFENDER_OUTPUT_KERNEL_IDX = 21
DEFENDER_OUTPUT_STATE_MIX_IDX = 22
DEFENDER_OUTPUT_EXP_MIX_IDX = 23
DEFENDER_OUTPUT_HASH_ROUNDS_IDX = 24
DEFENDER_OUTPUT_BOUQUET_SPREAD_IDX = 25
DEFENDER_OUTPUT_STATE_CHURN_IDX = 26
DEFENDER_OUTPUT_LANE_SALT_IDX = 27
DEFENDER_OUTPUT_TOKEN_SCRAMBLE_IDX = 28
DEFENDER_OUTPUT_PHASE_JITTER_IDX = 29

OUTPUT_INDICES = (
    DEFENDER_OUTPUT_ACTIVE_IDX,
    DEFENDER_OUTPUT_KERNEL_IDX,
    DEFENDER_OUTPUT_STATE_MIX_IDX,
    DEFENDER_OUTPUT_EXP_MIX_IDX,
    DEFENDER_OUTPUT_HASH_ROUNDS_IDX,
    DEFENDER_OUTPUT_BOUQUET_SPREAD_IDX,
    DEFENDER_OUTPUT_STATE_CHURN_IDX,
    DEFENDER_OUTPUT_LANE_SALT_IDX,
    DEFENDER_OUTPUT_TOKEN_SCRAMBLE_IDX,
    DEFENDER_OUTPUT_PHASE_JITTER_IDX,
)
ATTACK_OUTPUT_INDICES = (40, 41, 42)
INPUT_DECIMAL_COUNT = 18
ATTACK_INPUT_DECIMAL_COUNT = 16


OPERATION_COST_UNITS = {
    int(Operation.IF): 1.4,
    int(Operation.WHILE): 1.8,
    int(Operation.END): 0.4,
    int(Operation.SET): 0.6,
    int(Operation.RESULT): 0.3,
    int(Operation.FUNC): 1.6,
    int(Operation.CALL): 1.8,
    int(Operation.GT): 0.7,
    int(Operation.LT): 0.7,
    int(Operation.EQ): 0.7,
    int(Operation.GTE): 0.7,
    int(Operation.LTE): 0.7,
    int(Operation.NEQ): 0.7,
    int(Operation.AND): 0.5,
    int(Operation.OR): 0.5,
    int(Operation.NOT): 0.4,
    int(Operation.ADD): 0.5,
    int(Operation.SUB): 0.5,
    int(Operation.MUL): 0.9,
    int(Operation.DIV): 1.2,
    int(Operation.POW): 2.2,
    int(Operation.SQRT): 1.4,
    int(Operation.ABS): 0.4,
    int(Operation.SIN): 1.8,
    int(Operation.COS): 1.8,
    int(Operation.EXP): 2.0,
    int(Operation.LOG): 1.9,
    int(Operation.MOD): 1.1,
    int(Operation.PREPEND): 0.9,
    int(Operation.APPEND): 0.8,
    int(Operation.CLONE): 1.2,
    int(Operation.FIFO): 0.9,
    int(Operation.FILO): 0.9,
    int(Operation.LISTCOUNT): 0.8,
    int(Operation.LISTHASITEMS): 0.8,
    int(Operation.CONV): 3.5,
    int(Operation.LINEAR): 3.0,
    int(Operation.RELU): 0.7,
    int(Operation.POOL): 1.8,
    int(Operation.NORM): 1.9,
    int(Operation.DROPOUT): 1.0,
    int(Operation.SOFTMAX): 2.6,
    int(Operation.RESHAPE): 0.9,
    int(Operation.CONCAT): 1.2,
}
DEFAULT_OPERATION_COST = 1.0
PCPL_CUSTOM_OP_CODES: Dict[str, int] = {}
_EXECUTOR_CACHE_LOCAL = threading.local()
_MAX_THREAD_EXECUTOR_CACHE = 16


def _executor_cache_store() -> Dict[Tuple[str, Tuple[Tuple[str, str], ...]], GFSLExecutor]:
    store = getattr(_EXECUTOR_CACHE_LOCAL, "store", None)
    if isinstance(store, dict):
        return store
    created: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], GFSLExecutor] = {}
    _EXECUTOR_CACHE_LOCAL.store = created
    return created


def _executor_cache_key(
    *,
    role: str,
    runtime_kwargs: Dict[str, object],
) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    normalized = tuple(
        sorted((str(key), repr(value)) for key, value in runtime_kwargs.items())
    )
    return (str(role).strip().lower() or "default", normalized)


def _cached_scenario_executor(
    *,
    role: str,
    runtime_kwargs: Dict[str, object],
) -> GFSLExecutor:
    store = _executor_cache_store()
    key = _executor_cache_key(role=role, runtime_kwargs=runtime_kwargs)
    cached = store.get(key)
    if cached is not None:
        return cached
    if len(store) >= int(_MAX_THREAD_EXECUTOR_CACHE):
        store.clear()
    executor = GFSLExecutor(
        track_instruction_activity=False,
        **runtime_kwargs,
    )
    store[key] = executor
    return executor


@dataclass(frozen=True)
class ScenarioConfig:
    """Single evaluation scenario for PCPL empirical scoring."""

    name: str
    x: int
    cycles: int
    seed: int
    token_bits: int = 96
    prime_mode: str = "fixed"
    prime_bits: int = 20
    modulus_bits: int = 61
    compound_mode: str = "blend"
    compound_count: int = 5
    compound_primes: int = 3
    compound_offset: int = 3
    compound_prime_bits: int = 0
    compound_pool_size: int = 18
    cycle_budget_ms: float = 1.0
    timing_budget_ns: int = 4_000_000
    absolute_time_ms: float = 10_000.0
    sync_tolerance_ms: float = 25.0
    resync_window_ms: float = 4.0
    attack_token_bits: int = 16
    device_mhz: float = 100.0
    provider_mhz: float = 300.0
    max_test_time_seconds: float = 10.0


def _quantize_decimal(value: float) -> int:
    clipped = clamp(float(value), -1_000_000.0, 1_000_000.0)
    scaled = abs(clipped) * 1_000_000.0
    rounded = int(math.floor(scaled + 0.5))
    return -rounded if clipped < 0.0 else rounded


_U32_MASK = 0xFFFFFFFF


def _u32(value: int) -> int:
    return int(value) & int(_U32_MASK)


def _mix_u32(value: int) -> int:
    mixed = _u32(value)
    mixed ^= mixed >> 16
    mixed = _u32(mixed * 0x7FEB352D)
    mixed ^= mixed >> 15
    mixed = _u32(mixed * 0x846CA68B)
    mixed ^= mixed >> 16
    return _u32(mixed)


def _unit_from_u32(value: int) -> float:
    return float(_u32(value)) / float(0xFFFFFFFF)


def _pcpl_hashmix_op(a: float, b: float) -> float:
    qa = _quantize_decimal(a)
    qb = _quantize_decimal(b)
    seed = _u32(qa) ^ _u32(qb * 0x9E3779B1)
    unit = _unit_from_u32(_mix_u32(seed ^ 0xA5A5A5A5))
    return (2.0 * unit) - 1.0


def _pcpl_phasemix_op(a: float, b: float) -> float:
    qa = _quantize_decimal(a)
    qb = _quantize_decimal(b)
    folded = _u32((qa * 31) ^ (qb * 17))
    seed = folded ^ _u32(qa - qb) ^ 0xC3A5C85C
    unit = _unit_from_u32(_mix_u32(seed))
    return (2.0 * unit) - 1.0


def _pcpl_modhash_op(a: float, b: float) -> float:
    qa = _quantize_decimal(a)
    qb = _quantize_decimal(b)
    base = abs(qa) + 1
    mod = (abs(qb) % 1_000_003) + 97
    mixed = pow(base, 3, mod)
    seed = _u32(mixed) ^ _u32(mod * 0x27D4EB2D) ^ 0x85EBCA6B
    unit = _unit_from_u32(_mix_u32(seed))
    return (2.0 * unit) - 1.0


def _register_pcpl_custom_operations() -> None:
    specs = [
        ("PCPL_HASHMIX", _pcpl_hashmix_op, 2.2),
        ("PCPL_PHASEMIX", _pcpl_phasemix_op, 1.9),
        ("PCPL_MODHASH", _pcpl_modhash_op, 2.0),
    ]
    for name, function, cost in specs:
        existing = custom_operations.get_code_by_name(name)
        code = existing
        if code is None:
            try:
                code = register_custom_operation(
                    name=name,
                    target_type=DataType.DECIMAL,
                    function=function,
                    arity=2,
                    source_types=(DataType.DECIMAL, DataType.DECIMAL),
                    doc="PCPL-specific decimal hash mixer",
                )
            except Exception:
                code = custom_operations.get_code_by_name(name)
        if code is not None:
            PCPL_CUSTOM_OP_CODES[name] = int(code)
            OPERATION_COST_UNITS[int(code)] = float(cost)


_register_pcpl_custom_operations()


@dataclass(frozen=True)
class PolicyDecision:
    """Control outputs produced by an evolved GFSL circuit."""

    active_ratio: float
    kernel: int
    stride_seed: int
    state_mix: float
    exponent_mix: float
    hash_rounds: int
    bouquet_spread: float
    state_churn: float
    lane_salt: int
    token_scramble: float
    phase_jitter: float


@dataclass(frozen=True)
class LaneTokenResult:
    token: int
    active_count: int
    pow_ops: int
    hash_ops: int


@dataclass
class ScenarioMetrics:
    """Detailed score breakdown for one scenario."""

    scenario: str
    total_score: float
    principle_score: float
    sync_score: float
    security_score: float
    cost_score: float
    runtime_score: float
    stability_score: float
    operation_cost_score: float
    brute_force_resistance_score: float
    reverse_hack_resistance_score: float
    one_of_x_rate: float
    block_once_rate: float
    permutation_valid_rate: float
    attack_reject_rate: float
    twin_sync_rate: float
    timing_reject_rate: float
    sync_loss_rate: float
    resync_success_rate: float
    cross_lane_collision_rate: float
    replay_rate: float
    controller_fail_rate: float
    shared_device_match_rate: float
    attacker_lane_success_rate: float
    attacker_token_success_rate: float
    attacker_advantage_score: float
    avg_device_cycle_ms: float
    avg_provider_cycle_ms: float
    absolute_time_reference_ms: float
    projected_sync_loss_rate: float
    horizon_sync_score: float
    device_compound_ratio: float
    provider_compound_ratio: float
    elapsed_seconds: float
    qft_score: float = 0.0
    qft_period_bits: float = 0.0
    qft_period_ratio: float = 0.0
    linear_rank_score: float = 0.0
    linear_rank_mod2_ratio: float = 0.0
    linear_rank_mod65537_ratio: float = 0.0
    linear_rank_unique_ratio: float = 0.0
    compare_x_score: float = 0.0
    compare_x_period_ratio: float = 0.0
    compare_x_chain_ratio: float = 0.0
    phase_error_control_score: float = 0.0
    control_flow_score: float = 0.0
    native_execution_calls: float = 0.0
    native_kompute_calls: float = 0.0
    native_gpu_dispatch_count: float = 0.0
    native_cpu_fallback_count: float = 0.0
    native_cpu_full_sync_count: float = 0.0
    native_cpu_partial_sync_count: float = 0.0
    native_cpu_no_sync_count: float = 0.0
    native_cpu_synced_tensors: float = 0.0
    native_final_sync_count: float = 0.0
    native_gpu_share: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _empty_native_counter() -> Dict[str, int]:
    return {
        "execution_calls": 0,
        "kompute_calls": 0,
        "gpu_dispatch_count": 0,
        "cpu_fallback_count": 0,
        "cpu_full_sync_count": 0,
        "cpu_partial_sync_count": 0,
        "cpu_no_sync_count": 0,
        "cpu_synced_tensors": 0,
        "final_sync_count": 0,
    }


def _accumulate_executor_native_stats(counter: Dict[str, int], executor: GFSLExecutor) -> None:
    getter = getattr(executor, "last_execution_stats", None)
    if not callable(getter):
        return
    try:
        stats = getter()
    except Exception:
        return
    if not isinstance(stats, dict):
        return
    counter["execution_calls"] += 1
    if bool(stats.get("used_kompute", False)):
        counter["kompute_calls"] += 1
    counter["gpu_dispatch_count"] += max(0, int(stats.get("gpu_dispatch_count", 0)))
    counter["cpu_fallback_count"] += max(0, int(stats.get("cpu_fallback_count", 0)))
    counter["cpu_full_sync_count"] += max(0, int(stats.get("cpu_full_sync_count", 0)))
    counter["cpu_partial_sync_count"] += max(0, int(stats.get("cpu_partial_sync_count", 0)))
    counter["cpu_no_sync_count"] += max(0, int(stats.get("cpu_no_sync_count", 0)))
    counter["cpu_synced_tensors"] += max(0, int(stats.get("cpu_synced_tensors", 0)))
    counter["final_sync_count"] += max(0, int(stats.get("final_sync_count", 0)))


def sigmoid(value: float) -> float:
    if value < -40.0:
        return 0.0
    if value > 40.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


def _budget_score(ratio: float) -> float:
    if ratio <= 1.0:
        return 1.0
    return 1.0 / (1.0 + (ratio - 1.0))


def _analysis_seed_label(scenario_name: str, base_x: int, x_value: int) -> str:
    if int(x_value) == int(base_x):
        return f"PARAMS:{scenario_name}"
    return f"PARAMS:{scenario_name}:COMPARE_X:{int(x_value)}"


@lru_cache(maxsize=1024)
def _period_bits_for_x(
    x_value: int,
    token_bits: int,
    prime_mode: str,
    prime_bits: int,
    modulus_bits: int,
    seed: int,
    scenario_name: str,
    base_x: int,
) -> int:
    pcpl = load_reference_pcpl()
    build_params = pcpl["build_params"]
    derive_seed = pcpl["derive_seed"]
    schedule_period = pcpl["schedule_period"]

    param_rng = None
    if str(prime_mode) == "generated":
        label = _analysis_seed_label(str(scenario_name), int(base_x), int(x_value))
        param_rng = random.Random(derive_seed(int(seed), label))

    params = build_params(
        int(x_value),
        int(token_bits),
        prime_mode=str(prime_mode),
        prime_bits=int(prime_bits),
        modulus_bits=int(modulus_bits),
        rng=param_rng,
    )
    return int(schedule_period(params)).bit_length()


@lru_cache(maxsize=512)
def _compare_x_profile(
    x_value: int,
    token_bits: int,
    prime_mode: str,
    prime_bits: int,
    modulus_bits: int,
    seed: int,
    scenario_name: str,
) -> Tuple[float, float, float]:
    candidates = sorted(set((2, 3, 4, 5, 6, int(x_value))))
    period_bits = [
        _period_bits_for_x(
            int(candidate),
            int(token_bits),
            str(prime_mode),
            int(prime_bits),
            int(modulus_bits),
            int(seed),
            str(scenario_name),
            int(x_value),
        )
        for candidate in candidates
    ]

    current_bits = float(
        _period_bits_for_x(
            int(x_value),
            int(token_bits),
            str(prime_mode),
            int(prime_bits),
            int(modulus_bits),
            int(seed),
            str(scenario_name),
            int(x_value),
        )
    )
    max_bits = float(max(period_bits)) if period_bits else max(1.0, current_bits)
    min_bits = float(min(period_bits)) if period_bits else min(1.0, current_bits)

    ratio_vs_max = clamp(current_bits / float(max(1.0, max_bits)), 0.0, 1.0)
    if max_bits <= min_bits:
        ratio_vs_span = 1.0
    else:
        ratio_vs_span = clamp((current_bits - min_bits) / (max_bits - min_bits), 0.0, 1.0)
    compare_x_period_ratio = clamp((0.70 * ratio_vs_max) + (0.30 * ratio_vs_span), 0.0, 1.0)
    compare_x_chain_ratio = clamp(
        float(max(1, int(x_value) - 1)) / float(max(1, max(candidates) - 1)),
        0.0,
        1.0,
    )
    return current_bits, compare_x_period_ratio, compare_x_chain_ratio


@lru_cache(maxsize=1024)
def _linear_rank_profile(
    x_value: int,
    token_bits: int,
    prime_mode: str,
    prime_bits: int,
    modulus_bits: int,
    seed: int,
    scenario_name: str,
    num_compounds: int,
    window: int,
) -> Tuple[float, float, float]:
    pcpl = load_reference_pcpl()
    build_params = pcpl["build_params"]
    derive_seed = pcpl["derive_seed"]
    exponent_vector = pcpl["exponent_vector"]
    phase_clock = pcpl["phase_clock"]
    rank_mod = pcpl["rank_mod"]

    param_rng = None
    if str(prime_mode) == "generated":
        param_rng = random.Random(derive_seed(int(seed), f"PARAMS:{str(scenario_name)}"))

    params = build_params(
        int(x_value),
        int(token_bits),
        prime_mode=str(prime_mode),
        prime_bits=int(prime_bits),
        modulus_bits=int(modulus_bits),
        rng=param_rng,
    )

    compound_count = max(2, int(num_compounds))
    sample_window = max(8, min(128, int(window)))
    matrices: Dict[str, List[List[int]]] = {"A": [], "B": [], "C": []}

    for t in range(sample_window):
        phase = phase_clock(t, params)
        matrices["A"].append(exponent_vector(compound_count, phase.a, phase.u1, params))
        matrices["B"].append(exponent_vector(compound_count, phase.b, phase.u2, params))
        matrices["C"].append(exponent_vector(compound_count, phase.c, phase.u3, params))

    mod2_ratios: List[float] = []
    modp_ratios: List[float] = []
    unique_ratios: List[float] = []
    for rows in matrices.values():
        unique_rows = len({tuple(row) for row in rows})
        unique_ratios.append(unique_rows / float(max(1, sample_window)))
        mod2_ratios.append(rank_mod(rows, 2) / float(max(1, compound_count)))
        modp_ratios.append(rank_mod(rows, 65537) / float(max(1, compound_count)))

    return (
        clamp(sum(mod2_ratios) / float(max(1, len(mod2_ratios))), 0.0, 1.0),
        clamp(sum(modp_ratios) / float(max(1, len(modp_ratios))), 0.0, 1.0),
        clamp(sum(unique_ratios) / float(max(1, len(unique_ratios))), 0.0, 1.0),
    )


def default_scenarios(profile: str = "fast") -> List[ScenarioConfig]:
    """Default scenario sets. `fast` is CI/dev friendly; `full` is heavier."""
    if profile == "full":
        return [
            ScenarioConfig(name="x4-fixed", x=4, cycles=180, seed=1337, prime_mode="fixed"),
            ScenarioConfig(
                name="x6-generated",
                x=6,
                cycles=210,
                seed=2026,
                prime_mode="generated",
                prime_bits=20,
                modulus_bits=53,
                cycle_budget_ms=1.2,
            ),
            ScenarioConfig(
                name="x8-generated",
                x=8,
                cycles=240,
                seed=4242,
                prime_mode="generated",
                prime_bits=19,
                modulus_bits=53,
                cycle_budget_ms=1.4,
            ),
        ]

    return [
        ScenarioConfig(name="x4-fixed", x=4, cycles=48, seed=1337, prime_mode="fixed"),
        ScenarioConfig(
            name="x6-generated",
            x=6,
            cycles=60,
            seed=2026,
            prime_mode="generated",
            prime_bits=19,
            modulus_bits=47,
            cycle_budget_ms=1.1,
        ),
    ]


def ensure_genome_io(genome: GFSLGenome) -> None:
    """Seed input variable counts and ensure required outputs are tracked."""
    dtype_key = int(DataType.DECIMAL)
    genome.validator.variable_counts[dtype_key] = max(
        int(genome.validator.variable_counts[dtype_key]),
        INPUT_DECIMAL_COUNT,
        max(OUTPUT_INDICES) + 1,
    )

    existing_outputs = {
        (int(cat), int(dtype), int(idx)) for cat, dtype, idx in genome.outputs
    }
    for idx in OUTPUT_INDICES:
        marker = (1, int(DataType.DECIMAL), idx)  # Category.VARIABLE == 1
        if marker not in existing_outputs:
            genome.mark_output(DataType.DECIMAL, idx)

    injected = bool(getattr(genome, "_pcpl_scaffold_injected", False))
    if len(genome.extract_effective_algorithm()) == 0 or (not injected and not _has_pcpl_control_path(genome)):
        _inject_defender_output_path(genome)


def ensure_attacker_genome_io(genome: GFSLGenome) -> None:
    """Prepare attacker genome IO channels."""
    dtype_key = int(DataType.DECIMAL)
    genome.validator.variable_counts[dtype_key] = max(
        int(genome.validator.variable_counts[dtype_key]),
        ATTACK_INPUT_DECIMAL_COUNT,
        max(ATTACK_OUTPUT_INDICES) + 1,
    )
    existing_outputs = {
        (int(cat), int(dtype), int(idx)) for cat, dtype, idx in genome.outputs
    }
    for idx in ATTACK_OUTPUT_INDICES:
        marker = (1, int(DataType.DECIMAL), idx)
        if marker not in existing_outputs:
            genome.mark_output(DataType.DECIMAL, idx)

    if len(genome.extract_effective_algorithm()) == 0:
        _inject_attacker_output_path(genome)


def reference_pcpl_policy() -> PolicyDecision:
    """Deterministic reference policy aligned with current paper-like PCPL behavior."""
    return PolicyDecision(
        active_ratio=1.0,
        kernel=0,
        stride_seed=0,
        state_mix=0.5,
        exponent_mix=0.5,
        hash_rounds=1,
        bouquet_spread=0.5,
        state_churn=0.5,
        lane_salt=0,
        token_scramble=0.2689414213699951,  # sigmoid(-1)
        phase_jitter=0.2689414213699951,  # sigmoid(-1)
    )


def _append_reference_value_instruction(
    genome: GFSLGenome,
    *,
    target_idx: int,
    op_code: int,
    source1_value: float,
    source2_value: float,
) -> None:
    (
        GFSLExpressionBuilder(genome)
        .target_var(DataType.DECIMAL, target_idx)
        .op(op_code)
        .source1_value(float(source1_value))
        .source2_value(float(source2_value))
        .commit()
    )


def build_reference_defender_genome() -> GFSLGenome:
    """Create a deterministic paper-reference genome used as round-0 anchor."""
    genome = GFSLGenome("algorithm")
    dtype_key = int(DataType.DECIMAL)
    genome.validator.variable_counts[dtype_key] = max(
        int(genome.validator.variable_counts[dtype_key]),
        INPUT_DECIMAL_COUNT,
        max(OUTPUT_INDICES) + 1,
    )
    for idx in OUTPUT_INDICES:
        genome.mark_output(DataType.DECIMAL, idx)

    # Output constants chosen so _policy_from_outputs maps to paper-reference controls.
    reference_program = (
        (DEFENDER_OUTPUT_ACTIVE_IDX, int(Operation.ADD), 10.0, 10.0),   # ~1.0 after sigmoid
        (DEFENDER_OUTPUT_KERNEL_IDX, int(Operation.SUB), 0.0, 0.0),    # kernel selector -> 0
        (DEFENDER_OUTPUT_STATE_MIX_IDX, int(Operation.SUB), 0.0, 0.0),  # 0.5 after sigmoid
        (DEFENDER_OUTPUT_EXP_MIX_IDX, int(Operation.SUB), 0.0, 0.0),    # 0.5 after sigmoid
        (DEFENDER_OUTPUT_HASH_ROUNDS_IDX, int(Operation.SUB), 1.0, 1.0),  # hash_rounds -> 1
        (DEFENDER_OUTPUT_BOUQUET_SPREAD_IDX, int(Operation.SUB), 0.0, 0.0),  # 0.5 after sigmoid
        (DEFENDER_OUTPUT_STATE_CHURN_IDX, int(Operation.SUB), 0.0, 0.0),  # 0.5 after sigmoid
        (DEFENDER_OUTPUT_LANE_SALT_IDX, int(Operation.SUB), 0.0, 0.0),  # 0 lane salt
        (DEFENDER_OUTPUT_TOKEN_SCRAMBLE_IDX, int(Operation.SUB), 0.0, 1.0),  # sigmoid(-1)
        (DEFENDER_OUTPUT_PHASE_JITTER_IDX, int(Operation.SUB), 0.0, 1.0),  # sigmoid(-1)
    )
    for target_idx, op_code, src1, src2 in reference_program:
        _append_reference_value_instruction(
            genome,
            target_idx=int(target_idx),
            op_code=int(op_code),
            source1_value=float(src1),
            source2_value=float(src2),
        )

    genome.rebuild_validator_state()
    genome._pcpl_scaffold_injected = True  # type: ignore[attr-defined]
    ensure_genome_io(genome)
    return genome


def _has_pcpl_control_path(genome: GFSLGenome) -> bool:
    targeted = set()
    for instruction in genome.instructions:
        if int(instruction.target_cat) != int(Category.VARIABLE):
            continue
        if int(instruction.target_type) != int(DataType.DECIMAL):
            continue
        targeted.add(int(instruction.target_index))
    return len(targeted.intersection(set(OUTPUT_INDICES))) >= max(3, len(OUTPUT_INDICES) // 2)


def _inject_instruction(
    genome: GFSLGenome,
    *,
    target_idx: int,
    source_a_idx: int,
    source_b_idx: int,
    op_code: Optional[int] = None,
) -> bool:
    slot_count = int(max(7, genome.validator.slot_count))
    operation = int(Operation.ADD) if op_code is None else int(op_code)
    base_slots = [
        int(Category.VARIABLE),
        int(pack_type_index(DataType.DECIMAL, target_idx)),
        operation,
        int(Category.VARIABLE),
        int(pack_type_index(DataType.DECIMAL, source_a_idx)),
        int(Category.VARIABLE),
        int(pack_type_index(DataType.DECIMAL, source_b_idx)),
    ]
    if slot_count > len(base_slots):
        base_slots.extend([0] * (slot_count - len(base_slots)))
    instruction = GFSLInstruction(slots=base_slots, slot_count=slot_count)
    return genome.add_instruction(instruction)


def _inject_defender_output_path(genome: GFSLGenome) -> None:
    # Deterministic PCPL scaffold so genomes always expose protocol-relevant controls.
    scaffold = [
        (DEFENDER_OUTPUT_ACTIVE_IDX, 0, 1, int(Operation.ADD)),
        (DEFENDER_OUTPUT_KERNEL_IDX, 2, 3, int(Operation.MOD)),
        (DEFENDER_OUTPUT_STATE_MIX_IDX, 4, 5, int(Operation.MUL)),
        (DEFENDER_OUTPUT_EXP_MIX_IDX, 6, 7, PCPL_CUSTOM_OP_CODES.get("PCPL_HASHMIX", int(Operation.ADD))),
        (DEFENDER_OUTPUT_HASH_ROUNDS_IDX, 8, 9, PCPL_CUSTOM_OP_CODES.get("PCPL_PHASEMIX", int(Operation.ADD))),
        (DEFENDER_OUTPUT_BOUQUET_SPREAD_IDX, 10, 6, int(Operation.SUB)),
        (DEFENDER_OUTPUT_STATE_CHURN_IDX, 3, 11, PCPL_CUSTOM_OP_CODES.get("PCPL_MODHASH", int(Operation.MUL))),
        (DEFENDER_OUTPUT_LANE_SALT_IDX, 1, 8, int(Operation.ADD)),
        (DEFENDER_OUTPUT_TOKEN_SCRAMBLE_IDX, 2, 9, PCPL_CUSTOM_OP_CODES.get("PCPL_HASHMIX", int(Operation.SUB))),
        (DEFENDER_OUTPUT_PHASE_JITTER_IDX, 5, 10, int(Operation.MOD)),
    ]
    for target_idx, src_a, src_b, op_code in scaffold:
        _inject_instruction(
            genome,
            target_idx=target_idx,
            source_a_idx=src_a,
            source_b_idx=src_b,
            op_code=op_code,
        )
    genome.rebuild_validator_state()
    genome._pcpl_scaffold_injected = True  # type: ignore[attr-defined]


def _inject_attacker_output_path(genome: GFSLGenome) -> None:
    _inject_instruction(genome, target_idx=ATTACK_OUTPUT_INDICES[0], source_a_idx=6, source_b_idx=0)
    _inject_instruction(genome, target_idx=ATTACK_OUTPUT_INDICES[1], source_a_idx=7, source_b_idx=1)
    _inject_instruction(genome, target_idx=ATTACK_OUTPUT_INDICES[2], source_a_idx=8, source_b_idx=2)
    genome.rebuild_validator_state()


def estimate_operation_units(genome: Optional[GFSLGenome]) -> float:
    """Estimate controller cost from effective operations."""
    if genome is None:
        return 0.0
    ensure_genome_io(genome)
    effective = genome.extract_effective_algorithm()
    if not effective:
        return 0.0

    units = 0.0
    for idx in effective:
        op_code = int(genome.instructions[idx].operation)
        units += OPERATION_COST_UNITS.get(op_code, DEFAULT_OPERATION_COST)
    return units


def _policy_from_outputs(outputs: Dict[str, object]) -> PolicyDecision:
    raw_active = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_ACTIVE_IDX}", 0.0))
    raw_kernel = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_KERNEL_IDX}", 0.0))
    raw_mix = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_STATE_MIX_IDX}", 0.0))
    raw_exp_mix = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_EXP_MIX_IDX}", 0.0))
    raw_hash_rounds = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_HASH_ROUNDS_IDX}", 0.0))
    raw_spread = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_BOUQUET_SPREAD_IDX}", 0.0))
    raw_churn = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_STATE_CHURN_IDX}", 0.0))
    raw_salt = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_LANE_SALT_IDX}", 0.0))
    raw_scramble = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_TOKEN_SCRAMBLE_IDX}", 0.0))
    raw_phase = safe_float(outputs.get(f"d${DEFENDER_OUTPUT_PHASE_JITTER_IDX}", 0.0))

    selector = abs(int(raw_kernel * 1_000_003.0))
    lane_salt = abs(int(raw_salt * 1_000_033.0))
    hash_rounds = 1 + (abs(int(raw_hash_rounds * 1_000_007.0)) % 4)
    return PolicyDecision(
        active_ratio=sigmoid(raw_active),
        kernel=selector % 5,
        stride_seed=selector ^ lane_salt,
        state_mix=sigmoid(raw_mix),
        exponent_mix=sigmoid(raw_exp_mix),
        hash_rounds=hash_rounds,
        bouquet_spread=sigmoid(raw_spread),
        state_churn=sigmoid(raw_churn),
        lane_salt=lane_salt,
        token_scramble=sigmoid(raw_scramble),
        phase_jitter=sigmoid(raw_phase),
    )


def _choose_indices(total: int, active_count: int, stride_seed: int) -> List[int]:
    if total <= 0:
        return []
    if active_count >= total:
        return list(range(total))

    start = stride_seed % total
    step = 1
    if total > 1:
        step = 1 + (stride_seed % (total - 1))

    chosen: List[int] = []
    current = start
    seen = set()
    for _ in range(total * 3):
        if current not in seen:
            seen.add(current)
            chosen.append(current)
            if len(chosen) >= active_count:
                break
        current = (current + step) % total

    if len(chosen) < active_count:
        for idx in range(total):
            if idx not in seen:
                chosen.append(idx)
            if len(chosen) >= active_count:
                break
    return chosen


def _eval_selected_bouquet(
    bouquet: Sequence[int],
    xres: int,
    u: int,
    params: object,
    h_bytes: Callable[..., bytes],
    indices: Sequence[int],
    *,
    exponent_bias: int = 0,
    lane_salt: int = 0,
    phase_jitter: int = 0,
) -> int:
    acc = 1 % params.M
    for idx in indices:
        base = bouquet[idx] % params.M
        if base == 0:
            continue
        exponent_seed = int.from_bytes(
            h_bytes(xres, u, idx, lane_salt, phase_jitter, "EXP", out_len=32),
            "big",
        )
        exponent = (exponent_seed + exponent_bias + (phase_jitter * (idx + 1))) % (params.M - 1)
        exponent = max(1, exponent)
        acc = (acc * pow(base, exponent, params.M)) % params.M
    return acc


def _build_inputs(
    phase: object,
    params: object,
    lane_idx: int,
    t: int,
    x: int,
    last_token_hint: int,
) -> Dict[str, float]:
    lane_den = float(max(1, x - 1))
    token_hint = float(last_token_hint & ((1 << 24) - 1)) / float(1 << 24)
    phi_low = float(int.from_bytes(phase.phi[:4], "big") & ((1 << 24) - 1)) / float(1 << 24)
    phase_mix = float((phase.u1 ^ phase.u2 ^ phase.u3) & ((1 << 24) - 1)) / float(1 << 24)
    return {
        "d$0": float(phase.a) / float(params.P),
        "d$1": float(phase.b) / float(params.Q),
        "d$2": float(phase.c) / float(params.R),
        "d$3": float(phase.u1) / float(params.M),
        "d$4": float(phase.u2) / float(params.M),
        "d$5": float(phase.u3) / float(params.M),
        "d$6": float(lane_idx) / lane_den,
        "d$7": float(t % x) / lane_den,
        "d$8": float((t + lane_idx) % 97) / 97.0,
        "d$9": token_hint,
        "d$10": float((t // max(1, x)) % 29) / 29.0,
        "d$11": 1.0,
        "d$12": phi_low,
        "d$13": phase_mix,
        "d$14": float((phase.a + phase.c + lane_idx) % 97) / 97.0,
        "d$15": float((last_token_hint ^ int.from_bytes(phase.phi[-4:], "big")) & ((1 << 24) - 1)) / float(1 << 24),
        "d$16": float((lane_idx * (1 + (phase.b % 7))) % max(2, x + 1)) / float(max(2, x + 1)),
        "d$17": float((t + 1) % 113) / 113.0,
    }


def _build_attacker_inputs(
    phase: object,
    params: object,
    t: int,
    x: int,
    prev_token: int,
    prev_idx: int,
    absolute_phase: float,
) -> Dict[str, float]:
    x_norm = float(x) / 16.0
    lane_den = float(max(1, x - 1))
    token_hint = float(prev_token & ((1 << 24) - 1)) / float(1 << 24)
    phi_low = float(int.from_bytes(phase.phi[:4], "big") & ((1 << 24) - 1)) / float(1 << 24)
    return {
        "d$0": float(phase.a) / float(params.P),
        "d$1": float(phase.b) / float(params.Q),
        "d$2": float(phase.c) / float(params.R),
        "d$3": float(phase.u1) / float(params.M),
        "d$4": float(phase.u2) / float(params.M),
        "d$5": float(phase.u3) / float(params.M),
        "d$6": float(t % x) / lane_den,
        "d$7": token_hint,
        "d$8": float(prev_idx) / lane_den,
        "d$9": x_norm,
        "d$10": absolute_phase,
        "d$11": 1.0,
        "d$12": phi_low,
        "d$13": float((phase.u1 ^ phase.u2) & ((1 << 24) - 1)) / float(1 << 24),
        "d$14": float((phase.u3 + prev_idx + t) % 127) / 127.0,
        "d$15": float((prev_token ^ int.from_bytes(phase.phi[-4:], "big")) & ((1 << 24) - 1)) / float(1 << 24),
    }


def _lane_token(
    *,
    lane_idx: int,
    t: int,
    params: object,
    phase: object,
    secrets: object,
    decision: PolicyDecision,
    h_bytes: Callable[..., bytes],
    trunc_bits: Callable[..., int],
) -> LaneTokenResult:
    total = len(secrets.bouquetA)
    spread = 0.55 + (0.45 * decision.bouquet_spread)
    active_ratio = clamp(decision.active_ratio * spread, 0.0, 1.0)
    active_count = 1 + int(active_ratio * max(0, total - 1))
    active_count = max(1, min(total, active_count))

    phase_jitter = int(
        decision.phase_jitter
        * float((phase.u1 ^ phase.u2 ^ phase.u3) % 65_537)
    )
    seed_base = int(decision.stride_seed) + int(decision.lane_salt) + (lane_idx * 17)
    idx_a = _choose_indices(total, active_count, seed_base + phase_jitter)
    idx_b = _choose_indices(total, active_count, seed_base + 7 + (phase_jitter // 3))
    idx_c = _choose_indices(total, active_count, seed_base + 13 + (phase_jitter // 5))

    exponent_bias = int(decision.exponent_mix * 4096.0)

    ea = _eval_selected_bouquet(
        secrets.bouquetA,
        phase.a,
        phase.u1,
        params,
        h_bytes,
        idx_a,
        exponent_bias=exponent_bias,
        lane_salt=decision.lane_salt,
        phase_jitter=phase_jitter,
    )
    eb = _eval_selected_bouquet(
        secrets.bouquetB,
        phase.b,
        phase.u2,
        params,
        h_bytes,
        idx_b,
        exponent_bias=exponent_bias // 2,
        lane_salt=decision.lane_salt + 3,
        phase_jitter=phase_jitter // 2,
    )
    ec = _eval_selected_bouquet(
        secrets.bouquetC,
        phase.c,
        phase.u3,
        params,
        h_bytes,
        idx_c,
        exponent_bias=exponent_bias // 3,
        lane_salt=decision.lane_salt + 7,
        phase_jitter=phase_jitter // 3,
    )

    state_term = 1 + int(decision.state_mix * (params.M - 2))
    if decision.kernel == 0:
        mix = (ea + eb + ec + state_term) % params.M
    elif decision.kernel == 1:
        mix = (ea * ((eb + state_term) % params.M) + ec) % params.M
    elif decision.kernel == 2:
        mix = (pow((ea + state_term) % params.M, 2, params.M) + (eb * ec)) % params.M
    elif decision.kernel == 3:
        mix = int.from_bytes(
            h_bytes(
                lane_idx,
                t,
                ea,
                eb,
                ec,
                state_term,
                decision.lane_salt,
                phase.phi,
                "KERNEL3",
                out_len=32,
            ),
            "big",
        ) % params.M
    else:
        mix = ((ea ^ eb) + (ec * (1 + (decision.lane_salt % 1021))) + state_term) % params.M

    hash_rounds = max(1, int(decision.hash_rounds))
    dynamic_mix = mix
    for round_idx in range(hash_rounds):
        dynamic_mix = int.from_bytes(
            h_bytes(
                dynamic_mix,
                lane_idx,
                t,
                phase.phi,
                decision.lane_salt,
                round_idx,
                "PCPL-MIX",
                out_len=32,
            ),
            "big",
        ) % params.M
    mix = (mix + dynamic_mix + state_term) % params.M

    kdf = h_bytes(
        lane_idx,
        ea,
        eb,
        ec,
        mix,
        phase.phi,
        decision.lane_salt,
        "KDF",
        out_len=32,
    )
    for round_idx in range(max(0, hash_rounds - 1)):
        kdf = h_bytes(kdf, mix, round_idx, phase.phi, "KDFR", out_len=32)
    tok_hash = h_bytes(
        kdf,
        t,
        phase.phi,
        decision.lane_salt,
        int(decision.token_scramble * 1_000_000.0),
        "TOK",
        out_len=max(32, params.token_bytes),
    )
    for round_idx in range(max(0, hash_rounds - 1)):
        tok_hash = h_bytes(tok_hash, dynamic_mix, round_idx, "TOKR", out_len=max(32, params.token_bytes))
    token = trunc_bits(tok_hash, params.token_bits)
    if decision.token_scramble > 0.02:
        scramble = trunc_bits(
            h_bytes(
                dynamic_mix,
                phase.phi,
                lane_idx,
                t,
                decision.lane_salt,
                "SCRAMBLE",
                out_len=max(32, params.token_bytes),
            ),
            params.token_bits,
        )
        token = (token ^ scramble) & ((1 << params.token_bits) - 1)

    pow_ops = len(idx_a) + len(idx_b) + len(idx_c)
    hash_ops = (2 * hash_rounds) + 4
    return LaneTokenResult(
        token=token,
        active_count=active_count,
        pow_ops=pow_ops,
        hash_ops=hash_ops,
    )


def _update_device_state(
    state: object,
    idx: int,
    token: int,
    params: object,
    phase: object,
    h_bytes: Callable[..., bytes],
    int_to_bytes_fixed: Callable[..., bytes],
    *,
    state_churn: float = 0.5,
    lane_salt: int = 0,
) -> None:
    state.W[idx] = token
    churn_int = 1 + int(clamp(state_churn, 0.0, 1.0) * 7.0)
    chain_products = [
        ((state.W[i] * state.W[i + 1]) + (churn_int * (i + 1))) % params.M
        for i in range(params.x - 1)
    ]
    state.S = h_bytes(
        state.S,
        *[int_to_bytes_fixed(w, params.token_bytes) for w in state.W],
        *[int_to_bytes_fixed(m, params.mod_bytes) for m in chain_products],
        churn_int,
        lane_salt,
        phase.phi,
        "EVOLVE",
        out_len=params.seed_bytes,
    )


def evaluate_scenario(
    scenario: ScenarioConfig,
    genome: Optional[GFSLGenome],
    *,
    fixed_decision: Optional[PolicyDecision] = None,
    attacker: Optional[GFSLGenome] = None,
    executor_kwargs: Optional[Dict[str, object]] = None,
    timeout_deadline: Optional[float] = None,
) -> ScenarioMetrics:
    """Evaluate one scenario for a defender and optional attacker genome."""
    if genome is None and fixed_decision is None:
        raise ValueError("Either a defender genome or fixed_decision must be provided")

    pcpl = load_reference_pcpl()
    build_params = pcpl["build_params"]
    build_compound_config = pcpl["build_compound_config"]
    build_fixture = pcpl["build_fixture"]
    phase_clock = pcpl["phase_clock"]
    permutation_for_block = pcpl["permutation_for_block"]
    h_bytes = pcpl["h_bytes"]
    trunc_bits = pcpl["trunc_bits"]
    derive_seed = pcpl["derive_seed"]
    int_to_bytes_fixed = pcpl["int_to_bytes_fixed"]

    param_rng = None
    if scenario.prime_mode == "generated":
        param_rng = random.Random(derive_seed(scenario.seed, f"PARAMS:{scenario.name}"))

    params = build_params(
        scenario.x,
        scenario.token_bits,
        prime_mode=scenario.prime_mode,
        prime_bits=scenario.prime_bits,
        modulus_bits=scenario.modulus_bits,
        rng=param_rng,
    )
    compound_cfg = build_compound_config(
        scenario.seed,
        params,
        scenario.compound_count,
        scenario.compound_primes,
        scenario.compound_mode,
        scenario.compound_offset,
        scenario.compound_prime_bits,
        scenario.compound_pool_size,
        pool_label=f"COMPOUND_POOL:{scenario.name}",
    )
    secrets, state = build_fixture(params, scenario.seed, compound_cfg)
    twin_state = copy.deepcopy(state)
    shared_secrets, _shared_state = build_fixture(params, scenario.seed ^ 0x9E3779B9, compound_cfg)

    runtime_kwargs = dict(executor_kwargs or {})
    runtime_kwargs.pop("track_instruction_activity", None)
    defender_executor = _cached_scenario_executor(
        role="defender",
        runtime_kwargs=runtime_kwargs,
    )
    attacker_executor = _cached_scenario_executor(
        role="attacker",
        runtime_kwargs=runtime_kwargs,
    )

    defender_units = 0.0
    if genome is not None:
        ensure_genome_io(genome)
        defender_units = estimate_operation_units(genome)
    if attacker is not None:
        ensure_attacker_genome_io(attacker)

    full_blocks = max(1, scenario.cycles // scenario.x)
    block_counts = [[0 for _ in range(params.x)] for _ in range(full_blocks)]
    perm_ok = 0

    one_of_x_hits = 0
    attack_miss = 0
    attack_checks = 0
    replay_hits = 0
    replay_checks = 0
    cross_lane_collisions = 0
    collision_checks = 0
    controller_fails = 0
    shared_device_matches = 0

    device_compound_total = 0
    provider_compound_total = 0
    native_counters = _empty_native_counter()

    lane_attack_hits = 0
    token_attack_hits = 0

    emitted_tokens: List[int] = []
    provider_matrix: List[List[int]] = []
    replay_window: List[List[int]] = [[] for _ in range(params.x)]

    pow_cost_ms = 0.016 * (float(params.M.bit_length()) / 61.0)
    hash_cost_ms = 0.007
    controller_base_ms = 0.03
    controller_ms = controller_base_ms + defender_units * 0.018
    reference_ms = 0.0
    next_resync_checkpoint = float(scenario.absolute_time_ms) if scenario.absolute_time_ms > 0 else float("inf")
    device_time_ms = 0.0
    provider_time_ms = 0.0
    sync_lost = False
    sync_lost_cycles = 0
    resync_attempts = 0
    resync_successes = 0

    device_cycle_acc_ms = 0.0
    provider_cycle_acc_ms = 0.0
    device_freq_scale = 100.0 / float(max(1e-6, scenario.device_mhz))
    provider_freq_scale = 100.0 / float(max(1e-6, scenario.provider_mhz))

    prev_emitted = 0
    prev_idx = 0

    deadline = None
    if timeout_deadline is not None:
        try:
            candidate = float(timeout_deadline)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and math.isfinite(candidate):
            deadline = candidate

    start = time.perf_counter()
    for t in range(scenario.cycles):
        if deadline is not None and (t & 7) == 0 and time.perf_counter() >= deadline:
            raise TimeoutError("evaluation-timeout")
        phase = phase_clock(t, params)
        block = t // params.x
        slot = t % params.x

        phase_block = phase_clock(block * params.x, params)
        perm = permutation_for_block(block, params, state.perm_key, phase_block.phi)
        if sorted(perm) == list(range(params.x)):
            perm_ok += 1
        idx = perm[slot]

        lane_tokens: List[int] = []
        lane_actives: List[int] = []
        lane_hash_ops: List[int] = []
        lane_decisions: List[PolicyDecision] = []

        for lane in range(params.x):
            if deadline is not None and (lane & 3) == 0 and time.perf_counter() >= deadline:
                raise TimeoutError("evaluation-timeout")
            if fixed_decision is not None:
                decision = fixed_decision
            else:
                inputs = _build_inputs(
                    phase,
                    params,
                    lane,
                    t,
                    params.x,
                    emitted_tokens[-1] if emitted_tokens else 0,
                )
                try:
                    outputs = defender_executor.execute(genome, inputs, track_activity=False)
                    _accumulate_executor_native_stats(native_counters, defender_executor)
                    decision = _policy_from_outputs(outputs)
                except Exception:
                    _accumulate_executor_native_stats(native_counters, defender_executor)
                    controller_fails += 1
                    decision = PolicyDecision(
                        active_ratio=1.0,
                        kernel=0,
                        stride_seed=0,
                        state_mix=0.5,
                        exponent_mix=0.5,
                        hash_rounds=1,
                        bouquet_spread=0.5,
                        state_churn=0.5,
                        lane_salt=0,
                        token_scramble=0.0,
                        phase_jitter=0.0,
                    )

            token_info = _lane_token(
                lane_idx=lane,
                t=t,
                params=params,
                phase=phase,
                secrets=secrets[lane],
                decision=decision,
                h_bytes=h_bytes,
                trunc_bits=trunc_bits,
            )
            lane_tokens.append(token_info.token)
            lane_actives.append(token_info.active_count)
            lane_hash_ops.append(token_info.hash_ops)
            lane_decisions.append(decision)

        emitted = lane_tokens[idx]
        emitted_tokens.append(emitted)
        provider_matrix.append(lane_tokens)

        # One-of-x correctness and adversarial cross-lane attempts.
        matches = [lane for lane, token in enumerate(lane_tokens) if token == emitted]
        if matches == [idx]:
            one_of_x_hits += 1
        attack_checks += params.x - 1
        attack_successes = len([lane for lane in matches if lane != idx])
        attack_miss += (params.x - 1) - attack_successes

        # Block fairness accounting.
        if block < full_blocks:
            block_counts[block][idx] += 1

        # Collision and replay checks.
        unique_count = len(set(lane_tokens))
        cross_lane_collisions += params.x - unique_count
        collision_checks += max(1, params.x - 1)

        for lane, token in enumerate(lane_tokens):
            if token in replay_window[lane]:
                replay_hits += 1
            replay_checks += 1
            replay_window[lane].append(token)
            if len(replay_window[lane]) > 24:
                replay_window[lane].pop(0)

        # Shared-device impersonation benchmark (same algorithm, different seed lineage).
        shared_info = _lane_token(
            lane_idx=idx,
            t=t,
            params=params,
            phase=phase,
            secrets=shared_secrets[idx],
            decision=lane_decisions[idx],
            h_bytes=h_bytes,
            trunc_bits=trunc_bits,
        )
        if shared_info.token == emitted:
            shared_device_matches += 1

        device_compound_total += lane_actives[idx]
        provider_compound_total += sum(lane_actives)

        # Timing / sync drift model with absolute time reference.
        lane_pow_selected = 3 * lane_actives[idx]
        provider_pow_max = 3 * max(lane_actives) if lane_actives else 0
        lane_hash_selected = lane_hash_ops[idx] if lane_hash_ops else 0
        provider_hash_max = max(lane_hash_ops) if lane_hash_ops else 0

        device_cycle_ms = controller_ms + (lane_pow_selected * pow_cost_ms) + (lane_hash_selected * hash_cost_ms)
        provider_cycle_ms = controller_ms + (provider_pow_max * pow_cost_ms) + (provider_hash_max * hash_cost_ms)
        device_cycle_ms *= device_freq_scale
        provider_cycle_ms *= provider_freq_scale
        device_cycle_ms += 0.004
        provider_cycle_ms += 0.004

        device_cycle_acc_ms += device_cycle_ms
        provider_cycle_acc_ms += provider_cycle_ms

        reference_ms += float(scenario.cycle_budget_ms)
        device_time_ms += device_cycle_ms
        provider_time_ms += provider_cycle_ms

        drift = max(
            abs(device_time_ms - reference_ms),
            abs(provider_time_ms - reference_ms),
            abs(device_time_ms - provider_time_ms),
        )
        if drift > float(scenario.sync_tolerance_ms):
            sync_lost = True
        if sync_lost:
            sync_lost_cycles += 1

        if reference_ms >= next_resync_checkpoint:
            resync_attempts += 1
            if drift <= float(scenario.resync_window_ms):
                resync_successes += 1
                sync_lost = False
                device_time_ms = reference_ms
                provider_time_ms = reference_ms
            next_resync_checkpoint += float(scenario.absolute_time_ms)

        # Keep deterministic evolving state for synchronization checks.
        _update_device_state(
            state,
            idx,
            emitted,
            params,
            phase,
            h_bytes,
            int_to_bytes_fixed,
            state_churn=lane_decisions[idx].state_churn,
            lane_salt=lane_decisions[idx].lane_salt,
        )

        idx_twin = perm[slot]
        token_twin = lane_tokens[idx_twin]
        _update_device_state(
            twin_state,
            idx_twin,
            token_twin,
            params,
            phase,
            h_bytes,
            int_to_bytes_fixed,
            state_churn=lane_decisions[idx_twin].state_churn,
            lane_salt=lane_decisions[idx_twin].lane_salt,
        )

        # Optional evolved attacker benchmark.
        if attacker is not None:
            if deadline is not None and time.perf_counter() >= deadline:
                raise TimeoutError("evaluation-timeout")
            absolute_phase = 0.0
            if scenario.absolute_time_ms > 0:
                absolute_phase = (reference_ms % scenario.absolute_time_ms) / scenario.absolute_time_ms
            attack_inputs = _build_attacker_inputs(
                phase,
                params,
                t,
                params.x,
                prev_emitted,
                prev_idx,
                absolute_phase,
            )
            try:
                attack_outputs = attacker_executor.execute(attacker, attack_inputs, track_activity=False)
                _accumulate_executor_native_stats(native_counters, attacker_executor)
                lane_raw = safe_float(attack_outputs.get(f"d${ATTACK_OUTPUT_INDICES[0]}", 0.0))
                token_raw = safe_float(attack_outputs.get(f"d${ATTACK_OUTPUT_INDICES[1]}", 0.0))
                lane_guess = abs(int(lane_raw * 1_000_000.0)) % params.x
                mask = (1 << scenario.attack_token_bits) - 1
                token_guess = abs(int(token_raw * 1_000_000.0)) & mask
                token_low = emitted & mask
                if lane_guess == idx:
                    lane_attack_hits += 1
                if token_guess == token_low:
                    token_attack_hits += 1
            except Exception:
                _accumulate_executor_native_stats(native_counters, attacker_executor)
                # Broken attacker counts as no hit.
                pass

        prev_emitted = emitted
        prev_idx = idx

    elapsed = time.perf_counter() - start

    block_ok = 0
    for counts in block_counts:
        if all(count == 1 for count in counts):
            block_ok += 1

    timing_reject_hits = 0
    timing_checks = max(0, scenario.cycles - 1)
    for t in range(scenario.cycles - 1):
        idx_t = t % params.x
        perm_block = t // params.x
        phase_block = phase_clock(perm_block * params.x, params)
        perm = permutation_for_block(perm_block, params, state.perm_key, phase_block.phi)
        routed = perm[idx_t]
        if provider_matrix[t + 1][routed] != emitted_tokens[t]:
            timing_reject_hits += 1

    one_of_x_rate = one_of_x_hits / float(max(1, scenario.cycles))
    block_once_rate = block_ok / float(max(1, full_blocks))
    permutation_valid_rate = perm_ok / float(max(1, scenario.cycles))
    attack_reject_rate = attack_miss / float(max(1, attack_checks))

    twin_sync_rate = 1.0 if state.S == twin_state.S and state.W == twin_state.W else 0.0
    timing_reject_rate = timing_reject_hits / float(max(1, timing_checks)) if timing_checks else 1.0

    sync_loss_rate = sync_lost_cycles / float(max(1, scenario.cycles))
    resync_success_rate = resync_successes / float(max(1, resync_attempts)) if resync_attempts else 1.0

    horizon_cycles = int(
        max(
            1.0,
            (float(max(1e-6, scenario.max_test_time_seconds)) * 1000.0)
            / float(max(1e-9, scenario.cycle_budget_ms)),
        )
    )
    projection_ratio = horizon_cycles / float(max(1, scenario.cycles))
    effective_sync_loss = sync_loss_rate * (1.0 - (0.70 * resync_success_rate))
    projection_multiplier = min(8.0, max(1.0, projection_ratio ** 0.35))
    projected_sync_loss_rate = clamp(effective_sync_loss * projection_multiplier, 0.0, 1.0)
    horizon_sync_score = 1.0 - projected_sync_loss_rate

    cross_lane_collision_rate = cross_lane_collisions / float(max(1, collision_checks))
    replay_rate = replay_hits / float(max(1, replay_checks))
    controller_fail_rate = controller_fails / float(max(1, scenario.cycles * params.x))
    shared_device_match_rate = shared_device_matches / float(max(1, scenario.cycles))

    baseline_lane = 1.0 / float(params.x)
    baseline_token = 1.0 / float(1 << scenario.attack_token_bits)
    if attacker is None:
        attacker_lane_success_rate = baseline_lane
        attacker_token_success_rate = baseline_token
    else:
        attacker_lane_success_rate = lane_attack_hits / float(max(1, scenario.cycles))
        attacker_token_success_rate = token_attack_hits / float(max(1, scenario.cycles))

    lane_advantage = clamp(
        (attacker_lane_success_rate - baseline_lane) / float(max(1e-9, 1.0 - baseline_lane)),
        0.0,
        1.0,
    )
    token_advantage = clamp(
        (attacker_token_success_rate - baseline_token) / float(max(1e-9, 1.0 - baseline_token)),
        0.0,
        1.0,
    )
    attacker_advantage_score = 0.60 * lane_advantage + 0.40 * token_advantage

    brute_force_resistance_score = 1.0 - token_advantage
    reverse_hack_resistance_score = 1.0 - lane_advantage

    baseline_device = max(1, scenario.cycles * scenario.compound_count)
    baseline_provider = max(1, scenario.cycles * params.x * scenario.compound_count)
    device_compound_ratio = device_compound_total / float(baseline_device)
    provider_compound_ratio = provider_compound_total / float(baseline_provider)

    avg_device_cycle_ms = device_cycle_acc_ms / float(max(1, scenario.cycles))
    avg_provider_cycle_ms = provider_cycle_acc_ms / float(max(1, scenario.cycles))

    device_budget_ratio = avg_device_cycle_ms / float(max(1e-9, scenario.cycle_budget_ms))
    provider_budget_ratio = avg_provider_cycle_ms / float(max(1e-9, scenario.cycle_budget_ms))
    operation_cost_score = 0.50 * _budget_score(device_budget_ratio) + 0.50 * _budget_score(provider_budget_ratio)

    principle_score = (
        0.34 * one_of_x_rate
        + 0.26 * block_once_rate
        + 0.20 * permutation_valid_rate
        + 0.20 * attack_reject_rate
    )

    phase_error_level = clamp(
        (0.55 * sync_loss_rate) + (0.45 * projected_sync_loss_rate),
        0.0,
        1.0,
    )
    phase_recovery_score = clamp(
        (0.58 * resync_success_rate) + (0.42 * twin_sync_rate),
        0.0,
        1.0,
    )
    phase_oscillation = clamp(
        controller_fail_rate + abs(projected_sync_loss_rate - sync_loss_rate),
        0.0,
        1.0,
    )
    phase_error_control_score = clamp(
        (0.46 * (1.0 - phase_error_level))
        + (0.34 * phase_recovery_score)
        + (0.20 * (1.0 - phase_oscillation)),
        0.0,
        1.0,
    )

    sync_score = (
        0.16 * twin_sync_rate
        + 0.08 * timing_reject_rate
        + 0.16 * (1.0 - sync_loss_rate)
        + 0.16 * resync_success_rate
        + 0.26 * horizon_sync_score
        + 0.18 * phase_error_control_score
    )
    security_score = (
        0.22 * (1.0 - clamp(cross_lane_collision_rate, 0.0, 1.0))
        + 0.18 * (1.0 - clamp(replay_rate, 0.0, 1.0))
        + 0.20 * (1.0 - clamp(shared_device_match_rate, 0.0, 1.0))
        + 0.20 * brute_force_resistance_score
        + 0.20 * reverse_hack_resistance_score
    )
    cost_score = (
        0.45 * operation_cost_score
        + 0.275 * (1.0 - clamp(device_compound_ratio, 0.0, 1.0))
        + 0.275 * (1.0 - clamp(provider_compound_ratio, 0.0, 1.0))
    )

    ns_per_cycle = (elapsed * 1e9) / float(max(1, scenario.cycles))
    budget_ns = float(max(1, scenario.timing_budget_ns))
    runtime_score = _budget_score(ns_per_cycle / budget_ns)

    stability_score = 1.0 - clamp(
        controller_fail_rate + (0.20 * sync_loss_rate) + (0.30 * projected_sync_loss_rate),
        0.0,
        1.0,
    )

    effective_indices: List[int] = []
    if genome is not None:
        try:
            effective_indices = list(genome.extract_effective_algorithm())
        except Exception:
            effective_indices = []
    effective_size = len(effective_indices)
    compare_ops = {
        int(Operation.GT),
        int(Operation.LT),
        int(Operation.EQ),
        int(Operation.GTE),
        int(Operation.LTE),
        int(Operation.NEQ),
    }
    branch_ops = {int(Operation.IF), int(Operation.WHILE)}
    compare_count = 0
    branch_count = 0
    for idx in effective_indices:
        if genome is None or idx >= len(genome.instructions):
            continue
        op_code = int(genome.instructions[idx].operation)
        if op_code in compare_ops:
            compare_count += 1
        if op_code in branch_ops:
            branch_count += 1
    control_density = (
        (float(branch_count) + (0.60 * float(compare_count))) / float(max(1, effective_size))
        if effective_size > 0
        else 0.0
    )
    target_density = 0.18
    control_density_score = 1.0 - min(
        1.0,
        abs(control_density - target_density) / float(max(0.02, target_density)),
    )
    control_flow_score = clamp(
        (0.65 * control_density_score) + (0.35 * phase_error_control_score),
        0.0,
        1.0,
    )

    qft_period_bits, compare_x_period_ratio, compare_x_chain_ratio = _compare_x_profile(
        params.x,
        scenario.token_bits,
        scenario.prime_mode,
        scenario.prime_bits,
        scenario.modulus_bits,
        scenario.seed,
        scenario.name,
    )
    qft_target_bits = float(max(32, min(128, int(scenario.token_bits))))
    qft_period_ratio = clamp(float(qft_period_bits) / qft_target_bits, 0.0, 1.0)
    qft_score = clamp((0.58 * qft_period_ratio) + (0.42 * horizon_sync_score), 0.0, 1.0)

    avg_active_compounds = device_compound_total / float(max(1, scenario.cycles))
    rank_compounds = max(2, min(int(scenario.compound_count), int(round(avg_active_compounds))))
    linear_rank_mod2_ratio, linear_rank_mod65537_ratio, linear_rank_unique_ratio = _linear_rank_profile(
        params.x,
        scenario.token_bits,
        scenario.prime_mode,
        scenario.prime_bits,
        scenario.modulus_bits,
        scenario.seed,
        scenario.name,
        rank_compounds,
        min(64, scenario.cycles),
    )
    linear_rank_core = (
        0.44 * linear_rank_mod2_ratio
        + 0.44 * linear_rank_mod65537_ratio
        + 0.12 * linear_rank_unique_ratio
    )
    linear_rank_score = clamp(
        (0.80 * linear_rank_core) + (0.20 * (1.0 - controller_fail_rate)),
        0.0,
        1.0,
    )

    compare_x_score = clamp(
        0.40 * compare_x_period_ratio
        + 0.20 * compare_x_chain_ratio
        + 0.25 * one_of_x_rate
        + 0.15 * block_once_rate,
        0.0,
        1.0,
    )
    native_dispatch_total = int(native_counters["gpu_dispatch_count"]) + int(
        native_counters["cpu_fallback_count"]
    )
    native_gpu_share = (
        float(native_counters["gpu_dispatch_count"]) / float(native_dispatch_total)
        if native_dispatch_total > 0
        else 0.0
    )

    total_score = (
        0.17 * principle_score
        + 0.27 * sync_score
        + 0.18 * security_score
        + 0.08 * cost_score
        + 0.02 * runtime_score
        + 0.12 * stability_score
        + 0.04 * qft_score
        + 0.03 * linear_rank_score
        + 0.03 * compare_x_score
        + 0.04 * phase_error_control_score
        + 0.02 * control_flow_score
    )

    if one_of_x_rate < 1.0:
        total_score -= 0.60 * (1.0 - one_of_x_rate)
    if block_once_rate < 1.0:
        total_score -= 0.35 * (1.0 - block_once_rate)

    if projected_sync_loss_rate > 0.72:
        total_score -= min(0.25, 0.45 * (projected_sync_loss_rate - 0.72))
    if horizon_sync_score < 0.30:
        total_score -= min(0.18, 0.40 * (0.30 - horizon_sync_score))

    # Penalize degenerate defender circuits.
    if genome is not None and effective_size == 0:
        total_score -= 0.40
    elif genome is not None and effective_size < 3:
        total_score -= (3 - effective_size) * 0.08
    if effective_size > 0:
        total_score -= min(0.08, max(0.0, (effective_size - 16) * 0.0020))

    # Penalize if attacker has significant advantage.
    total_score -= 0.20 * attacker_advantage_score

    return ScenarioMetrics(
        scenario=scenario.name,
        total_score=total_score,
        principle_score=principle_score,
        sync_score=sync_score,
        security_score=security_score,
        cost_score=cost_score,
        runtime_score=runtime_score,
        stability_score=stability_score,
        operation_cost_score=operation_cost_score,
        brute_force_resistance_score=brute_force_resistance_score,
        reverse_hack_resistance_score=reverse_hack_resistance_score,
        one_of_x_rate=one_of_x_rate,
        block_once_rate=block_once_rate,
        permutation_valid_rate=permutation_valid_rate,
        attack_reject_rate=attack_reject_rate,
        twin_sync_rate=twin_sync_rate,
        timing_reject_rate=timing_reject_rate,
        sync_loss_rate=sync_loss_rate,
        resync_success_rate=resync_success_rate,
        cross_lane_collision_rate=cross_lane_collision_rate,
        replay_rate=replay_rate,
        controller_fail_rate=controller_fail_rate,
        shared_device_match_rate=shared_device_match_rate,
        attacker_lane_success_rate=attacker_lane_success_rate,
        attacker_token_success_rate=attacker_token_success_rate,
        attacker_advantage_score=attacker_advantage_score,
        avg_device_cycle_ms=avg_device_cycle_ms,
        avg_provider_cycle_ms=avg_provider_cycle_ms,
        absolute_time_reference_ms=float(scenario.absolute_time_ms),
        projected_sync_loss_rate=projected_sync_loss_rate,
        horizon_sync_score=horizon_sync_score,
        device_compound_ratio=device_compound_ratio,
        provider_compound_ratio=provider_compound_ratio,
        elapsed_seconds=elapsed,
        phase_error_control_score=phase_error_control_score,
        control_flow_score=control_flow_score,
        qft_score=qft_score,
        qft_period_bits=float(qft_period_bits),
        qft_period_ratio=qft_period_ratio,
        linear_rank_score=linear_rank_score,
        linear_rank_mod2_ratio=linear_rank_mod2_ratio,
        linear_rank_mod65537_ratio=linear_rank_mod65537_ratio,
        linear_rank_unique_ratio=linear_rank_unique_ratio,
        compare_x_score=compare_x_score,
        compare_x_period_ratio=compare_x_period_ratio,
        compare_x_chain_ratio=compare_x_chain_ratio,
        native_execution_calls=float(native_counters["execution_calls"]),
        native_kompute_calls=float(native_counters["kompute_calls"]),
        native_gpu_dispatch_count=float(native_counters["gpu_dispatch_count"]),
        native_cpu_fallback_count=float(native_counters["cpu_fallback_count"]),
        native_cpu_full_sync_count=float(native_counters["cpu_full_sync_count"]),
        native_cpu_partial_sync_count=float(native_counters["cpu_partial_sync_count"]),
        native_cpu_no_sync_count=float(native_counters["cpu_no_sync_count"]),
        native_cpu_synced_tensors=float(native_counters["cpu_synced_tensors"]),
        native_final_sync_count=float(native_counters["final_sync_count"]),
        native_gpu_share=float(native_gpu_share),
    )


def evaluate_across_scenarios(
    scenarios: Sequence[ScenarioConfig],
    genome: Optional[GFSLGenome],
    *,
    fixed_decision: Optional[PolicyDecision] = None,
    attacker: Optional[GFSLGenome] = None,
    executor_kwargs: Optional[Dict[str, object]] = None,
    timeout_deadline: Optional[float] = None,
) -> Tuple[float, List[ScenarioMetrics]]:
    deadline = None
    if timeout_deadline is not None:
        try:
            candidate = float(timeout_deadline)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and math.isfinite(candidate):
            deadline = candidate

    metrics: List[ScenarioMetrics] = []
    for scenario in scenarios:
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("evaluation-timeout")
        metrics.append(
            evaluate_scenario(
                scenario,
                genome,
                fixed_decision=fixed_decision,
                attacker=attacker,
                executor_kwargs=executor_kwargs,
                timeout_deadline=deadline,
            )
        )

    total = sum(item.total_score for item in metrics) / float(max(1, len(metrics)))
    return total, metrics
