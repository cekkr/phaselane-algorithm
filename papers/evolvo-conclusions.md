# Evolvo Conclusions (Run 20260406-140503-fast)

Date: 2026-04-06  
Run analyzed: `demo/pcpl-evolvo/runs/20260406-140503-fast`

## Scope and data used

- Continuous mode snapshot analyzed at `2026-04-06T16:40:44`.
- Iterations completed: `19`.
- Grid size configured: `64` combinations.
- Combinations with results/archives: `19`.
- Each evaluated combination currently has `rounds_completed=1`.

## Empirical findings

### Pros

- Protocol correctness is consistently strong:
  - mean `principle_score = 1.0000` across analyzed combinations.
  - exact 1-of-x and block fairness constraints are satisfied in practice for evaluated runs.
- Security behavior is generally good:
  - mean `security_score = 0.9923` (min `0.9840`, max `0.9980`).
  - `shared_device_match_rate = 0.0` in all analyzed best defenders.
  - `brute_force_resistance_score = 1.0` in all analyzed best defenders.
- Cost/performance tradeoff improved compared to fixed baselines:
  - best defender score `0.946921` (`p18-g26-i18-ap12-ag10-e12`).
  - baseline `minimal-cost` score is `0.9227`, so best observed gain is `+0.0242` (about `+2.62%` relative).

### Cons and risks

- Synchronization robustness is still the weakest area:
  - mean `sync_score = 0.8547`.
  - mean `sync_loss_rate = 0.4150` (range `0.3167..0.4813`).
  - `resync_success_rate = 1.0` indicates recovery works, but drift events are too frequent.
- Adversarial pressure is non-zero and variable:
  - mean `attacker_advantage_score = 0.0231` (max `0.0480`).
  - indicates some evolved defenders still leak useful structure under attacker co-evolution.
- Coverage is still partial:
  - only `19/64` combinations were executed at this snapshot.
  - no full sweep yet (`sweeps_completed=0`), so conclusions are promising but not final.
- Depth is limited:
  - one round per combination is insufficient for strong convergence claims.

## Suggested improvements for `papers/main-paper.md`

These updates should be added in future revisions (especially sections 8, 9, and 10).

1. Add a dedicated empirical methodology subsection in section 8.
- Include co-evolution setup (defender vs attacker), scenario profiles, and archive/resume behavior.
- Report how many combinations and rounds were executed before deriving conclusions.

2. Add a quantitative score breakdown table to section 8/10.
- Include `principle`, `sync`, `security`, `cost`, `attacker_advantage`.
- Publish min/mean/max across runs, not only single best snapshots.

3. Expand section 9 (limitations) with synchronization stress findings.
- Explicitly state that observed `sync_loss_rate` remains material despite successful resync windows.
- Clarify that this is currently a practical tuning target, not a solved property.

4. Add attacker-evolution limitations and open problems.
- Mention that attacker advantage can still rise in some configurations.
- Propose stronger anti-structure defenses (lane KDF hardening, schedule jitter constraints, stricter anti-collision penalties).

5. Add reproducibility references.
- Link to run artifact structure (`archive.json`, per-round reports, leaderboards, conclusions files).
- Add exact command lines used for the published benchmark snapshot.

## Engineering improvements applied after this analysis

To reduce random dispersivity and improve CPU utilization in future runs, the implementation was updated:

- Persistent parallel worker pools are now reused across generations/rounds.
  - reduces repeated process spawn overhead.
  - measured benchmark on the same workload improved from `23.98s` to `18.87s` real time (~`21.3%` faster).
- Focused evolution strategy added:
  - adaptive mutation schedule based on stagnation,
  - parent selection focused on top-ranked pool,
  - local refinement pressure around high-quality genomes.
- New tuning controls exposed in CLI:
  - `--parent-pool-ratio`, `--stagnation-patience`, `--mutation-floor`, `--mutation-ceiling`, `--mutation-step`.
- Statistical predictive staged evaluation added:
  - quick/mid/full cycle fractions with predictive cuts (`--quick-cycle-fraction`, `--mid-cycle-fraction`, keep ratios),
  - novelty-aware ranking to prioritize non-duplicate genomes and prune expected/no-new candidates.
  - benchmark with staged mode and key variants: `off=36.55s` vs `process(8 workers)=18.93s` real time (~`48.2%` faster).
- Multi-key validation added in staged evaluation:
  - each scenario can be expanded to multiple key-generation/key-sharing variants (`--key-variants`) to stress validity and attacker resistance.
- Long-horizon timing projection added:
  - default `10s` projection with simulated frequencies (`--device-mhz=100`, `--provider-mhz=300`),
  - projected sync-loss and horizon sync metrics are now tracked in scoring outputs.

## Next recommended run protocol

1. Complete at least one full sweep (`64/64`) before drawing comparative claims.
2. For top combinations, run at least `3-5` rounds each to test stability and attacker adaptation.
3. Track and publish:
- score variance over rounds,
- sync-loss trend over rounds,
- attacker-advantage trend over rounds,
- runtime/cost trend with process workers enabled.

This file is intentionally incremental and should be revised after each major continuous run.
