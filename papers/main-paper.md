
<style>
table, tr {
	width:100%; 
	border-style:none;
}
</style>
<table><tr><td>Riccardo Cecchini</td><td>rcecchini.ds[at]gmail.com</td><td>25 December 2025</td></tr></table>

# Prime-Compound Phase-Lane Token Protocol (PCPL) for Symmetric Continuous Tokenizer Devices

Version 1.1 - 26 December 2025

## Abstract
I present the Prime-Compound Phase-Lane Token Protocol (PCPL), a no-handshake token system where a device emits one token per cycle and exactly one provider can validate it. PCPL combines (1) a public phase clock derived from coprime residues, (2) hidden prime-compound bouquets per provider, and (3) device-only state evolution that chains all lanes. I also introduce the symmetric continuous tokenizer device model, motivated by FPGA-based dynamic hash circuits and twin circuits for peer validation. A step-by-step algorithm description, correctness properties, and a deterministic simulation trace are provided.

## 1. Symmetric continuous tokenizer devices
PCPL runs on a “symmetric continuous tokenizer” device designed for consumer computing. The device is envisioned as a reconfigurable hardware unit (for example, an FPGA-based key) that can:

- Acquire unique, device-specific hashing circuits or internal start variables.
- Continuously generate short-lived tokens or keys.
- Be validated only by its twin circuit(s), which share the same circuit family or seed lineage.

The symmetry comes from pairing: two devices can load the same dynamic hash circuit and evolve internal state in the same way, enabling mutual validation without exposing the evolving secrets.

### 1.1 Forks by variable alternation
Beyond PCPL, the same circuit can be “forked” by alternating variable sets over time windows. Let a device maintain a base circuit $C$ and a family of variables $V_k$ selected by time window $W_k$. Each fork evolves as:

$$
\begin{aligned}
S_{t+1}^{(k)} &= H\!\left(C,\, S_t^{(k)},\, V_k,\, t\right), \\
&\quad t \in W_k.
\end{aligned}
$$

This creates multiple parallel token streams sharing the same circuit but with distinct, time-delimited variable schedules. Such forks can be used for provider lanes (as in PCPL) or for isolated peer-to-peer sessions that are difficult to parallelize or replay.

### 1.2 Peer-to-peer continuity
The device model also targets in-loco connections among peers. Two devices that share a circuit family and seed lineage can establish an isolated encryption context by evolving state in lockstep without querying a central provider.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart LR
  subgraph Device["Tokenizer device"]
    A["Device A<br/>dynamic hash circuit"]
  end

  subgraph Endpoints["Endpoints"]
    B["Device B<br/>twin circuit"]
    P1["Provider 1"]
    P2["Provider 2"]
    P3["Provider 3"]
  end

  A -- "continuous tokens" --> B
  A -- "lane token" --> P1
  A -- "lane token" --> P2
  A -- "lane token" --> P3
