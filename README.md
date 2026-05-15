# phaselane-algorithm

Prime-Compound Phase-Lane Token Protocol (PCPL): a deterministic no-handshake
token protocol where a device emits one token per cycle and exactly one
provider lane can validate it.

This repository contains protocol papers, deterministic validation scripts, and
the Evolvo synthesis conclusions used for current design decisions.

## Current status (May 13, 2026)

Canonical status is defined by:
- `papers/main-paper.md` (Version 1.8).
- `papers/evolvo-conclusions.md` (repository-local full-run synthesis).

Current paper-facing position:
- Core invariants are stable in tested evidence: exact 1-of-x matching,
  per-block fairness, permutation validity, replay rejection, and cross-lane
  separation.
- Evolved defenders confirm sparse activation as the best practical shape, but
  do not beat the hand sparse baseline (`minimal-cost` family).
- Observed attacker pressure is mainly lane/route inference from public
  structure; accepted-token recovery stays at zero in the complete valid
  evidence slice.
- Long-horizon synchronization drift remains the dominant unresolved weakness.
- Practical architecture is split into:
  1. sparse feed-forward token core,
  2. GPS-disciplined synchronization supervisor,
  3. route-hardening monitor,
  4. runtime/backend audit gates.

## Protocol goals and constraints

- No runtime challenge/response after initial provisioning.
- One token emitted per cycle; exactly one provider match per cycle.
- Providers must recompute locally from public data and their lane-local
  secrets only.
- Any control value that affects provider token derivation must be public,
  provider-observable, lane-local, or explicitly carried with the token.
- Post-init handshake-based recovery is out of scope by design.

## Protocol shape (high level)

1. Public phase clock from coprime residues (`P`, `Q`, `R`) and modular mixes.
2. Device-only per-block lane permutation enforcing exact 1-of-x routing.
3. Per-lane secret bouquets (A/B/C) with sparse active subset evaluation.
4. Domain-separated KDF/token hashing and truncation.
5. Device-only state evolution/chaining after token emission.

## Documents

- `papers/main-paper.md`: current paper and implementation interpretation.
- `papers/evolvo-conclusions.md`: canonical synthesis and constraints.
- `papers/phase-shift-tokens.md`: main protocol spec and pseudocode.
- `papers/symmetric-tokenizer-circuit-concept.md`: broader concept notes.
- `papers/pcpl-results.md`: deterministic multi-configuration snapshots.
- `papers/token-trace.md`: exported per-lane deterministic traces.

## Demo and validation

Main deterministic validator:

```bash
python3 demo/pcpl_cycle_test.py --cycles 200
```

Useful options:
- `--x`
- `--token-bits`
- `--seed`
- `--verbose`
- `--no-chaining-check`

Token trace export:

```bash
python3 demo/export_token_trace.py --blocks 4 --out papers/token-trace.md
```

What `demo/pcpl_cycle_test.py` checks:
- per-block schedule is a true permutation,
- exactly 1-of-x provider match per cycle,
- each provider appears once per full block,
- optional device-chaining divergence checks.

## Evolvo project

Evolvo tooling lives in `demo/pcpl-evolvo/` and is used for design-space
exploration, not as a correctness proof replacement.

Quick run:

```bash
python3 demo/pcpl-evolvo/run_experiments.py --profile fast --rounds 1
```

For paper claims, prefer `papers/evolvo-conclusions.md` as the canonical
repository-local synthesis rather than transient run-folder artifacts.

## Repository layout

- `README.md`: project snapshot and quickstart.
- `papers/`: spec, paper, conclusions, and trace/results material.
- `demo/pcpl_cycle_test.py`: deterministic cycle-by-cycle validator.
- `demo/export_token_trace.py`: deterministic trace exporter.
- `demo/pcpl-evolvo/`: co-evolution experiments and analysis tooling.

## Next steps (aligned with current conclusions)

- Promote profile parameters explicitly: bouquet `inventory_size` and
  per-cycle `active_count` (start with active=1, compare active=2).
- Add stronger route-hardening objectives and attacker panels focused on lane
  prediction and schedule bias.
- Specify and test GPS-disciplined synchronization supervisor regimes
  (steady/recovery/fail-closed) without introducing post-init handshakes.
- Keep runtime/backend evaluability as a separate promotion gate from token
  correctness.
- Extend property checks to larger `x`, replay windows, and adversarial
  cross-lane attempts, then mirror chosen concrete parameters in demo defaults.
