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

## Evolvo full co-evolution run: `20260430-223959-full`

Source:
`demo/pcpl-evolvo/runs/20260430-223959-full`

The run directory is still marked `running`, but the evidence used here is only
the completed `round-results.json` set for rounds `0000..0024`. Rounds
`0025..0029` contain only `round-progress.json` markers and are not included in
the statistics below.

Run configuration summary:

- profile: `full`
- seed: `9490397`
- planned rounds: `36`
- completed and valid rounds: `25/25`
- population/generations: defender `152 x 52`, attacker `104 x 16`
- evaluator backend: `kompute`, native mode, preferred device `rocm`
- round parallel plan: `5` concurrent lanes, `3` workers per round
- completed-round window: `2026-04-30T22:26:51Z` to `2026-05-02T02:37:54Z`

### Evidence summary

The completed run validates the PCPL invariants very strongly, but it does not
solve long-horizon synchronization. The score ceiling is dominated by projected
sync loss, sparse/dense bouquet cost, and evaluator runtime pressure.

| metric | mean | min | max | interpretation |
|---|---:|---:|---:|---|
| defender score | 0.478504 | 0.468951 | 0.487908 | narrow plateau across valid rounds |
| principle score | 1.000000 | 1.000000 | 1.000000 | schedule invariants saturated |
| security score | 0.998517 | 0.994586 | 1.000000 | no practical token inversion found |
| sync score | 0.528241 | 0.525437 | 0.531512 | weak because horizon term is near zero |
| cost score | 0.766794 | 0.670000 | 0.890000 | sparse activation is rewarded |
| runtime score | 0.222373 | 0.065582 | 0.500005 | execution budget remains limiting |
| stability score | 0.536114 | 0.532953 | 0.540476 | bounded by sync loss |
| projected sync loss | 0.995123 | 0.991744 | 1.000000 | dominant unresolved weakness |
| horizon sync score | 0.004877 | 0.000000 | 0.008256 | long-window behavior is not solved |
| linear rank score | 0.999990 | 0.999956 | 1.000000 | pre-hash rank remains effectively full |
| compare-x score | 0.951852 | 0.951852 | 0.951852 | fixed by the selected scenario family |
| qft score | 0.373571 | 0.372165 | 0.378611 | low because it includes horizon sync |
| control-flow score | 0.192910 | 0.192496 | 0.193571 | evolved defenders are not branch-rich |
| attacker advantage | 0.004439 | 0.000000 | 0.016243 | mostly lane inference, not token recovery |

Across the `150` final scenario metric rows (`25` rounds x `6` scenarios), the
hard protocol checks are saturated:

| invariant/security check | mean | min | max |
|---|---:|---:|---:|
| one-of-x rate | 1.000000 | 1.000000 | 1.000000 |
| block-once rate | 1.000000 | 1.000000 | 1.000000 |
| permutation valid rate | 1.000000 | 1.000000 | 1.000000 |
| attack reject rate | 1.000000 | 1.000000 | 1.000000 |
| twin sync rate | 1.000000 | 1.000000 | 1.000000 |
| timing reject rate | 1.000000 | 1.000000 | 1.000000 |
| cross-lane collision rate | 0.000000 | 0.000000 | 0.000000 |
| replay rate | 0.000000 | 0.000000 | 0.000000 |
| shared-device match rate | 0.000000 | 0.000000 | 0.000000 |

This means the simulation found no failure of the core PCPL routing and token
separation properties. The unresolved issue is not "does a provider match
exactly once per block"; it is "can a practical circuit keep the phase pipeline
inside a long runtime budget without accumulating unacceptable drift".

### Baselines and score interpretation

The run reports three hand-coded fixed-policy baselines:

| baseline | mean score | practical meaning |
|---|---:|---|
| `reference-full` | 0.5082 | dense paper-reference policy |
| `balanced` | 0.5170 | intermediate policy |
| `minimal-cost` | 0.5326 | sparse 1-active-compound policy |

The best evolved defender (`0.487908`) improves over its same-round reference
anchor by `+0.028941`, but it does **not** beat the fixed-policy baseline table
(`-0.020253` versus `reference-full`, `-0.0447` versus `minimal-cost`). This is
important for paper wording: the evolutionary run is strong evidence for design
motifs and failure modes, not proof that the searched GFSL controller is already
globally optimal.

