"""Experiment runner: Evolvo search + baseline comparison + reporting."""

from __future__ import annotations

import json
import copy
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable
from .simulation import (
    PolicyDecision,
    ScenarioConfig,
    ScenarioMetrics,
    default_scenarios,
    ensure_genome_io,
    evaluate_across_scenarios,
)

ensure_evolvo_importable()

from evolvo import GFSLEvolver


@dataclass(frozen=True)
class ExperimentConfig:
    out_dir: Path
    profile: str = "fast"
    seed: int = 1337
    population_size: int = 18
    generations: int = 16
    initial_instructions: int = 12


class SafeGFSLEvolver(GFSLEvolver):
    """
    Wrapper around GFSLEvolver that prevents deadlock when diversity saturates.

    Upstream evolver keeps a monotonic diversity cache and can loop indefinitely
    while trying to create only-new signatures. For practical experiments we cap
    attempts and allow fallback cloning so runs always terminate.
    """

    def evolve(
        self,
        generations: int,
        evaluator,
        progress_callback=None,
    ):
        for gen in range(generations):
            self.generation = gen

            for genome in self.population:
                if genome.fitness is None:
                    try:
                        genome.fitness = evaluator(genome)
                    except Exception:
                        genome.fitness = -float("inf")

            self.population.sort(
                key=lambda g: g.fitness or -float("inf"), reverse=True
            )

            if self.supervised_guide:
                self.supervised_guide.observe_population(self.population)

            if progress_callback and self.population:
                best = self.population[0]
                progress_callback(gen, best, best.fitness or -float("inf"))

            elite_size = max(1, int(self.population_size * self.elite_ratio))
            new_population = self.population[:elite_size]

            # Keep the diversity set local to the current generation only.
            seen = {genome.get_signature() for genome in new_population}
            attempts = 0
            max_attempts = max(self.population_size * 80, 200)

            while len(new_population) < self.population_size and attempts < max_attempts:
                attempts += 1
                parent1 = self._tournament_select()
                parent2 = self._tournament_select()

                if random.random() < self.crossover_rate:
                    child = self.crossover(parent1, parent2)
                else:
                    child = copy.deepcopy(random.choice([parent1, parent2]))

                child.fitness = None
                child.generation = gen + 1

                if random.random() < self.mutation_rate:
                    if self.supervised_guide:
                        child = self.supervised_guide.propose_mutation(self, child)
                    else:
                        child = self.mutate(child)
                    child.fitness = None
                    child.generation = gen + 1

                signature = child.get_signature()
                if signature in seen:
                    continue
                seen.add(signature)
                new_population.append(child)

            while len(new_population) < self.population_size:
                fallback = copy.deepcopy(random.choice(self.population[:elite_size]))
                fallback.fitness = None
                fallback.generation = gen + 1
                new_population.append(fallback)

            self.population = new_population[: self.population_size]


def _metrics_rows(metrics: Sequence[ScenarioMetrics]) -> List[Dict[str, Any]]:
    return [metric.to_dict() for metric in metrics]


def _scenario_table(metrics: Sequence[ScenarioMetrics]) -> str:
    lines = []
    lines.append("| scenario | total | principle | sync | security | cost | runtime |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        lines.append(
            "| {name} | {total:.4f} | {principle:.4f} | {sync:.4f} | {security:.4f} | {cost:.4f} | {runtime:.4f} |".format(
                name=metric.scenario,
                total=metric.total_score,
                principle=metric.principle_score,
                sync=metric.sync_score,
                security=metric.security_score,
                cost=metric.cost_score,
                runtime=metric.runtime_score,
            )
        )
    return "\n".join(lines)


