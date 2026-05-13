# Evolvo Conclusions (Complete Full Run Synthesis)

Date: 2026-05-13

Status: Canonical repository-local synthesis for paper-facing claims. This file is self-contained: the paper does not need generated run folders to preserve the conclusions below.

## Executive Conclusion

The complete full Evolvo evidence strengthens the same architectural conclusion: PCPL's deployable hot path should stay sparse, deterministic, feed-forward, and provider-recomputable. Synchronization, route hardening, and backend promotion must live in separate supervisory layers. The run does not discover a final self-healing controller and does not beat the hand sparse baseline family.

The strongest evolved defender scores `0.493379`, with saturated protocol principles and zero observed token-recovery events in the valid rows. Its value is confirmatory rather than decisive: it lands in the one-active-compound region, below `reference-full`, `balanced`, and `minimal-cost` baselines. The best attacker still expresses pressure as lane/route inference from public structure, not accepted token material.

The paper-facing claim should therefore be conservative and stronger: evolution independently confirms sparse activation as the practical token-core shape, while exposing the unresolved frontiers: long-horizon drift, route/lane predictability, runtime headroom, and archive evaluability.

## Evidence Base

- Full rounds completed: `50`.
- Valid final metric rounds: `46`.
- Skipped rounds: `4`, all due to `missing-defender-metrics`.
- Valid rate: `0.920`.
- Trailing unevaluable rounds: `0`.
- Timeout recheck and rescue were used in all `50/50` rounds.
- Attacker-panel expansion collapsed before full expansion in `47/50` rounds.
- The selected attacker panel size was `1` in all rounds, although the requested panel size was `4`.
- Defender generation-0 survival count was `50/50`, and every defender stopped with `identical-generations`.
- Final valid scenarios covered `x=4`, `x=6`, and `x=8`, with base and shared-low-entropy variants.
- Timing assumptions used a `100 MHz` device, `300 MHz` provider, and `10 s` maximum timing horizon.

This is enough evidence to support architectural conclusions, but not enough to claim automatic optimization maturity. The search and selection process remained rescue-dominated, and four rounds had no final defender metrics. Future promotion must require final metrics, a wider stable attacker panel, and reduced timeout dependence.

## Statistical Summary

| signal | value | semantic reading |
| --- | ---: | --- |
| Defender score, valid rounds | mean `0.479707`, range `0.461891..0.493379` | narrow plateau with one sparse best point |
| Best evolved defender | `0.493379` | useful candidate, not a new score ceiling |
| Principle score | mean/min/max `1.0000` | one-of-$x$, block fairness, and permutation validity stay saturated |
| Security score | mean `0.998966`, min `0.996362` | collision/replay/shared-device failures are not the limiting factor |
| Token success | mean/min/max `0.0000` | no token-recovery events in this complete valid slice |
| Lane success | mean `0.169699`, range `0.109788..0.186886` | attacker pressure is route/lane inference |
| Attacker advantage | mean `0.003102`, max `0.010915` | small but nonzero public-structure bias |
| Sync score | mean `0.528126`, range `0.526718..0.530988` | local sync terms are stable but capped |
| Projected sync loss | mean `0.994362`, max `1.000000` | long-window model nearly saturates failure |
| Horizon sync score | mean `0.005638`, min `0.000000` | the token core does not solve long-horizon recovery |
| QFT score | mean `0.373274` | public-period margin remains weak |
| Linear-rank score | mean/min/max `1.0000` | linear-rank checks are saturated |
| Compare-$x$ score | mean/min/max `0.951852` | compare-$x$ is stable but not perfect |
| Cost score | mean `0.791585`, range `0.640710..0.890000` | score tracks active-compound sparsity |
| Runtime score | mean `0.108604`, range `0.044420..0.469895` | native runtime headroom remains weak |
| Device compound ratio | mean `0.378939`, range `0.200000..0.653255` | search mostly stays sparse-to-moderate |

Across valid rounds, defender score is strongly anti-correlated with active compound ratio (`-0.865`) and strongly correlated with cost score (`+0.865`). Runtime also helps (`+0.532`), but the dominant shape is still sparse activation. Adding active compounds did not buy compensating security, sync, or route-hardening gains in this objective family.

## Baseline Reading

