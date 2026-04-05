#!/usr/bin/env python3
"""Run PCPL evolvo experiments (single-run or continuous resumable mode)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pcpl_evolvo.experiment import ExperimentConfig, run_experiment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PCPL continuous empirical/evolutionary runner (Evolvo-backed)."
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default="fast",
        help="Scenario profile. fast is shorter; full is heavier.",
    )
    parser.add_argument("--seed", type=int, default=1337, help="Random seed.")
    parser.add_argument(
        "--population-size",
        type=int,
        default=18,
        help="Defender population size.",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=16,
        help="Defender generations per round.",
    )
    parser.add_argument(
        "--initial-instructions",
        type=int,
        default=12,
        help="Max random seed instruction count.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Continuous rounds to run in this invocation.",
    )
    parser.add_argument(
        "--attacker-population-size",
        type=int,
        default=12,
        help="Attacker population size per round.",
    )
    parser.add_argument(
        "--attacker-generations",
        type=int,
        default=6,
        help="Attacker generations per round.",
    )
    parser.add_argument(
        "--elite-pool",
        type=int,
        default=12,
        help="Number of top archived genomes used to seed each new round.",
    )
    parser.add_argument(
        "--archive-limit",
        type=int,
        default=64,
        help="Max defender/attacker elites kept in archive.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not load previous archive state from --out-dir.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help=(
            "Output directory. Default: "
            "demo/pcpl-evolvo/runs/<timestamp>-<profile>"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = (PROJECT_DIR / "runs" / f"{stamp}-{args.profile}").resolve()

    config = ExperimentConfig(
        out_dir=out_dir,
        profile=args.profile,
        seed=args.seed,
        population_size=args.population_size,
        generations=args.generations,
        initial_instructions=args.initial_instructions,
        rounds=args.rounds,
        attacker_population_size=args.attacker_population_size,
        attacker_generations=args.attacker_generations,
        elite_pool=args.elite_pool,
        archive_limit=args.archive_limit,
        resume=not args.no_resume,
    )

    summary = run_experiment(config)

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("[pcpl-evolvo] completed")
    print(f"[pcpl-evolvo] out_dir={summary['out_dir']}")
    print(f"[pcpl-evolvo] best_score={summary['best_score']:.6f}")
    print(f"[pcpl-evolvo] best_attacker_score={summary['best_attacker_score']:.6f}")
    print(f"[pcpl-evolvo] rounds_completed={summary['rounds_completed']}")
    print(f"[pcpl-evolvo] results={summary['results_json']}")
    print(f"[pcpl-evolvo] report={summary['report_path']}")
    print(f"[pcpl-evolvo] archive={summary['archive_path']}")


if __name__ == "__main__":
    main()
