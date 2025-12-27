#!/usr/bin/env python3
"""
Cycle-by-cycle PCPL simulation from papers/phase-shift-tokens.md.
Models both roles:
- Device emitter: selects a lane via perm_key, computes only that lane token,
  and updates W and S.
- Provider validators: each lane recomputes its own token every cycle; matches
  occur 1-of-x by construction.
Validates the 1-of-x lane property and the per-block permutation schedule, with
optional dynamic prime generation and difficulty reporting.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import math
import random
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple


PRIME_POOL = [
    3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67
]
PERM_TABLE_24 = [tuple(p) for p in itertools.permutations(range(4))]
MR_BASES_64 = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)


@dataclass(frozen=True)
class Params:
    x: int
    P: int
    Q: int
    R: int
    M: int
    a0: int
    b0: int
    c0: int
    token_bits: int
    token_bytes: int
    seed_bytes: int
    mod_bytes: int


@dataclass(frozen=True)
class Phase:
    a: int
    b: int
    c: int
    u1: int
    u2: int
    u3: int
    phi: bytes


@dataclass(frozen=True)
class ProviderSecrets:
    bouquetA: List[int]
    bouquetB: List[int]
    bouquetC: List[int]


@dataclass(frozen=True)
class CompoundConfig:
    num_compounds: int
    primes_per_compound: int
    mode: str
    offset_max: int
    exponent_min: int
    exponent_max: int
    prime_pool: Sequence[int]


@dataclass
class DeviceState:
    W: List[int]
    S: bytes
    perm_key: bytes
    secrets: List[ProviderSecrets]


def int_to_bytes_fixed(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def trunc_bits(data: bytes, bits: int) -> int:
    byte_len = (bits + 7) // 8
    value = int.from_bytes(data[:byte_len], "big")
    extra = (byte_len * 8) - bits
    if extra:
        value >>= extra
    return value


def _encode_part(part: object) -> bytes:
    if isinstance(part, bytes):
        tag = b"B"
        payload = part
    elif isinstance(part, str):
        tag = b"S"
        payload = part.encode("ascii")
    elif isinstance(part, int):
        if part < 0:
            raise ValueError("Negative integers are not supported")
        payload = b"\x00" if part == 0 else part.to_bytes((part.bit_length() + 7) // 8, "big")
        tag = b"I"
    else:
        raise TypeError(f"Unsupported part type: {type(part)}")
    return tag + len(payload).to_bytes(4, "big") + payload


def h_bytes(*parts: object, out_len: int = 32) -> bytes:
    if not (1 <= out_len <= 64):
        raise ValueError("out_len must be between 1 and 64 bytes for blake2b")
    hasher = hashlib.blake2b(digest_size=out_len)
    for part in parts:
        hasher.update(_encode_part(part))
    return hasher.digest()


def derive_seed(seed: int, label: str) -> int:
    return int.from_bytes(h_bytes(seed, label, out_len=8), "big")


def is_prime_small(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    limit = int(math.isqrt(n))
    for p in range(3, limit + 1, 2):
        if n % p == 0:
            return False
    return True


def is_probable_prime(n: int) -> bool:
    if n < 2:
        return False
    small_primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    for p in small_primes:
        if n % p == 0:
            return n == p

    d = n - 1
    s = 0
    while d % 2 == 0:
        d //= 2
        s += 1

    for a in MR_BASES_64:
        if a % n == 0:
            continue
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def generate_prime(
    rng: random.Random,
    bits: int,
    avoid_gcd: int,
    avoid_set: Optional[Set[int]] = None,
) -> int:
    if bits < 2:
        raise ValueError("bits must be >= 2")
    if avoid_set is None:
        avoid_set = set()
    while True:
        candidate = rng.getrandbits(bits)
        candidate |= (1 << (bits - 1)) | 1
        if math.gcd(candidate, avoid_gcd) != 1:
            continue
        if candidate in avoid_set:
            continue
        if is_probable_prime(candidate):
            return candidate


def generate_prime_pool(
    rng: random.Random,
    pool_size: int,
    bits: int,
    avoid_set: Optional[Set[int]] = None,
) -> List[int]:
    if pool_size <= 0:
        raise ValueError("pool_size must be positive")
    if avoid_set is None:
        avoid_set = set()
    pool: List[int] = []
    while len(pool) < pool_size:
        prime = generate_prime(rng, bits, avoid_gcd=1, avoid_set=avoid_set | set(pool))
        pool.append(prime)
    return pool


def generate_coprime_primes(
    rng: random.Random,
    x: int,
    bits: int,
) -> Tuple[int, int, int]:
    primes = []
    while len(primes) < 3:
        prime = generate_prime(rng, bits, avoid_gcd=x, avoid_set=set(primes))
        primes.append(prime)
    return primes[0], primes[1], primes[2]


def next_prime_avoiding(start: int, avoid: int) -> int:
    candidate = start
    while True:
        if is_prime_small(candidate) and math.gcd(candidate, avoid) == 1:
            return candidate
        candidate += 1


def build_params(
    x: int,
    token_bits: int,
    seed_bytes: int = 32,
    prime_mode: str = "fixed",
    prime_bits: int = 20,
    modulus_bits: int = 61,
    rng: Optional[random.Random] = None,
) -> Params:
    if x < 2:
        raise ValueError("x must be at least 2")
    if token_bits <= 0:
        raise ValueError("token_bits must be positive")
    token_bytes = (token_bits + 7) // 8
    if token_bytes > 64:
        raise ValueError("token_bits too large for blake2b truncation")

    if prime_mode == "fixed":
        P = next_prime_avoiding(1_000_003, x)
        Q = next_prime_avoiding(1_000_033, x)
        R = next_prime_avoiding(1_000_037, x)
        M = (1 << 61) - 1  # 2^61 - 1, known prime
    elif prime_mode == "generated":
        if rng is None:
            raise ValueError("rng is required when prime_mode='generated'")
        if prime_bits < 8:
            raise ValueError("prime_bits too small for generated primes")
        if modulus_bits < 16:
            raise ValueError("modulus_bits too small for generated modulus")
        P, Q, R = generate_coprime_primes(rng, x, prime_bits)
        M = generate_prime(rng, modulus_bits, avoid_gcd=x, avoid_set={P, Q, R})
    else:
        raise ValueError("prime_mode must be 'fixed' or 'generated'")
    mod_bytes = (M.bit_length() + 7) // 8

    if len({P, Q, R}) != 3:
        raise ValueError("P, Q, R must be distinct primes")
    if math.gcd(M, x) != 1:
        raise ValueError("M must be coprime with x")

    return Params(
        x=x,
        P=P,
        Q=Q,
        R=R,
        M=M,
        a0=1,
        b0=2,
        c0=3,
        token_bits=token_bits,
        token_bytes=token_bytes,
        seed_bytes=seed_bytes,
        mod_bytes=mod_bytes,
    )


def phase_clock(t: int, params: Params) -> Phase:
    a = (params.a0 + t) % params.P
    b = (params.b0 + t) % params.Q
    c = (params.c0 + t) % params.R

    u1 = (a * b) % params.M
    u2 = (b * c) % params.M
    u3 = (c * a) % params.M

    phi = h_bytes(a, b, c, u1, u2, u3, "PHASE", out_len=32)
    return Phase(a=a, b=b, c=c, u1=u1, u2=u2, u3=u3, phi=phi)


def permutation_for_block(B: int, params: Params, perm_key: bytes, phi_block: bytes) -> Sequence[int]:
    if params.x == 4:
        perm_id = int.from_bytes(h_bytes(perm_key, B, phi_block, "PERM", out_len=4), "big") % 24
        return PERM_TABLE_24[perm_id]

    perm = list(range(params.x))
    seed = h_bytes(perm_key, B, phi_block, "PERMSEED", out_len=32)
    for k in range(params.x - 1, 0, -1):
        r = int.from_bytes(h_bytes(seed, k, "R", out_len=8), "big") % (k + 1)
        perm[k], perm[r] = perm[r], perm[k]
    return perm


def device_destination_provider(t: int, params: Params, perm_key: bytes) -> int:
    block = t // params.x
    slot = t % params.x
    phase_block = phase_clock(block * params.x, params)
    perm = permutation_for_block(block, params, perm_key, phase_block.phi)
    return perm[slot]


def eval_bouquet(bouquet: Sequence[int], xres: int, u: int, params: Params) -> int:
    acc = 1 % params.M
    for j, compound in enumerate(bouquet):
        base = compound % params.M
        if base == 0:
            raise ValueError("Compound is divisible by M; choose different primes")
        exponent = int.from_bytes(h_bytes(xres, u, j, "EXP", out_len=32), "big") % (params.M - 1)
        acc = (acc * pow(base, exponent, params.M)) % params.M
    return acc


def exponent_vector(num_compounds: int, xres: int, u: int, params: Params) -> List[int]:
    return [
        int.from_bytes(h_bytes(xres, u, j, "EXP", out_len=32), "big") % (params.M - 1)
        for j in range(num_compounds)
    ]


def modinv(value: int, mod: int) -> int:
    if mod == 2:
        return 1
    return pow(value, mod - 2, mod)


def rank_mod(matrix: List[List[int]], mod: int) -> int:
    if not matrix:
        return 0
    rows = [row[:] for row in matrix]
    row_count = len(rows)
    col_count = len(rows[0])
    rank = 0
    for col in range(col_count):
        pivot = None
        for r in range(rank, row_count):
            if rows[r][col] % mod != 0:
                pivot = r
                break
        if pivot is None:
            continue
        rows[rank], rows[pivot] = rows[pivot], rows[rank]
        inv = modinv(rows[rank][col] % mod, mod)
        for c in range(col, col_count):
            rows[rank][c] = (rows[rank][c] * inv) % mod
        for r in range(row_count):
            if r == rank or rows[r][col] % mod == 0:
                continue
            factor = rows[r][col] % mod
            for c in range(col, col_count):
                rows[r][c] = (rows[r][c] - factor * rows[rank][c]) % mod
        rank += 1
        if rank == col_count:
            break
    return rank


def linear_difficulty_report(params: Params, num_compounds: int, window: int) -> None:
    window = max(1, window)
    matrices = {"A": [], "B": [], "C": []}
    for t in range(window):
        phase = phase_clock(t, params)
        matrices["A"].append(exponent_vector(num_compounds, phase.a, phase.u1, params))
        matrices["B"].append(exponent_vector(num_compounds, phase.b, phase.u2, params))
        matrices["C"].append(exponent_vector(num_compounds, phase.c, phase.u3, params))

    for label in ("A", "B", "C"):
        rows = matrices[label]
        unique_rows = len({tuple(row) for row in rows})
        rank_mod2 = rank_mod(rows, 2)
        rank_modp = rank_mod(rows, 65537)
        print(
            f"linear-{label}: unique={unique_rows}/{window} "
            f"rank_mod2={rank_mod2}/{num_compounds} "
            f"rank_mod65537={rank_modp}/{num_compounds}"
        )


def lcm(a: int, b: int) -> int:
    return a // math.gcd(a, b) * b


def schedule_period(params: Params) -> int:
    return lcm(lcm(lcm(params.P, params.Q), params.R), params.x)


def qft_report(params: Params) -> None:
    period = schedule_period(params)
    print(f"qft-period: {period} (~{period.bit_length()} bits)")


def lane_token(_index: int, t: int, phase: Phase, params: Params, secrets: ProviderSecrets) -> int:
    """Shared per-cycle token derivation used by device and provider circuits."""
    ea = eval_bouquet(secrets.bouquetA, phase.a, phase.u1, params)
    eb = eval_bouquet(secrets.bouquetB, phase.b, phase.u2, params)
    ec = eval_bouquet(secrets.bouquetC, phase.c, phase.u3, params)

    kdf = h_bytes(ea, eb, ec, phase.phi, "KDF", out_len=32)
    tok_hash = h_bytes(kdf, t, phase.phi, "TOK", out_len=max(32, params.token_bytes))
    return trunc_bits(tok_hash, params.token_bits)


def provider_cycle(
    t: int,
    lane_idx: int,
    params: Params,
    secrets: ProviderSecrets,
    phase: Optional[Phase] = None,
) -> int:
    """Provider-side per-cycle recomputation of the expected lane token."""
    if phase is None:
        phase = phase_clock(t, params)
    return lane_token(lane_idx, t, phase, params, secrets)


def device_cycle(t: int, params: Params, state: DeviceState) -> Tuple[int, int]:
    phase = phase_clock(t, params)

    idx = device_destination_provider(t, params, state.perm_key)

    state.W[idx] = lane_token(idx, t, phase, params, state.secrets[idx])

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

    return idx, state.W[idx]


def generate_provider_secrets(rng: random.Random, compound_cfg: CompoundConfig) -> ProviderSecrets:
    def classic_compound() -> int:
        value = 1
        for _ in range(compound_cfg.primes_per_compound):
            prime = rng.choice(compound_cfg.prime_pool)
            exponent = rng.randint(compound_cfg.exponent_min, compound_cfg.exponent_max)
            value *= prime**exponent
        return value

    def prime_power_compound() -> int:
        prime = rng.choice(compound_cfg.prime_pool)
        exponent = rng.randint(max(2, compound_cfg.exponent_min), compound_cfg.exponent_max)
        return prime**exponent

    def semiprime_compound() -> int:
        prime_a = rng.choice(compound_cfg.prime_pool)
        prime_b = rng.choice(compound_cfg.prime_pool)
        return prime_a * prime_b

    def offset_compound() -> int:
        base = classic_compound()
        if compound_cfg.offset_max > 0:
            base += rng.randint(1, compound_cfg.offset_max)
        return base

    def make_compound() -> int:
        mode = compound_cfg.mode
        if mode == "classic":
            return classic_compound()
        if mode == "prime-power":
            return prime_power_compound()
        if mode == "semiprime":
            return semiprime_compound()
        if mode == "offset":
            return offset_compound()
        if mode == "blend":
            roll = rng.random()
            if roll < 0.5:
                return classic_compound()
            if roll < 0.7:
                return prime_power_compound()
            if roll < 0.85:
                return semiprime_compound()
            return offset_compound()
        raise ValueError(f"Unknown compound mode: {mode}")

    return ProviderSecrets(
        bouquetA=[make_compound() for _ in range(compound_cfg.num_compounds)],
        bouquetB=[make_compound() for _ in range(compound_cfg.num_compounds)],
        bouquetC=[make_compound() for _ in range(compound_cfg.num_compounds)],
    )


def build_fixture(
    params: Params,
    seed: int,
    compound_cfg: CompoundConfig,
) -> Tuple[List[ProviderSecrets], DeviceState]:
    rng = random.Random(seed)
    secrets = [
        generate_provider_secrets(rng, compound_cfg) for _ in range(params.x)
    ]

    seed_material = rng.getrandbits(256).to_bytes(32, "big")
    perm_key = h_bytes(seed_material, "PERMKEY", out_len=32)
    seed_state = h_bytes(seed_material, "SEED", out_len=params.seed_bytes)
    token_hash_len = max(32, params.token_bytes)
    w_init = [
        trunc_bits(h_bytes(seed_material, "W", i, out_len=token_hash_len), params.token_bits)
        for i in range(params.x)
    ]

    state = DeviceState(W=w_init, S=seed_state, perm_key=perm_key, secrets=secrets)
    return secrets, state


def validate_permutation(params: Params, perm_key: bytes, blocks: int) -> None:
    for block in range(blocks):
        phase_block = phase_clock(block * params.x, params)
        perm = permutation_for_block(block, params, perm_key, phase_block.phi)
        if sorted(perm) != list(range(params.x)):
            raise AssertionError(f"Block {block} permutation is invalid: {perm}")


def validate_cycles(
    params: Params,
    secrets: List[ProviderSecrets],
    state: DeviceState,
    cycles: int,
    verbose: bool = False,
) -> None:
    full_blocks = cycles // params.x
    block_counts = [[0 for _ in range(params.x)] for _ in range(full_blocks)]

    for t in range(cycles):
        idx, token = device_cycle(t, params, state)

        # Providers run their per-cycle hash pipeline continuously and compare.
        phase = phase_clock(t, params)
        matches = [
            i
            for i in range(params.x)
            if provider_cycle(t, i, params, secrets[i], phase=phase) == token
        ]
        if matches != [idx]:
            raise AssertionError(f"Cycle {t} expected match {idx}, got {matches}")

        block = t // params.x
        if block < full_blocks:
            block_counts[block][idx] += 1

        if verbose and t < 10:
            token_hex = f"{token:0{params.token_bytes * 2}x}"
            print(f"t={t:04d} provider={idx} token=0x{token_hex}")

    for block, counts in enumerate(block_counts):
        if any(count != 1 for count in counts):
            raise AssertionError(f"Block {block} counts invalid: {counts}")


def validate_chaining(params: Params, seed: int, compound_cfg: CompoundConfig) -> None:
    _, state_a = build_fixture(params, seed, compound_cfg)
    _, state_b = build_fixture(params, seed, compound_cfg)

    phase_block = phase_clock(0, params)
    perm = permutation_for_block(0, params, state_a.perm_key, phase_block.phi)
    flip_idx = (perm[0] + 1) % params.x
    state_b.W[flip_idx] ^= 1

    device_cycle(0, params, state_a)
    device_cycle(0, params, state_b)
    if state_a.S == state_b.S:
        raise AssertionError("Chaining check failed: seed did not diverge after mutation")


def build_compound_config(
    seed: int,
    params: Params,
    num_compounds: int,
    primes_per_compound: int,
    compound_mode: str,
    compound_offset: int,
    compound_prime_bits: int,
    compound_pool_size: int,
    pool_label: str,
) -> CompoundConfig:
    if compound_prime_bits > 0:
        rng_pool = random.Random(derive_seed(seed, pool_label))
        prime_pool = generate_prime_pool(
            rng_pool,
            compound_pool_size,
            compound_prime_bits,
            avoid_set={params.M},
        )
    else:
        prime_pool = PRIME_POOL
    if not prime_pool:
        raise ValueError("Prime pool cannot be empty")
    return CompoundConfig(
        num_compounds=num_compounds,
        primes_per_compound=primes_per_compound,
        mode=compound_mode,
        offset_max=max(0, compound_offset),
        exponent_min=1,
        exponent_max=3,
        prime_pool=prime_pool,
    )


def parse_x_list(values: str) -> List[int]:
    parts = [part.strip() for part in values.split(",") if part.strip()]
    if not parts:
        raise ValueError("compare-x list is empty")
    parsed = []
    for part in parts:
        value = int(part)
        if value < 2:
            raise ValueError("x values must be at least 2")
        parsed.append(value)
    return parsed


def compare_x_modes(args: argparse.Namespace) -> None:
    x_values = parse_x_list(args.compare_x)
    print("compare-x: x | period_bits | chain_edges | perm0 | P,Q,R")
    for x in x_values:
        param_rng = None
        if args.prime_mode == "generated":
            param_rng = random.Random(derive_seed(args.seed, f"PARAMS:{x}"))
        params = build_params(
            x,
            args.token_bits,
            prime_mode=args.prime_mode,
            prime_bits=args.prime_bits,
            modulus_bits=args.modulus_bits,
            rng=param_rng,
        )
        compound_cfg = build_compound_config(
            args.seed,
            params,
            args.compound_count,
            args.compound_primes,
            args.compound_mode,
            args.compound_offset,
            args.compound_prime_bits,
            args.compound_pool_size,
            pool_label=f"COMPOUND_POOL:{x}",
        )
        _, state = build_fixture(params, args.seed, compound_cfg)
        phase_block = phase_clock(0, params)
        perm = permutation_for_block(0, params, state.perm_key, phase_block.phi)
        period_bits = schedule_period(params).bit_length()
        print(
            f"compare-x: {x} | {period_bits} | {x - 1} | {perm} | "
            f"{params.P},{params.Q},{params.R}"
        )
        if args.linear_report:
            linear_difficulty_report(params, compound_cfg.num_compounds, args.analysis_window)
        if args.qft_report:
            qft_report(params)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PCPL cycle-by-cycle demo test.")
    parser.add_argument("--cycles", type=int, default=200, help="Number of cycles to simulate.")
    parser.add_argument("--x", type=int, default=4, help="Number of providers.")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic RNG seed.")
    parser.add_argument("--token-bits", type=int, default=128, help="Token size in bits.")
    parser.add_argument(
        "--prime-mode",
        choices=("fixed", "generated"),
        default="fixed",
        help="Prime selection mode for P/Q/R (and M when generated).",
    )
    parser.add_argument("--prime-bits", type=int, default=20, help="Bit size for generated P/Q/R.")
    parser.add_argument("--modulus-bits", type=int, default=61, help="Bit size for generated modulus M.")
    parser.add_argument(
        "--compound-mode",
        choices=("classic", "prime-power", "semiprime", "offset", "blend"),
        default="classic",
        help="Compound generation mode for bouquets.",
    )
    parser.add_argument("--compound-count", type=int, default=4, help="Compounds per bouquet.")
    parser.add_argument("--compound-primes", type=int, default=3, help="Primes per compound.")
    parser.add_argument(
        "--compound-offset",
        type=int,
        default=0,
        help="Offset added to compounds for offset/blend modes.",
    )
    parser.add_argument(
        "--compound-prime-bits",
        type=int,
        default=0,
        help="Bit size for generated prime pool (0 uses built-in pool).",
    )
    parser.add_argument(
        "--compound-pool-size",
        type=int,
        default=len(PRIME_POOL),
        help="Prime pool size when generating compound primes.",
    )
    parser.add_argument(
        "--analysis-window",
        type=int,
        default=64,
        help="Cycles to sample for linear difficulty report.",
    )
    parser.add_argument("--linear-report", action="store_true", help="Print pre-hash linear metrics.")
    parser.add_argument("--qft-report", action="store_true", help="Print QFT-visible period metrics.")
    parser.add_argument(
        "--compare-x",
        type=str,
        default="",
        help="Comma-separated x values to compare and exit.",
    )
    parser.add_argument("--show-params", action="store_true", help="Print P, Q, R, M values.")
    parser.add_argument("--verbose", action="store_true", help="Print first few cycles.")
    parser.add_argument("--no-chaining-check", action="store_true", help="Skip chaining divergence check.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.compare_x:
        compare_x_modes(args)
        return

    param_rng = None
    if args.prime_mode == "generated":
        param_rng = random.Random(derive_seed(args.seed, "PARAMS"))
    params = build_params(
        args.x,
        args.token_bits,
        prime_mode=args.prime_mode,
        prime_bits=args.prime_bits,
        modulus_bits=args.modulus_bits,
        rng=param_rng,
    )
    compound_cfg = build_compound_config(
        args.seed,
        params,
        args.compound_count,
        args.compound_primes,
        args.compound_mode,
        args.compound_offset,
        args.compound_prime_bits,
        args.compound_pool_size,
        pool_label="COMPOUND_POOL",
    )
    secrets, state = build_fixture(params, args.seed, compound_cfg)

    validate_permutation(params, state.perm_key, blocks=max(1, args.cycles // params.x))
    validate_cycles(params, secrets, state, args.cycles, verbose=args.verbose)
    if not args.no_chaining_check:
        validate_chaining(params, args.seed, compound_cfg)

    if args.show_params:
        print(f"params: P={params.P} Q={params.Q} R={params.R} M={params.M}")
    if args.linear_report:
        linear_difficulty_report(
            params,
            compound_cfg.num_compounds,
            min(args.analysis_window, args.cycles),
        )
    if args.qft_report:
        qft_report(params)

    blocks = args.cycles // params.x
    print(
        "OK: cycles={cycles} providers={providers} blocks={blocks} token_bits={bits}".format(
            cycles=args.cycles,
            providers=params.x,
            blocks=blocks,
            bits=params.token_bits,
        )
    )


if __name__ == "__main__":
    main()
