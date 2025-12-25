# Prime-Compound Phase-Lane Token Protocol (PCPL)

## 1) Goal and threat model

### Goal

A device generates **one token per cycle**. The device routes the token to **one provider** (out of `x`, default 4). Only that provider can validate it; the other providers would compute different tokens for that same cycle.

**No interaction requirement:**
No runtime challenge/response, no synchronization negotiation, no “server status” exchange. A server validates by locally recomputing what it expects for that device at cycle `t`, using shared parameters and its own secret share.

### Threat model (minimal)

* A provider should **not** be able to compute tokens for other providers (“alternative lane execution”).
* Observing its own accepted tokens should not reveal other lanes.
* The public “phase clock” should not become a vulnerability (prime-only indexers are predictable; we avoid that).

---

## 2) High-level structure

There are three layers:

1. **Public phase clock** (integer-only): CRT-style residues over coprime primes + 3-product coupling.
2. **Hidden prime compounds** per provider: three independent “bouquets” (A,B,C) of secret *compounds*, each compound being itself a product of primes (recursive products).
3. **Device-only seed evolution** (continuative hashing): device updates one lane per cycle but evolves its internal seed using all lanes’ stored states (so stale/wrong lanes still matter). This matches your continuative seed approach.

---

## 3) Public parameters (shared by device and all providers)

* `x` = number of providers (default 4)

* Primes for phase counters: `P, Q, R` pairwise coprime (choose them also coprime with `x`)

* A prime modulus `M` for modular multiplication/exponentiation (details below)

* Cryptographic primitives:

  * `H()` = hash / sponge / dynamic hash circuit family (can be your evolving hash library)
  * `Trunc_k()` = truncate to k bits (token size, e.g. 128 or 256)

* Shared phase starts: `a0, b0, c0`

* Cycle definition: `t` is a monotonic counter or epoch index (server and device must interpret `t` consistently; server may accept a small ±window to tolerate drift).

---

## 4) Provider secrets (hidden “recursive products”)

For each provider `i ∈ {0..x-1}`, define **three independent secret bouquets**:

* `BouquetA_i`, `BouquetB_i`, `BouquetC_i`

Each bouquet is a list of **compounds**:

* A compound is: `C = ∏_{j} p_j^{e_j}` where each `p_j` is a prime (not equal to `M`) and `e_j ≥ 1`.
* A bouquet is: `Bouquet = [C_0, C_1, ..., C_{m-1}]`

This is your “recursive products”: primes → compounds → bouquet.
The secrecy is **which primes**, **which exponents**, and **which grouping into compounds**.

> Number-theory correctness note: we do not rely on “factoring hardness” of revealed integers; we never reveal the integer bouquet value. We only ever use it inside modular arithmetic + hash, where the hidden compound structure remains hidden.

---

## 5) Public phase clock (CRT + 3-product coupling)

### Phase residues (integer-only)

At cycle `t`:

* `a_t = (a0 + t) mod P`
* `b_t = (b0 + t) mod Q`
* `c_t = (c0 + t) mod R`

Since `P,Q,R` are coprime, the combined state `(a_t,b_t,c_t)` has period `P·Q·R`.

### 3-product coupling

Compute:

* `u1 = (a_t * b_t) mod M`
* `u2 = (b_t * c_t) mod M`
* `u3 = (c_t * a_t) mod M`

Compress:

* `Φ_t = H( a_t || b_t || c_t || u1 || u2 || u3 || "PHASE" )`

This keeps linear stepping, but avoids “pure modular periodic output” by hashing, consistent with your dynamic hashing direction.

---

## 6) Lane schedule: “returns every x” (mathematically exact)

You wanted: *the phase is right only 1 time to x*, and **returns every x** while the device is always right but with different servers.

We enforce this *exactly* by scheduling in **blocks of x cycles**.

Define:

* `B = floor(t / x)`  (block index)
* `s = t mod x`       (slot inside block)

For each block `B`, the device constructs a **permutation** `π_B` of `{0..x-1}`.
Then:

* `idx_t = π_B[s]`

Because `π_B` is a permutation, every provider appears **exactly once per block** → *exactly 1 match per cycle* and *exactly 1 match per provider per x cycles*.

### How to build π_B without leaking other providers’ schedule

`π_B` can be device-only (providers don’t need to know it). Two options:

* **x=4 fast path:** choose one of the 24 permutations using a hash-derived index (constant-time table).
* **general x:** Fisher–Yates shuffle driven by a PRF stream.

Either way, the “return every x” property is *mathematically guaranteed* by the permutation definition.

---

## 7) Provider token derivation using hidden compounds (number-theory clean)

