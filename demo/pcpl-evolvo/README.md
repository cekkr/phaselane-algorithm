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
- `run_auto.sh` - one-command execution with timestamped logs.
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
- global summary in `results.json` and `report.md`.

Subsequent runs can resume from the same `--out-dir` and continue evolving from top archived genomes.

## Usage

### Single round

```bash
python3 demo/pcpl-evolvo/run_experiments.py --profile fast --rounds 1
```

### Continuous/resumable rounds

```bash
python3 demo/pcpl-evolvo/run_experiments.py \
  --profile fast \
  --out-dir demo/pcpl-evolvo/runs/mainline \
  --rounds 5
```

Run again with the same `--out-dir` to continue from archive elites.

### Auto script

```bash
./demo/pcpl-evolvo/run_auto.sh --profile fast --rounds 3
```

## Main CLI options

- `--population-size`, `--generations`
- `--attacker-population-size`, `--attacker-generations`
- `--elite-pool`, `--archive-limit`
- `--no-resume` (start fresh even if archive exists)