### Best defender

Best defender: round `0003`, score `0.48790812047632276`, robust score
`0.4877864302143618`, signature `d6d8447924aab2f1ad06eb212ee87528`.

Mean metrics for this defender:

- principle `1.0000`
- security `0.9997`
- sync `0.5278`
- stability `0.5353`
- phase-error control `0.5514`
- control-flow `0.1930`
- cost `0.8900`
- projected sync loss `0.9940`
- horizon sync `0.0060`
- attacker advantage `0.00094`
- device/provider compound ratio about `0.20`

Effective genome:

```text
d$24 = PCPL_PHASEMIX(d$8, d$9)
FUNC d&0
d!0 = PREPEND(-1.0)
d$0 = FIFO(d!0)
d$20 = ADD(d$0, d$1)
d$21 = MOD(d$2, d$3)
d$22 = MUL(d$4, d$5)
d$23 = PCPL_HASHMIX(d$6, d$7)
d$25 = SUB(d$10, d$6)
d$26 = PCPL_MODHASH(d$3, d$11)
d$28 = PCPL_HASHMIX(d$2, d$9)
d$27 = CALL(d&0)
```

Algorithmic reading:

- The evolved circuit keeps a compact PCPL arithmetic spine rather than a rich
  feedback controller.
- The key outputs map to the simulator's practical control vector:
  active ratio, kernel selector, state mix, exponent mix, hash-round count,
  bouquet spread, state churn, lane salt, token scramble, and phase jitter.
- The best genome leaves no evidence that heavy per-cycle branching is useful
  in the hot path. Its `control_flow_score` is low and almost flat across the
  run.
- The strongest motif is sparse activation. A `0.20` compound ratio means one
  active compound out of five on the evaluated scenarios. The hidden bouquet
  remains larger than the active subset; the circuit spends only a small active
  slice each cycle.
- The best genome uses `d$9` (`last_token_hint` in the simulator) in two control
  outputs. That is acceptable only if the previous emitted token, or a derived
  public hint, is visible to every validator that must recompute the current
  control path. If PCPL keeps point-to-point lane delivery, future evolved
  controllers must be constrained to provider-observable inputs.

### Sparse activation buckets

| device compound ratio | rounds | mean defender score | reading |
|---:|---|---:|---|
| 0.2000 | `0003`, `0023` | 0.487241 | best region; one active compound out of five |
| 0.3333 | `0012`, `0020`, `0022` | 0.477123 | sparse, but weaker attacker/cost tradeoff |
| 0.4000 | `0006`, `0007`, `0008`, `0009`, `0011`, `0013`, `0014`, `0015`, `0016`, `0017`, `0018`, `0019`, `0021`, `0024` | 0.479735 | common plateau; about two active compounds out of five |
| 0.6000 | `0000`, `0001`, `0002`, `0004`, `0005`, `0010` | 0.473407 | densest completed bucket and lowest mean score |

Round-level score correlation is strongly negative with device/provider compound
ratio (`-0.773`) and strongly positive with cost score (`+0.773`). This does not
mean "always use one compound" for every deployment; it means the current
objective rewards an architecture where bouquet size is secret inventory and
active compound count is a separate per-cycle budget knob.

### Strongest attacker

Best attacker: round `0020`, score `0.016243386243386244`, signature
`b5cc30b5e97a2561b070325044f3ed84`.

Effective genome:

```text
d!0 = APPEND(2.0)
d$0 = FILO(d!0)
d$1 = FILO(d!0)
d$40 = ADD(d$6, d$0)
d$41 = ADD(d$7, d$1)
d$42 = ADD(d$8, d$2)
```

The attacker family is also simple: it is a public-feature lane predictor. The
best attacker reaches lane-success `0.2029` on the round mean, while token
success is `0.0000` for that strongest attacker. Across all valid rounds,
token-success mean is only `0.000045` and the observed max is `0.001134` in the
low-bit attack benchmark. The practical attack surface identified by the run is
therefore lane/route inference from public timing and phase features, not
inversion of bouquet-derived token material.

