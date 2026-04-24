# Evolvo Conclusions (Run `20260423-080945-full`, rounds `0..24`)

Date: 2026-04-24  
Source: `demo/pcpl-evolvo/runs/20260423-080945-full`  
Status: the run is still active; the conclusions below use the 25 round directories currently present (`round-0000` .. `round-0024`).

## Scope and reading stance

This run is sufficient for semantic conclusions about which PCPL circuit families are robust, which ones are merely cheap, and which ones fail practical evaluation.  
The goal here is not a leaderboard summary; it is to read the evolved circuits as design evidence for PCPL and for realistic hardware-style implementations.

## Grounding facts

- `25` rounds are available; `16` produced valid final defender metrics and `9` ended with `missing-defender-metrics` after `timeout-collapse-before-expand`.
- Best valid defender: `round-0002`, score `0.48830750`.
- Best attacker: `round-0022`, score `0.01529101`.
- In every round, defender generation `0` remained the round winner through the last generation (`25/25` rounds with no within-round improvement).
- On valid rounds, `principle_score` is always `1.0`, `linear_rank_score` is always `1.0`, and `compare_x_score` stays `0.9519`.
- The unresolved term is still long-horizon sync: valid-round `horizon_sync_score` mean `0.00511`, max `0.00604`.

## Circuit-family conclusions

### 1) The best defender is a fixed PCPL spine with a tiny biasing prelude

Across the highest-scoring valid rounds (`0002`, `0004`, `0010`, `0024`), the effective defender keeps the same scaffolded outputs:

- `active`
- `kernel`
- `state_mix`
- `exp_mix`
- `hash_rounds`
- `bouquet_spread`
- `state_churn`
- `lane_salt`
- `token_scramble`
- `phase_jitter`

What actually changes is the short prelude before that scaffold: usually 1-6 list/constant instructions such as `PREPEND(-1.0)`, `FIFO`, `FILO`, `LISTCOUNT`, or a trivial local call.

Meaning: Evolvo is not finding radically different token circuits. It is repeatedly selecting the same PCPL arithmetic datapath and only retuning the scalar biases that feed it.

### 2) The strongest practical motif is bouquet sparsification

The top three valid defenders (`round-0002`, `round-0004`, `round-0010`) all have:

- `cost_score = 0.8900`
- `operation_cost_score = 1.0000`
- `device_compound_ratio = provider_compound_ratio = 0.2000`

With `compound_count = 5`, that means the best circuits are effectively using about **1 active compound out of 5** per bouquet on average.

The next stable family (`round-0000`, `0006`, `0014`, `0024`) sits at:

- `cost_score = 0.7800`
- `device_compound_ratio = provider_compound_ratio = 0.4000`

That corresponds to about **2 active compounds out of 5**.

Meaning: the evolved implementation preference is not "use more bouquet structure for more strength." It is "keep the bouquet large as secret inventory, but activate only a sparse subset per cycle." For practical PCPL circuits, sparse bouquet selection should be treated as a first-class design parameter.

### 3) The evolved controllers prefer stable biasing over rich feedback

In the best defenders, the prelude often overwrites low-index controller inputs with constants or constant-like list outputs before the PCPL scaffold runs. The phase/lane/time signals still enter through untouched scaffold inputs, but the controller is mostly biasing a fixed mixer, not building a true stateful feedback law.

Meaning: current best circuits are better described as **biased selectors plus deterministic PCPL mixing**, not as full phase-adaptive controllers. This matters for the paper: the evidence supports compact feed-forward control in the token core, not sophisticated in-core branching.

### 4) Attackers learn lane exposure much more than token contents

The attacker elites are even narrower than the defenders. Their active core is almost always:

- `d$40 = ADD(d$6, d$0)`
- `d$41 = ADD(d$7, d$1)`
- `d$42 = ADD(d$8, d$2)`

So the attacker family is basically a linear public-phase predictor with a tiny constant seed. In valid rounds:

- `attacker_token_success_rate` stays `0.0`
- the non-zero attacker score comes from `attacker_lane_success_rate` rising slightly above baseline