```

## 2. System model and goals
PCPL is designed for:

- No runtime challenge/response or synchronization negotiation.
- One token per cycle, routed to exactly one provider out of $x$.
- Provider-side validation by local recomputation.

Threat model (minimal):

- A provider should not compute tokens for other providers.
- Observing accepted tokens should not reveal other lanes.
- Public time/phase information should not enable cross-lane forgery.

## 3. Notation and public parameters
Let:

- $x$ be the number of providers (lanes).
- $P, Q, R$ be pairwise coprime primes (also coprime with $x$).
- $M$ be a prime modulus for multiplicative-group arithmetic.
- $H(\cdot)$ be a cryptographic hash (or a dynamic hash circuit).
- $\mathrm{Trunc}_k(\cdot)$ be truncation to $k$ bits.
- $t$ be the cycle counter.
- $\|$ denote byte/bit-string concatenation.

Each provider $i$ has three secret bouquets: $\mathrm{BouquetA}_i, \mathrm{BouquetB}_i, \mathrm{BouquetC}_i$, each a list of prime compounds.

### 3.1 Seed construction and coprime extraction
The device bootstraps a root seed $Z$ from device-local entropy and context (for example: device secret, serial, provider list, and a boot nonce). In the demo, $Z$ is produced by a deterministic RNG seeded with `--seed`, then bound to labels with $H(\cdot)$:

- $\mathrm{perm\_key} = H(Z \| \text{"PERMKEY"})$
- $S_0 = H(Z \| \text{"SEED"})$
- $W_i = \mathrm{Trunc}_k(H(Z \| \text{"W"} \| i))$

To extrapolate coprimes for $P,Q,R$ (and optionally $M$), derive candidates from a seeded stream and select the first primes that are distinct and coprime with $x$:

1. $c_k \leftarrow \mathrm{next\_prime}(H(Z \| \text{"PRIME"} \| k) \bmod 2^b)$
2. accept $c_k$ if $\gcd(c_k, x)=1$ and $c_k \notin \{P,Q,R,M\}$
3. continue until $P,Q,R$ (and $M$ if generated) are assigned

### 3.2 Best-practice coprimes, compounds, and key selection
Parameter and key selection should scale with the peer count and keep strict domain separation between device-only and provider-only secrets.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart TD
  Start["Choose peer count x"] --> Scale["Set security horizon and target period size"]
  Scale --> Adjust["If x grows: raise prime bits, bouquet size, primes per compound"]
  Adjust --> Bits["Select bit sizes for P/Q/R (and M if generated)"]
  Bits --> Gen["Generate candidates from seeded stream"]
  Gen --> Coprime["Accept only if gcd(candidate, x)=1 and pairwise coprime"]
  Coprime --> Assign["Assign P, Q, R (and M)"]
  Assign --> Pool["Build prime pool for compounds"]
  Pool --> Mode["Choose compound mode per provider"]
  Mode --> Comp["Compound = product of >= 2 primes (or prime-power/offset)"]
  Comp --> Check["Reject bases divisible by M"]
  Check --> Bouquets["Generate BouquetA/B/C per provider i"]
  SeedRoot["Root seed Z from device secret + nonce + provider list"] --> Keys["perm_key, S0, W[i] (device-only)"]
  SeedRoot --> ProvSeed["Provider seed = H(Z || provider_id || 'PROVIDER')"]
  ProvSeed --> Bouquets
  Bouquets --> Provision["Provision provider i and device with derived secrets"]
```

## 4. PCPL protocol overview
The protocol uses:

1. A public phase clock (CRT residues and coupled products).
2. A per-block permutation schedule to enforce “returns every $x$”.
3. Hidden bouquets to derive lane-specific tokens.
4. Device-only seed evolution that chains all lanes.

### 4.1 User device circuit (emitter)
The device knows the full schedule and all lane secrets, so it computes only the
active lane per cycle and emits exactly one token.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart TD
  Public["Public clock t, P,Q,R,M, x"] --> Phase["Phase residues + Phi_t"]
  Secrets["Device-only: perm_key, S_t, all bouquets, W[ ]"] --> Perm["Permutation pi_B for block B"]
  Phase --> Perm
  Perm --> Pick["idx_t = pi_B[s]"]
  Pick --> Tok["Compute token T_idx_t(t)"]
  Tok --> Send["Send token to provider idx_t"]
  Tok --> Evolve["Update W[idx_t], evolve S_{t+1}"]
  Evolve --> Next["Advance to t+1"]
```

### 4.2 Blind provider circuit (validator)
Each provider only knows its own bouquets. It recomputes $T_i(t)$ every cycle,
but the received token matches only once per block of $x$ cycles. The other
$x-1$ cycles are expected mismatches because the device emitted a different lane.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart TD
  Public["Public clock t, P,Q,R,M, x"] --> Phase["Phase residues + Phi_t"]
  Bouquets["Provider i bouquets only"] --> Expect["Compute expected T_i(t)"]
  Phase --> Expect
  Rx["Received token from device (or none)"] --> Compare["Compare expected vs received"]
  Expect --> Compare
  Compare --> Match{"Match?"}
  Match -->|yes| Accept["Accept token (1 per block)"]
  Match -->|no| Reject["Reject / ignore (x-1 cycles)"]
  Accept --> Next["Advance to t+1"]
  Reject --> Next
```

