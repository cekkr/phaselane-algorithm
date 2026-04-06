"""Continuous experiment runner with persistent co-evolution archives."""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import os
import platform
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
    parallel_workers: int = 0
    parallel_backend: str = "auto"  # auto|process|thread|off
    use_supervised_guide: bool = True
    preferred_device: str = "auto"  # auto|cpu|cuda|mps


@dataclass(frozen=True)
class ResourcePlan:
    cpu_count: int
    parallel_workers: int
    parallel_backend: str
    gpu_backend: str
    gpu_available: bool
    torch_available: bool
    platform: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SafeGFSLEvolver(GFSLEvolver):
    """GFSLEvolver variant that cannot deadlock on diversity saturation."""

    def evolve(
        self,
        generations: int,
        evaluator,
        progress_callback=None,
        batch_evaluator=None,
    ):
        for gen in range(generations):
            self.generation = gen

            if batch_evaluator is not None:
                try:
                    batch_evaluator(self.population)
                except Exception:
                    for genome in self.population:
                        if genome.fitness is None:
                            try:
                                genome.fitness = evaluator(genome)
                            except Exception:
                                genome.fitness = -float("inf")
            else:
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


def _detect_gpu_backend(preferred_device: str = "auto") -> Tuple[str, bool, bool]:
    torch_available = False
    backend = "none"
    try:
        import torch  # type: ignore

        torch_available = True
        if preferred_device != "auto":
            preferred = preferred_device.lower()
            if preferred == "cuda" and torch.cuda.is_available():
                backend = "cuda"
            elif preferred == "mps":
                mps_ok = bool(
                    hasattr(torch.backends, "mps")
                    and torch.backends.mps.is_available()
                )
                if mps_ok:
                    backend = "mps"
            elif preferred == "cpu":
                backend = "none"
        else:
            if torch.cuda.is_available():
                backend = "cuda"
            elif (
                hasattr(torch.backends, "mps")
                and torch.backends.mps.is_available()
            ):
                backend = "mps"
    except Exception:
        torch_available = False
        backend = "none"

    return backend, backend != "none", torch_available


def _resolve_resource_plan(config: ExperimentConfig, max_population: int) -> ResourcePlan:
    cpu_count = max(1, int(os.cpu_count() or 1))
    workers = int(config.parallel_workers)
    if workers <= 0:
        workers = cpu_count
    workers = max(1, min(workers, max(1, max_population)))

    requested_backend = str(config.parallel_backend).lower()
    if requested_backend not in {"auto", "process", "thread", "off"}:
        requested_backend = "auto"
    if requested_backend == "off":
        resolved_backend = "off"
        workers = 1
    elif requested_backend == "auto":
        resolved_backend = "process" if workers > 1 else "off"
    else:
        resolved_backend = requested_backend if workers > 1 else "off"

    if platform.system().lower().startswith("win") and resolved_backend == "process":
        # Keep behavior predictable on macOS/Linux request, but avoid fragile forks elsewhere.
        resolved_backend = "thread"

    gpu_backend, gpu_available, torch_available = _detect_gpu_backend(config.preferred_device)
    return ResourcePlan(
        cpu_count=cpu_count,
        parallel_workers=workers,
        parallel_backend=resolved_backend,
        gpu_backend=gpu_backend,
        gpu_available=gpu_available,
        torch_available=torch_available,
        platform=platform.platform(),
    )


def _metrics_from_rows(rows: Sequence[Dict[str, Any]]) -> List[ScenarioMetrics]:
    return [ScenarioMetrics(**row) for row in rows]


def _build_supervised_guide_if_available(
    config: ExperimentConfig,
    plan: ResourcePlan,
):
    if not config.use_supervised_guide or not plan.torch_available:
        return None
    try:
        from evolvo.supervised import GFSLSupervisedGuide
    except Exception:
        return None

    if config.preferred_device.lower() != "auto":
        device = config.preferred_device.lower()
    elif plan.gpu_backend != "none":
        device = plan.gpu_backend
    else:
        device = "cpu"
    try:
        return GFSLSupervisedGuide(device=device)
    except Exception:
        return None


def _defender_eval_worker(task: Tuple[GFSLGenome, Sequence[ScenarioConfig], Optional[GFSLGenome]]) -> Tuple[float, List[Dict[str, Any]]]:
    genome, scenarios, attacker = task
    ensure_genome_io(genome)
    if attacker is not None:
        ensure_attacker_genome_io(attacker)
    score, metrics = evaluate_across_scenarios(scenarios, genome, attacker=attacker)
    return float(score), _metrics_rows(metrics)


