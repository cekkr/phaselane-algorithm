#!/usr/bin/env python3
"""Run PCPL evolvo experiments (single-run or continuous resumable mode)."""

from __future__ import annotations

import argparse
import itertools
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
import sys

PROJECT_DIR = Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pcpl_evolvo.experiment import ExperimentConfig, run_experiment


def _safe_int(value: int, minimum: int = 1) -> int:
    return max(minimum, int(value))


def _param_choices(base: int, *, minimum: int = 1, high_factor: float = 1.5) -> List[int]:
    base = _safe_int(base, minimum=minimum)
    high = _safe_int(round(base * high_factor), minimum=minimum)
    high = max(high, base + 1)
    if high == base:
        return [base]
    return [base, high]


def _combo_label(combo: Dict[str, Any]) -> str:
    return (
        f"p{combo['population_size']}-g{combo['generations']}"
        f"-i{combo['initial_instructions']}"
        f"-ap{combo['attacker_population_size']}"
        f"-ag{combo['attacker_generations']}"
        f"-e{combo['elite_pool']}"
    )


def _build_continuous_grid(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Generate a finite exhaustive parameter grid for continuous sweeps."""
    populations = _param_choices(args.population_size, minimum=4, high_factor=1.5)
    generations = _param_choices(args.generations, minimum=1, high_factor=1.6)
    instructions = _param_choices(args.initial_instructions, minimum=3, high_factor=1.5)
    attacker_populations = _param_choices(
        args.attacker_population_size,
        minimum=3,
        high_factor=1.5,
    )
    attacker_generations = _param_choices(
        args.attacker_generations,
        minimum=1,
        high_factor=1.6,
    )
    elites = _param_choices(args.elite_pool, minimum=4, high_factor=1.4)

    combos: List[Dict[str, Any]] = []
    for values in itertools.product(
        populations,
        generations,
        instructions,
        attacker_populations,
        attacker_generations,
        elites,
    ):
        pop, gen, instr, apop, agen, elite = values
        combos.append(
            {
                "population_size": pop,
                "generations": gen,
                "initial_instructions": instr,
                "attacker_population_size": apop,
                "attacker_generations": agen,
                "elite_pool": min(pop, elite),
                "archive_limit": max(args.archive_limit, min(pop * 6, args.archive_limit * 2)),
            }
        )

    dedup: Dict[str, Dict[str, Any]] = {}
    for combo in combos:
        dedup[_combo_label(combo)] = combo
    return list(dedup.values())


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _run_once(config: ExperimentConfig) -> Dict[str, Any]:
    summary = run_experiment(config)
    summary_path = Path(summary["out_dir"]) / "summary.json"
    _write_json(summary_path, summary)
    return summary


def _print_summary(summary: Dict[str, Any]) -> None:
    print("[pcpl-evolvo] completed")
    print(f"[pcpl-evolvo] out_dir={summary['out_dir']}")
    print(f"[pcpl-evolvo] best_score={summary['best_score']:.6f}")
    print(f"[pcpl-evolvo] best_attacker_score={summary['best_attacker_score']:.6f}")
    print(f"[pcpl-evolvo] rounds_completed={summary['rounds_completed']}")
    print(f"[pcpl-evolvo] results={summary['results_json']}")
    print(f"[pcpl-evolvo] report={summary['report_path']}")
    print(f"[pcpl-evolvo] archive={summary['archive_path']}")
    if "resource_plan" in summary:
        plan = summary["resource_plan"]
        print(
            "[pcpl-evolvo] resources backend={backend} workers={workers} gpu={gpu}".format(
                backend=plan.get("parallel_backend"),
                workers=plan.get("parallel_workers"),
                gpu=plan.get("gpu_backend"),
            )
        )
    if "index_path" in summary:
        print(f"[pcpl-evolvo] index={summary['index_path']}")
    if "conclusion_path" in summary:
        print(f"[pcpl-evolvo] conclusions={summary['conclusion_path']}")


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
    parser.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "Run forever (until Ctrl+C), sweeping all generated parameter "
            "combinations and continuously saving archives/stats."
        ),
    )
    parser.add_argument(
        "--continuous-max-iterations",
        type=int,
        default=0,
        help=(
            "Optional cap for continuous mode iterations; 0 means infinite "
            "(until user stop)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help=(
            "Parallel fitness workers. 0 means auto (use all available CPU cores)."
        ),
    )
    parser.add_argument(
        "--parallel-backend",
        choices=("auto", "process", "thread", "off"),
        default="auto",
        help="Parallel backend for fitness evaluation.",
    )
    parser.add_argument(
        "--no-supervised-guide",
        action="store_true",
        help="Disable optional supervised guide acceleration.",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda", "mps"),
        default="auto",
        help="Preferred compute device for supervised guide.",
    )
    parser.add_argument(
        "--parent-pool-ratio",
        type=float,
        default=0.60,
        help="Fraction of top genomes used as parent pool (less random dispersivity).",
    )
    parser.add_argument(
        "--stagnation-patience",
        type=int,
        default=4,
        help="Generations without improvement before increasing mutation pressure.",
    )
    parser.add_argument(
        "--mutation-floor",
        type=float,
        default=0.12,
        help="Minimum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-ceiling",
        type=float,
        default=0.55,
        help="Maximum adaptive mutation rate.",
    )
    parser.add_argument(
        "--mutation-step",
        type=float,
        default=0.05,
        help="Adaptive mutation step when stagnating/improving.",
    )
    parser.add_argument(
        "--no-statistical-predictive",
        action="store_true",
        help="Disable staged statistical evaluation and run full evaluation on all genomes.",
    )
    parser.add_argument(
        "--quick-cycle-fraction",
        type=float,
        default=0.14,
        help="Initial fraction of cycles used by quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-cycle-fraction",
        type=float,
        default=0.50,
        help="Initial fraction of cycles used by medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--quick-keep-ratio",
        type=float,
        default=0.55,
        help="Initial fraction of genomes kept after quick stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--mid-keep-ratio",
        type=float,
        default=0.30,
        help="Initial fraction of genomes kept after medium stage (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--key-variants",
        type=int,
        default=2,
        help="Initial key generation/sharing variants per scenario (auto-tuned at runtime).",
    )
    parser.add_argument(
        "--no-auto-statistical-tuning",
        action="store_true",
        help="Keep staged fractions/ratios fixed to CLI values (disable real-time auto-tuning).",
    )
    parser.add_argument(
        "--novelty-bonus",
        type=float,
        default=0.03,
        help="Fitness bonus for novel non-duplicate genomes during staged ranking.",
    )
    parser.add_argument(
        "--predictive-penalty",
        type=float,
        default=0.05,
        help="Penalty applied when a genome is cut by predictive stages.",
    )
    parser.add_argument(
        "--device-mhz",
        type=float,
        default=100.0,
        help="Simulated consumer device frequency in MHz.",
    )
    parser.add_argument(
        "--provider-mhz",
        type=float,
        default=300.0,
        help="Simulated provider frequency in MHz.",
    )
    parser.add_argument(
        "--max-test-seconds",
        type=float,
        default=10.0,
        help="Long-horizon timing projection target (seconds).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = (PROJECT_DIR / "runs" / f"{stamp}-{args.profile}").resolve()

    if not args.continuous:
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
            parallel_workers=args.workers,
            parallel_backend=args.parallel_backend,
            use_supervised_guide=not args.no_supervised_guide,
            preferred_device=args.device,
            parent_pool_ratio=args.parent_pool_ratio,
            stagnation_patience=args.stagnation_patience,
            mutation_floor=args.mutation_floor,
            mutation_ceiling=args.mutation_ceiling,
            mutation_step=args.mutation_step,
            statistical_predictive=not args.no_statistical_predictive,
            quick_cycle_fraction=args.quick_cycle_fraction,
            mid_cycle_fraction=args.mid_cycle_fraction,
            quick_keep_ratio=args.quick_keep_ratio,
            mid_keep_ratio=args.mid_keep_ratio,
            key_variant_count=args.key_variants,
            novelty_bonus=args.novelty_bonus,
            predictive_penalty=args.predictive_penalty,
            auto_statistical_tuning=not args.no_auto_statistical_tuning,
            device_mhz=args.device_mhz,
            provider_mhz=args.provider_mhz,
            max_test_time_seconds=args.max_test_seconds,
        )

        summary = _run_once(config)
        _print_summary(summary)
        return

    if args.no_resume:
        print("[pcpl-evolvo] warning: --no-resume ignored in --continuous mode")

    out_dir.mkdir(parents=True, exist_ok=True)
    runs_root = out_dir / "continuous-runs"
    runs_root.mkdir(parents=True, exist_ok=True)

    grid = _build_continuous_grid(args)
    if not grid:
        raise RuntimeError("Continuous grid is empty")

    state_path = out_dir / "continuous-state.json"
    leaderboard_path = out_dir / "continuous-leaderboard.json"
    log_path = out_dir / "continuous.log"

    rng = random.Random(args.seed)
    order = list(range(len(grid)))
    rng.shuffle(order)

    leaderboard: Dict[str, Dict[str, Any]] = {}
    total_iterations = 0
    total_sweeps = 0

    print(
        "[pcpl-evolvo] continuous mode: combos={count} rounds-per-iteration={rounds} output={out}".format(
            count=len(grid),
            rounds=max(1, args.rounds),
            out=out_dir,
        )
    )

    try:
        while True:
            for slot, combo_idx in enumerate(order):
                combo = grid[combo_idx]
                combo_name = _combo_label(combo)
                combo_dir = runs_root / combo_name
                combo_dir.mkdir(parents=True, exist_ok=True)

                run_seed = args.seed + (total_iterations * 7_919) + slot
                config = ExperimentConfig(
                    out_dir=combo_dir,
                    profile=args.profile,
                    seed=run_seed,
                    population_size=combo["population_size"],
                    generations=combo["generations"],
                    initial_instructions=combo["initial_instructions"],
                    rounds=max(1, args.rounds),
                    attacker_population_size=combo["attacker_population_size"],
                    attacker_generations=combo["attacker_generations"],
                    elite_pool=combo["elite_pool"],
                    archive_limit=combo["archive_limit"],
                    resume=True,
                    parallel_workers=args.workers,
                    parallel_backend=args.parallel_backend,
                    use_supervised_guide=not args.no_supervised_guide,
                    preferred_device=args.device,
                    parent_pool_ratio=args.parent_pool_ratio,
                    stagnation_patience=args.stagnation_patience,
                    mutation_floor=args.mutation_floor,
                    mutation_ceiling=args.mutation_ceiling,
                    mutation_step=args.mutation_step,
                    statistical_predictive=not args.no_statistical_predictive,
                    quick_cycle_fraction=args.quick_cycle_fraction,
                    mid_cycle_fraction=args.mid_cycle_fraction,
                    quick_keep_ratio=args.quick_keep_ratio,
                    mid_keep_ratio=args.mid_keep_ratio,
                    key_variant_count=args.key_variants,
                    novelty_bonus=args.novelty_bonus,
                    predictive_penalty=args.predictive_penalty,
                    auto_statistical_tuning=not args.no_auto_statistical_tuning,
                    device_mhz=args.device_mhz,
                    provider_mhz=args.provider_mhz,
                    max_test_time_seconds=args.max_test_seconds,
                )

                print(
                    "[pcpl-evolvo] iter={iter} sweep={sweep} combo={combo} seed={seed}".format(
                        iter=total_iterations,
                        sweep=total_sweeps,
                        combo=combo_name,
                        seed=run_seed,
                    )
                )
                summary = _run_once(config)
                leaderboard[combo_name] = {
                    "combo": combo,
                    "best_score": summary["best_score"],
                    "best_signature": summary["best_signature"],
                    "best_attacker_score": summary["best_attacker_score"],
                    "best_attacker_signature": summary["best_attacker_signature"],
                    "rounds_completed": summary["rounds_completed"],
                    "archive_path": summary["archive_path"],
                    "updated_at": datetime.now().isoformat(),
                }

                top = sorted(
                    leaderboard.values(),
                    key=lambda item: float(item["best_score"]),
                    reverse=True,
                )[:20]
                state_payload = {
                    "continuous": True,
                    "profile": args.profile,
                    "iterations_completed": total_iterations + 1,
                    "sweeps_completed": total_sweeps,
                    "grid_size": len(grid),
                    "latest_combo": combo_name,
                    "latest_summary": summary,
                    "updated_at": datetime.now().isoformat(),
                }
                _write_json(state_path, state_payload)
                _write_json(
                    leaderboard_path,
                    {
                        "updated_at": datetime.now().isoformat(),
                        "leaders": top,
                    },
                )

                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        "{ts} iter={iter} sweep={sweep} combo={combo} score={score:.6f} attacker={attack:.6f} rounds={rounds}\n".format(
                            ts=datetime.now().isoformat(),
                            iter=total_iterations,
                            sweep=total_sweeps,
                            combo=combo_name,
                            score=float(summary["best_score"]),
                            attack=float(summary["best_attacker_score"]),
                            rounds=int(summary["rounds_completed"]),
                        )
                    )

                _print_summary(summary)
                total_iterations += 1

                if (
                    args.continuous_max_iterations > 0
                    and total_iterations >= args.continuous_max_iterations
                ):
                    print(
                        "[pcpl-evolvo] continuous stop: reached --continuous-max-iterations="
                        f"{args.continuous_max_iterations}"
                    )
                    return

            total_sweeps += 1
            rng.shuffle(order)
            print(
                "[pcpl-evolvo] sweep complete: sweeps={sweeps} iterations={iters}".format(
                    sweeps=total_sweeps,
                    iters=total_iterations,
                )
            )
    except KeyboardInterrupt:
        print("[pcpl-evolvo] continuous mode stopped by user (Ctrl+C)")
        print(f"[pcpl-evolvo] state={state_path}")
        print(f"[pcpl-evolvo] leaderboard={leaderboard_path}")
        print(f"[pcpl-evolvo] log={log_path}")


if __name__ == "__main__":
    main()
