# Evolvo Conclusions (Run `20260422-062356-full`)

Date: 2026-04-23  
Source: `demo/pcpl-evolvo/runs/20260422-062356-full`

## Scope and data reliability

This run is sufficient to diagnose the current evolutionary pipeline behavior, but it is **not sufficient** for final semantic ranking of circuit families.

- `results.json` is still `status: running`, with `rounds_completed: 15` out of planned `36` and active batch `[15,16,17,18,19]`.
- Persisted artifacts exist for rounds `0000..0014` only.
- Archive volume for grounding: `15` round records, `180` defender generation rows, `120` attacker generation rows.

## Empirical core (grounding)

- Defender round score mean/min/max: `-3.000000 / -3.000000 / -3.000000`.
- Attacker round score mean/min/max: `0.000000 / 0.000000 / 0.000000`.
- Reference delta is null in all persisted rounds (`score_delta = 0.0` always).
- Scenario metric rows in round summaries: `0` (all rounds).
- `selection_panel.timeout_recheck = true` in `15/15` rounds.
- Panel size actually used: `1` for rounds `0..4`, then `4` for rounds `5..14`.
- Defender stop reason: `identical-generations` in `15/15` rounds.
- Attacker stop reason: mostly `identical-generations` (`11/15`), plus `flat-score-window` (`3/15`).

Internal (pre-selection) defender signal was still present:

- Defender generation best-score mean/min/max: `0.506567 / 0.500499 / 0.521657`.
- Inside each round, best score never improved after generation 0 (no-improvement ratio `1.0000`).
- Internal means:  
  `principle=1.0000`, `security=0.9981`, `sync=0.5660`, `stability=0.5786`, `cost=0.7015`, `attacker_adv=0.0057`,  
  `projected_sync_loss=0.9715`, `horizon_sync=0.0285`, `phase_error_control=0.5701`, `control_flow_score=0.1995`.

Runtime was active (not idle):

- Defender generation batch seconds mean/min/max: `59.82 / 36.55 / 112.06`.
- Native GPU share mean: `0.8230`.
- Mean stage eval counts per defender generation: `quick=58.5`, `mid=34.64`, `full=5.5`.

## New conclusions

### 1) The dominant failure is evaluator-timeout collapse, not circuit quality collapse

This run’s round-level scores are dominated by timeout cut scores (`-3.0` defender), with empty metric payloads.  
The current persisted results therefore describe a selection/evaluation bottleneck more than true circuit fitness.

### 2) The timeout recheck path currently preserves collapse instead of recovering from it

The reranking safeguard is triggered every persisted round, but final records still end with the same sentinel score family and zero metrics.  
Pragmatically, this means the fallback path is not restoring discriminative signal under current load.

### 3) Selection-stage workload is mismatched to timeout budget

The selection stage runs hard scenarios at full cycle fraction with higher key-variant breadth and attacker-panel coupling.  
Given observed batch times, that configuration is repeatedly pushed into timeout cuts, flattening round-level outcomes.

### 4) Circuit interpretation must be split into two layers

- What remains supported: invariants-related components stay high in internal stage (`principle`, `security`).
- What remains unresolved: long-horizon synchrony is still weak (`projected_sync_loss` high, `horizon_sync` low).

This is aligned with the paper direction: phase-control quality remains the key frontier, but this run cannot rank candidate families reliably until evaluator stability is fixed.

## Updates, fixings, and improvements (aligned with `papers/main-paper.md`)

### A) Add explicit evaluation-status semantics before scoring

- Distinguish `timeout`, `error`, and `valid` evaluation states.
- Do not collapse timeout into the same numeric channel as genuine low fitness.
- Persist timeout ratio per round and per stage as first-class diagnostics.

### B) Make timeout budgets adaptive to measured scenario cost

- Calibrate full-stage timeout from rolling runtime percentiles (separately for quick/mid/full and defender/attacker).
- Use stricter timeout only when the run shows stable throughput margins; otherwise expand budget temporarily.

### C) Make attacker-panel robustness progressive instead of all-at-once

- First pass: single-attacker direct score to establish a valid metric baseline.
- Second pass: panel worst-case reranking only for top-N survivors.
- If timeout rate spikes, auto-reduce panel breadth or key-variant count for that round.

### D) Gate archival promotion on metric presence

- Do not promote a round-level elite when `metrics=[]`.
- Force a reduced-cost reevaluation profile (lower variants / shorter cycles) until a non-empty metric vector is produced.

### E) Keep the paper’s phase-control upgrades, but apply them on valid metrics only

From the main-paper optimization direction (phase-error control, horizon-sync gating, attacker-coupled selection, anti-neutrality pressure):

- Keep phase-error control as an explicit objective.
- Keep horizon-sync gating, but only when evaluation status is valid.
- Keep attacker-panel-coupled ranking, but behind progressive budgeting.
- Keep anti-neutrality penalties, but disable them on timeout-only generations to avoid false stagnation signals.

### F) Improve circuit search space toward synchronization control logic

- Increase mutation templates for comparator/branch-driven resync controllers.
- Prioritize effective-op diversity in control-flow instructions, not only arithmetic/hash mixing.
- Track and reward measurable recovery behavior across perturbation windows.

## Next-run acceptance criteria

1. `timeout_recheck` should occur in at most `10%` of completed rounds.  
2. No completed round should end with `metrics=[]`.  
3. Round-level `score_delta_vs_reference` must become non-zero and positive in a majority of rounds.  
4. Synchronization targets should improve jointly: `projected_sync_loss_rate` down and `horizon_sync_score` up, while keeping `principle/security` saturation.
