#!/usr/bin/env python3
"""
Cycle-by-cycle PCPL simulation from papers/phase-shift-tokens.md.
Validates the 1-of-x lane property and the per-block permutation schedule.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import math
import random
from dataclasses import dataclass
from typing import List, Sequence, Tuple


PRIME_POOL = [
    3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67
]
PERM_TABLE_24 = [tuple(p) for p in itertools.permutations(range(4))]


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


def next_prime_avoiding(start: int, avoid: int) -> int:
    candidate = start
    while True:
        if is_prime_small(candidate) and math.gcd(candidate, avoid) == 1:
            return candidate
        candidate += 1


def build_params(x: int, token_bits: int, seed_bytes: int = 32) -> Params:
    if x < 2:
        raise ValueError("x must be at least 2")
    if token_bits <= 0:
        raise ValueError("token_bits must be positive")
    token_bytes = (token_bits + 7) // 8
    if token_bytes > 64:
        raise ValueError("token_bits too large for blake2b truncation")

    P = next_prime_avoiding(1_000_003, x)
    Q = next_prime_avoiding(1_000_033, x)
    R = next_prime_avoiding(1_000_037, x)
    M = (1 << 61) - 1  # 2^61 - 1, known prime
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


def eval_bouquet(bouquet: Sequence[int], xres: int, u: int, params: Params) -> int:
    acc = 1 % params.M
    for j, compound in enumerate(bouquet):
        base = compound % params.M
        if base == 0:
            raise ValueError("Compound is divisible by M; choose different primes")
        exponent = int.from_bytes(h_bytes(xres, u, j, "EXP", out_len=32), "big") % (params.M - 1)
        acc = (acc * pow(base, exponent, params.M)) % params.M
    return acc


def lane_token(_index: int, t: int, phase: Phase, params: Params, secrets: ProviderSecrets) -> int:
    ea = eval_bouquet(secrets.bouquetA, phase.a, phase.u1, params)
    eb = eval_bouquet(secrets.bouquetB, phase.b, phase.u2, params)
    ec = eval_bouquet(secrets.bouquetC, phase.c, phase.u3, params)

    kdf = h_bytes(ea, eb, ec, phase.phi, "KDF", out_len=32)
    tok_hash = h_bytes(kdf, t, phase.phi, "TOK", out_len=max(32, params.token_bytes))
    return trunc_bits(tok_hash, params.token_bits)


def device_cycle(t: int, params: Params, state: DeviceState) -> Tuple[int, int]:
    phase = phase_clock(t, params)

    block = t // params.x
    slot = t % params.x
    phase_block = phase_clock(block * params.x, params)
    perm = permutation_for_block(block, params, state.perm_key, phase_block.phi)
    idx = perm[slot]

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


def generate_provider_secrets(rng: random.Random, num_compounds: int, primes_per_compound: int) -> ProviderSecrets:
    def make_compound() -> int:
        value = 1
        for _ in range(primes_per_compound):
            prime = rng.choice(PRIME_POOL)
            exponent = rng.randint(1, 3)
            value *= prime ** exponent
        return value

    return ProviderSecrets(
        bouquetA=[make_compound() for _ in range(num_compounds)],
        bouquetB=[make_compound() for _ in range(num_compounds)],
        bouquetC=[make_compound() for _ in range(num_compounds)],
    )


def build_fixture(params: Params, seed: int) -> Tuple[List[ProviderSecrets], DeviceState]:
    rng = random.Random(seed)
    secrets = [
        generate_provider_secrets(rng, num_compounds=4, primes_per_compound=3)
        for _ in range(params.x)
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

        phase = phase_clock(t, params)
        matches = [
            i
            for i in range(params.x)
            if lane_token(i, t, phase, params, secrets[i]) == token
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


def validate_chaining(params: Params, seed: int) -> None:
    _, state_a = build_fixture(params, seed)
    _, state_b = build_fixture(params, seed)

    phase_block = phase_clock(0, params)
    perm = permutation_for_block(0, params, state_a.perm_key, phase_block.phi)
    flip_idx = (perm[0] + 1) % params.x
    state_b.W[flip_idx] ^= 1

    device_cycle(0, params, state_a)
    device_cycle(0, params, state_b)
    if state_a.S == state_b.S:
        raise AssertionError("Chaining check failed: seed did not diverge after mutation")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PCPL cycle-by-cycle demo test.")
    parser.add_argument("--cycles", type=int, default=200, help="Number of cycles to simulate.")
    parser.add_argument("--x", type=int, default=4, help="Number of providers.")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic RNG seed.")
    parser.add_argument("--token-bits", type=int, default=128, help="Token size in bits.")
    parser.add_argument("--verbose", action="store_true", help="Print first few cycles.")
    parser.add_argument("--no-chaining-check", action="store_true", help="Skip chaining divergence check.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    params = build_params(args.x, args.token_bits)
    secrets, state = build_fixture(params, args.seed)

    validate_permutation(params, state.perm_key, blocks=max(1, args.cycles // params.x))
    validate_cycles(params, secrets, state, args.cycles, verbose=args.verbose)
    if not args.no_chaining_check:
        validate_chaining(params, args.seed)

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