def _build_markdown_report(
    *,
    config: ExperimentConfig,
    scenarios: Sequence[ScenarioConfig],
    baselines: Sequence[Dict[str, Any]],
    best_summary: Dict[str, Any],
    generation_log: Sequence[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append("# PCPL Evolvo Empirical Report")
    lines.append("")
    lines.append("Generated automatically by `demo/pcpl-evolvo/run_experiments.py`.")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- profile: `{config.profile}`")
    lines.append(f"- seed: `{config.seed}`")
    lines.append(f"- population_size: `{config.population_size}`")
    lines.append(f"- generations: `{config.generations}`")
    lines.append(f"- initial_instructions: `{config.initial_instructions}`")
    lines.append("")
    lines.append("### Scenarios")
    for scenario in scenarios:
        lines.append(
            "- `{name}`: x={x}, cycles={cycles}, seed={seed}, prime_mode={mode}, compounds={compounds}".format(
                name=scenario.name,
                x=scenario.x,
                cycles=scenario.cycles,
                seed=scenario.seed,
                mode=scenario.prime_mode,
                compounds=scenario.compound_count,
            )
        )

    lines.append("")
    lines.append("## Baselines")
    lines.append("")
    lines.append("| policy | mean score |")
    lines.append("| --- | ---: |")
    for baseline in baselines:
        lines.append(
            f"| {baseline['name']} | {baseline['mean_score']:.4f} |"
        )

    lines.append("")
    for baseline in baselines:
        lines.append(f"### Baseline: {baseline['name']}")
        lines.append("")
        lines.append(_scenario_table(baseline["metrics"]))
        lines.append("")

    lines.append("## Best Evolved Circuit")
    lines.append("")
    lines.append(f"- mean score: `{best_summary['mean_score']:.4f}`")
    lines.append(f"- signature: `{best_summary['signature']}`")
    lines.append(f"- effective instructions: `{best_summary['effective_size']}`")
    lines.append("")
    lines.append("### Scenario Breakdown")
    lines.append("")
    lines.append(_scenario_table(best_summary["metrics"]))
    lines.append("")
    lines.append("### Effective Instruction Trace")
    lines.append("")
    lines.append("```text")
    for line in best_summary["human_readable"]:
        lines.append(line)
    lines.append("```")

    lines.append("")
    lines.append("## Evolution Log")
    lines.append("")
    lines.append("| gen | best_score | principle | security | cost | runtime |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in generation_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {security:.4f} | {cost:.4f} | {runtime:.4f} |".format(
                generation=row["generation"],
                best_score=row["best_score"],
                principle=row["principle"],
                security=row["security"],
                cost=row["cost"],
                runtime=row["runtime"],
            )
        )

    return "\n".join(lines) + "\n"


def run_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Run the complete PCPL evolvo experiment and write report artifacts."""
    scenario_list = list(scenarios) if scenarios is not None else default_scenarios(config.profile)

    random.seed(config.seed)

    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    baselines: List[Dict[str, Any]] = []
    baseline_policies = [
        ("reference-full", PolicyDecision(active_ratio=1.0, kernel=0, stride_seed=0, state_mix=0.5)),
        ("balanced", PolicyDecision(active_ratio=0.65, kernel=1, stride_seed=19, state_mix=0.5)),
        ("minimal-cost", PolicyDecision(active_ratio=0.25, kernel=2, stride_seed=31, state_mix=0.5)),
    ]
    for name, decision in baseline_policies:
        score, metrics = evaluate_across_scenarios(scenario_list, None, fixed_decision=decision)
        baselines.append(
            {
                "name": name,
                "mean_score": score,
                "metrics": metrics,
            }
        )

    evolver = SafeGFSLEvolver(population_size=config.population_size)
    evolver.initialize_population("algorithm", initial_instructions=config.initial_instructions)
    for genome in evolver.population:
        ensure_genome_io(genome)

    generation_log: List[Dict[str, Any]] = []

    def fitness(genome) -> float:
        ensure_genome_io(genome)
        score, metrics = evaluate_across_scenarios(scenario_list, genome)
        genome._pcpl_metrics = metrics  # type: ignore[attr-defined]
        return score

    def progress(gen: int, best, best_fitness: float) -> None:
        metrics = getattr(best, "_pcpl_metrics", None)
        if not metrics:
            _, metrics = evaluate_across_scenarios(scenario_list, best)
            best._pcpl_metrics = metrics
        principle = sum(item.principle_score for item in metrics) / float(len(metrics))
        security = sum(item.security_score for item in metrics) / float(len(metrics))
        cost = sum(item.cost_score for item in metrics) / float(len(metrics))
        runtime = sum(item.runtime_score for item in metrics) / float(len(metrics))
        row = {
            "generation": gen,
            "best_score": float(best_fitness),
            "principle": float(principle),
            "security": float(security),
            "cost": float(cost),
            "runtime": float(runtime),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo] gen={gen:03d} score={score:.5f} principle={principle:.4f} security={security:.4f} cost={cost:.4f}".format(
                gen=gen,
                score=best_fitness,
                principle=principle,
                security=security,
                cost=cost,
            )
        )

    started = time.perf_counter()
    evolver.evolve(config.generations, fitness, progress_callback=progress)
    elapsed = time.perf_counter() - started

    best = evolver.population[0]
    best_score, best_metrics = evaluate_across_scenarios(scenario_list, best)
    best_signature = best.get_signature()
    best_human = best.to_human_readable(include_weights=False)
    best_effective_size = len(best.extract_effective_algorithm())

    best_summary = {
        "mean_score": best_score,
        "signature": best_signature,
        "effective_size": best_effective_size,
        "metrics": best_metrics,
        "human_readable": best_human,
    }

    output_payload = {
        "config": {
            **asdict(config),
            "out_dir": str(out_dir),
            "elapsed_seconds": elapsed,
        },
        "scenarios": [asdict(scenario) for scenario in scenario_list],
        "baselines": [
            {
                "name": item["name"],
                "mean_score": item["mean_score"],
                "metrics": _metrics_rows(item["metrics"]),
            }
            for item in baselines
        ],
        "best": {
            "mean_score": best_summary["mean_score"],
            "signature": best_summary["signature"],
            "effective_size": best_summary["effective_size"],
            "metrics": _metrics_rows(best_summary["metrics"]),
            "human_readable": best_summary["human_readable"],
        },
        "generation_log": generation_log,
    }

    results_json = out_dir / "results.json"
    results_json.write_text(json.dumps(output_payload, indent=2), encoding="utf-8")

    jsonl_path = out_dir / "generation-log.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in generation_log:
            handle.write(json.dumps(row) + "\n")

    genome_txt = out_dir / "best-genome.txt"
    genome_txt.write_text("\n".join(best_human) + "\n", encoding="utf-8")

    report_md = _build_markdown_report(
        config=config,
        scenarios=scenario_list,
        baselines=baselines,
        best_summary=best_summary,
        generation_log=generation_log,
    )
    report_path = out_dir / "report.md"
    report_path.write_text(report_md, encoding="utf-8")

    summary = {
        "out_dir": str(out_dir),
        "results_json": str(results_json),
        "report_path": str(report_path),
        "best_score": best_score,
        "best_signature": best_signature,
        "elapsed_seconds": elapsed,
    }
    return summary
