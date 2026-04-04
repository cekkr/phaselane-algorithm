"""PCPL simulation environment and scoring primitives for Evolvo genomes."""

from __future__ import annotations

import copy
import math
import random
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable, load_reference_pcpl

ensure_evolvo_importable()

from evolvo import DataType, GFSLExecutor, GFSLGenome


OUTPUT_INDICES = (20, 21, 22)
INPUT_DECIMAL_COUNT = 12


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
    timing_budget_ns: int = 4_000_000


@dataclass(frozen=True)
class PolicyDecision:
    """Control outputs produced by the evolved GFSL circuit."""

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
    """Score breakdown for one scenario."""

    scenario: str
    total_score: float
    principle_score: float
    sync_score: float
    security_score: float
    cost_score: float
    runtime_score: float
    stability_score: float
    one_of_x_rate: float
    block_once_rate: float
    permutation_valid_rate: float
    attack_reject_rate: float
    twin_sync_rate: float
    timing_reject_rate: float
    cross_lane_collision_rate: float
    replay_rate: float
    controller_fail_rate: float
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


def default_scenarios(profile: str = "fast") -> List[ScenarioConfig]:
    """Default scenario sets. `fast` is CI/dev friendly; `full` is heavier."""
    if profile == "full":
        return [
            ScenarioConfig(name="x4-fixed", x=4, cycles=160, seed=1337, prime_mode="fixed"),
            ScenarioConfig(
                name="x6-generated",
                x=6,
                cycles=180,
                seed=2026,
                prime_mode="generated",
                prime_bits=20,
                modulus_bits=53,
            ),
            ScenarioConfig(
                name="x8-generated",
                x=8,
                cycles=224,
                seed=4242,
                prime_mode="generated",
                prime_bits=19,
                modulus_bits=53,
            ),
        ]

    return [
        ScenarioConfig(name="x4-fixed", x=4, cycles=40, seed=1337, prime_mode="fixed"),
        ScenarioConfig(
            name="x6-generated",
            x=6,
            cycles=48,
            seed=2026,
            prime_mode="generated",
            prime_bits=19,
            modulus_bits=47,
        ),
    ]


