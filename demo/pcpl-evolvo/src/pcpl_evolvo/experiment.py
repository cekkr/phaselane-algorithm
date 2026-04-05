"""Continuous experiment runner with persistent co-evolution archives."""

from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bootstrap import ensure_evolvo_importable
from .simulation import (
    PolicyDecision,
    ScenarioConfig,
    ScenarioMetrics,
    default_scenarios,
    ensure_attacker_genome_io,
    ensure_genome_io,
    evaluate_across_scenarios,
)

ensure_evolvo_importable()

from evolvo import GFSLGenome, GFSLInstruction, GFSLEvolver


@dataclass(frozen=True)
class ExperimentConfig:
    out_dir: Path
    profile: str = "fast"
    seed: int = 1337
    population_size: int = 18
    generations: int = 16
    initial_instructions: int = 12
    rounds: int = 1
    attacker_population_size: int = 12
    attacker_generations: int = 6
    elite_pool: int = 12
    archive_limit: int = 64
    resume: bool = True


class SafeGFSLEvolver(GFSLEvolver):
    """GFSLEvolver variant that cannot deadlock on diversity saturation."""

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

            seen = {_canonical_signature(genome) for genome in new_population}
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

                signature = _canonical_signature(child)
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mean_metric(metrics: Sequence[ScenarioMetrics], attr: str) -> float:
    if not metrics:
        return 0.0
    return sum(float(getattr(item, attr)) for item in metrics) / float(len(metrics))


def _metrics_rows(metrics: Sequence[ScenarioMetrics]) -> List[Dict[str, Any]]:
    return [metric.to_dict() for metric in metrics]


def _scenario_table(metrics: Sequence[ScenarioMetrics]) -> str:
    lines = []
    lines.append("| scenario | total | principle | sync | security | cost | op-cost | attacker-adv |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        lines.append(
            "| {name} | {total:.4f} | {principle:.4f} | {sync:.4f} | {security:.4f} | {cost:.4f} | {op_cost:.4f} | {attack_adv:.4f} |".format(
                name=metric.scenario,
                total=metric.total_score,
                principle=metric.principle_score,
                sync=metric.sync_score,
                security=metric.security_score,
                cost=metric.cost_score,
                op_cost=metric.operation_cost_score,
                attack_adv=metric.attacker_advantage_score,
            )
        )
    return "\n".join(lines)


def _prune_to_effective(genome: GFSLGenome) -> GFSLGenome:
    cloned = copy.deepcopy(genome)
    effective = set(cloned.extract_effective_algorithm())
    if not effective:
        cloned.rebuild_validator_state()
        return cloned

    new_instructions = []
    new_activity = []
    for idx, instruction in enumerate(cloned.instructions):
        if idx in effective:
            new_instructions.append(instruction.copy())
            if idx < len(cloned.instruction_activity):
                new_activity.append(copy.deepcopy(cloned.instruction_activity[idx]))

    cloned.instructions = new_instructions
    cloned.instruction_activity = new_activity
    cloned.rebuild_validator_state()
    return cloned