## 5. Step-by-step algorithm

### 5.1 Phase clock
For cycle $t$:

$$
\begin{aligned}
a_t &= (a_0 + t) \bmod P, \\
b_t &= (b_0 + t) \bmod Q, \\
c_t &= (c_0 + t) \bmod R.
\end{aligned}
$$

Coupled products:

$$
\begin{aligned}
u_1 &= (a_t\, b_t) \bmod M, \\
u_2 &= (b_t\, c_t) \bmod M, \\
u_3 &= (c_t\, a_t) \bmod M.
\end{aligned}
$$

Phase digest:

$$
\Phi_t = H\!\left(a_t \| b_t \| c_t \| u_1 \| u_2 \| u_3 \| \text{"PHASE"}\right).
$$

### 5.2 Permutation schedule (“returns every x”)
Let:

$$
B = \left\lfloor \frac{t}{x} \right\rfloor, \quad s = t \bmod x.
$$

Compute a permutation $\pi_B$ of $\{0,\ldots,x-1\}$ using a hash-driven shuffle seeded by a block-level phase digest (computed at $t = B\cdot x$) so the schedule is stable within each block. Then:

$$
\mathrm{idx}_t = \pi_B[s].
$$

This guarantees each provider appears exactly once per block.

### 5.2.1 Device-side destination selection
The device determines the current destination provider using only public phase data and its private permutation key:

$$
\begin{aligned}
&B = \left\lfloor \frac{t}{x} \right\rfloor,\quad s = t \bmod x \\
&\pi_B = \mathrm{Permute}\left(\mathrm{perm\_key}, B, \Phi_{B\cdot x}\right) \\
&\mathrm{idx}_t = \pi_B[s]
\end{aligned}
$$

Providers do not know $\mathrm{perm\_key}$, so the schedule is blinded from them even though $t$ and $\Phi_t$ are public.

### 5.3 Bouquet evaluation
Each bouquet is a list of compounds $C_j$, each a product of primes. For a residue $x_{\mathrm{res}}$ and coupling $u$, define:

$$
e_j = H\!\left(x_{\mathrm{res}} \| u \| j \| \text{"EXP"}\right) \bmod (M-1).
$$

$$
\mathrm{Eval}(\mathrm{Bouquet}, x_{\mathrm{res}}, u) = \prod_j C_j^{e_j} \bmod M.
$$

For provider $i$:

$$
\begin{aligned}
EA_i(t) &= \mathrm{Eval}(\mathrm{BouquetA}_i, a_t, u_1), \\
EB_i(t) &= \mathrm{Eval}(\mathrm{BouquetB}_i, b_t, u_2), \\
EC_i(t) &= \mathrm{Eval}(\mathrm{BouquetC}_i, c_t, u_3).
\end{aligned}
$$

### 5.3.1 Prime-compound construction variants
Compounds do not need to be prime: any base coprime with $M$ is valid. This allows richer “prime compounds” that keep continuity while increasing algebraic complexity:

- **Prime powers:** $C = p^k$ (smooth but non-prime bases).
- **Semiprimes:** $C = p q$ (harder to factor, still structured).
- **Offset compounds:** $C = \left(\prod p_i^{e_i}\right) + \delta$ with small $\delta$ to create a quasi-continuous family.
- **Quantized reals:** map a real parameter $\rho$ to $C = \lfloor \alpha \rho \rfloor$ for fixed scale $\alpha$, then ensure $\gcd(C, M)=1$.

