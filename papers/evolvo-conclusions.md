# Evolvo Conclusions (Complete Full Run Synthesis)

Date: 2026-05-07 (updated with latest explorer-mode full evidence)

Status: This is the canonical repository-local synthesis for paper-facing claims. It is self-contained: the paper does not need ignored runtime folders to preserve the conclusions below.

## Executive Conclusion

The completed full Evolvo run strengthens the same design reading, but with better evidence quality: PCPL's hot token path should stay sparse, deterministic, and feed-forward, while long-horizon synchronization belongs in a separate GPS-disciplined supervisory layer. The protocol invariants are stable; token-recovery pressure stays near zero, with only isolated weak-profile events in low-entropy stress scenarios. The remaining risk is route/lane inference from public structure and timing, plus long-window drift behavior.

The run should not be presented as automatic discovery of a final controller. Baseline ordering varies with objective/version, but the stable pattern is unchanged: evolved defenders do not establish a robust ceiling above the hand sparse baselines. The correct conclusion is narrower and stronger: the evolutionary search independently converges toward sparse activation and exposes the failure frontier that must be handled outside the per-cycle token mixer.

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
| Token success | mean/min/max `0.0000` | no token-recovery events in this 35-round slice; see addendum for rare nonzero stress-case behavior in the latest dynamic/full evidence |
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
- One-of-$x$ matching, per-block fairness, permutation validity, replay rejection, and cross-lane separation are stable in this evidence family.
- Token-recovery signal remains near zero overall, but conclusions should avoid absolute-zero wording and keep low-entropy stress-scenario pressure in attacker panels.
- Long-horizon synchronization is the dominant unresolved engineering frontier.
- Current attacker pressure is mainly lane/route inference from public timing and phase structure, not token material recovery.
- Evolutionary search is useful for discovering circuit-family motifs and failure families; it is not yet evidence of a complete self-healing controller.
- Relative baseline position is objective-version dependent; in the latest explorer/full evidence, the best evolved defender remains below `reference-full`, `balanced`, and `minimal-cost`.

## Additional Conclusion From Latest Dynamic-Mode Full Evidence

This addendum captures the latest dynamic/full evidence family in a self-contained way (fitness schema: `auto-dynamic-full-5ad147bd8aea8067`).

- Completed rounds: `16/16` valid (`0` skipped), so claims remain evaluation-backed.
- Panel fragility is still high: timeout rescue was used in `16/16` rounds; progressive stop `timeout-collapse-before-expand` appeared in `12/16`.
- Best defender score: `0.492819`; baseline means: `reference-full=0.508161`, `balanced=0.516961`, `minimal-cost=0.532565`.
- Principle invariants remain saturated (`1.0000`) and security remains high (mean `0.998591`).
- Long-horizon sync is still the dominant unresolved frontier (`projected_sync_loss` mean `0.994471`, `horizon_sync` mean `0.005529`).
- Sparse activation remains the winning shape:
  - ratio `0.2000`: mean defender score `0.489340`,
  - ratio `0.4000`: mean defender score `0.480580`,
  - ratio `0.6667`: mean defender score `0.461385`,
  - score vs compound-ratio correlation: `-0.858`.
- Token success is near-zero but not absolute-zero:
  - round mean `0.000072`,
  - max `0.001157`,
  - the only nonzero event appeared in an `x8` quick base scenario under the weakest dense profile (`compound ratio 0.6667`).

Additional paper-facing conclusion: the right wording is "near-zero token recovery in this evidence family," not "impossible token recovery." Keep targeted low-entropy stress-scenario attacker panels as a permanent regression check, while maintaining the sparse feed-forward core and GPS-disciplined supervisory synchronization.

## Additional Conclusion From Latest Explorer-Mode Full Evidence

This addendum captures the latest explorer/full evidence family in a self-contained way (fitness schema: `auto-explorer-full-5ad147bd8aea8067`).

- Completed rounds: `16/16` valid (`0` skipped), so claims remain evaluation-backed.
- Panel fragility is still high: timeout rescue was used in `16/16` rounds; progressive stop `timeout-collapse-before-expand` appeared in `11/16`.
- Search plateau risk increased: generation-0 survivors were `15/16`, so most final selections stayed close to first-generation candidates.
- Best defender score: `0.485253`; baseline means: `reference-full=0.508161`, `balanced=0.516961`, `minimal-cost=0.532565`.
- Principle invariants remain saturated (`1.0000`) and security remains high (mean `0.998922`).
- Long-horizon sync is still the dominant unresolved frontier (`projected_sync_loss` mean `0.994508`, `horizon_sync` mean `0.005492`).
- Sparse activation remains the winning shape:
  - ratio `0.2000`: mean defender score `0.485253`,
  - ratio `0.4000`: mean defender score `0.479362`,
  - ratio `0.5918`: mean defender score `0.476515`,
  - ratio `0.6000`: mean defender score `0.475679`,
  - score vs compound-ratio correlation: `-0.622`.
- Token success is near-zero but not absolute-zero:
  - round mean `0.000062`,
  - max `0.000992`,
  - the only nonzero event appeared in `x8-generated:mid:shared-low-entropy:f70` (round `0003`, dense profile near compound ratio `0.592`).

Additional paper-facing conclusion: explorer mode reinforces the same architecture outcome (sparse feed-forward core plus external GPS-disciplined supervision), but it also highlights plateau sensitivity. Future runs should explicitly include anti-plateau gates and maintain low-entropy stress-scenario attacker checks as mandatory regressions.

## Recommended Next Updates

1. Keep the hot path sparse: start concrete parameter work around one active compound per cycle unless a future run beats the minimal-cost baseline with stronger security or sync evidence.
2. Split reports into `token-core invariants`, `route/lane inference`, `supervisory horizon sync`, and `runtime backend` sections.
3. Add explicit route-hardening objectives for lane prediction pressure.
4. Specify a GPS-disciplined supervisory synchronization layer with bounded drift and recovery windows, without post-initialization handshakes.
5. Stabilize full attacker-panel evaluation so archive promotion does not depend on timeout rescue.
6. Keep `minimal-cost` as the benchmark that evolved defenders must beat before claiming a new algorithmic improvement.