### Native execution and circuit practicality

Final scenario metrics show substantial native Kompute use, but not full GPU
coverage:

- aggregate GPU dispatches: `1,005,817`
- aggregate CPU fallback dispatches: `568,974`
- aggregate GPU dispatch share: `0.6387`
- mean per-scenario GPU share: `0.6529`

This matters for hardware/circuit design. A PCPL circuit that is elegant on
paper but pushes many operations into unsupported CPU fallback is not a practical
continuous tokenizer. The hot path should prefer operations with native
coverage: integer arithmetic, modular arithmetic, bounded selectors, fixed hash
mixers, and explicit state registers. List/function machinery can be useful for
search expression, but it should be compiled away or moved out of the per-cycle
hardware path.

### Search/evaluator issues

The run improved evaluability versus the older `20260425-153845-full` evidence:
all `25` completed rounds have valid final metrics. However, the search process
is still under severe timeout pressure:

- timeout recheck: `25/25`
- timeout rescue used: `25/25`
- progressive stop reason `timeout-collapse-before-expand`: `20`
- generation-0 defender survived final selection in `7/25` rounds
- defender generation logs: mean timeout ratio `0.6831`, mean valid ratio
  `0.0251`, mean mutation rate `0.8902`
- attacker generation logs: mean timeout ratio `0.9284`

The evaluator is therefore good enough to identify stable motifs, but not yet a
clean open-ended optimizer. Future archive promotion should hard-gate on final
scenario metrics, low timeout ratio, and native execution coverage, not only
score.

### Paper-facing conclusions

1. Core PCPL invariants are robust in the completed run: exact one-of-x,
   per-block fairness, replay rejection, cross-lane separation, and twin-state
   consistency all saturate.
2. The best practical token core is sparse and feed-forward. Bouquet size and
   active compound count should be separate parameters.
3. The fixed `minimal-cost` policy currently outperforms evolved GFSL
   controllers under the baseline table, so the paper should not overclaim
   evolutionary superiority.
4. Long-horizon synchronization is the dominant unresolved practical weakness.
   It should be handled by an explicit supervisory layer rather than hidden
   inside the hot token derivation path.
5. Attacker pressure should focus on lane/route inference as a first-class
   benchmark. Token guessing did not become the meaningful attack path in this
   run.
6. Native execution coverage is a circuit requirement. Operations that force
   CPU fallback should be penalized or compiled out before claiming hardware
   suitability.

### Potential improvements to implement next

- Formalize `active_count` as a public implementation knob:
  `active_count = 1 + floor(alpha * (compound_count - 1))`, with `alpha`
  produced by a small bounded controller and with a deployment floor/ceiling.
- Keep the hot token core branch-light: phase residues, selected bouquet
  products, bounded kernel selection, KDF, token hash, and state register update.
- Add a supervisory resync layer with explicit inputs: clock drift estimate,
  missed-token count, recent reject density, native execution saturation, and
  attacker lane-pressure score.
- Add route-hardening objectives: lane-salt diversity, per-block schedule
  decorrelation, phase-jitter bounds, and attacker panels specialized for lane
  prediction.
- Split future scoring into at least two reported scores:
  `token-core correctness/efficiency` and `supervisory horizon-sync recovery`.
- Penalize candidates early when they require CPU fallback, exceed timeout
  budgets, or lack final scenario metrics.

### Issues to avoid

- Do not describe dense bouquet evaluation as automatically stronger. In this
  run, denser activation lowered the score because cost and drift dominated.
- Do not treat QFT/linear-rank scores as evidence of full security. They validate
  important public-period and pre-hash properties, but they are nearly constant
  under this scenario family and provide limited search gradient.
- Do not conflate same-round reference-anchor improvement with beating the
  fixed-policy baseline table.
- Do not call the evolved circuit a complete resynchronization solution. The
  horizon-sync score remains near zero.
- Do not evaluate attackers only by token guesses. Lane inference is the
  stronger empirical signal.
- Do not allow search to use non-provider-observable inputs in provider-side
  token derivation. A candidate can look valid in a centralized simulator while
  violating the blind-provider contract.