The demo exposes these families via compound generation modes while keeping the exponent schedule unchanged.

### 5.4 Token derivation
Key derivation:

$$
K_i(t) = H\!\left(EA_i \| EB_i \| EC_i \| \Phi_t \| \text{"KDF"}\right).
$$

Token:

$$
T_i(t) = \mathrm{Trunc}_k\!\left(H\!\left(K_i \| t \| \Phi_t \| \text{"TOK"}\right)\right).
$$

### 5.5 Device emission and state evolution
The device computes only $T_{\mathrm{idx}_t}(t)$ and updates internal state:

- $W[i]$ stores the last token for lane $i$.
- The seed $S$ evolves using all lanes and adjacent products.

For $x$ lanes, define (non-cyclic adjacency):

$$
m_\ell = (W_\ell \cdot W_{\ell+1}) \bmod M, \quad \ell = 0,\ldots,x-2.
$$

$$
S_{t+1} = H\!\left(
S_t \| W_0 \| \cdots \| W_{x-1} \| m_0 \| \cdots \| m_{x-2} \| \Phi_t \| \text{"EVOLVE"}
\right).
$$

### 5.6 Provider verification
Provider $i$ recomputes $T_i(t)$ and accepts the token if it matches.

### 5.7 Device-side vs provider-side variables
The protocol deliberately separates what the device computes from what providers can infer:

- **Public inputs:** $t$, $x$, $P,Q,R,M$, and the permutation algorithm (but not the key).
- **Device-only state:** $\mathrm{perm\_key}$, $S_t$, all lane secrets, and the last tokens $W[0..x-1]$.
- **Provider $i$ secrets:** $\mathrm{BouquetA}_i, \mathrm{BouquetB}_i, \mathrm{BouquetC}_i$.
- **Ignored by providers:** $\mathrm{perm\_key}$, $S_t$, other providers’ bouquets, and the full $W$ vector.

The device computes only $T_{\mathrm{idx}_t}(t)$ for the current lane; the provider computes only its own lane token and does not need the device seed.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart LR
  Public["Public clock<br/>t, P,Q,R, x"] --> Phase["Phase residues<br/>a_t,b_t,c_t,u_1,u_2,u_3"]
  Device["Device-only<br/>perm_key, S_t, all bouquets"] --> Select["idx_t = pi_B[s]"]
  Phase --> Select
  Select --> Token["Token T_idx_t(t)"]
  Token --> Provider["Provider i<br/>Bouquet_i only"]
  Phase --> Provider

  subgraph Repeat["Predictable repeats / exposure"]
    R1["Phase period = lcm(P,Q,R,x)"]
    R2["Permutation repeats per block"]
    R3["Provider sees 1/x duty cycle"]
  end

  Phase -.-> Repeat
  Select -.-> Repeat
