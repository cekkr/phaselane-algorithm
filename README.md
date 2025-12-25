# phaselane-algorithm
Description and testing of Prime-Compound Phase-Lane Token Protocol (PCPL).

## Docs
- `papers/phase-shift-tokens.md`: main PCPL spec and pseudocode (phase clock, permutation schedule, bouquets, device/server flow).
- `papers/symmetric-tokenizer-circuit-concept.md`: broader concept notes.

## Demo
Cycle-by-cycle Python simulation of the PCPL algorithm with deterministic parameters.

```bash
python3 demo/pcpl_cycle_test.py --cycles 200
```

Notes:
- Hashing uses blake2b with length-prefixed encoding to avoid ambiguous concatenation.
- Tokens are truncated to the requested bit length; defaults are meant for validation, not production.