def _canonical_signature(genome: GFSLGenome) -> str:
    pruned = _prune_to_effective(genome)
    fixed_indices = pruned.extract_operation_indices(order="fixed")
    parts = [pruned.instructions[idx].get_signature() for idx in fixed_indices]
    outputs = sorted((int(cat), int(dtype), int(idx)) for cat, dtype, idx in pruned.outputs)
    payload = "|".join(parts) + "|outs=" + json.dumps(outputs, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def _serialize_genome(genome: GFSLGenome, *, role: str) -> Dict[str, Any]:
    pruned = _prune_to_effective(genome)
    return {
        "role": role,
        "genome_type": pruned.genome_type,
        "slot_count": int(pruned.validator.slot_count),
        "outputs": [
            [int(cat), int(dtype), int(idx)] for cat, dtype, idx in pruned.outputs
        ],
        "instructions": [
            {
                "slots": [int(slot) for slot in instruction.slots],
                "weight": instruction.weight,
            }
            for instruction in pruned.instructions
        ],
        "signature": pruned.get_signature(),
        "canonical_signature": _canonical_signature(pruned),
        "effective_size": len(pruned.extract_effective_algorithm()),
    }


def _deserialize_genome(payload: Dict[str, Any]) -> GFSLGenome:
    slot_count = int(payload.get("slot_count", 7))
    genome = GFSLGenome(payload.get("genome_type", "algorithm"), slot_count=slot_count)
    instructions = payload.get("instructions", [])
    genome.instructions = []
    for item in instructions:
        slots = [int(slot) for slot in item.get("slots", [])]
        if not slots:
            continue
        instruction = GFSLInstruction(
            slots=slots,
            slot_count=len(slots),
            weight=item.get("weight"),
        )
        genome.instructions.append(instruction)

    outputs = payload.get("outputs", [])
    genome.outputs = [
        (int(cat), int(dtype), int(idx)) for cat, dtype, idx in outputs
    ]
    genome.rebuild_validator_state()
    return genome


def _load_archive(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "defender_elites": [],
            "attacker_elites": [],
            "rounds": [],
            "updated_at": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "defender_elites": [],
            "attacker_elites": [],
            "rounds": [],
            "updated_at": None,
        }
    payload.setdefault("defender_elites", [])
    payload.setdefault("attacker_elites", [])
    payload.setdefault("rounds", [])
    payload.setdefault("updated_at", None)
    return payload


def _save_archive(path: Path, archive: Dict[str, Any]) -> None:
    archive["updated_at"] = _utc_now_iso()
    path.write_text(json.dumps(archive, indent=2), encoding="utf-8")


def _insert_elite(
    entries: List[Dict[str, Any]],
    record: Dict[str, Any],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    genome_payload = record.get("genome", {})
    if int(genome_payload.get("effective_size", 0)) <= 0:
        return entries

    canonical = str(record["canonical_signature"])
    by_signature: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        key = str(entry["canonical_signature"])
        existing = by_signature.get(key)
        if existing is None or float(entry["score"]) > float(existing["score"]):
            by_signature[key] = entry

    existing = by_signature.get(canonical)
    if existing is None or float(record["score"]) > float(existing["score"]):
        by_signature[canonical] = record

    ordered = sorted(
        by_signature.values(),
        key=lambda item: float(item["score"]),
        reverse=True,
    )
    return ordered[: max(1, int(limit))]


def _seed_population_from_archive(
    *,
    evolver: SafeGFSLEvolver,
    archive_elites: Sequence[Dict[str, Any]],
    io_initializer,
    population_size: int,
    initial_instructions: int,
    elite_pool: int,
) -> None:
    evolver.initialize_population("algorithm", initial_instructions=initial_instructions)
    seeded: List[GFSLGenome] = []
    seen: set[str] = set()

    def push(genome: GFSLGenome) -> bool:
        io_initializer(genome)
        signature = _canonical_signature(genome)
        if signature in seen:
            return False
        seen.add(signature)
        seeded.append(genome)
        return True

    for entry in archive_elites[: max(1, elite_pool)]:
        try:
            genome = _deserialize_genome(entry["genome"])
            push(genome)
        except Exception:
            continue

    # Add random mutations from elites so each round explores around known tops.
    elite_mutation_parents = seeded[:]
    for parent in elite_mutation_parents:
        if len(seeded) >= population_size:
            break
        try:
            mutated = evolver.mutate(copy.deepcopy(parent))
            push(mutated)
        except Exception:
            continue

    for genome in evolver.population:
        if len(seeded) >= population_size:
            break
        push(genome)

    while len(seeded) < population_size:
        fallback = copy.deepcopy(random.choice(evolver.population))
        io_initializer(fallback)
        seeded.append(fallback)

    evolver.population = seeded[:population_size]


def _build_round_report(
    *,
    config: ExperimentConfig,
    round_index: int,
    scenarios: Sequence[ScenarioConfig],
    defender_score: float,
    defender_signature: str,
    defender_metrics: Sequence[ScenarioMetrics],
    attacker_score: float,
    attacker_signature: str,
    defender_log: Sequence[Dict[str, Any]],
    attacker_log: Sequence[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append("# PCPL Evolvo Continuous Round")
    lines.append("")
    lines.append(f"- round: `{round_index}`")
    lines.append(f"- profile: `{config.profile}`")
    lines.append(f"- defender score: `{defender_score:.6f}`")
    lines.append(f"- defender signature: `{defender_signature}`")
    lines.append(f"- attacker score: `{attacker_score:.6f}`")
    lines.append(f"- attacker signature: `{attacker_signature}`")
    lines.append("")
    lines.append("## Scenarios")
    for scenario in scenarios:
        lines.append(
            "- `{name}`: x={x}, cycles={cycles}, budget_ms={budget}, abs_ref_ms={abs_ms}".format(
                name=scenario.name,
                x=scenario.x,
                cycles=scenario.cycles,
                budget=scenario.cycle_budget_ms,
                abs_ms=scenario.absolute_time_ms,
            )
        )
    lines.append("")
    lines.append("## Defender Metrics")
    lines.append("")
    lines.append(_scenario_table(defender_metrics))
    lines.append("")
    lines.append("## Defender Evolution")
    lines.append("")
    lines.append("| gen | best | principle | security | cost | sync-loss | attacker-adv |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in defender_log:
        lines.append(
            "| {generation} | {best_score:.4f} | {principle:.4f} | {security:.4f} | {cost:.4f} | {sync_loss:.4f} | {attacker_adv:.4f} |".format(
                generation=row["generation"],
                best_score=row["best_score"],
                principle=row["principle"],
                security=row["security"],
                cost=row["cost"],
                sync_loss=row["sync_loss"],
                attacker_adv=row["attacker_adv"],
            )
        )
    lines.append("")
    lines.append("## Attacker Evolution")
    lines.append("")
    lines.append("| gen | attack_score | lane_success | token_success | attacker_adv |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for row in attacker_log:
        lines.append(
            "| {generation} | {attack_score:.4f} | {lane_success:.4f} | {token_success:.4f} | {attacker_adv:.4f} |".format(
                generation=row["generation"],
                attack_score=row["attack_score"],
                lane_success=row["lane_success"],
                token_success=row["token_success"],
                attacker_adv=row["attacker_adv"],
            )
        )
    return "\n".join(lines) + "\n"


def _baseline_rows(scenarios: Sequence[ScenarioConfig]) -> List[Dict[str, Any]]:
    baselines = [
        ("reference-full", PolicyDecision(active_ratio=1.0, kernel=0, stride_seed=0, state_mix=0.5)),
        ("balanced", PolicyDecision(active_ratio=0.65, kernel=1, stride_seed=19, state_mix=0.5)),
        ("minimal-cost", PolicyDecision(active_ratio=0.25, kernel=2, stride_seed=31, state_mix=0.5)),
    ]
    rows: List[Dict[str, Any]] = []
    for name, policy in baselines:
        score, metrics = evaluate_across_scenarios(scenarios, None, fixed_decision=policy)
        rows.append({
            "name": name,
            "mean_score": score,
            "metrics": _metrics_rows(metrics),
        })
    return rows


def _run_defender_round(
    *,
    config: ExperimentConfig,
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    attacker: Optional[GFSLGenome],
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    evolver = SafeGFSLEvolver(population_size=config.population_size)
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=archive.get("defender_elites", []),
        io_initializer=ensure_genome_io,
        population_size=config.population_size,
        initial_instructions=config.initial_instructions,
        elite_pool=config.elite_pool,
    )

    generation_log: List[Dict[str, Any]] = []

    def fitness(genome: GFSLGenome) -> float:
        ensure_genome_io(genome)
        score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
        genome._pcpl_metrics = metrics  # type: ignore[attr-defined]
        return score

    def progress(gen: int, best: GFSLGenome, best_fitness: float) -> None:
        metrics = getattr(best, "_pcpl_metrics", None)
        if metrics is None:
            _, metrics = evaluate_across_scenarios(scenarios, best, attacker=attacker)
            best._pcpl_metrics = metrics

        row = {
            "generation": int(gen),
            "best_score": float(best_fitness),
            "principle": _mean_metric(metrics, "principle_score"),
            "security": _mean_metric(metrics, "security_score"),
            "cost": _mean_metric(metrics, "cost_score"),
            "sync_loss": _mean_metric(metrics, "sync_loss_rate"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][defender] gen={gen:03d} score={score:.5f} sec={security:.4f} cost={cost:.4f} attack_adv={attack_adv:.4f}".format(
                gen=gen,
                score=best_fitness,
                security=row["security"],
                cost=row["cost"],
                attack_adv=row["attacker_adv"],
            )
        )

    evolver.evolve(config.generations, fitness, progress_callback=progress)
    return evolver, generation_log


def _run_attacker_round(
    *,
    config: ExperimentConfig,
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    defender: GFSLGenome,
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    evolver = SafeGFSLEvolver(population_size=config.attacker_population_size)
    _seed_population_from_archive(
        evolver=evolver,
        archive_elites=archive.get("attacker_elites", []),
        io_initializer=ensure_attacker_genome_io,
        population_size=config.attacker_population_size,
        initial_instructions=max(4, config.initial_instructions // 2),
        elite_pool=max(4, config.elite_pool // 2),
    )

    generation_log: List[Dict[str, Any]] = []

    def fitness(attacker: GFSLGenome) -> float:
        ensure_attacker_genome_io(attacker)
        _, metrics = evaluate_across_scenarios(scenarios, defender, attacker=attacker)
        attacker._attack_metrics = metrics  # type: ignore[attr-defined]
        attack_adv = _mean_metric(metrics, "attacker_advantage_score")
        lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
        token_success = _mean_metric(metrics, "attacker_token_success_rate")
        effective = len(attacker.extract_effective_algorithm())
        if effective == 0:
            return attack_adv - 0.08
        complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
        return attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty

    def progress(gen: int, best: GFSLGenome, best_fitness: float) -> None:
        metrics = getattr(best, "_attack_metrics", None)
        if metrics is None:
            _, metrics = evaluate_across_scenarios(scenarios, defender, attacker=best)
            best._attack_metrics = metrics
        row = {
            "generation": int(gen),
            "attack_score": float(best_fitness),
            "lane_success": _mean_metric(metrics, "attacker_lane_success_rate"),
            "token_success": _mean_metric(metrics, "attacker_token_success_rate"),
            "attacker_adv": _mean_metric(metrics, "attacker_advantage_score"),
        }
        generation_log.append(row)
        print(
            "[pcpl-evolvo][attacker] gen={gen:03d} score={score:.5f} lane={lane:.4f} token={token:.4f}".format(
                gen=gen,
                score=best_fitness,
                lane=row["lane_success"],
                token=row["token_success"],
            )
        )

    evolver.evolve(config.attacker_generations, fitness, progress_callback=progress)
    return evolver, generation_log


def run_continuous_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Run persistent defender/attacker co-evolution for one or more rounds."""
    scenario_list = list(scenarios) if scenarios is not None else default_scenarios(config.profile)

    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)

    archive_path = out_dir / "archive.json"
    archive = _load_archive(archive_path) if config.resume else _load_archive(Path("/dev/null"))
    baseline_rows = _baseline_rows(scenario_list)

    random.seed(config.seed)

    last_defender_score = -float("inf")
    last_attacker_score = -float("inf")
    last_defender_signature = ""
    last_attacker_signature = ""
    last_round_dir = None

    current_attacker: Optional[GFSLGenome] = None
    if archive.get("attacker_elites"):
        try:
            current_attacker = _deserialize_genome(archive["attacker_elites"][0]["genome"])
            ensure_attacker_genome_io(current_attacker)
        except Exception:
            current_attacker = None

    start_round = len(archive.get("rounds", []))
    for offset in range(max(1, config.rounds)):
        round_index = start_round + offset
        round_dir = rounds_dir / f"round-{round_index:04d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        # Defender evolution under current strongest attacker.
        defender_evolver, defender_log = _run_defender_round(
            config=config,
            scenarios=scenario_list,
            archive=archive,
            attacker=current_attacker,
        )

        # Preliminary best defender from the round.
        preliminary_defender = defender_evolver.population[0]

        # Attacker co-evolution against this defender.
        attacker_evolver, attacker_log = _run_attacker_round(
            config=config,
            scenarios=scenario_list,
            archive=archive,
            defender=preliminary_defender,
        )
        best_attacker = attacker_evolver.population[0]
        ensure_attacker_genome_io(best_attacker)

        # Select robust defender among top candidates against the new attacker.
        top_candidates = [
            genome for genome in defender_evolver.population
            if len(genome.extract_effective_algorithm()) > 0
        ]
        if not top_candidates:
            top_candidates = defender_evolver.population[:]
        top_candidates = top_candidates[: min(5, len(top_candidates))]
        selected_defender = top_candidates[0]
        selected_metrics: List[ScenarioMetrics] = []
        selected_score = -float("inf")
        for candidate in top_candidates:
            ensure_genome_io(candidate)
            score, metrics = evaluate_across_scenarios(
                scenario_list,
                candidate,
                attacker=best_attacker,
            )
            if score > selected_score:
                selected_score = score
                selected_metrics = metrics
                selected_defender = candidate

        attacker_score, attacker_metrics = evaluate_across_scenarios(
            scenario_list,
            selected_defender,
            attacker=best_attacker,
        )
        attack_adv = _mean_metric(attacker_metrics, "attacker_advantage_score")

        defender_signature = selected_defender.get_signature()
        attacker_signature = best_attacker.get_signature()

        defender_record = {
            "role": "defender",
            "round": round_index,
            "timestamp": _utc_now_iso(),
            "score": float(selected_score),
            "signature": defender_signature,
            "canonical_signature": _canonical_signature(selected_defender),
            "metrics": _metrics_rows(selected_metrics),
            "genome": _serialize_genome(selected_defender, role="defender"),
        }
        attacker_record = {
            "role": "attacker",
            "round": round_index,
            "timestamp": _utc_now_iso(),
            "score": float(attack_adv),
            "signature": attacker_signature,
            "canonical_signature": _canonical_signature(best_attacker),
            "metrics": _metrics_rows(attacker_metrics),
            "genome": _serialize_genome(best_attacker, role="attacker"),
        }

        archive["defender_elites"] = _insert_elite(
            archive.get("defender_elites", []),
            defender_record,
            limit=config.archive_limit,
        )
        archive["attacker_elites"] = _insert_elite(
            archive.get("attacker_elites", []),
            attacker_record,
            limit=config.archive_limit,
        )

        round_summary = {
            "round": round_index,
            "timestamp": _utc_now_iso(),
            "defender_score": float(selected_score),
            "defender_signature": defender_signature,
            "attacker_score": float(attack_adv),
            "attacker_signature": attacker_signature,
            "defender_log": defender_log,
            "attacker_log": attacker_log,
            "metrics": _metrics_rows(selected_metrics),
        }
        archive.setdefault("rounds", []).append(round_summary)

        # Round artifacts.
        (round_dir / "defender-genome.txt").write_text(
            "\n".join(selected_defender.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (round_dir / "attacker-genome.txt").write_text(
            "\n".join(best_attacker.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (round_dir / "round-results.json").write_text(
            json.dumps(round_summary, indent=2),
            encoding="utf-8",
        )
        round_report = _build_round_report(
            config=config,
            round_index=round_index,
            scenarios=scenario_list,
            defender_score=selected_score,
            defender_signature=defender_signature,
            defender_metrics=selected_metrics,
            attacker_score=attack_adv,
            attacker_signature=attacker_signature,
            defender_log=defender_log,
            attacker_log=attacker_log,
        )
        (round_dir / "round-report.md").write_text(round_report, encoding="utf-8")

        _save_archive(archive_path, archive)

        current_attacker = best_attacker
        last_defender_score = selected_score
        last_attacker_score = attack_adv
        last_defender_signature = defender_signature
        last_attacker_signature = attacker_signature
        last_round_dir = round_dir

        print(
            "[pcpl-evolvo] round={round:04d} defender={def_score:.6f} attacker={atk_score:.6f}".format(
                round=round_index,
                def_score=selected_score,
                atk_score=attack_adv,
            )
        )

    # Build global summary and report.
    final_summary = {
        "config": {
            **asdict(config),
            "out_dir": str(out_dir),
        },
        "baselines": baseline_rows,
        "rounds_completed": len(archive.get("rounds", [])),
        "best_defender": archive.get("defender_elites", [])[:1],
        "best_attacker": archive.get("attacker_elites", [])[:1],
        "last_round": {
            "score": last_defender_score,
            "signature": last_defender_signature,
            "attacker_score": last_attacker_score,
            "attacker_signature": last_attacker_signature,
            "round_dir": str(last_round_dir) if last_round_dir else None,
        },
    }

    results_json = out_dir / "results.json"
    results_json.write_text(json.dumps(final_summary, indent=2), encoding="utf-8")

    report_lines: List[str] = []
    report_lines.append("# PCPL Evolvo Continuous Report")
    report_lines.append("")
    report_lines.append(f"- profile: `{config.profile}`")
    report_lines.append(f"- rounds completed: `{len(archive.get('rounds', []))}`")
    report_lines.append(f"- best defender score: `{last_defender_score:.6f}`")
    report_lines.append(f"- best attacker score: `{last_attacker_score:.6f}`")
    report_lines.append("")
    report_lines.append("## Baselines")
    report_lines.append("")
    report_lines.append("| policy | mean score |")
    report_lines.append("| --- | ---: |")
    for row in baseline_rows:
        report_lines.append(f"| {row['name']} | {row['mean_score']:.4f} |")
    report_lines.append("")
    report_lines.append("## Latest Round")
    report_lines.append("")
    if last_round_dir:
        report_lines.append(f"- round dir: `{last_round_dir}`")
    report_lines.append(f"- defender signature: `{last_defender_signature}`")
    report_lines.append(f"- attacker signature: `{last_attacker_signature}`")

    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    summary = {
        "out_dir": str(out_dir),
        "results_json": str(results_json),
        "report_path": str(report_path),
        "archive_path": str(archive_path),
        "best_score": last_defender_score,
        "best_signature": last_defender_signature,
        "best_attacker_score": last_attacker_score,
        "best_attacker_signature": last_attacker_signature,
        "rounds_completed": len(archive.get("rounds", [])),
    }
    return summary


def run_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Backward-compatible entry point; now supports continuous rounds."""
    return run_continuous_experiment(config, scenarios=scenarios)
