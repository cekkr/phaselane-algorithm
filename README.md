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

## Validation results (sample runs)
Exact outputs from sample validation runs:

| Command | Output |
| --- | --- |
| `python3 demo/pcpl_cycle_test.py --cycles 2000` | `OK: cycles=2000 providers=4 blocks=500 token_bits=128` |
| `python3 demo/pcpl_cycle_test.py --x 6 --cycles 1200` | `OK: cycles=1200 providers=6 blocks=200 token_bits=128` |
| `python3 demo/pcpl_cycle_test.py --x 8 --cycles 1600 --seed 2024` | `OK: cycles=1600 providers=8 blocks=200 token_bits=128` |

## Token trace (device vs server, x=4)
Example trace showing the effective matching behavior. Parameters: `x=4`,
`token_bits=128`, seed `1337`. For each cycle, the device emits one token and
exactly one server token matches.

| t | block | slot | device idx | device token | server 0 token | server 1 token | server 2 token | server 3 token |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 3 | `0xaa81443db40b5b1c43097327166e0e02` | `0xeeafece1251ccf687691135f062cb4d7` | `0xed74cbe26554bf9a270f1d1d90dcb25d` | `0xf84da16f0a9d23ca92b9598c2cccc4fb` | `0xaa81443db40b5b1c43097327166e0e02` |
| 1 | 0 | 1 | 0 | `0x21faa3d7dacdd0e36103bdf69b2dbe77` | `0x21faa3d7dacdd0e36103bdf69b2dbe77` | `0xfcb1104a0c0ba7f9fa257abb65f4484f` | `0xb7f7f5fb372075bde5349158efa5fca9` | `0xcb440e82f41dd12c05d861a210208faa` |
| 2 | 0 | 2 | 2 | `0x888b21379781bc887f5d778c7b903179` | `0x88ad5efb2c5761de52f141d23bc88540` | `0x673bce6b3a80330a1421b1bcf7102326` | `0x888b21379781bc887f5d778c7b903179` | `0x39001496e37a7d61dcd0b67485376618` |
| 3 | 0 | 3 | 1 | `0xa591e8bf0845bb6a46322befc003b4b4` | `0x816f62b524482e9f535a2554d1b201a4` | `0xa591e8bf0845bb6a46322befc003b4b4` | `0x8593eef83487f09b5b30612843e00397` | `0xc204fbda03f97faa7bcf2a6b737960b3` |
| 4 | 1 | 0 | 2 | `0x5da9a61cd51d3ff367ba3113eb1d52ff` | `0xfbcc12ba1996112e91f04f5e5752007b` | `0xdfb35020bcc5eda2736a65a8660cd188` | `0x5da9a61cd51d3ff367ba3113eb1d52ff` | `0x0984a1b59d2493b60a85a6b186402bf0` |
| 5 | 1 | 1 | 0 | `0x8abe0866002f7ce535808b65879d17b6` | `0x8abe0866002f7ce535808b65879d17b6` | `0xc64b06a0c2b808e627c3fe2910f0536d` | `0x1715f8e9f51c0129907481780e0d03bc` | `0x7a42a0eae1727b8d0b41996c692ad5e3` |
| 6 | 1 | 2 | 1 | `0x39d33ef184e9b1ddde964a83f06fd92e` | `0xaf724603668afe9e530ec505758015fd` | `0x39d33ef184e9b1ddde964a83f06fd92e` | `0x9dd506512423c063f94c259c4517aff2` | `0xa2a9e6284b90f8fa643db155f801e918` |
| 7 | 1 | 3 | 3 | `0xe25bb134f354591bc4575918a9064674` | `0x69e26d5ca4b56e5ea99891eaaf15792c` | `0x5c0d14d5cabd38ca17d323d54d8f0bcf` | `0xffa8817c6c4d6b3806705a3446de34ba` | `0xe25bb134f354591bc4575918a9064674` |

## Repository layout
- `README.md`: human-readable overview (this file).
- `papers/phase-shift-tokens.md`: spec and pseudocode.
- `papers/symmetric-tokenizer-circuit-concept.md`: background concepts.
- `demo/pcpl_cycle_test.py`: deterministic validation script.

## Publication
Currently published on ResearchGate as method: [https://www.researchgate.net/publication/399075707_Prime-Compound_Phase-Lane_Token_Protocol_PCPL_for_Symmetric_Continuous_Tokenizer_Devices_Symmetric_continuous_encryption](https://www.researchgate.net/publication/399075707_Prime-Compound_Phase-Lane_Token_Protocol_PCPL_for_Symmetric_Continuous_Tokenizer_Devices_Symmetric_continuous_encryption).

Current version (v1) it's representative for the method but incomplete in possible approaches and explanations. Currently, even this repo has methodology limitations:
- Not implemented the algorithm device-side for determining exact current destination provider
- Not implemented real time complex primes management to test it without arbitrary values
- Lack of imagination about possible "prime compounds" (aka real numbers)
- It still doesn't handle quantum fourier transform difficulty
- Well, honestly current test env lacks totally of linear difficulty evaluation (pre-hash)
- Demonstrate clearly how algorithm (approach) changes with different number of peers (even prime^exponent ones)

## Next steps (suggested)
- Add property tests for larger x and longer runs.
- Add replay-window checks and adversarial cross-lane attempts.
- If choosing concrete parameter sets, mirror them in the demo defaults.
