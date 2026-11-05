"""Steps 4-5 together: run the target on every test case, grade each response, score the run.

Safety rails (see docs/decisions.md and the API cost policy):
- The run is refused if its estimated cost is above the spending cap, and stops cleanly if the
  actual cost reaches it.
- One "canary" case runs first; if it errors, nothing else runs.
- The run stops if 3 cases in a row error.
- Identical calls are answered from the cache, so re-running after a small change only pays for
  the cases that changed.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .adapters import make_adapter
from .config import Settings
from .estimate import Estimate, estimate_run
from .grading import GradeResult, case_score, code_grade, disagreements, judge
from .models import EvalSet, Grade, ResponseRecord, Rubric, RubricSet, Run, Target, TestCase, UseCaseCard
from .store import Store

CASE_WORKERS = 4
JUDGE_WORKERS = 4
STOP_AFTER_ERRORS_IN_A_ROW = 3


class RunError(ValueError):
    """The run can't start. The message says why and what to do."""


@dataclass
class CaseOutcome:
    case_id: str
    score: float
    passed: bool
    error: str | None = None
    failed_musts: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class RunOutcome:
    run_id: int
    status: str
    summary: dict
    stopped_reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_approved(store: Store, project_id: int):
    """The approved card, eval set, rubrics and target, or a clear error naming what's missing."""
    versions = {k: store.latest_approved(project_id, k) for k in ("usecase", "evalset", "rubrics", "target")}
    missing = [k for k, v in versions.items() if v is None]
    if missing:
        raise RunError(f"Approve these first: {', '.join(missing)}. A run only uses approved versions "
                       f"(e.g. `evalstudio approve <project> {missing[0]}`).")
    return (versions, UseCaseCard.model_validate(versions["usecase"].data),
            EvalSet.model_validate(versions["evalset"].data), RubricSet.model_validate(versions["rubrics"].data),
            Target.model_validate(versions["target"].data))


def prepare(store: Store, project_id: int, settings: Settings, double: bool = False, limit: int | None = None,
            only: list[str] | None = None) -> tuple[dict, list[TestCase], RubricSet, Target, Estimate]:
    versions, _card, evalset, rubrics, target = load_approved(store, project_id)
    cases = [c for c in evalset.cases if not only or c.id in only]
    if only and len(cases) < len(set(only)):
        unknown = set(only) - {c.id for c in cases}
        raise RunError(f"Unknown test case(s): {', '.join(sorted(unknown))}")
    cases = cases[:limit] if limit else cases
    no_rubric = [c.id for c in cases if rubrics.for_case(c.id) is None]
    if no_rubric:
        raise RunError(f"{len(no_rubric)} case(s) have no rubric (e.g. {no_rubric[0]}). "
                       f"Run `evalstudio rubrics <project>` and approve the rubrics.")
    return versions, cases, rubrics, target, estimate_run(store, cases, rubrics, target, settings.judge, double)


def run_project(store: Store, project_id: int, settings: Settings, *, double: bool = False,
                limit: int | None = None, only: list[str] | None = None, max_cost: float | None = None,
                progress: Callable[[CaseOutcome, int, int], None] | None = None) -> RunOutcome:
    versions, cases, rubrics, target, est = prepare(store, project_id, settings, double, limit, only)
    cap = max_cost if max_cost is not None else settings.spend_cap_usd
    if est.cost_usd > cap:
        raise RunError(f"Estimated cost ${est.cost_usd:.2f} is above the ${cap:.2f} cap. Run fewer cases "
                       f"(--limit or --case), or raise the cap for this run with --max-cost.")

    run_id = store.create_run(Run(
        project_id=project_id, usecase_version=versions["usecase"].version,
        evalset_version=versions["evalset"].version, rubrics_version=versions["rubrics"].version,
        target_version=versions["target"].version, judge_model=settings.judge.model, status="running",
        estimated_cost_usd=round(est.cost_usd, 4), started_at=_now(),
        summary={"target": target.name, "double_graded": double}))
    adapter = make_adapter(target, cache=store.cache)
    lock = threading.Lock()
    state = {"cost": 0.0, "errors_in_a_row": 0, "stop": ""}
    outcomes: list[CaseOutcome] = []

    def one(case: TestCase) -> CaseOutcome | None:
        if state["stop"]:
            return None
        outcome = _run_case(store, project_id, run_id, case, rubrics.for_case(case.id), adapter, target,
                            settings, double)
        with lock:
            outcomes.append(outcome)
            state["cost"] += outcome.cost_usd
            state["errors_in_a_row"] = state["errors_in_a_row"] + 1 if outcome.error else 0
            if state["errors_in_a_row"] >= STOP_AFTER_ERRORS_IN_A_ROW and not state["stop"]:
                state["stop"] = f"{STOP_AFTER_ERRORS_IN_A_ROW} cases in a row had errors (last: {outcome.error})"
            if state["cost"] >= cap and not state["stop"]:
                state["stop"] = f"the ${cap:.2f} spending cap was reached"
        if progress:
            progress(outcome, len(outcomes), len(cases))
        return outcome

    canary = one(cases[0])
    if canary.error:
        state["stop"] = f"the first case ('{canary.case_id}') failed: {canary.error}. Nothing else was run"
    elif len(cases) > 1:
        with ThreadPoolExecutor(CASE_WORKERS) as pool:
            list(pool.map(one, cases[1:]))

    summary = {**summarise(cases, outcomes), "target": target.name, "double_graded": double}
    status = "stopped" if state["stop"] else "done"
    store.update_run(run_id, status=status, finished_at=_now(), actual_cost_usd=round(state["cost"], 4),
                     summary=summary | ({"stopped_reason": state["stop"]} if state["stop"] else {}))
    return RunOutcome(run_id, status, summary, state["stop"])


