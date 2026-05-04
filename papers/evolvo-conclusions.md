# Evolvo Conclusions (Complete Full Run Synthesis)

Date: 2026-05-04

Status: This is the canonical repository-local synthesis for paper-facing claims. It is self-contained: the paper does not need ignored runtime folders to preserve the conclusions below.

## Executive Conclusion

The completed full Evolvo run strengthens the same design reading, but with better evidence quality: PCPL's hot token path should stay sparse, deterministic, and feed-forward, while long-horizon synchronization belongs in a separate GPS-disciplined supervisory layer. The protocol invariants are stable; the search did not find token recovery attacks. The remaining risk is route/lane inference from public structure and timing, plus long-window drift behavior.

The run should not be presented as automatic discovery of a final controller. The best evolved defender beats the reference and balanced baselines, but it does not beat the hand minimal-cost baseline. The correct conclusion is narrower and stronger: the evolutionary search independently converges toward sparse activation and exposes the failure frontier that must be handled outside the per-cycle token mixer.

## Evidence Base

- Full co-evolution rounds completed: `35`.
- Valid final defender metric rounds: `35`.
- Skipped or trailing unevaluable rounds: `0`.
- Selection rescue was used in all `35` rounds; attacker-panel expansion stopped with `timeout-collapse-before-expand` in `30` rounds.
- Defender generation-0 survival count: `0/35`, so the final selections were not simply first-generation plateaus.
- Evaluation scenarios represented `x=4`, `x=6`, and `x=8`, with base and shared-low-entropy variants in final candidate metrics.
- Timing assumptions in the run used `100 MHz` device, `300 MHz` provider, `10 s` absolute reference window, and `16 s` maximum timing horizon.

The important quality change from the earlier snapshot is evaluability: every archived round has final metrics. The important remaining process limitation is panel fragility: full attacker-panel selection repeatedly needed rescue logic, so future claims should continue to require final metric availability and should not treat panel timeouts as benign.

## Statistical Summary

| signal | value | semantic reading |
| --- | ---: | --- |
| Defender score, all valid rounds | mean `0.468920`, range `0.462577..0.483386` | narrow plateau; one clear best but no large breakthrough |
| Best evolved defender | `0.483386` | improves over reference and balanced baselines |
| Best attacker | `0.010325` | small attacker advantage, not token recovery |
| Principle score | mean/min/max `1.0000` | one-of-$x$, per-block fairness, and permutation validity are saturated |
| Security score | mean `0.998039`, min `0.996558` | collision/replay/shared-device failures are not the limiting factor |
| Token success | mean/min/max `0.0000` | evolved attackers did not recover token material |
| Lane success | mean `0.174070`, range `0.158399..0.183721` | attacker pressure is lane/route inference around chance-like rates |
| Attacker advantage | mean `0.005884`, max `0.010325` | route leakage exists but remains small in this scoring family |
| Sync score | mean `0.525490`, range `0.523823..0.527059` | local sync terms are stable but capped |
| Projected sync loss | mean/min/max `1.0000` | long-horizon model saturates failure |
| Horizon sync score | mean/min/max `0.0000` | long-window recovery is not solved |
| Cost score | mean `0.686090`, range `0.560000..0.890000` | score is strongly driven by active compound sparsity |
| Runtime score | mean `0.325151`, range `0.063986..0.525787` | runtime headroom remains weak under native execution |
| Native GPU share | mean `0.655035`, range `0.240942..1.000000` | Kompute path is exercised, but backend coverage is uneven |

Across all valid rounds, score is strongly anti-correlated with active compound ratio (`-0.864`) and positively correlated with cost score (`+0.864`). In this evaluator, adding active compounds did not create compensating security or sync gains.

## Baseline Reading

| candidate family | mean score | interpretation |
| --- | ---: | --- |
| Reference full policy | `0.469218` | canonical reference anchor |
| Balanced policy | `0.478018` | stronger hand policy with moderate sparsity |
| Minimal-cost policy | `0.495370` | best hand baseline; one-active-compound direction |
| All evolved defenders | `0.468920` | broad search distribution remains near reference |
| Best evolved defender | `0.483386` | `+0.014168` vs reference, `+0.005368` vs balanced, `-0.011983` vs minimal-cost |

The best evolved defender is useful evidence, but not a new score ceiling. Its main value is confirmatory: it lands in the same sparse region as the minimal-cost policy while retaining saturated correctness and near-saturated security. The minimal-cost baseline must remain a first-class benchmark for future runs.

## Sparse Activation Result