### Group assumptions

Choose `M` prime. Then the multiplicative group `F_M^*` has order `M-1`.
For any base `g ≠ 0 (mod M)`, `g^(k mod (M-1)) mod M` is consistent (Fermat).

So we enforce:

* all primes in compounds are not `M`
* exponents are reduced mod `(M-1)` when used as exponents

### Bouquet evaluation

We evaluate each bouquet into a field element:

* `EA_i(t) = EvalBouquet(BouquetA_i, a_t, u1, M)`
* `EB_i(t) = EvalBouquet(BouquetB_i, b_t, u2, M)`
* `EC_i(t) = EvalBouquet(BouquetC_i, c_t, u3, M)`

Then derive:

* `K_i(t) = H( EA_i(t) || EB_i(t) || EC_i(t) || Φ_t || "KDF" )`
* `T_i(t) = Trunc_k( H( K_i(t) || t || Φ_t || "TOK" ) )`

Device outputs `T_{idx_t}(t)` and routes it to provider `idx_t`.

Provider `i` validates by recomputing `T_i(t)` and comparing.

---

## 8) “Every token is needed for the next one” without computing x lanes

The server does not need the device’s internal chaining. The *device* needs it to satisfy your “every token matters” rule.

Device maintains:

* `W[0..x-1]` = last computed token for each provider lane (stateful registers)
* `S` = device-only evolving seed (continuative state)

At cycle `t`:

1. compute `Φ_t`
2. compute `idx_t` via the permutation schedule
3. compute only:

   * `W[idx_t] = T_{idx_t}(t)`
4. evolve seed using all lane states + 3-lane products:

   * `m1 = (W[0] * W[1]) mod M`
   * `m2 = (W[1] * W[2]) mod M`
   * `m3 = (W[2] * W[3]) mod M` (extend as rolling chain for x>4)
   * `S ← H( S || W[0]||...||W[x-1] || m1||m2||m3 || Φ_t || "EVOLVE" )`

So even though 3 lanes are “wrong/stale” for this cycle, they still influence the next seed, matching your requirement.

---

# MAL2 pseudocode (reference implementation)

```mal
function phase_clock(t, params) {
    // PROBLEM: integer-only phase with CRT period and 3-product coupling

    a = (params.a0 + t) mod params.P;
    b = (params.b0 + t) mod params.Q;
    c = (params.c0 + t) mod params.R;

    u1 = (a * b) mod params.M;
    u2 = (b * c) mod params.M;
    u3 = (c * a) mod params.M;

    Φ = H(a||b||c||u1||u2||u3||"PHASE");
    return {a,b,c,u1,u2,u3,Φ};
}

function permutation_for_block(B, x, perm_key, Φ_block) {
    // REQUIREMENT: exact "returns every x" schedule
    // Output π_B: a permutation of 0..x-1

    // For x=4: 24 fixed permutations indexed by hash
    if (x == 4) {
        perm_id = H(perm_key || B || Φ_block || "PERM") mod 24;
        π = PERM_TABLE_24[perm_id];
        return π;
    }

    // For general x: Fisher-Yates with PRF stream
    π = [0,1,2,...,x-1];
    seed = H(perm_key || B || Φ_block || "PERMSEED");

    for (k = x-1; k >= 1; k--) {
        r = PRF(seed, k || "R") mod (k+1);
        swap(π[k], π[r]);
    }

    return π;
}

function eval_bouquet(bouquet, xres, u, M) {
    // bouquet = [Compound_0, Compound_1, ...]
    // Compound_j is itself a hidden prime-product integer (recursive product)
    // We never reveal it; we only use it modulo M.

    acc = 1 mod M;

    for (j = 0; j < len(bouquet); j++) {
        Cj = bouquet[j] mod M;   // interpreted in F_M^*
        // Exponent schedule: deterministic from residues, but bouquet stays secret
        e  = H(xres || u || j || "EXP") mod (M-1);

        acc = (acc * pow_mod(Cj, e, M)) mod M;
    }

    return acc;
}

function lane_token(i, t, phase, secrets) {
    EA = eval_bouquet(secrets.BouquetA[i], phase.a, phase.u1, secrets.M);
    EB = eval_bouquet(secrets.BouquetB[i], phase.b, phase.u2, secrets.M);
    EC = eval_bouquet(secrets.BouquetC[i], phase.c, phase.u3, secrets.M);

    Ki = H(EA||EB||EC||phase.Φ||"KDF");
    Ti = Trunc_k( H(Ki || t || phase.Φ || "TOK") );

    return Ti;
}

function device_cycle(t, x, params, state) {
    phase = phase_clock(t, params);

    B = floor(t / x);
    s = t mod x;

    // Use Φ at block start for stable schedule inside the block
    phase_block = phase_clock(B*x, params);
    π = permutation_for_block(B, x, state.perm_key, phase_block.Φ);

    idx = π[s];

    // Compute ONE lane only (energy constraint)
    state.W[idx] = lane_token(idx, t, phase, state.secrets);

    // "every token needed" via chaining
    // Example x=4; generalize as rolling chain
    m1 = (state.W[0] * state.W[1]) mod params.M;
    m2 = (state.W[1] * state.W[2]) mod params.M;
    m3 = (state.W[2] * state.W[3]) mod params.M;

    state.S = H(state.S || state.W[0]||state.W[1]||state.W[2]||state.W[3]
                     || m1||m2||m3 || phase.Φ || "EVOLVE");

    return {provider: idx, token: state.W[idx]};
}

function server_verify(i, t, token_in, params, server_secret_i) {
    phase = phase_clock(t, params);
    expected = lane_token(i, t, phase, server_secret_i);
    return (expected == token_in);
}
```