def ensure_genome_io(genome: GFSLGenome) -> None:
    """Seed input variable counts and ensure required outputs are tracked."""
    dtype_key = int(DataType.DECIMAL)
    genome.validator.variable_counts[dtype_key] = max(
        int(genome.validator.variable_counts[dtype_key]),
        INPUT_DECIMAL_COUNT,
    )

    existing_outputs = {
        (int(cat), int(dtype), int(idx)) for cat, dtype, idx in genome.outputs
    }
    for idx in OUTPUT_INDICES:
        marker = (1, int(DataType.DECIMAL), idx)  # Category.VARIABLE == 1
        if marker not in existing_outputs:
            genome.mark_output(DataType.DECIMAL, idx)


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
) -> ScenarioMetrics:
    """Evaluate one scenario for either an evolved genome or a fixed policy."""
    if genome is None and fixed_decision is None:
        raise ValueError("Either a genome or fixed_decision must be provided")

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

    executor = GFSLExecutor(track_instruction_activity=False)
    effective_size = 0
    if genome is not None:
        ensure_genome_io(genome)
        effective_size = len(genome.extract_effective_algorithm())

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

    device_compound_total = 0
    provider_compound_total = 0

    emitted_tokens: List[int] = []
    provider_matrix: List[List[int]] = []
    replay_window: List[List[int]] = [[] for _ in range(params.x)]

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
                    outputs = executor.execute(genome, inputs, track_activity=False)
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

        emitted = lane_tokens[idx]
        emitted_tokens.append(emitted)
        provider_matrix.append(lane_tokens)

        # One-of-x correctness and adversarial cross-lane attempts
        matches = [lane for lane, token in enumerate(lane_tokens) if token == emitted]
        if matches == [idx]:
            one_of_x_hits += 1
        attack_checks += params.x - 1
        attack_successes = len([lane for lane in matches if lane != idx])
        attack_miss += (params.x - 1) - attack_successes

        # Block fairness accounting
        if block < full_blocks:
            block_counts[block][idx] += 1

        # Collision and replay checks
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

        device_compound_total += lane_actives[idx]
        provider_compound_total += sum(lane_actives)

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

    elapsed = time.perf_counter() - start

    block_ok = 0
    for counts in block_counts:
        if all(count == 1 for count in counts):
            block_ok += 1

    timing_reject_hits = 0
    timing_checks = max(0, scenario.cycles - 1)
    for t in range(scenario.cycles - 1):
        idx = t % params.x
        perm_block = t // params.x
        phase_block = phase_clock(perm_block * params.x, params)
        perm = permutation_for_block(perm_block, params, state.perm_key, phase_block.phi)
        routed = perm[idx]
        if provider_matrix[t + 1][routed] != emitted_tokens[t]:
            timing_reject_hits += 1

    one_of_x_rate = one_of_x_hits / float(max(1, scenario.cycles))
    block_once_rate = block_ok / float(max(1, full_blocks))
    permutation_valid_rate = perm_ok / float(max(1, scenario.cycles))
    attack_reject_rate = attack_miss / float(max(1, attack_checks))

    twin_sync_rate = 1.0 if state.S == twin_state.S and state.W == twin_state.W else 0.0
    timing_reject_rate = timing_reject_hits / float(max(1, timing_checks)) if timing_checks else 1.0

    cross_lane_collision_rate = cross_lane_collisions / float(max(1, collision_checks))
    replay_rate = replay_hits / float(max(1, replay_checks))
    controller_fail_rate = controller_fails / float(max(1, scenario.cycles * params.x))

    baseline_device = max(1, scenario.cycles * scenario.compound_count)
    baseline_provider = max(1, scenario.cycles * params.x * scenario.compound_count)
    device_compound_ratio = device_compound_total / float(baseline_device)
    provider_compound_ratio = provider_compound_total / float(baseline_provider)

    principle_score = (
        0.35 * one_of_x_rate
        + 0.25 * block_once_rate
        + 0.20 * permutation_valid_rate
        + 0.20 * attack_reject_rate
    )
    sync_score = 0.70 * twin_sync_rate + 0.30 * timing_reject_rate
    security_score = 0.50 * (1.0 - clamp(cross_lane_collision_rate, 0.0, 1.0)) + 0.50 * (
        1.0 - clamp(replay_rate, 0.0, 1.0)
    )
    cost_score = 1.0 - 0.50 * clamp(device_compound_ratio, 0.0, 1.0) - 0.50 * clamp(
        provider_compound_ratio,
        0.0,
        1.0,
    )

    ns_per_cycle = (elapsed * 1e9) / float(max(1, scenario.cycles))
    if ns_per_cycle <= float(scenario.timing_budget_ns):
        runtime_score = 1.0
    else:
        overload = (ns_per_cycle - float(scenario.timing_budget_ns)) / float(
            scenario.timing_budget_ns
        )
        runtime_score = 1.0 / (1.0 + overload)

    stability_score = 1.0 - clamp(controller_fail_rate, 0.0, 1.0)

    total_score = (
        0.42 * principle_score
        + 0.18 * sync_score
        + 0.20 * security_score
        + 0.12 * cost_score
        + 0.05 * runtime_score
        + 0.03 * stability_score
    )

    if one_of_x_rate < 1.0:
        total_score -= 0.55 * (1.0 - one_of_x_rate)
    if block_once_rate < 1.0:
        total_score -= 0.35 * (1.0 - block_once_rate)

    # Mild complexity regularization to keep generated circuits practical.
    if effective_size > 0:
        total_score -= min(0.08, max(0.0, (effective_size - 14) * 0.0025))

    return ScenarioMetrics(
        scenario=scenario.name,
        total_score=total_score,
        principle_score=principle_score,
        sync_score=sync_score,
        security_score=security_score,
        cost_score=cost_score,
        runtime_score=runtime_score,
        stability_score=stability_score,
        one_of_x_rate=one_of_x_rate,
        block_once_rate=block_once_rate,
        permutation_valid_rate=permutation_valid_rate,
        attack_reject_rate=attack_reject_rate,
        twin_sync_rate=twin_sync_rate,
        timing_reject_rate=timing_reject_rate,
        cross_lane_collision_rate=cross_lane_collision_rate,
        replay_rate=replay_rate,
        controller_fail_rate=controller_fail_rate,
        device_compound_ratio=device_compound_ratio,
        provider_compound_ratio=provider_compound_ratio,
        elapsed_seconds=elapsed,
    )


def evaluate_across_scenarios(
    scenarios: Sequence[ScenarioConfig],
    genome: Optional[GFSLGenome],
    *,
    fixed_decision: Optional[PolicyDecision] = None,
) -> Tuple[float, List[ScenarioMetrics]]:
    metrics = [
        evaluate_scenario(scenario, genome, fixed_decision=fixed_decision)
        for scenario in scenarios
    ]
    total = sum(item.total_score for item in metrics) / float(max(1, len(metrics)))
    return total, metrics
