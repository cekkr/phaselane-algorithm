# PCPL multi-configuration results (deterministic)

This file collects deterministic validation outputs from `demo/pcpl_cycle_test.py`
across multiple configurations. It is meant to be a compact companion to
`papers/token-trace.md`.

## Peer-count sweep (fixed primes, seed=1337)

Command:
`python3 demo/pcpl_cycle_test.py --compare-x 2,3,4,5,6 --linear-report --analysis-window 64 --qft-report`

| x | chain width (x-1) | QFT period bits | QFT period (decimal) |
|---:|---:|---:|---|
| 2 | 1 | 61 | 2000146002862007326 |
| 3 | 2 | 62 | 3000219004293010989 |
| 4 | 3 | 62 | 4000292005724014652 |
| 5 | 4 | 63 | 5000365007155018315 |
| 6 | 5 | 63 | 6000438008586021978 |

Linear pre-hash metrics (all x above, 64-cycle window):
- A/B/C: unique=64/64, rank_mod2=4/4, rank_mod65537=4/4

## Generated primes and compound modes (x=4)

Each run uses generated coprimes for P/Q/R (and M) and a generated prime pool
for compounds. All runs validate permutation, 1-of-x matching, and chaining.

| seed | compound mode | compound offset | P | Q | R | M | QFT period bits | QFT period (decimal) |
|---:|---|---:|---:|---:|---:|---:|---:|---|
| 1337 | blend | 0 | 2096669299 | 1747608157 | 1866608729 | 1273159183829412833 | 95 | 27358185054648849675767961788 |
| 2024 | semiprime | 0 | 1423693267 | 1141001293 | 1348017509 | 2083707438551447381 | 93 | 8759071917926854366514362316 |
| 4242 | offset | 17 | 1492027703 | 1497078911 | 1415283803 | 1408207852224782437 | 94 | 12645182665728960170139598796 |
| 9001 | prime-power | 0 | 1472641301 | 1773408209 | 1301135711 | 1671108227926378139 | 94 | 13592153759865553508995561196 |

Run template (adjust seed/compound mode as needed):
`python3 demo/pcpl_cycle_test.py --x 4 --seed SEED --cycles 64 --prime-mode generated --prime-bits 31 --modulus-bits 61 --compound-mode MODE --compound-offset 17 --compound-prime-bits 12 --compound-pool-size 20 --linear-report --analysis-window 64 --qft-report --show-params`
