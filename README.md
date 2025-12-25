# phaselane-algorithm
Prime-Compound Phase-Lane Token Protocol (PCPL): a no-handshake token system
that emits one token per cycle and routes it to exactly one provider lane.
Only the chosen provider can validate the token for that cycle.

This repository contains the spec, background notes, and a deterministic
cycle-by-cycle demo implementation to validate correctness.

## Documents
- `papers/phase-shift-tokens.md`: complete PCPL spec and pseudocode.
- `papers/symmetric-tokenizer-circuit-concept.md`: broader concept notes.

## Goals and threat model (short)
PCPL targets a minimal, low-interaction setting:
- Device emits one token per cycle and routes it to one provider out of x.
- Providers validate locally; no challenge/response or runtime negotiation.
- A provider should not be able to compute other providers' tokens.

## High-level structure
PCPL is built from three layers:
1) Public phase clock (integer-only, CRT residues).
2) Hidden prime compounds per provider (three bouquets A/B/C).
3) Device-only seed evolution that chains all lanes.

## Step-by-step: what happens each cycle
The algorithm is deterministic and can be recomputed by the device and
each provider independently.

1) Phase clock (public)
   - a_t = (a0 + t) mod P
   - b_t = (b0 + t) mod Q
   - c_t = (c0 + t) mod R
   - u1 = (a_t * b_t) mod M
   - u2 = (b_t * c_t) mod M
   - u3 = (c_t * a_t) mod M
   - Phi_t = H(a_t || b_t || c_t || u1 || u2 || u3 || "PHASE")

2) Provider schedule (exactly 1-of-x)
   - Split time into blocks of size x.
   - For each block B, compute a permutation pi_B of {0..x-1}.
   - The slot s = t mod x determines the provider index: idx_t = pi_B[s].
   - This guarantees each provider appears exactly once per block.

3) Bouquet evaluation (provider secrets)
   - Each provider i has three secret bouquets: A_i, B_i, C_i.
   - Each bouquet is a list of compounds, each compound is a product of primes.
   - For each bouquet:
     acc = prod(pow(compound_j, e_j) mod M), where
     e_j = H(residue || u || j || "EXP") mod (M-1).
   - This yields EA_i(t), EB_i(t), EC_i(t).

4) Token derivation (provider i)
   - K_i(t) = H(EA_i || EB_i || EC_i || Phi_t || "KDF")
   - T_i(t) = Trunc_k( H(K_i || t || Phi_t || "TOK") )

5) Device output and chaining
   - The device only computes T_idx_t(t) and routes it to provider idx_t.
   - The device still evolves its internal seed using all lane states W[0..x-1],
     plus rolling products between adjacent lanes. This makes each prior token
     influence future states without computing all lanes every cycle.

6) Provider verification
   - Provider i recomputes T_i(t) locally and compares to the received token.
   - Exactly one provider should match for any given cycle.

## Why the "returns every x" property is exact
The permutation schedule ensures each provider appears once per block of x
cycles, which means:
- exactly one provider matches per cycle,
- each provider matches exactly once per block.

## Integer-only arithmetic
PCPL uses integer residues and modular exponentiation only:
- P, Q, R are coprime primes for the phase clock.
- M is a prime modulus for the multiplicative group.
- Exponents are reduced mod (M-1) to stay within F_M^*.

## Demo
Cycle-by-cycle Python simulation with deterministic parameters.

```bash
python3 demo/pcpl_cycle_test.py --cycles 200
```

What it validates:
- The permutation is valid and returns every x.
- Exactly 1-of-x providers match per cycle.
- Each provider matches exactly once per block of x cycles.
- Optional chaining divergence check.

Notes:
- The demo uses blake2b with length-prefixed encoding to avoid ambiguous
  concatenation.
- Tokens are truncated to the requested bit length; defaults are for validation.

## Repository layout
- `README.md`: human-readable overview (this file).
- `papers/phase-shift-tokens.md`: spec and pseudocode.
- `papers/symmetric-tokenizer-circuit-concept.md`: background concepts.
- `demo/pcpl_cycle_test.py`: deterministic validation script.

## Next steps (suggested)
- Add property tests for larger x and longer runs.
- Add replay-window checks and adversarial cross-lane attempts.
- If choosing concrete parameter sets, mirror them in the demo defaults.
