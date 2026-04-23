# PCPL Evolvo (Continuous Co-Evolution)

This project provides a practical, empirical PCPL optimizer with:

- continuous defender evolution,
- adversarial attacker evolution (circuit breakers),
- persistent elite archive and resumable rounds,
- scoring for correctness, sync drift/timing, cost, and security resistance.

It is built directly on top of the deterministic protocol reference in `demo/pcpl_cycle_test.py`.

## Structure

- `evolvo/` - git submodule (`Geckos-Ink/evolvo`).
- `src/pcpl_evolvo/simulation.py` - PCPL simulation + scoring model.
- `src/pcpl_evolvo/experiment.py` - continuous co-evolution engine + archive persistence.
- `run_experiments.py` - CLI runner.
- `config.py` - central presets/defaults (`balanced`, `conclusion`, `dynamic`, `explorer`).
- `run_auto.sh` - one-command execution with timestamped logs.
- `compile_perf.sh` - optimized bytecode compile script (macOS/Linux).
- `compile_perf.ps1` - optimized bytecode compile script (PowerShell).
- `runs/` - generated artifacts (ignored in git, except `.gitkeep`).

## Scoring model

Defender score includes:

- protocol principles: exact `1-of-x`, block fairness, permutation validity,
- synchronization: drift loss rate and absolute-time reference resync (`0..10000 ms` windows),
- operation/device/provider costs: operation-weighted cycle timing vs budget,
- security: collision/replay/shared-device impersonation resistance,
- brute-force and reverse-hack resistance measured against evolved attackers.

Attacker score is based on learned advantage at predicting routed lane/token fragments.

## Continuous evolution

The engine runs round-by-round and saves:

- defender + attacker elites in `archive.json`,
- per-round reports in `runs/<run>/rounds/round-XXXX/`,
- global summary in `results.json` and `report.md`,
- structured views:
  - `views/index.md`,
  - `leaderboards/defender-top10.md`, `leaderboards/attacker-top10.md`,
  - `best/best-defender-genome.txt`, `best/best-attacker-genome.txt`,
  - `summaries/conclusions.md`.

Subsequent runs can resume from the same `--out-dir` and continue evolving from top archived genomes.

## Usage

Default behavior is now driven by `config.py` mode presets.  
This avoids passing many low-level flags on the CLI.

Current default preset:

- profile: `full`
- mode: `conclusion`
- workers: `0` (auto, uses all available CPU cores)
- parallel backend: `process`
- rounds per execution: `12` (`conclusion/full`)

This default is tuned for generating evidence suitable for conclusions/improvement work (not just quick smoke tests).

### List preset modes

```bash
python3 demo/pcpl-evolvo/run_experiments.py --list-modes
```

### Print resolved config before running

```bash
python3 demo/pcpl-evolvo/run_experiments.py --mode dynamic --print-effective-config
```

### Single round

```bash
python3 demo/pcpl-evolvo/run_experiments.py --profile fast --mode dynamic --rounds 1
```

### Verify torch accelerator usage (ROCm/CUDA/MPS)

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --mode dynamic \
  --rounds 1 \
  --device rocm \
  --supervised-end-round-only
```

Look for logs like:

- `supervised guide enabled ... backend=rocm resolved=cuda ... probe=ok`

and the `report.md` line:

- `supervised runtime: defender(... device=..., probe=ok, train_calls=..., predict_calls=...)`

If probe is `failed`, the run will print the probe error and fallback behavior.

### Verify Kompute planning/simulation path

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --mode dynamic \
  --rounds 1 \
  --executor-backend kompute-sim
```

This forces Kompute compatibility/planner checks and then runs simulated execution
for compatible genomes (CPU-backed semantics). Native backend (`--executor-backend kompute`)
dispatches supported scalar stages through Vulkan and transparently falls back to CPU for unsupported stages.
When Kompute is enabled, process-pool evaluation is auto-switched to thread mode by default
to avoid Vulkan instability with forked workers; set `EVOLVO_KOMPUTE_ALLOW_PROCESS_POOL=1`
only if you explicitly want to keep process workers.

### Vulkan / Kompute dependency doctor

```bash
python3 demo/pcpl-evolvo/run_experiments.py --kompute-check-libs
```

This checks:
- Vulkan loader (`libvulkan.so.1`)
- ICD JSON files and their driver `library_path` targets
- `vulkaninfo --summary` execution
- `kp` import + API mode detection (`explicit-sync` vs `shared-memory`) + Vulkan manager initialization

Exit code is `0` on pass and non-zero on failure.

### Quick native Kompute self-test (recommended before long runs)

```bash
python3 demo/pcpl-evolvo/run_experiments.py --kompute-self-test
```

This performs:
- a raw `kp` Vulkan dispatch test (simple add shader), and
- an `evolvo` native executor test (`compute_backend=kompute`, `execution_mode=native`),

then exits immediately with status `0` on success (non-zero on failure).
It supports both `kp` API variants:
- `explicit-sync` (`OpSyncDevice`/`OpSyncLocal` or `OpTensorSyncDevice`/`OpTensorSyncLocal` available)
- `shared-memory` (older/minimal bindings without those ops)

If default Vulkan device/queue selection fails on your host, override selection:

```bash
EVOLVO_KOMPUTE_DEVICE_INDEX=1 EVOLVO_KOMPUTE_QUEUE_FAMILY=0 \
python3 demo/pcpl-evolvo/run_experiments.py --kompute-self-test
```

### Debug watchdog/heartbeat for long evaluator phases

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --mode dynamic \
  --rounds 1 \
  --executor-backend kompute \
  --debug-eval-timeout-seconds 30 \
  --debug-eval-log-interval-seconds 10