def _attacker_eval_worker(task: Tuple[GFSLGenome, GFSLGenome, Sequence[ScenarioConfig]]) -> Tuple[float, List[Dict[str, Any]]]:
    attacker, defender, scenarios = task
    ensure_attacker_genome_io(attacker)
    ensure_genome_io(defender)
    _, metrics = evaluate_across_scenarios(scenarios, defender, attacker=attacker)
    attack_adv = _mean_metric(metrics, "attacker_advantage_score")
    lane_success = _mean_metric(metrics, "attacker_lane_success_rate")
    token_success = _mean_metric(metrics, "attacker_token_success_rate")
    effective = len(attacker.extract_effective_algorithm())
    if effective == 0:
        score = attack_adv - 0.08
    else:
        complexity_penalty = min(0.10, max(0.0, (effective - 18) * 0.0025))
        score = attack_adv + (0.04 * lane_success) + (0.02 * token_success) - complexity_penalty
    return float(score), _metrics_rows(metrics)


def _evaluate_pending_parallel(
    *,
    pending: Sequence[Tuple[int, GFSLGenome]],
    backend: str,
    workers: int,
    worker_fn,
    build_task,
    attr_name: str,
) -> None:
    if not pending:
        return

    if backend == "off" or workers <= 1:
        for _, genome in pending:
            score, rows = worker_fn(build_task(genome))
            setattr(genome, attr_name, _metrics_from_rows(rows))
            genome.fitness = float(score)
        return

    tasks = [build_task(genome) for _, genome in pending]
    if backend == "process":
        executor_cls = concurrent.futures.ProcessPoolExecutor
    else:
        executor_cls = concurrent.futures.ThreadPoolExecutor

    with executor_cls(max_workers=workers) as executor:
        results = list(executor.map(worker_fn, tasks))

    for (_, genome), (score, rows) in zip(pending, results):
        setattr(genome, attr_name, _metrics_from_rows(rows))
        genome.fitness = float(score)


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


def _leaderboard_markdown(entries: Sequence[Dict[str, Any]], *, title: str) -> str:
    lines = [f"# {title}", "", "| rank | score | round | signature | effective |", "| ---: | ---: | ---: | --- | ---: |"]
    for idx, entry in enumerate(entries, start=1):
        genome = entry.get("genome", {})
        lines.append(
            "| {rank} | {score:.6f} | {round_idx} | `{sig}` | {effective} |".format(
                rank=idx,
                score=float(entry.get("score", 0.0)),
                round_idx=int(entry.get("round", -1)),
                sig=str(entry.get("signature", "")),
                effective=int(genome.get("effective_size", 0)),
            )
        )
    return "\n".join(lines) + "\n"


