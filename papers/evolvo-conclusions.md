# Evolvo Conclusions (Repository-Local Snapshot)

Date: 2026-04-28
Status: This document is the canonical repository-local synthesis for paper-facing claims. It is intentionally self-contained and does not depend on ignored runtime folders.

## Scope

These conclusions summarize the latest available co-evolution evidence already materialized inside tracked repository files. The aim is design guidance, not execution chronicle.

## Grounded signals

- Available rounds considered: `21`.
- Rounds with valid final defender metrics: `9`.
- Rounds skipped for missing final defender metrics: `12`.
- Timeout-collapse pressure dominates skipped rounds (`timeout-collapse-before-expand` is the main failure family).
- Best valid defender score: `0.48497968` (delta vs reference `+0.02676134`).
- Best attacker score: `0.01624339`.
- In all observed rounds, defender generation `0` remains the winner at round end.
- Core invariants stay saturated on valid rounds (`principle_score`, `linear_rank_score`, `operation_cost_score` remain at `1.0`; `compare_x_score` is stable near `0.9519`).
- Main unresolved frontier remains long-horizon sync (`projected_sync_loss_rate` mean `0.99688763`, `horizon_sync_score` mean `0.00311237`).

## Algorithmic conclusions

### 1) Stable core: sparse feed-forward PCPL spine

The robust defender family is not a dense branch controller. It is a compact arithmetic token spine with light biasing and sparse bouquet activation.

### 2) Sparse activation is a first-class knob

The strongest region is approximately `1..2` active compounds out of `5` per cycle. Higher activation density does not improve this scoring family.

### 3) Attacker pressure is route/lane inference

Attackers gain mainly through lane exposure from public structure. Token inversion remains negligible in this evidence family. Route-hardening must be explicit.

### 4) Long-horizon sync requires a separate supervisory layer

Token-core invariants are robust; drift recovery is not. Synchronization governance should be external to the hot per-cycle token path.

### 5) Evaluability is a hard acceptance condition

Candidates without final metrics are unusable for archive promotion. Timeout-safe completion and final-metric availability are mandatory, not optional.

## Practical constraints for implementation and search

- Preserve no-post-init-handshake operation. Any control signal required by provider recomputation must be provider-observable from public clock/state channels.
- Assume a precise external synchronization layer is available (GPS-grade timing reference). Use it directly instead of embedding fragile ad hoc re-sync complexity in the hot path.
- Keep branch-heavy logic at supervisory boundaries; keep per-cycle token path deterministic and compact.
- Keep random-search pressure high (mutation/novelty/diversity) while enforcing strict evaluability gates.

## Paper-facing claims supported by this snapshot

- PCPL correctness does not require dense controller logic.
- Practical defender families are sparse and feed-forward.
- Long-horizon synchronization remains the dominant unresolved engineering frontier.
- Current attacker realism is lane/route inference, not token recovery.
- Evolutionary search is best interpreted as circuit-family discovery plus failure-family discovery, not automatic synthesis of a complete self-healing controller.

## Recommended next updates in pcpl-evolvo

1. Enforce provider-observable input contract in evaluation/scaffolds.
2. Keep strict archive gates for final-metric availability.
3. Increase attacker-panel emphasis for lane-inference pressure.
4. Separate reporting into `token-core invariants` vs `supervisory sync recovery`.
5. Maintain a stochastic-first research lane (high mutation/novelty) alongside evaluability audit lanes.