```

These debug-only flags add periodic evaluator progress logs and emit timeout diagnostics
when no parallel task completion is observed for the timeout window.

### Continuous/resumable rounds

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --mode dynamic \
  --out-dir demo/pcpl-evolvo/runs/mainline \
  --rounds 5 \
  --parallel-backend process \
  --workers 0
```

Run again with the same `--out-dir` to continue from archive elites.

### Continuous exhaustive parameter sweep (`--continuous`)

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --mode dynamic \
  --out-dir demo/pcpl-evolvo/runs/mainline \
  --rounds 1 \
  --continuous
```

This mode keeps running until you stop it (`Ctrl+C`), executes all generated
parameter combinations in sweeps, and continuously saves:

- per-combo archives/results under `continuous-runs/`,
- global live state in `continuous-state.json`,
- top scores/genomes in `continuous-leaderboard.json`,
- append-only run log in `continuous.log`.

Optional stop cap:

```bash
python3 demo/pcpl-evolvo/run_experiments.py --continuous --continuous-max-iterations 10
```

### Auto script

```bash
./demo/pcpl-evolvo/run_auto.sh --profile fast --rounds 3
```

### Compile For Performance (Startup/Import)

macOS/Linux:

```bash
./demo/pcpl-evolvo/compile_perf.sh --clean
```

PowerShell:

```powershell
./demo/pcpl-evolvo/compile_perf.ps1 -Clean
```

## Main CLI options

- `--mode {balanced,conclusion,dynamic,explorer}` (recommended high-level tuning control from `config.py`)
- `--list-modes`, `--print-effective-config`
- `--population-size`, `--generations`
- `--attacker-population-size`, `--attacker-generations`
- `--elite-pool`, `--archive-limit`
- `--no-resume` (start fresh even if archive exists)
- `--parallel-backend {auto,process,thread,off}`, `--workers` (`0` = all CPUs)
- `--round-parallelism` (`0` = auto lanes) and `--minimum-parallel-rounds` (best-effort floor for concurrent rounds)
- `--executor-backend {auto,cpu,kompute,kompute-sim}` (GFSL executor path for scenario evaluation)
- `--kompute-check-libs` (Vulkan/Kompute dependency doctor and manager init probe)
- `--kompute-self-test` (fast raw `kp` + evolvo native dispatch smoke test)
- `--kompute-native-enable-decimal/--no-kompute-native-enable-decimal`
- `--kompute-native-enable-boolean-compare/--no-kompute-native-enable-boolean-compare`
- `--kompute-native-enable-boolean-logic/--no-kompute-native-enable-boolean-logic`
- `--kompute-native-enable-list-query/--no-kompute-native-enable-list-query`
- `--debug-eval-timeout-seconds` (debug-only stall watchdog: emit timeout diagnostics if parallel eval makes no completion for N seconds)
- `--debug-eval-log-interval-seconds` (debug-only heartbeat logs during long parallel eval phases)
- `--no-supervised-guide`
- `--supervised-end-round-only`, `--no-supervised-end-round-only`
- `--device {auto,cpu,cuda,rocm,mps}` (for optional supervised guide acceleration)
- `--parent-pool-ratio`, `--stagnation-patience`, `--mutation-floor`, `--mutation-ceiling`, `--mutation-step`
- `--quick-cycle-fraction`, `--mid-cycle-fraction`, `--quick-keep-ratio`, `--mid-keep-ratio` (initial seeds, auto-tuned in real time)
- `--key-variants`, `--novelty-bonus`, `--predictive-penalty`
- `--device-mhz`, `--provider-mhz`, `--max-test-seconds` (long-horizon timing projection; default 10s)
- `--no-statistical-predictive` (force full brute evaluation for all genomes)
- `--no-auto-statistical-tuning` (keep stage fractions/ratios fixed to CLI values)

Notes:
- Edit `demo/pcpl-evolvo/config.py` if you want persistent defaults instead of long CLI command lines.
- `--continuous` now runs multiple combo lanes in parallel by default (auto-planned from available CPUs), with per-lane worker splitting so `--workers 0` exploits all cores without extra flags.
- Process workers are now reused across generations/rounds for lower spawn overhead and higher sustained CPU utilization.
- The adaptive parent-pool + mutation schedule reduces random dispersivity and focuses search around top genomes while preserving exploration.
- Staged statistical mode (default) now applies dynamic quick-stage budgeting under pressure (see `qskip=` in logs), so only the most novel/promising genomes proceed when populations are large.
- Stage fractions/keep ratios/key-variants are auto-tuned from runtime statistics (probe false-negative rate, novelty rate, keep-throughput) and persisted in `archive.json` for following rounds.
- In `--supervised-end-round-only` mode, the guide is now warmed once at round start and reused for proposal (no per-generation retraining).
- Duplicate genomes are collapsed before stage execution and cached by fast evaluation signature + scenario fingerprint + opponent signature, so repeated candidates are not re-executed.
- Evaluator outcomes are now tracked with explicit status buckets (`valid`, `valid-no-metrics`, `timeout-cut`, `complexity-cut`, `error-empty`) and surfaced in generation logs as `estatus=...`.
- Defender sync-gate/anti-neutrality penalties are automatically skipped when full-stage timeout ratio is too high, avoiding false pressure from timeout-only batches.
- Defender panel selection now expands attackers progressively and can fall back to a rescue selection profile (reduced complexity + longer timeout) when timeout collapse is detected.
- Round archive promotion now requires non-empty defender metrics; timeout-only rounds are persisted for diagnostics but skipped for elite insertion.
- Process evaluation dispatch uses chunked batch workers to reduce per-genome IPC/pickling overhead in `process` backend.
- For `--profile full`, startup tuning is automatically leaner and generation-time budgeted (target ~3s) without extra flags.