```

## 6. Correctness and periodicity

### 6.1 Exact 1-of-x matching
Within each block of length $x$, $\pi_B$ is a permutation. Therefore each provider index appears exactly once per block, and exactly one provider matches per cycle.

### 6.2 Phase periodicity
If $P, Q, R$ are coprime, the tuple $(a_t, b_t, c_t)$ repeats after $PQR$. If $P, Q, R$ are also coprime with $x$, the deterministic schedule repeats after $PQRx$.

### 6.3 Modular exponent correctness
With $M$ prime, the multiplicative group $\mathbb{F}_M^{\ast}$ has order $M-1$. Reducing exponents modulo $M-1$ makes $C_j^{e_j} \bmod M$ well-defined for any base $C_j$ not divisible by $M$.

### 6.4 Peer-count variations (x=2,3,4 and prime powers)
Changing $x$ changes the block size, the number of permutations, and the chain width:

| x | block length | permutations | chain products | note |
|---:|---:|---:|---:|---|
| 2 | 2 | 2 | 1 | twin pairing (2 lanes) |
| 3 | 3 | 6 | 2 | prime lane count |
| 4 | 4 | 24 | 3 | $2^2$ prime power |

In general: block length $= x$, permutation space $= x!$, chain width $= x-1$, and schedule period $= \mathrm{lcm}(P,Q,R,x)$. For $x=p^k$, choose $P,Q,R$ coprime with $p$ to avoid shrinking the period.

## 7. Security intuition (informal)
- **Lane isolation:** each provider uses distinct secret bouquets, so observing one lane does not reveal others.
- **Phase coupling:** public residues are mixed and hashed, preventing linear predictability from the CRT clock alone.
- **Device chaining:** even stale lanes influence future state, reinforcing the requirement that “every token matters”.
- **Quantum period-finding:** QFT can reveal the public period $\mathrm{lcm}(P,Q,R,x)$ but not the hidden bouquets or $\mathrm{perm\_key}$; use large coprimes and device-only chaining to avoid exploitable structure.

## 8. Experimental validation (deterministic simulation)
A simulator was implemented a cycle-by-cycle to validate correctness. The demo verifies:

- Each block yields a valid permutation.
- Exactly one provider matches each cycle.
- Each provider appears once per block.
- Optional pre-hash difficulty metrics and QFT-visible period reports.
- Optional prime/compound generation modes for non-arbitrary parameter testing.

### 8.1 Sample token trace (x=4, seed=1337)
For PDF export, the original wide table was replaced with an A4-friendly summary table and a sequence diagram (tokens truncated for readability; the matched provider’s recomputed token equals the device token by construction).

| t | block | slot | idx_t | device token (truncated) | matched provider |
|---:|---:|---:|---:|---|---:|
| 0 | 0 | 0 | 3 | `0xaa81443d…6e0e02` | 3 |
| 1 | 0 | 1 | 0 | `0x21faa3d7…2dbe77` | 0 |
| 2 | 0 | 2 | 2 | `0x888b2137…903179` | 2 |
| 3 | 0 | 3 | 1 | `0xa591e8bf…03b4b4` | 1 |
| 4 | 1 | 0 | 2 | `0x5da9a61c…1d52ff` | 2 |
| 5 | 1 | 1 | 0 | `0x8abe0866…9d17b6` | 0 |
| 6 | 1 | 2 | 1 | `0x39d33ef1…6fd92e` | 1 |
| 7 | 1 | 3 | 3 | `0xe25bb134…064674` | 3 |

```mermaid
%%{init: {"theme":"neutral"} }%%
sequenceDiagram
  participant D as Device
  participant P0 as Provider 0
  participant P1 as Provider 1
  participant P2 as Provider 2
  participant P3 as Provider 3

  D->>P3: t=0  0xaa81443d…6e0e02
  D->>P0: t=1  0x21faa3d7…2dbe77
  D->>P2: t=2  0x888b2137…903179
  D->>P1: t=3  0xa591e8bf…03b4b4
  D->>P2: t=4  0x5da9a61c…1d52ff
  D->>P0: t=5  0x8abe0866…9d17b6
  D->>P1: t=6  0x39d33ef1…6fd92e
  D->>P3: t=7  0xe25bb134…064674
