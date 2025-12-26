Project summary:
- Prime-Compound Phase-Lane Token Protocol (PCPL) design and validation docs.
- Main spec and pseudocode live in `papers/phase-shift-tokens.md`.
- Broader background notes in `papers/symmetric-tokenizer-circuit-concept.md`.
- Paper-style writeup with Mermaid in `papers/main-paper.md`.

Code and tooling:
- `demo/pcpl_cycle_test.py`: cycle-by-cycle PCPL simulation and validation.
  - Uses blake2b with length-prefixed encoding for H() to avoid ambiguous concatenation.
  - Default params: x=4, P/Q/R are small primes near 1e6, M=2^61-1.
  - Secret bouquets are generated deterministically from small primes.
  - Validates: permutation is a true per-block schedule, exactly 1-of-x match per cycle, and each provider once per full block; optional chaining divergence check.
- `demo/export_token_trace.py`: exports deterministic token trace tables to Markdown.
  - Default output: `papers/token-trace.md` (A4-friendly, per-lane tables).
  - Multi-configuration results: `papers/pcpl-results.md`.

How to run:
- `python3 demo/pcpl_cycle_test.py --cycles 200`
- Options: `--x`, `--token-bits`, `--seed`, `--verbose`, `--no-chaining-check`.
- `python3 demo/export_token_trace.py --blocks 4 --out papers/token-trace.md`

Next steps:
- Add more property checks (replay window, adversarial cross-lane attempts, larger x).
- If concrete parameter sets are chosen, document them and mirror in the demo defaults.
