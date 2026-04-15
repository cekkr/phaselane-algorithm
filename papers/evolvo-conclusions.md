# Evolvo Conclusions (Run `20260414-185147-auto`)

Date: 2026-04-15  
Source: `demo/pcpl-evolvo/runs/external_archive/20260414-185147-auto`

## Scope and sufficiency

This archive is sufficient for semantic conclusions about the current evolutionary behavior (14 rounds, 252 scenario rows, 715 defender generation rows, 223 attacker generation rows).  
It is especially useful for diagnosing why the search stalls even when compute usage remains high.

## Empirical core (for grounding)

- Defender score mean/min/max: `0.78198 / 0.78009 / 0.78572`.
- Attacker score mean/min/max: `0.01766 / 0.00362 / 0.02472`.
- Defender beats attacker in all rounds (`14/14`), but late-round margin degrades.
- Delta vs reference remains positive (`+0.03235` mean), driven mostly by cost term.

Component means (current run):

- `principle_score = 1.0000` (saturated).
- `security_score = 0.9941` (high).
- `cost_score = 0.9214` (saturated and constant).
- `sync_score = 0.4629`, `stability_score = 0.5377` (still weak).
- `projected_sync_loss_rate = 0.9551`, `horizon_sync_score = 0.0449` (primary unresolved issue).
- `qft_score = 0.4112`, `linear_rank_score = 1.0000`, `compare_x_score = 0.9535`.

## Semantic conclusions

### 1) The search is not compute-starved; it is gradient-starved

Inside rounds, CPU work is continuous (high eval counts, no underutilization boost), but fitness movement is minimal:

- Defender flat-score ratio: `96.07%`.
- Defender longest no-improvement streak: mean `46.36` generations (max `51`).
- Defender improvements happen early (mostly gen `0-9`), then nearly stop.

Meaning: the current objective/scenario landscape has broad neutral plateaus. More raw mutation or workers does not solve this by itself.

### 2) Late-round lock-in is real and damaging

Rounds `6..13` collapse to a single round-level phenotype:

- Defender score fixed at `0.78009453` every round.
- Attacker score fixed at `0.02471958` every round.
- Metric fingerprint identical across all those rounds.

Compared to rounds `0..5`:

- Defender mean drops from `0.78449` to `0.78009` (`-0.56%`).
- Attacker mean rises from `0.00825` to `0.02472` (`+199.66%`).

Meaning: the run converges to a stable but strategically weaker basin, not to a stronger equilibrium.

### 3) Current gains over reference are mostly economic, not synchrony breakthroughs

Average delta (current vs reference anchor):

- `cost_score: +0.3143` (dominant gain).
- `sync_score: +0.0022`, `stability_score: +0.0019` (very small).
- `horizon_sync_score: +0.0031` (small).

Meaning: evolution is excellent at cheaper circuits, but weak at discovering better long-horizon sync behavior.

### 4) QFT / compare-x are useful diagnostics, but weak evolutionary drivers here

- `qft_score` varies a little by scenario, but round means stay near `0.4112`.
- `compare_x_score` and `linear_rank_score` are near-saturated and nearly constant.

Meaning: these terms validate properties, but currently add limited within-round selection gradient.

### 5) Effective circuit motifs are narrow

Effective defender instruction sequences are short and repetitive (mean effective length `15.93`, only 7 unique effective op sequences across 14 rounds).  
Top effective ops are arithmetic/hash and simple list mechanics (`ADD`, `MOD`, `PCPL_HASHMIX`, `PCPL_PHASEMIX`, `PCPL_MODHASH`, `CLONE`, `FILO`), with very little effective branching/comparator behavior.

Meaning: the evolver is mostly optimizing a compact arithmetic pipeline; it is not reliably discovering richer control logic for drift/resync adaptation.

## Practical circuit/algorithm changes (actionable)

### A) Add explicit phase-error control structure to the genome target

Introduce a “phase-error controller” subcircuit objective (not only generic sync score):

- Compute bounded phase error `e_t` between expected and observed lane timing.
- Reward corrective action monotonicity (`|e_{t+1}| < |e_t|`) across perturbation windows.
- Penalize oscillatory corrections (sign-flip churn without net improvement).

Why: this creates a local gradient for sync recovery, instead of waiting for coarse total-score changes.

### B) Make long-horizon sync a first-class optimization stage

Add a hard-gated stage before final ranking:

- Reject/penalize genomes with `projected_sync_loss_rate > threshold` (dynamic threshold by round percentile).
- Increase weight of `horizon_sync_score` when a round becomes flat for N generations.

Why: this directly breaks the “cheap but drift-prone” attractor that dominates late rounds.

### C) Force adversarial pressure coupling during defender selection

During defender ranking, inject attacker-conditioned penalty from current attacker elites:

- Penalize defender candidates by worst-case attacker lane success over a small attacker panel.
- Keep a dedicated “anti-attacker” elite slice (not merged with pure-score elites).

Why: rounds 6+ show defender/attacker co-locking into a weaker equilibrium; this coupling prevents defender-only complacency.

### D) Expand control-flow mutation operators for resync logic

Bias mutation templates toward comparator/branch patterns on timing/state variables:

- Prefer templates using compare/threshold + bounded fallback transitions.
- Add micro-macros for “if drift high -> resync path else steady path”.

Why: effective genomes currently underuse control logic; sync robustness needs conditional behavior, not only arithmetic mixing.

### E) Add anti-neutrality penalties at phenotype level

Track metric-fingerprint repetition and penalize prolonged neutrality:

- If scenario-level metric vector repeats for K generations, add novelty pressure on sync/horizon subspace.
- Reward measurable movement in `sync_score`, `horizon_sync_score`, or attacker suppression even when total score is flat.

Why: prevents endless genotype churn with unchanged phenotype (observed in rounds 6..13).

## Alignment with paper purpose

For `papers/main-paper.md` narrative goals (finding strong and weak circuit families):

- Strong claim supported: invariant correctness and economic efficiency are robustly evolvable.
- Weak point identified: long-horizon synchronization remains the main unresolved algorithmic frontier.
- New design direction: move from “general evolutionary pressure” to “phase-control-aware evolutionary pressure”, so evolved circuits include explicit drift-correction behavior.

## Recommended next run protocol

1. Keep this run as baseline lock-in example (`20260414-185147-auto`).
2. Run a new campaign with the five changes above, same scenario families, same hardware profile.
3. Promote success criteria from raw total score to a joint target:
   `projected_sync_loss_rate ↓`, `attacker_advantage_score ↓`, while preserving `principle/security` saturation.
4. Report not only best round score, but also “post-gen10 improvement count” to verify stall reduction.
