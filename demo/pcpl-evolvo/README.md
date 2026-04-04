# PCPL Evolvo (Empirical Practical Harness)

This project adds an empirical, evolvable PCPL environment focused on:

- preserving core PCPL protocol properties (1-of-x, per-block lane fairness, deterministic sync),
- evaluating computational/timing cost trade-offs for device and provider circuits,
- searching for practical math control circuits with `evolvo`.

It is intentionally built on top of the existing deterministic reference implementation in `demo/pcpl_cycle_test.py`.

## Structure

- `evolvo/` - Git submodule ([Geckos-Ink/evolvo](https://github.com/Geckos-Ink/evolvo)).
- `src/pcpl_evolvo/simulation.py` - PCPL scenario simulator, scoring, and metric extraction.
- `src/pcpl_evolvo/experiment.py` - baseline evaluation + evolution loop + report generation.
- `run_experiments.py` - main runner.
- `run_auto.sh` - one-command run with timestamped logs.
- `runs/` - generated results.

## What is evolved

A GFSL circuit outputs per-cycle control values that tune practical math behavior:

- active compound ratio (cost/security trade-off),
- kernel choice for lane mixing,
- stride seed for compound index selection.

The simulator then applies those controls while keeping PCPL scheduling semantics and deterministic timing behavior.

## Scoring dimensions

Each scenario is scored from:

- protocol correctness (`one_of_x`, block fairness, permutation validity, cross-lane rejection),
- symmetric synchronization and timing discrimination,
- security proxies (cross-lane collisions, replay-window repeats),
- computational cost proxy (device/provider compound usage ratio),
- runtime budget compliance.

## Usage

From repository root:

```bash
python3 demo/pcpl-evolvo/run_experiments.py --profile fast
```

or with automatic timestamped logs:

```bash
./demo/pcpl-evolvo/run_auto.sh --profile fast
```

Heavier profile:

```bash
python3 demo/pcpl-evolvo/run_experiments.py --profile full --generations 40 --population-size 32
```

## Outputs

Each run creates `demo/pcpl-evolvo/runs/<timestamp>-<profile>/` with:

- `results.json` - full machine-readable results,
- `summary.json` - short run summary,
- `generation-log.jsonl` - generation-by-generation scoring,
- `best-genome.txt` - evolved effective GFSL trace,
- `report.md` - human-readable experiment report,
- `console.log` when using `run_auto.sh`.

## Notes

- The evolvo source is loaded directly from `demo/pcpl-evolvo/evolvo/src`.
- The reference PCPL implementation is loaded dynamically from `demo/pcpl_cycle_test.py` to keep protocol semantics aligned.