| candidate family | mean score | interpretation |
| --- | ---: | --- |
| Reference full policy | `0.508161` | dense hand reference baseline |
| Balanced policy | `0.516961` | hand policy with moderate sparsity |
| Minimal-cost policy | `0.532565` | strongest hand baseline; one-active-compound direction |
| All valid evolved defenders | `0.479707` | broad search distribution below the hand baselines |
| Best evolved defender | `0.493379` | `-0.014783` vs reference-full, `-0.023583` vs balanced, `-0.039187` vs minimal-cost |

The run summary also reports a positive delta against an internal reference anchor signature. That anchor is not the same comparison as the hand baseline table above. For the paper, the baseline conclusion is the table conclusion: evolved candidates have not yet beaten the hand sparse policy family.

## Sparse Activation Result

| active compound ratio | valid rounds | mean defender score | interpretation |
| ---: | ---: | ---: | --- |
| `0.2000` | `6` | `0.487632` | best bucket; roughly one active compound out of five |
| `0.3333` | `1` | `0.475419` | isolated middle point |
| `0.4000` | `37` | `0.479109` | common plateau; useful but below the sparse best bucket |
| `0.4446` | `1` | `0.476393` | no compensating gain |
| `0.6533` | `1` | `0.461891` | weakest bucket |

The top six valid defenders all use ratio `0.2000`. The strongest dense-ish profile, ratio `0.6533`, is the weakest valid defender. This makes sparse bouquet activation a specification-level parameter, not a mere implementation optimization. A concrete PCPL profile should state both provisioned bouquet inventory size and active subset size.

## Algorithmic Result

The strongest evolved defender has `12` effective operations. Translated away from GFSL indices, its behavior is a low-bias sparse policy generator around a fixed PCPL arithmetic/hash scaffold:

```text
DefenderPolicy(public_phase, lane_id, slot, public_hints):
    carry = fixed_low_bias()

    active_level = sigmoid(carry + normalized_phase_b)
    kernel_id = bounded_selector(normalized_phase_c, coupled_phase_u1)
    state_mix = sigmoid(coupled_phase_u2 * coupled_phase_u3)
    exponent_mix = hash_mix(normalized_lane_id, normalized_slot)
    hash_round_limit = bounded_rounds(phase_mix(public_hints))
    bouquet_spread = sigmoid(block_phase_hint - normalized_lane_id)
    state_churn = modular_hash(coupled_phase_u1, constant_one)
    lane_salt = public_lane_salt(normalized_phase_b, slot_hint)
    token_scramble = hash_mix(normalized_phase_c, public_lane_hint)
    phase_jitter = bounded_mod(coupled_phase_u3, block_hint)

    return public_or_provider_observable_policy(
        active_level,
        kernel_id,
        state_mix,
        exponent_mix,
        hash_round_limit,
        bouquet_spread,
        state_churn,
        lane_salt,
        token_scramble,
        phase_jitter,
    )
```

The best semantic reading is not "complex controller discovered." It is "sparse policy around deterministic arithmetic/hash token derivation." The low active level drives one active compound per bouquet in the best profile, while the remaining controls are bounded selectors and mixers derived from public phase, lane, slot, and provider-observable hints.

The token-core pseudocode supported by this result remains:

```text
TokenCore(i, t, bouquets_i, public_policy):
    phase = PublicPhase(t, P, Q, R)
    active_A = choose_sparse_subset(bouquets_i.A, phase, i, public_policy)
    active_B = choose_sparse_subset(bouquets_i.B, phase, i, public_policy)
    active_C = choose_sparse_subset(bouquets_i.C, phase, i, public_policy)

    a_mix = modular_product(active_A, phase.a, public_policy)
    b_mix = modular_product(active_B, phase.b, public_policy)
    c_mix = modular_product(active_C, phase.c, public_policy)

    lane_value = bounded_hash_phase_mix(a_mix, b_mix, c_mix, phase.Phi)
    key = H(KDF || enc_i(i) || enc_t(t) || enc(lane_value) || phase.Phi)
    token = Trunc_k(H(TOK || key || enc_t(t) || phase.Phi))
    return token
```

Every input that changes `public_policy` must be public, time-derived, lane-local, provider-observable, or explicitly carried in the emitted message. Device-only history may update the device chain after emission, but it cannot be required for blind provider recomputation.

## Attacker Result

The strongest attacker scores `0.010915`, with mean lane success `0.186886`, mean token success `0.000000`, and maximum per-scenario advantage `0.028571`. Its effective behavior is a small route predictor:

```text
RouteProbe(public_phase, previous_route_hint, previous_token_hint):
    lane_guess = combine(previous_token_hint, public_phase_features)
    token_probe = combine(previous_token_hint, normalized_phase_b)
    confidence = combine(previous_route_hint, normalized_phase_c)
    return lane_guess, token_probe, confidence
```

The attacker does not recover accepted token material. It extracts weak route information from public phase and previous observable hints. That distinction matters: security work should not focus only on token guessing. The permanent attacker panel needs lane-prediction objectives, schedule-bias objectives, and shared-low-entropy stress cases.

Recommended route-hardening objectives:

- lane-salt diversity across providers and public epochs,
- schedule decorrelation visible in compare-$x$ profiles,
- bounded phase jitter that remains provider-recomputable,
- attacker panels specialized for lane prediction,
- separate reporting for lane success, token success, and final attacker advantage.

## Synchronization Result

Immediate protocol invariants remain clean: one-of-$x$, block-once, permutation validity, replay rejection, cross-lane collision rejection, timing rejection, attack rejection, and twin sync stay saturated in the valid evidence family.

The failure remains long horizon behavior. For the strongest defender, mean sync loss is `0.833428`, projected sync loss is `0.996167`, and horizon sync is `0.003833`. Across all valid rounds, projected sync loss averages `0.994362`.

Therefore PCPL should assume an external precise synchronization reference and use it deliberately. The synchronization supervisor should own drift estimates, acceptance windows, recovery limits, dead-idle avoidance, and fail-closed behavior. It must not introduce runtime challenge/response handshakes after initial provisioning.

## Runtime And Evaluability Result

Native execution is exercised but not solved. The strongest defender reaches native GPU share around `0.82` across its final scenarios, yet runtime score is only `0.469895`; the valid-round runtime mean is `0.108604`. The hand baselines report runtime score `1.000000`, so current evolved programs still need backend-specific promotion gates.

The process weakness is stronger than in-protocol weakness:

- `50/50` rounds used timeout rescue.
- `47/50` rounds collapsed before full attacker-panel expansion.
- `4/50` rounds lacked defender metrics.
- `50/50` defender selections had generation-0 survival.
- Attacker panels effectively evaluated one attacker instead of the requested four.

Future Evolvo work should treat evaluability as part of the objective, not as post-processing. A profile should not be promoted unless final metrics exist, timeout rescue is bounded, attacker-panel breadth is stable, and native backend coverage is measured separately from protocol correctness.

## Paper-Facing Claims Supported

- PCPL correctness does not require dense controller logic.
- The practical token core should be sparse, deterministic, feed-forward, and provider-recomputable.
- One-of-$x$ matching, per-block fairness, permutation validity, replay rejection, and cross-lane separation are stable in this evidence family.
- Token recovery is zero in this complete valid run, but the paper should still say "near-zero in tested evidence," not "impossible."
- Current attacker pressure is mainly lane/route inference from public timing and phase structure.
- Long-horizon synchronization is the dominant unresolved engineering frontier.
- A GPS-disciplined supervisory synchronization layer should be assumed and specified; it must fail closed instead of negotiating after provisioning.
- Evolutionary search is useful for finding circuit motifs and failure families; it is not yet evidence of a complete self-healing controller.
- The hand sparse `minimal-cost` baseline remains the benchmark to beat before claiming a new evolved algorithmic improvement.

## Recommended Next Updates

1. Promote `inventory_size` and `active_count` into the PCPL profile definition. Start concrete parameter work around one active compound per bouquet per cycle, with two active compounds as the first robustness comparison.
2. Keep the token core limited to phase extraction, sparse modular products, bounded mixing, KDF, truncation, and local state update.
3. Specify the GPS-disciplined synchronization supervisor with drift regimes, acceptance windows, recovery limits, dead-idle avoidance, and fail-closed behavior.
4. Add route-hardening objectives for lane prediction, schedule bias, public phase feature learning, and shared-low-entropy scenarios.
5. Split future reports into token-core invariants, route/lane inference, supervisory horizon sync, and runtime backend behavior.
6. Stabilize full attacker-panel evaluation so archive promotion does not depend on timeout rescue.
7. Keep `minimal-cost` as the hard baseline until an evolved profile beats it or justifies a clear security tradeoff it lacks.
