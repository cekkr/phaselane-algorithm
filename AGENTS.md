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

# pcpl-evolvo notes
- When updating main-paper.md following latest evolvo-conclusions.md report, don't quote any file that is ignored in the repo, like the pcpl-evolvo/runs: they're simply not accessible, quoting them is dispersive. When updating the two md files, be "auto conclusive" inside the effective available files in the repo after a clean clone. 
- In main-paper.md in sections like "Latest Evolvo run: interpretation and constraints" don't write report about an execution itself (it's not conclusive, execution time etc), it's just relative and meaningless for the main paper.
- Don't report in main-paper.md Evolvo's GFSL algorithms: translate them in general pseudo code, also seen that many GFSL variable indices have no sense without an exact context.
- Assume that a syncro layer and a "GPS-precise timing" is available: making a syncro precise circuit is important to avoid desyncronizations or dead idles useful for attacker, but continuing to presume that may be possible without an external precise clock as reference is a waste of resource in computing a too much sensible circuit.
- Avoid in any case to find a solution that requires handshakes after the initial one, it's a big vulnerability and liability in the basis of the research.
- While elaborating evolvo run results for conclusions: convert the genetic code to pseudo code, then compare them between their scores and draw conclusions through narrative and logic point of view using codes and their statistical data for conclusive interpretations, obtaining improvements, weak points and future steps for development

# Phase lane dilemma
To having sense a phase lane circuit should be reach these goals:
- The provider circuit should be have the common key that, anyway, should be advantageous respect than a single line cycle computation but at the same time should be computational expensive enough to avoid an easy cloning
- If a user device is compromised, shouldn't be also the other twins. If a user device is compromised, should be difficult enough to require a constant parallel execution by adversial device.
- The boquet is a classic approach, but looks weak. It's needed an equation that returns a vector whose precision is needed to next step. A continuous "key computation debt".