```

### 8.2 Full token trace (verbatim values)

The full deterministic trace (block permutations, schedule, device tokens, and per-lane tokens) is exported to a separate, auto-generated file to keep the paper A4-friendly, following the papers and script developed in repository [cekkr/phaselane-algorithm@github.com](https://github.com/cekkr/phaselane-algorithm). See `papers/token-trace.md`, generated by `demo/export_token_trace.py`.

Regenerate with:
`python3 demo/export_token_trace.py --blocks 4 --out papers/token-trace.md`

### 8.3 Pre-hash difficulty and period reporting
The demo can emit a linear pre-hash difficulty report (rank of exponent vectors modulo 2 and 65537) and the QFT-visible public period:

- `python3 demo/pcpl_cycle_test.py --linear-report --analysis-window 64`
- `python3 demo/pcpl_cycle_test.py --qft-report`
- `python3 demo/pcpl_cycle_test.py --compare-x 2,3,4,5`
- `python3 demo/pcpl_cycle_test.py --prime-mode generated --prime-bits 31 --compound-mode blend --compound-prime-bits 12`

### 8.4 Multi-configuration results snapshot
All runs below completed the full correctness checks (permutation, 1-of-x matching, chaining).

Fixed primes (P/Q/R near 1e6, seed=1337) with compare-x and 64-cycle linear window:

| x | chain width (x-1) | QFT period bits | QFT period (decimal) |
|---:|---:|---:|---|
| 2 | 1 | 61 | 2000146002862007326 |
| 3 | 2 | 62 | 3000219004293010989 |
| 4 | 3 | 62 | 4000292005724014652 |
| 5 | 4 | 63 | 5000365007155018315 |

Across all x above, the pre-hash exponent vectors reached full rank (4/4) modulo 2 and 65537, with 64/64 unique rows for A/B/C over the sample window.

Generated primes (x=4, 64 cycles, 12-bit compound primes):

| seed | compound mode | P | Q | R | M | QFT period bits | QFT period (decimal) |
|---:|---|---:|---:|---:|---:|---:|---|
| 1337 | blend | 2096669299 | 1747608157 | 1866608729 | 1273159183829412833 | 95 | 27358185054648849675767961788 |
| 2024 | semiprime | 1423693267 | 1141001293 | 1348017509 | 2083707438551447381 | 93 | 8759071917926854366514362316 |

Full multi-configuration outputs (additional compound modes and seeds) are in `papers/pcpl-results.md`.

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart TD
  Cfg["Config: x, seed, prime/compound modes"] --> Primes["Derive P/Q/R (and M if generated)"]
  Primes --> Bouquets["Generate provider bouquets"]
  Bouquets --> Init["Init device state: perm_key, S0, W[ ]"]
  Init --> Loop["Device cycle t"]
  Loop --> Phase["Phase clock: a_t,b_t,c_t,u1,u2,u3"]
  Phase --> Dest["idx_t from pi_B[s]"]
  Dest --> Tok["Emit T_idx_t(t)"]
  Tok --> Evolve["Update W[idx_t], evolve S_{t+1}"]
  Evolve --> Loop
  Tok --> Report["Reports: linear rank, QFT period, compare-x"]
```

```mermaid
%%{init: {"theme":"neutral","flowchart":{"curve":"basis"}} }%%
flowchart TD
  Public["Public clock t, P,Q,R,M, x"] --> Phase["Phase clock: a_t,b_t,c_t,u1,u2,u3"]
  Phase --> ProvTok["Provider i recompute T_i(t)"]
  Bouquet["Provider i bouquets only"] --> ProvTok
  ProvTok --> Accept["Accept if token matches"]
  Accept --> Window["Window checks / replay rules (if used)"]
```

## 9. Discussion and limitations
- Parameter choice matters; $P, Q, R, M$ must be prime and pairwise coprime.
- The permutation schedule is device-only; leakage of the permutation key can reveal lane order, but not lane tokens.
- The security of the scheme relies on the strength of $H(\cdot)$ and the secrecy of bouquets, not on the hardness of factoring revealed integers.
- The public period $\mathrm{lcm}(P,Q,R,x)$ is visible (and QFT-recoverable), so period size should be chosen large enough for the deployment horizon.
- For testing, primes and compound bases can be generated from a seeded stream to avoid arbitrary constants.
- This paper was developed and formatted with an heavy OpenAI models' help.

## 10. Conclusion
PCPL provides a deterministic, no-handshake token protocol with exact 1-of-$x$ matching and a device-only chaining mechanism. Combined with symmetric continuous tokenizer devices, it supports both provider validation and peer-to-peer isolation with dynamic, evolving secrets. The included simulation and trace demonstrate the protocol’s behavior cycle by cycle.
