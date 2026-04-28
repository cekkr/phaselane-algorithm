# Evolvo Conclusions (Run `20260425-153845-full`, rounds `0..20`)

Date: 2026-04-28
Source: `demo/pcpl-evolvo/runs/20260425-153845-full`
Status: the run is still active. The configured target is `36` rounds; the conclusions below use the `21` round directories currently present (`round-0000` .. `round-0020`) and the archive updated at `2026-04-28T01:01:07Z`.

## Scope and reading stance

This run supersedes the earlier `20260423-080945-full` conclusions for paper-facing claims. It is not a clean final leaderboard, because more than half of the completed rounds fail final metric production. It is still useful design evidence: the valid rounds identify the circuit family that survives full evaluation, while the invalid rounds show where the current search process collapses under the real evaluation budget.

## Grounding facts

- `21` rounds are available; `9` produced valid final defender metrics and `12` were skipped as `missing-defender-metrics`.
- `11/12` skipped rounds explicitly report `timeout-collapse-before-expand`; all skipped rounds have `timeout_recheck = true` and `timeout_rescue_used = true`.
- Best valid defender: `round-0013`, score `0.48497968`, score delta vs reference `+0.02676134`.
- Best attacker: `round-0002`, score `0.01624339`.
- In every round, defender generation `0` remained the round winner through the last logged defender generation (`21/21` rounds with no within-round improvement).
- On valid rounds, `principle_score`, `linear_rank_score`, and `operation_cost_score` are always `1.0`. Round-mean `compare_x_score` is fixed at `0.95185185`.
- Valid-round `security_score` remains high: mean `0.99812948`, min `0.99458554`, max `1.0`.
- The unresolved term is still long-horizon sync: valid-round `horizon_sync_score` mean `0.00311237`, max `0.00604454`; valid-round `projected_sync_loss_rate` mean `0.99688763`.

## Circuit-family conclusions

### 1) The best defender is still a fixed PCPL spine with a tiny biasing prelude

The highest-scoring valid defender (`round-0013`) has only a short active prelude:

- `APPEND(-1.0)`
- `FIFO`

It then uses the canonical ten-output arithmetic PCPL suffix that dominates the valid family:

- `d$20 = ADD(d$0, d$1)`
- `d$21 = MOD(d$2, d$3)`
- `d$22 = MUL(d$4, d$5)`
- `d$23 = PCPL_HASHMIX(d$6, d$7)`
- `d$24 = PCPL_PHASEMIX(d$8, d$9)`
- `d$25 = SUB(d$10, d$6)`
- `d$26 = PCPL_MODHASH(d$3, d$11)`
- `d$27 = ADD(d$1, d$8)`
- `d$28 = PCPL_HASHMIX(d$2, d$9)`
- `d$29 = MOD(d$5, d$10)`

Other high-scoring valid rounds mostly change the prelude (`APPEND`, `PREPEND`, `FIFO`, `FILO`, `CALL`, `LISTCOUNT`) and keep the same output slots, with occasional low-index substitutions such as the `d$21` feed. The effective pattern is therefore not a newly discovered controller. It is a stable arithmetic PCPL datapath with small scalar/list biases ahead of it.

Meaning: Evolvo continues to select a compact PCPL spine and retune its inputs, rather than discovering radically different token circuits.

### 2) Sparse bouquet activation remains the strongest implementation motif, but the new run is more tiered

The best valid defender (`round-0013`) has:

- `cost_score = 0.8900`
- `device_compound_ratio = provider_compound_ratio = 0.2000`

With `compound_count = 5`, that means about **1 active compound out of 5** per bouquet on average.

The next valid families are also sparse, but less extreme:

- `round-0002`, `0007`, `0009`: ratio `0.3333`, or about **1.7 active compounds out of 5**
- `round-0000`, `0004`, `0005`, `0011`: ratio `0.4000`, or about **2 active compounds out of 5**
- `round-0008`: ratio about `0.6533`, or about **3.3 active compounds out of 5**, and it is the weakest valid defender in this run

Meaning: the practical preference is still "keep the bouquet as hidden inventory, but evaluate a small active subset." The new run weakens the older claim that the top few defenders all use exactly one active compound; the stronger and more robust claim is that the useful region is approximately one to two active compounds out of five, while denser use does not help in this scoring regime.

### 3) The evolved controllers are biased feed-forward selectors, not rich feedback laws

The valid defenders continue to rely on constants, list endpoints, and low-index arithmetic before the fixed PCPL suffix. The control metrics are nearly flat:

- valid-round `control_flow_score` mean `0.19283956`
- min `0.19260490`, max `0.19312798`
- valid-round `phase_error_control_score` mean `0.55097018`

Meaning: the successful circuits are best described as **biased selectors plus deterministic PCPL mixing**. The search is not finding dense branch logic or a full drift-adaptive controller inside the hot token path.

### 4) Attackers still learn lane exposure, not token contents