def _run_case(store, project_id, run_id, case: TestCase, rubric: Rubric, adapter, target: Target,
              settings: Settings, double: bool) -> CaseOutcome:
    reply = adapter.respond(case)
    if target.config.adapter == "prompt":
        store.log_usage(project_id, "run: target", target.config.provider, target.config.model, reply.input_tokens,
                        reply.cached_tokens, reply.output_tokens, reply.cost_usd, from_cache=reply.from_cache)
    response_id = store.add_response(ResponseRecord(
        run_id=run_id, test_case_id=case.id, output=reply.output, error=reply.error, latency_ms=reply.latency_ms,
        input_tokens=reply.input_tokens, output_tokens=reply.output_tokens, cost_usd=reply.cost_usd,
        model=reply.model))
    if reply.output is None:
        store.set_response_score(response_id, 0.0, False)
        return CaseOutcome(case.id, 0.0, False, error=reply.error or "no response", cost_usd=reply.cost_usd)

    grades: list[GradeResult] = []
    jobs = []
    for crit in rubric.criteria:
        if crit.check is not None:
            grades.append(code_grade(case, reply.output, crit))
        if crit.grader in ("llm", "code+llm"):
            jobs += [(crit, attempt) for attempt in ((1, 2) if double else (1,))]
    with ThreadPoolExecutor(JUDGE_WORKERS) as pool:
        grades += pool.map(lambda job: judge(case, reply.output, job[0], settings.judge, store, project_id,
                                             attempt=job[1]), jobs)
    for g in grades:
        store.add_grade(Grade(response_id=response_id, criterion_id=g.criterion_id, grader=g.grader,
                              attempt=g.attempt, score=g.score, passed=g.passed, reasoning=g.reasoning,
                              evidence=g.evidence, cost_usd=g.cost_usd))
    score, passed, failed_musts = case_score(rubric.criteria, grades, settings.pass_mark)
    store.set_response_score(response_id, score, passed)
    judge_errors = [g for g in grades if g.error]
    return CaseOutcome(case.id, score, passed,
                       error=judge_errors[0].reasoning if judge_errors else None, failed_musts=failed_musts,
                       disagreements=disagreements(grades),
                       cost_usd=reply.cost_usd + sum(g.cost_usd for g in grades))


def summarise(cases: list[TestCase], outcomes: list[CaseOutcome]) -> dict:
    by_id = {c.id: c for c in cases}
    done = [o for o in outcomes if o.case_id in by_id]

    def group(key):
        acc = defaultdict(list)
        for o in done:
            acc[getattr(by_id[o.case_id], key)].append(o)
        return {k: {"cases": len(v), "score": round(sum(o.score for o in v) / len(v), 4),
                    "pass_rate": round(sum(o.passed for o in v) / len(v), 4)} for k, v in sorted(acc.items())}

    return {
        "cases": len(cases), "graded": len(done), "errors": sum(1 for o in done if o.error),
        "score": round(sum(o.score for o in done) / len(done), 4) if done else 0.0,
        "pass_rate": round(sum(o.passed for o in done) / len(done), 4) if done else 0.0,
        "by_category": group("category"), "by_difficulty": group("difficulty"), "by_type": group("type"),
        "disagreements": {o.case_id: o.disagreements for o in done if o.disagreements},
    }