Meaning: the hidden bouquet/twin-state path is not what attackers are exploiting here. The exposed surface is public schedule correlation and lane predictability. Paper language should reflect that the present attacker evidence is about **lane inference pressure**, not token recovery.

### 5) PCPL core correctness is easy to preserve; long-horizon self-healing is not

Valid defenders keep:

- `principle_score = 1.0`
- `security_score` very high (`0.9949` to `0.9998`)
- `phase_error_control_score` around `0.55`
- `sync_score` around `0.526` to `0.531`

But even the best valid rounds keep:

- `projected_sync_loss_rate` essentially saturated (`0.9940` to `1.0000`)
- `horizon_sync_score` near zero (`0.0000` to `0.0060`)

Meaning: the protocol core is robust as a token routing and secrecy mechanism, yet the evolved circuits do not become long-horizon drift controllers. Practical implementations should not assume the token-generation datapath alone will solve extended timing drift.

### 6) The search currently identifies motifs and failure modes, not steady improvement

Two search-pathology findings are too strong to ignore:

- all `25/25` rounds keep the generation-0 defender as the round winner
- `9/25` rounds fail final defender evaluation and are skipped with `missing-defender-metrics`, each preceded by `timeout-collapse-before-expand`

Meaning: the archive is successfully preserving usable motifs, but the live evolutionary step is not reliably improving them, and some candidates that look acceptable in progressive selection fail full evaluation. For paper purposes, this run is still valuable, but as evidence of **which circuit family survives** and **which family collapses**, not as evidence of continuous open-ended optimization.

## Algorithmic reading for PCPL

### A) PCPL wants a two-layer implementation

The evolved evidence points to a separation:

- a hot-path token core that is compact, feed-forward, integer-friendly, and sparse in active bouquet usage
- a slower supervisory layer that handles drift detection, resync policy, and mode switching

Trying to force full resynchronization intelligence into the same tiny controller that selects compounds and mixes lane state is not what the successful circuits are doing.

### B) Sparse activation is not a corner case; it is the dominant efficient pattern

A large bouquet inventory still matters because it gives hidden structure and selection space, but per-cycle evaluation should remain sparse. In practice, PCPL circuits should be built to choose a small active subset cheaply rather than evaluate every compound every cycle.

### C) Branch logic should sit at mode boundaries, not inside every token step

The search does not reward dense comparator/branch logic inside the token path. The useful control signal appears to be "which mode / which subset / which churn level," not a deeply branched per-cycle algorithm. This fits real hardware better: a deterministic datapath plus explicit mode switching is simpler, faster, and closer to what the good genomes are approximating.

### D) Lane hardening deserves more attention than token-format complication

Because attackers gain almost exclusively through lane prediction, practical hardening should prioritize:

- lane-salt diversity
- state churn diversity
- phase jitter / schedule decorrelation
- evaluation against attacker panels that focus on route inference, not only token guessing

### E) Full-scenario evaluability must be a hard gate

A circuit that cannot produce final metrics under the real evaluation budget is not a usable PCPL circuit, even if it looks good in progressive selection. Timeout-safe executability and final-metric availability should be treated as first-class acceptance conditions.

## Paper-facing implications

For `papers/main-paper.md`, this run supports these stronger claims:

- PCPL invariants and basic secrecy do not require dense or complicated control circuits.
- The most practical defender family is a sparse, feed-forward arithmetic PCPL spine with small bias terms.
- The main unresolved frontier is not token correctness but long-horizon synchronization supervision.
- The most realistic current attacker model is route/lane inference from public structure, not token inversion.
- Evolutionary search should be described as identifying robust circuit families and failure families, not as automatically synthesizing a complete self-healing controller.

## Recommended next protocol/evolver changes

1. Keep the current scaffolded PCPL spine, but add an explicit supervisory resync controller outside the hot token path.
2. Expose active compound count and subset policy as a formal design knob in the paper and in the evaluator.
3. Promote lane-inference resistance to a first-class attacker objective.
4. Hard-reject archive candidates that finish progressive selection but fail final metric generation.
5. Measure future runs by two separate criteria: `token-core efficiency/invariance` and `supervisory long-horizon sync recovery`.