| active compound ratio | rounds | mean defender score | interpretation |
| ---: | ---: | ---: | --- |
| `0.2000` | `1` | `0.483386` | best evolved point; roughly one active compound out of five |
| `0.4000` | `5` | `0.475170` | second-best region; still sparse |
| `0.5998..0.6000` | `24` | about `0.4677` | common plateau; weaker than sparse variants |
| `0.6058..0.6720` | `4` | about `0.4662` | no compensating gain from density |
| `0.8000` | `1` | `0.462577` | weakest bucket |

The semantic conclusion is clear: sparse bouquet activation is not merely an implementation optimization; it is the winning shape under the current objective. Dense activation spends budget without improving the observed correctness, security, or sync frontier.

## Algorithmic Result

The strongest evolved defender has a compact effective program (`12` effective operations). Its behavior is best described as a sparse arithmetic/hash token spine, not as a branch-heavy controller.

Generalized form:

```text
carry <- previous compact state
phase_terms <- modular_add_mul_sub(public_phase, lane_local_inputs, carry)
mixed_terms <- hash_mix_and_phase_mix(phase_terms, lane_salt, compact_state)
lane_value <- modular_hash(mixed_terms)
token <- domain_separated_kdf_and_truncate(lane_value, phase, cycle)
```

This is compatible with the PCPL construction: the hot path remains deterministic, provider-recomputable for the provider's own lane, and independent of post-initialization handshakes. The evolved result does not justify embedding complex recovery logic in the token core. If a future candidate needs control hints, those hints must be public, provider-observable, or already part of mirrored lane-local state.

## Attacker Result

The strongest attacker has score `0.010325`, lane success mean `0.179941`, and token success `0.000000`. Its highest pressure is still lane/route inference, not token inversion. In scenario terms, the attacker sometimes gains above chance on generated/shared-low-entropy conditions, but it does not produce accepted token material.

This means security work should not focus only on token guess resistance. Route-hardening needs explicit objectives:

- lane-salt diversity across providers and epochs,
- schedule decorrelation visible in compare-$x$ profiles,
- bounded phase jitter that remains provider-recomputable,
- attacker panels specialized for lane prediction, not only token prediction,
- reporting that separates lane success, token success, and final attacker advantage.

## Synchronization Result

Immediate invariants remain clean: twin sync, timing rejection, attack rejection, resync success, one-of-$x$, block-once, and permutation validity all reach `1.0000` in the best defender. The failure is long horizon behavior: best-defender projected sync loss is `1.0000`, horizon sync is `0.0000`, and mean sync loss is `0.833428`.

Therefore PCPL should assume an external precise synchronization reference and use it deliberately. The supervisory layer should own drift estimates, miss windows, recovery mode limits, and route-pressure response. It must not introduce runtime challenge/response handshakes after the initial provisioning step, because that would violate the no-handshake research basis and add an attack surface.

## Runtime And Evaluability Result

Native execution is present but not yet a solved engineering layer. The best defender reaches native GPU share near `0.8405`, but runtime score is only `0.254621`; the all-round runtime mean is `0.325151`. Selection rescue in every round also shows that full-panel evaluation remains too brittle for unattended archive promotion.

Future Evolvo work should treat evaluability as part of the objective:

- every promoted candidate must have final metrics,
- timeout-rescue paths should be reduced and reported separately,
- full attacker-panel evaluation should become stable enough to avoid rescue-dominated selection,
- runtime headroom should be optimized without increasing hot-path branch complexity,
- native GPU coverage should be measured separately from protocol quality.

## Paper-Facing Claims Supported

- PCPL correctness does not require dense controller logic.
- The practical token core should be sparse, deterministic, and feed-forward.
- One-of-$x$ matching, per-block fairness, permutation validity, replay rejection, cross-lane separation, and token non-recovery are stable in this evidence family.
- Long-horizon synchronization is the dominant unresolved engineering frontier.
- Current attacker pressure is mainly lane/route inference from public timing and phase structure, not token material recovery.
- Evolutionary search is useful for discovering circuit-family motifs and failure families; it is not yet evidence of a complete self-healing controller.
- The best evolved defender should be reported as better than `reference-full` and `balanced`, but not better than `minimal-cost`.

## Recommended Next Updates

1. Keep the hot path sparse: start concrete parameter work around one active compound per cycle unless a future run beats the minimal-cost baseline with stronger security or sync evidence.
2. Split reports into `token-core invariants`, `route/lane inference`, `supervisory horizon sync`, and `runtime backend` sections.
3. Add explicit route-hardening objectives for lane prediction pressure.
4. Specify a GPS-disciplined supervisory synchronization layer with bounded drift and recovery windows, without post-initialization handshakes.
5. Stabilize full attacker-panel evaluation so archive promotion does not depend on timeout rescue.
6. Keep `minimal-cost` as the benchmark that evolved defenders must beat before claiming a new algorithmic improvement.
