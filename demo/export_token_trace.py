#!/usr/bin/env python3
"""
Generate a markdown token trace from the PCPL demo implementation.
The output is A4-friendly by splitting tables per provider lane.
"""

from __future__ import annotations

import argparse
import math
import runpy
from pathlib import Path


def load_pcpl_module() -> dict:
    module_path = Path(__file__).with_name("pcpl_cycle_test.py")
    return runpy.run_path(str(module_path))


def format_token(value: int, token_bits: int) -> str:
    width = (token_bits + 3) // 4
    return f"0x{value:0{width}x}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export PCPL token trace to Markdown.")
    parser.add_argument("--x", type=int, default=4, help="Number of providers.")
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Number of cycles to export. If omitted, uses blocks * x.",
    )
    parser.add_argument(
        "--blocks",
        type=int,
        default=4,
        help="Number of full blocks to export when --cycles is omitted.",
    )
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic RNG seed.")
    parser.add_argument("--token-bits", type=int, default=128, help="Token size in bits.")
    parser.add_argument(
        "--out",
        type=str,
        default="papers/token-trace.md",
        help="Output markdown path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.x < 2:
        raise ValueError("x must be at least 2")
    if args.blocks < 1:
        raise ValueError("blocks must be at least 1")

    cycles = args.cycles if args.cycles is not None else args.blocks * args.x
    if cycles < 1:
        raise ValueError("cycles must be at least 1")

    pcpl = load_pcpl_module()
    build_params = pcpl["build_params"]
    build_compound_config = pcpl["build_compound_config"]
    build_fixture = pcpl["build_fixture"]
    phase_clock = pcpl["phase_clock"]
    lane_token = pcpl["lane_token"]
    device_cycle = pcpl["device_cycle"]
    permutation_for_block = pcpl["permutation_for_block"]

    params = build_params(args.x, args.token_bits)
    compound_cfg = build_compound_config(
        args.seed,
        params,
        num_compounds=4,
        primes_per_compound=3,
        compound_mode="classic",
        compound_offset=0,
        compound_prime_bits=0,
        compound_pool_size=len(pcpl["PRIME_POOL"]),
        pool_label="COMPOUND_POOL",
    )
    secrets, state = build_fixture(params, args.seed, compound_cfg)

    block_count = math.ceil(cycles / params.x)
    block_perms = []
    for block in range(block_count):
        phase_block = phase_clock(block * params.x, params)
        perm = list(permutation_for_block(block, params, state.perm_key, phase_block.phi))
        block_perms.append((block, perm))

    rows = []
    for t in range(cycles):
        idx, token = device_cycle(t, params, state)
        phase = phase_clock(t, params)
        server_tokens = [lane_token(i, t, phase, params, secrets[i]) for i in range(params.x)]
        rows.append((t, t // params.x, t % params.x, idx, token, server_tokens))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = (
        f"python3 demo/export_token_trace.py --x {args.x} "
        f"--token-bits {args.token_bits} --seed {args.seed} "
        f"{'--cycles ' + str(cycles) if args.cycles is not None else '--blocks ' + str(args.blocks)}"
    )

    lines = []
    lines.append("# PCPL Token Trace (generated)")
    lines.append("")
    lines.append("This file is auto-generated. Do not edit by hand.")
    lines.append(f"Regenerate with: `{cmd}`")
    lines.append("")
    lines.append("Parameters:")
    lines.append(f"- x = {args.x}")
    lines.append(f"- cycles = {cycles}")
    lines.append(f"- seed = {args.seed}")
    lines.append(f"- token_bits = {args.token_bits}")
    lines.append("")
    lines.append(
        "Provider matching order is defined per block by a permutation seeded from the "
        "block phase digest. The order is not round-robin and can repeat across block "
        "boundaries."
    )
    lines.append("")
    lines.append("Permutation formula:")
    lines.append("")
    lines.append("$$")
    lines.append(r"\pi_B = Permute(perm_key, \Phi_{B \cdot x}), \quad idx_t = \pi_B[t \bmod x]")
    lines.append("$$")
    lines.append("")
    lines.append("## Block-level permutations")
    lines.append("")
    lines.append("| block B | pi_B (slot order 0..x-1) | matching order |")
    lines.append("| --- | --- | --- |")
    for block, perm in block_perms:
        order = " -> ".join(f"P{i}" for i in perm)
        lines.append(f"| {block} | {perm} | {order} |")
    lines.append("")
    lines.append("## Schedule (device-selected provider per cycle)")
    lines.append("")
    lines.append("| t | block | slot | idx (device routes to) |")
    lines.append("| --- | --- | --- | --- |")
    for t, block, slot, idx, _token, _server_tokens in rows:
        lines.append(f"| {t} | {block} | {slot} | {idx} |")
    lines.append("")
    lines.append("## Device tokens (verbatim)")
    lines.append("")
    lines.append("| t | device token | matches provider |")
    lines.append("| --- | --- | --- |")
    for t, _block, _slot, idx, token, _server_tokens in rows:
        token_hex = format_token(token, args.token_bits)
        lines.append(f"| {t} | `{token_hex}` | P{idx} |")
    lines.append("")

    for lane in range(params.x):
        lines.append(f"## Provider lane P{lane}")
        lines.append("")
        lines.append(f"| t | P{lane} token | match |")
        lines.append("| --- | --- | --- |")
        for t, _block, _slot, idx, _token, server_tokens in rows:
            token_hex = format_token(server_tokens[lane], args.token_bits)
            match = "match" if idx == lane else ""
            lines.append(f"| {t} | `{token_hex}` | {match} |")
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