def _mean_metrics_row(metrics_rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not metrics_rows:
        return {}
    keys = [k for k, v in metrics_rows[0].items() if isinstance(v, (float, int))]
    means: Dict[str, float] = {}
    for key in keys:
        values = [float(row.get(key, 0.0)) for row in metrics_rows]
        means[key] = sum(values) / float(max(1, len(values)))
    return means


def _write_view_outputs(
    *,
    out_dir: Path,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    archive: Dict[str, Any],
    baseline_rows: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    views_dir = out_dir / "views"
    best_dir = out_dir / "best"
    leaderboards_dir = out_dir / "leaderboards"
    summaries_dir = out_dir / "summaries"
    for path in (views_dir, best_dir, leaderboards_dir, summaries_dir):
        path.mkdir(parents=True, exist_ok=True)

    defender_top = list(archive.get("defender_elites", []))
    attacker_top = list(archive.get("attacker_elites", []))

    defender_top10 = defender_top[:10]
    attacker_top10 = attacker_top[:10]
    (leaderboards_dir / "defender-top10.json").write_text(
        json.dumps(defender_top10, indent=2),
        encoding="utf-8",
    )
    (leaderboards_dir / "attacker-top10.json").write_text(
        json.dumps(attacker_top10, indent=2),
        encoding="utf-8",
    )
    (leaderboards_dir / "defender-top10.md").write_text(
        _leaderboard_markdown(defender_top10, title="Defender Top 10"),
        encoding="utf-8",
    )
    (leaderboards_dir / "attacker-top10.md").write_text(
        _leaderboard_markdown(attacker_top10, title="Attacker Top 10"),
        encoding="utf-8",
    )

    best_defender = defender_top10[:1]
    best_attacker = attacker_top10[:1]
    best_paths: Dict[str, str] = {}
    if best_defender:
        defender_entry = best_defender[0]
        defender_genome = _deserialize_genome(defender_entry["genome"])
        defender_path = best_dir / "best-defender-genome.txt"
        defender_path.write_text(
            "\n".join(defender_genome.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (best_dir / "best-defender.json").write_text(
            json.dumps(defender_entry, indent=2),
            encoding="utf-8",
        )
        best_paths["best_defender_genome"] = str(defender_path)

    if best_attacker:
        attacker_entry = best_attacker[0]
        attacker_genome = _deserialize_genome(attacker_entry["genome"])
        attacker_path = best_dir / "best-attacker-genome.txt"
        attacker_path.write_text(
            "\n".join(attacker_genome.to_human_readable()) + "\n",
            encoding="utf-8",
        )
        (best_dir / "best-attacker.json").write_text(
            json.dumps(attacker_entry, indent=2),
            encoding="utf-8",
        )
        best_paths["best_attacker_genome"] = str(attacker_path)

    conclusion_lines: List[str] = []
    conclusion_lines.append("# PCPL Evolvo Conclusions")
    conclusion_lines.append("")
    conclusion_lines.append(f"- profile: `{config.profile}`")
    conclusion_lines.append(f"- rounds completed: `{len(archive.get('rounds', []))}`")
    conclusion_lines.append(f"- backend: `{resource_plan.parallel_backend}`")
    conclusion_lines.append(f"- workers: `{resource_plan.parallel_workers}`")
    conclusion_lines.append(f"- gpu backend: `{resource_plan.gpu_backend}`")
    conclusion_lines.append("")
    if best_defender:
        defender_mean = _mean_metrics_row(best_defender[0].get("metrics", []))
        conclusion_lines.append("## Best Defender Summary")
        conclusion_lines.append(
            "- score={score:.6f}, principle={principle:.4f}, security={security:.4f}, sync={sync:.4f}, cost={cost:.4f}".format(
                score=float(best_defender[0].get("score", 0.0)),
                principle=float(defender_mean.get("principle_score", 0.0)),
                security=float(defender_mean.get("security_score", 0.0)),
                sync=float(defender_mean.get("sync_score", 0.0)),
                cost=float(defender_mean.get("cost_score", 0.0)),
            )
        )
        conclusion_lines.append(
            "- brute_force_resistance={bf:.4f}, reverse_hack_resistance={rh:.4f}, sync_loss={sl:.4f}".format(
                bf=float(defender_mean.get("brute_force_resistance_score", 0.0)),
                rh=float(defender_mean.get("reverse_hack_resistance_score", 0.0)),
                sl=float(defender_mean.get("sync_loss_rate", 0.0)),
            )
        )
        conclusion_lines.append("")
    if best_attacker:
        attacker_mean = _mean_metrics_row(best_attacker[0].get("metrics", []))
        conclusion_lines.append("## Strongest Attacker Summary")
        conclusion_lines.append(
            "- score={score:.6f}, lane_success={lane:.4f}, token_success={token:.4f}, attacker_adv={adv:.4f}".format(
                score=float(best_attacker[0].get("score", 0.0)),
                lane=float(attacker_mean.get("attacker_lane_success_rate", 0.0)),
                token=float(attacker_mean.get("attacker_token_success_rate", 0.0)),
                adv=float(attacker_mean.get("attacker_advantage_score", 0.0)),
            )
        )
        conclusion_lines.append("")
    conclusion_lines.append("## Baseline Means")
    conclusion_lines.append("")
    for row in baseline_rows:
        conclusion_lines.append(
            "- `{name}`: {score:.4f}".format(
                name=str(row["name"]),
                score=float(row["mean_score"]),
            )
        )
    conclusion_path = summaries_dir / "conclusions.md"
    conclusion_path.write_text("\n".join(conclusion_lines) + "\n", encoding="utf-8")

    index_lines: List[str] = []
    index_lines.append("# PCPL Evolvo Run Index")
    index_lines.append("")
    index_lines.append("## Quick Links")
    index_lines.append(f"- conclusions: `{conclusion_path}`")
    index_lines.append(f"- defender leaderboard: `{leaderboards_dir / 'defender-top10.md'}`")
    index_lines.append(f"- attacker leaderboard: `{leaderboards_dir / 'attacker-top10.md'}`")
    if "best_defender_genome" in best_paths:
        index_lines.append(f"- best defender genome: `{best_paths['best_defender_genome']}`")
    if "best_attacker_genome" in best_paths:
        index_lines.append(f"- best attacker genome: `{best_paths['best_attacker_genome']}`")
    index_lines.append("")
    index_lines.append("## Output Layout")
    index_lines.append("- `rounds/`: per-round artifacts and reports")
    index_lines.append("- `best/`: best genome snapshots + metadata")
    index_lines.append("- `leaderboards/`: top ranked defenders/attackers")
    index_lines.append("- `summaries/`: final conclusions and compact overview")
    index_path = views_dir / "index.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    return {
        "index_path": str(index_path),
        "conclusion_path": str(conclusion_path),
        "defender_leaderboard_path": str(leaderboards_dir / "defender-top10.md"),
        "attacker_leaderboard_path": str(leaderboards_dir / "attacker-top10.md"),
        **best_paths,
    }


def _run_defender_round(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    attacker: Optional[GFSLGenome],
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(config, resource_plan)
    evolver = SafeGFSLEvolver(
        population_size=config.population_size,
        supervised_guide=guide,
    )
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

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, genome)
            for idx, genome in enumerate(population)
            if genome.fitness is None
        ]
        if not pending:
            return

        _evaluate_pending_parallel(
            pending=pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            worker_fn=_defender_eval_worker,
            build_task=lambda g: (
                g,
                scenarios,
                copy.deepcopy(attacker) if attacker is not None else None,
            ),
            attr_name="_pcpl_metrics",
        )

    evolver.evolve(
        config.generations,
        fitness,
        progress_callback=progress,
        batch_evaluator=batch_eval,
    )
    return evolver, generation_log


def _run_attacker_round(
    *,
    config: ExperimentConfig,
    resource_plan: ResourcePlan,
    scenarios: Sequence[ScenarioConfig],
    archive: Dict[str, Any],
    defender: GFSLGenome,
) -> Tuple[SafeGFSLEvolver, List[Dict[str, Any]]]:
    guide = _build_supervised_guide_if_available(config, resource_plan)
    evolver = SafeGFSLEvolver(
        population_size=config.attacker_population_size,
        supervised_guide=guide,
    )
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

    def batch_eval(population: List[GFSLGenome]) -> None:
        pending = [
            (idx, attacker_genome)
            for idx, attacker_genome in enumerate(population)
            if attacker_genome.fitness is None
        ]
        if not pending:
            return

        _evaluate_pending_parallel(
            pending=pending,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            worker_fn=_attacker_eval_worker,
            build_task=lambda attacker_genome: (
                attacker_genome,
                copy.deepcopy(defender),
                scenarios,
            ),
            attr_name="_attack_metrics",
        )

    evolver.evolve(
        config.attacker_generations,
        fitness,
        progress_callback=progress,
        batch_evaluator=batch_eval,
    )
    return evolver, generation_log


def run_continuous_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Run persistent defender/attacker co-evolution for one or more rounds."""
    scenario_list = list(scenarios) if scenarios is not None else default_scenarios(config.profile)
    resource_plan = _resolve_resource_plan(
        config,
        max(config.population_size, config.attacker_population_size),
    )

    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds_dir = out_dir / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)

    archive_path = out_dir / "archive.json"
    archive = _load_archive(archive_path) if config.resume else _load_archive(Path("/dev/null"))
    baseline_rows = _baseline_rows(scenario_list)

    print(
        "[pcpl-evolvo] resources cpu={cpu} backend={backend} workers={workers} gpu={gpu} torch={torch}".format(
            cpu=resource_plan.cpu_count,
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            gpu=resource_plan.gpu_backend,
            torch=resource_plan.torch_available,
        )
    )

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
            resource_plan=resource_plan,
            scenarios=scenario_list,
            archive=archive,
            attacker=current_attacker,
        )

        # Preliminary best defender from the round.
        preliminary_defender = defender_evolver.population[0]

        # Attacker co-evolution against this defender.
        attacker_evolver, attacker_log = _run_attacker_round(
            config=config,
            resource_plan=resource_plan,
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
    view_paths = _write_view_outputs(
        out_dir=out_dir,
        config=config,
        resource_plan=resource_plan,
        archive=archive,
        baseline_rows=baseline_rows,
    )

    final_summary = {
        "config": {
            **asdict(config),
            "out_dir": str(out_dir),
        },
        "resources": resource_plan.to_dict(),
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
        "views": view_paths,
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
    report_lines.append(
        "- resources: backend={backend} workers={workers} gpu={gpu}".format(
            backend=resource_plan.parallel_backend,
            workers=resource_plan.parallel_workers,
            gpu=resource_plan.gpu_backend,
        )
    )
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
        "resource_plan": resource_plan.to_dict(),
        **view_paths,
    }
    return summary


def run_experiment(
    config: ExperimentConfig,
    scenarios: Optional[Sequence[ScenarioConfig]] = None,
) -> Dict[str, Any]:
    """Backward-compatible entry point; now supports continuous rounds."""
    return run_continuous_experiment(config, scenarios=scenarios)