The attacker elites remain narrow. Their active core is usually a few additions around the public/derived lane features:

- `d$40 = ADD(...)`
- `d$41 = ADD(...)`
- `d$42 = ADD(...)`

The best attacker (`round-0002`) reaches score `0.01624339`, but the mechanism is lane exposure:

- valid-round `attacker_token_success_rate` is always `0.0`
- valid-round `attacker_lane_success_rate` mean `0.17391993`
- valid-round `attacker_lane_success_rate` max `0.20287698`
- valid-round scenario-level lane success reaches `0.27777778`

Meaning: the hidden bouquet/twin-state path is not being inverted. The exposed surface remains public schedule correlation and lane predictability. Paper language should continue to frame the empirical attacker pressure as **lane/route inference**, not token recovery.

### 5) PCPL core correctness is easy to preserve; long-horizon self-healing is not

Valid defenders keep:

- `principle_score = 1.0`
- `linear_rank_score = 1.0`
- `operation_cost_score = 1.0`
- `security_score` mean `0.99812948`
- `sync_score` mean `0.52927726`
- `phase_error_control_score` mean `0.55097018`

But long-horizon behavior remains poor:

- valid-round `projected_sync_loss_rate` mean `0.99688763`
- valid-round `horizon_sync_score` mean `0.00311237`
- best valid-round `horizon_sync_score` only `0.00604454`

Meaning: the protocol core is robust as a token routing and secrecy mechanism, but the evolved circuit should not be treated as a long-horizon drift controller. Resynchronization supervision still needs to sit outside the hot token datapath.

### 6) The search now shows stronger evaluability pressure than optimization progress

Two search-pathology findings are stronger in this run:

- `21/21` rounds keep the generation-0 defender as the round winner
- `12/21` rounds fail final defender metric production and are skipped as `missing-defender-metrics`

The later part of the run is especially concerning: `round-0014` through `round-0020` all fail final metric production.

Meaning: the archive is still useful for motif extraction, but the live evolutionary process is not reliably improving candidates and is increasingly spending work on candidates that cannot survive full evaluation. For paper purposes, this run supports claims about **robust circuit families, failure families, and evaluator gates**, not claims about continuous open-ended optimization.

## Algorithmic reading for PCPL

### A) PCPL wants a two-layer implementation

The evolved evidence points to a separation:

- a compact, feed-forward, integer-friendly token core with sparse bouquet activation
- a slower supervisory layer for drift detection, resync policy, mode switching, and timeout-safe recovery

Trying to force long-horizon resynchronization intelligence into the same tiny controller that selects compounds and mixes lane state is not what the successful circuits are doing.

### B) Sparse activation is a formal design knob

The paper should not describe bouquet size only as "more compounds means more strength." The more precise implementation parameter is active compound count per cycle. In this run, the useful region is about one to two active compounds out of five, with the best valid defender at one out of five.

### C) Branch logic belongs near mode boundaries

The search does not reward rich comparator/branch logic inside every token step. The useful control signal appears to be "which mode, subset, or churn level," not a deeply branched per-cycle algorithm. This supports a deterministic hot datapath plus explicit supervisory mode switching.

### D) Lane hardening deserves more attention than token-format complication

Because attackers gain through lane prediction while token success remains zero, practical hardening should prioritize:

- lane-salt diversity
- state-churn diversity
- phase jitter / schedule decorrelation
- attacker panels that focus on route inference, not only token guessing

### E) Full-scenario evaluability must be a hard gate

A circuit that cannot produce final metrics under the real evaluation budget is not a usable PCPL circuit, even if it looked acceptable in progressive selection. Timeout-safe executability and final-metric availability should be first-class acceptance conditions.

## Paper-facing implications

For `papers/main-paper.md`, this run supports these claims:

- PCPL invariants and basic secrecy do not require dense or complicated control circuits.
- The most practical defender family is a sparse, feed-forward arithmetic PCPL spine with small bias terms.
- Sparse bouquet activation should be described as a first-class implementation mechanism, with the current useful region around one to two active compounds out of five.
- The main unresolved frontier is not token correctness but long-horizon synchronization supervision and timeout-safe evaluation.
- The most realistic current attacker model is route/lane inference from public structure, not token inversion.
- Evolutionary search should be described as identifying robust circuit families and failure families, not as automatically synthesizing a complete self-healing controller.

## Recommended next protocol/evolver changes

1. Keep the current scaffolded PCPL spine, but add an explicit supervisory resync controller outside the hot token path.
2. Expose active compound count and subset policy as formal design knobs in the paper and evaluator.
3. Promote lane-inference resistance to a first-class attacker objective.
4. Hard-reject archive candidates that finish progressive selection but fail final metric generation.
5. Add a timeout/evaluability objective that penalizes candidates before they dominate late-round search.
6. Report future runs with two separate criteria: `token-core efficiency/invariance` and `supervisory long-horizon sync recovery`.