---

# 9) Mathematical correctness checklist

### A) “Returns every x” (exact)

For each block `B`, `π_B` is a permutation. Therefore over slots `s=0..x-1`, each provider index appears exactly once.

### B) Phase periodicity (CRT)

If `P,Q,R` are coprime, the tuple `(a_t,b_t,c_t)` repeats after `P·Q·R`.
If you also want the *whole* protocol to repeat (including block schedule), its deterministic part repeats after `lcm(P,Q,R,x)`. If `P,Q,R` are also coprime with `x`, that is `P·Q·R·x`.

### C) Modular exponent math

With `M` prime, bases in `F_M^*` and exponents reduced mod `(M-1)` are well-defined.

---

# 10) Implementation notes (testing-first)

### Choose M for power vs purity

* **Lite (FPGA-friendly):** `M` is a 61–64-bit prime; bouquet evaluation is cheap; security comes from `H()` (hash circuit) and secret compounds.
* **Heavy (pure number-theory field):** `M` is a 255–256-bit prime; evaluation is stronger algebraically but costs much more power.

Given your hardware emphasis on *dynamic hash circuits and seed evolution*, it’s totally consistent to keep the number-theory part “lite” and let the hash layer carry the cryptographic weight.

### Token size

* Use at least 128 bits; 256 bits if tokens are used as session keys.

### No-decimals guarantee

Everything is residues, products, modular exponentiation—integer arithmetic only.

---

# 11) Testing plan (practical)

### Unit tests (deterministic)

1. **Phase clock reproducibility:** device and server compute identical `(a,b,c,u1,u2,u3,Φ)` for known params and t.
2. **Permutation property:** for many blocks, assert `π_B` is a true permutation and each provider appears exactly once per block.
3. **Lane token consistency:** for random test vectors, server `i` matches device token only when device routed to `i`.

### Property tests (large runs)

4. **Exact 1-of-x matching:** simulate N cycles; assert exactly one provider validates each cycle.
5. **Return-to-phase:** in each block of length x, each provider validates exactly once.
6. **Chaining dependency:** flip one bit of `W[k]` or one compound prime in bouquet → observe `S` diverges quickly (avalanche).

### Adversarial tests

7. **Cross-lane forgery attempt:** with only provider i’s secrets, attempt to predict other lanes (should fail empirically—tokens look random).
8. **Replay window:** ensure server rejects old `t` outside allowed window.

### Hardware validation tie-in

If you implement this inside your reconfigurable hashing framework, you can reuse your existing validation discipline (behavioral testing, side-channel checks, formal properties on critical paths).

---

This is a **final, implementation-oriented document** for a *no-handshake*, *prime-compound* “phase-lane” token system that:

* is **number-theory correct** (CRT periods, modular groups, unique-factor style secrecy via hidden compounds),
* guarantees **exactly 1 matching provider out of x per cycle**, and **exactly once per block of x cycles** (so it *returns every x to phase*),
* lets the **device always be in-phase** (it always outputs the “right” token for the lane it chose),
* makes **every lane token needed** to evolve the next internal device state (without computing x lanes every cycle),
* uses **≥3 independent secret variables** per provider (your minimum secure separation),
* stays compatible with your “continuative seed hashing / evolving circuit” framing.

---

If you want, produce a **concrete parameter set** (example primes, bouquet sizes, compound depth, exponent schedule) optimized for either:

* ESP32 + “fast bouquet check” + FPGA hash core (your doc mentions this split), 
  or
* full FPGA token with higher-throughput seed evolution.
