"""PCPL simulation environment, scoring and adversarial benchmarks."""

from __future__ import annotations

import copy
import math
import random
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable, load_reference_pcpl

ensure_evolvo_importable()

from evolvo import (
    Category,
    DataType,
    GFSLExecutor,
    GFSLGenome,
    GFSLInstruction,
    Operation,
    pack_type_index,
)


OUTPUT_INDICES = (20, 21, 22)
ATTACK_OUTPUT_INDICES = (40, 41, 42)
INPUT_DECIMAL_COUNT = 12
ATTACK_INPUT_DECIMAL_COUNT = 12


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


@dataclass(frozen=True)
class PolicyDecision:
    """Control outputs produced by an evolved GFSL circuit."""

    active_ratio: float
    kernel: int
    stride_seed: int
    state_mix: float


@dataclass(frozen=True)
class LaneTokenResult:
    token: int
    active_count: int
    pow_ops: int


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
    device_compound_ratio: float
    provider_compound_ratio: float
    elapsed_seconds: float

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

    if len(genome.extract_effective_algorithm()) == 0:
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


def _inject_instruction(
    genome: GFSLGenome,
    *,
    target_idx: int,
    source_a_idx: int,
    source_b_idx: int,
) -> bool:
    slot_count = int(max(7, genome.validator.slot_count))
    base_slots = [
        int(Category.VARIABLE),
        int(pack_type_index(DataType.DECIMAL, target_idx)),
        int(Operation.ADD),
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
    # Deterministic fallback to avoid degenerate no-op circuits.
    _inject_instruction(genome, target_idx=OUTPUT_INDICES[0], source_a_idx=0, source_b_idx=1)
    _inject_instruction(genome, target_idx=OUTPUT_INDICES[1], source_a_idx=2, source_b_idx=3)
    _inject_instruction(genome, target_idx=OUTPUT_INDICES[2], source_a_idx=4, source_b_idx=5)
    genome.rebuild_validator_state()


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
    raw_active = safe_float(outputs.get(f"d${OUTPUT_INDICES[0]}", 0.0))
    raw_kernel = safe_float(outputs.get(f"d${OUTPUT_INDICES[1]}", 0.0))
    raw_mix = safe_float(outputs.get(f"d${OUTPUT_INDICES[2]}", 0.0))

    selector = abs(int(raw_kernel * 1_000_000.0))
    return PolicyDecision(
        active_ratio=sigmoid(raw_active),
        kernel=selector % 3,
        stride_seed=selector,
        state_mix=sigmoid(raw_mix),
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
) -> int:
    acc = 1 % params.M
    for idx in indices:
        base = bouquet[idx] % params.M
        if base == 0:
            continue
        exponent = int.from_bytes(h_bytes(xres, u, idx, "EXP", out_len=32), "big") % (params.M - 1)
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
    active_count = 1 + int(decision.active_ratio * max(0, total - 1))
    active_count = max(1, min(total, active_count))

    idx_a = _choose_indices(total, active_count, decision.stride_seed)
    idx_b = _choose_indices(total, active_count, decision.stride_seed + 7)
    idx_c = _choose_indices(total, active_count, decision.stride_seed + 13)

    ea = _eval_selected_bouquet(secrets.bouquetA, phase.a, phase.u1, params, h_bytes, idx_a)
    eb = _eval_selected_bouquet(secrets.bouquetB, phase.b, phase.u2, params, h_bytes, idx_b)
    ec = _eval_selected_bouquet(secrets.bouquetC, phase.c, phase.u3, params, h_bytes, idx_c)

    state_term = 1 + int(decision.state_mix * (params.M - 2))
    if decision.kernel == 0:
        mix = (ea + eb + ec + state_term) % params.M
    elif decision.kernel == 1:
        mix = (ea * ((eb + state_term) % params.M) + ec) % params.M
    else:
        mix = (pow((ea + state_term) % params.M, 2, params.M) + (eb * ec)) % params.M

    kdf = h_bytes(lane_idx, ea, eb, ec, mix, phase.phi, "KDF", out_len=32)
    tok_hash = h_bytes(kdf, t, phase.phi, "TOK", out_len=max(32, params.token_bytes))
    token = trunc_bits(tok_hash, params.token_bits)

    pow_ops = len(idx_a) + len(idx_b) + len(idx_c)
    return LaneTokenResult(token=token, active_count=active_count, pow_ops=pow_ops)


def _update_device_state(
    state: object,
    idx: int,
    token: int,
    params: object,
    phase: object,
    h_bytes: Callable[..., bytes],
    int_to_bytes_fixed: Callable[..., bytes],
) -> None:
    state.W[idx] = token
    chain_products = [
        (state.W[i] * state.W[i + 1]) % params.M for i in range(params.x - 1)
    ]
    state.S = h_bytes(
        state.S,
        *[int_to_bytes_fixed(w, params.token_bytes) for w in state.W],
        *[int_to_bytes_fixed(m, params.mod_bytes) for m in chain_products],
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

    defender_executor = GFSLExecutor(track_instruction_activity=False)
    attacker_executor = GFSLExecutor(track_instruction_activity=False)

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

    prev_emitted = 0
    prev_idx = 0

    start = time.perf_counter()
    for t in range(scenario.cycles):
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
        lane_decisions: List[PolicyDecision] = []

        for lane in range(params.x):
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
                    decision = _policy_from_outputs(outputs)
                except Exception:
                    controller_fails += 1
                    decision = PolicyDecision(active_ratio=1.0, kernel=0, stride_seed=0, state_mix=0.5)

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

        device_cycle_ms = controller_ms + (lane_pow_selected * pow_cost_ms) + (3.0 * hash_cost_ms)
        provider_cycle_ms = controller_ms + (provider_pow_max * pow_cost_ms) + (3.0 * hash_cost_ms)
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
        _update_device_state(state, idx, emitted, params, phase, h_bytes, int_to_bytes_fixed)

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
        )

        # Optional evolved attacker benchmark.
        if attacker is not None:
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
    sync_score = (
        0.35 * twin_sync_rate
        + 0.15 * timing_reject_rate
        + 0.35 * (1.0 - sync_loss_rate)
        + 0.15 * resync_success_rate
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

    stability_score = 1.0 - clamp(controller_fail_rate + (0.40 * sync_loss_rate), 0.0, 1.0)

    total_score = (
        0.30 * principle_score
        + 0.20 * sync_score
        + 0.24 * security_score
        + 0.16 * cost_score
        + 0.06 * runtime_score
        + 0.04 * stability_score
    )

    if one_of_x_rate < 1.0:
        total_score -= 0.60 * (1.0 - one_of_x_rate)
    if block_once_rate < 1.0:
        total_score -= 0.35 * (1.0 - block_once_rate)

    # Penalize degenerate defender circuits.
    effective_size = len(genome.extract_effective_algorithm()) if genome is not None else 0
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
        device_compound_ratio=device_compound_ratio,
        provider_compound_ratio=provider_compound_ratio,
        elapsed_seconds=elapsed,
    )


def evaluate_across_scenarios(
    scenarios: Sequence[ScenarioConfig],
    genome: Optional[GFSLGenome],
    *,
    fixed_decision: Optional[PolicyDecision] = None,
    attacker: Optional[GFSLGenome] = None,
) -> Tuple[float, List[ScenarioMetrics]]:
    metrics = [
        evaluate_scenario(
            scenario,
            genome,
            fixed_decision=fixed_decision,
            attacker=attacker,
        )
        for scenario in scenarios
    ]
    total = sum(item.total_score for item in metrics) / float(max(1, len(metrics)))
    return total, metrics
