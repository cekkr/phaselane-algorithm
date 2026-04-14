# Evolvo Conclusions (External Archive Aggregate)

Date: 2026-04-14  
Source scope: `demo/pcpl-evolvo/runs/external_archive` and `demo/pcpl-evolvo/runs/external_archive/runs`

## Data sufficiency verdict

Yes, there is enough data to update conclusions with semantic findings, with two caveats:

- The dataset is strong for **directional conclusions** (23 completed rounds, 324 scenario metric rows, 1503 generation-log rows).
- The dataset is not yet final for publication-level claims, because it mixes **two scoring eras** (before and after QFT/linear-rank/compare-x integration), so absolute score values are not directly comparable across all runs.

## Runs included

- Included archives with valid `round-results.json`:  
  `20260413-091027-full`, `20260413-150834-full`, `20260413-184702-full`, `runs/20260413-201028-full`, `runs/20260414-121658-full`
- Excluded from stats (missing usable round results):  
  `20260412-204828-full`

## Method (reproducible)

All statistics below were computed from per-round JSON artifacts (`round-results.json`) with a Python aggregation script run locally over the archive tree.  
Round-level metric values are means across each round's scenario rows.

## Aggregate empirical summary (23 rounds)

| metric | mean | min | max |
|---|---:|---:|---:|
| defender_score | 0.8073 | 0.7801 | 0.8753 |
| attacker_score | 0.0104 | 0.0000 | 0.0247 |
| defender_delta_vs_reference | +0.0380 | +0.0322 | +0.0509 |
| principle_score | 1.0000 | 1.0000 | 1.0000 |
| sync_score | 0.4656 | 0.4615 | 0.5259 |
| stability_score | 0.5400 | 0.5365 | 0.5900 |
| security_score | 0.9965 | 0.9918 | 1.0000 |
| cost_score | 0.9214 | 0.9214 | 0.9214 |
| runtime_score | 1.0000 | 1.0000 | 1.0000 |
| attacker_advantage_score | 0.0104 | 0.0000 | 0.0247 |
| sync_loss_rate | 0.8793 | 0.8767 | 0.8954 |
| projected_sync_loss_rate | 0.9472 | 0.7697 | 0.9569 |
| horizon_sync_score | 0.0528 | 0.0431 | 0.2303 |

## Score-era split (important)

Because scoring changed during development, score magnitudes split into two regimes:

- Pre QFT/linear-rank/compare-x scoring (7 rounds): mean defender score `0.8614`, mean delta vs reference `+0.0507`.
- Post QFT/linear-rank/compare-x scoring (16 rounds): mean defender score `0.7836`, mean delta vs reference `+0.0325`.

Interpretation: the raw score drop is expected after scoring rebalance and additional terms; comparisons should be made **within the same scoring era**.

## Mathematical semantics from the data

1. Protocol invariants are empirically robust.
- `principle_score = 1.0` in all analyzed rounds.
- `one_of_x_rate`, `block_once_rate`, `permutation_valid_rate`, and `attack_reject_rate` are effectively saturated.
- This supports the core deterministic schedule correctness claims.

2. Security envelope is strong, but not the active bottleneck.
- `security_score` remains high (`0.9965` mean).
- `attacker_advantage_score` is low (`0.0104` mean), but non-zero.
- Security improvements are incremental, not the main driver of total score variance.

3. Synchronization remains the primary mathematical weakness.
- `sync_score` and `stability_score` are much lower than principle/security.
- `sync_loss_rate` and `projected_sync_loss_rate` stay high in long-horizon projection.
- `horizon_sync_score` is low (`0.0528` mean), confirming timing/synchronization is the unresolved frontier.

4. QFT / linear-rank / compare-x terms are currently stronger as diagnostics than gradients.
- Available in 16 post-update rounds:
  - `qft_score = 0.4112` (constant in this dataset)
  - `qft_period_bits ≈ 64.94` (constant)
  - `linear_rank_score = 1.0` (constant)
  - `compare_x_score = 0.9535` (constant)
- These terms confirm paper-alignment checks, but their near-constant behavior in this archive means they currently add limited intra-run selection pressure.

5. Compare-x behavior shows better envelope with larger x in this dataset.
- Aggregated by scenario family:
  - `x4-fixed`: total `0.7870`, attacker_adv `0.0151`
  - `x6-generated`: total `0.7918`, attacker_adv `0.0117`
  - `x8-generated`: total `0.7999`, attacker_adv `0.0060`
- In these runs, larger x trends toward lower attacker advantage and slightly better total score, while sync/horizon still need work.

## Algorithmic-evolution semantics from generation logs

1. Plateau behavior is real and quantifiable.
- Defender logs: average 50 generations per round.
- Average best-score improvement events per round: `1.70`.
- Mean same-score step ratio: `96.44%`.
- Mean longest same-score streak: `45.7` generations (max `51`).

2. Mutation pressure is already very high during stalls.
- Defender mutation rate mean `0.918` (often near ceiling `0.99`).
- High mutation alone is not sufficient to escape flat fitness neighborhoods.

3. Staged evaluation is aggressive and throughput-oriented.
- Defender means: `quick_fraction 0.054`, `mid_fraction 0.199`, `quick_keep 0.762`, `mid_keep 0.150`.
- Logs show frequent quick-stage pruning and high unique evaluations.
- Despite this, late-generation score progress remains sparse.

4. Versus-reference gains come mostly from cost shaping, not sync breakthroughs.
- Mean per-round delta vs reference by component:
  - `cost_score: +0.3143`
  - `sync_score: +0.0021`
  - `stability_score: +0.0018`
  - `security_score: +0.0002`
  - `attacker_advantage_score: -0.0007` (lower is better)
- Practical meaning: evolver reliably finds cheaper/stable-enough policies, but has not yet materially reduced sync-loss dynamics.

## Notes to incorporate in `papers/main-paper.md`

1. Separate what is proven by invariants from what remains empirical.
- Keep strong claims for schedule/permutation/1-of-x correctness.
- Frame long-horizon synchronization as an open optimization problem, not solved.

2. Add a score-era/versioning note in empirical sections.
- Report that scoring changed (new paper-alignment terms + weight rebalance).
- Avoid cross-era absolute-score comparisons.

3. Clarify QFT/linear-rank/compare-x interpretation.
- In current evolvo runs these are mostly compliance diagnostics.
- To make them evolutionary drivers, they need scenario/genome variability (otherwise they saturate).

4. Emphasize horizon-sync as the top research objective.
- The largest weakness in this archive is projected sync loss.
- Main-paper future-work should prioritize drift modeling, resync policy tuning, and time-reference robustness.

5. Include parameter-scale caveat for QFT period.
- Observed period bits here are around 65 in the tested evolvo settings.
- Distinguish benchmark-scale parameters from larger production/paper parameter examples.

## Practical next protocol for stronger paper-grade evidence

1. Run post-update scoring campaigns only, then publish that subset as canonical.
2. Increase rounds per configuration and report confidence intervals across repeated seeds.
3. Add explicit sync-stress scenarios (clock drift, jitter, resync-window sweeps) and track reductions in projected sync-loss as the primary KPI.
4. Keep QFT/linear-rank/compare-x in reports, but inject parameter variability so those terms contribute meaningful selection gradients.
