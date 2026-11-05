"""Step 5, grading: code checks first, then the LLM judge one criterion at a time, then scoring.

Scoring: each criterion is normalised to 0-1 (pass = 1, fail = 0; on the 1-4 scale 1 -> 0 and
4 -> 1, and a 3 or 4 counts as passed). A test case's score is the weighted average. It passes
if the score reaches the pass mark and no must-pass criterion failed. A criterion graded by both
a code check and the judge counts as passed only if both pass, with the lower score.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import prompts
from .ai import NULLABLE_STR, STR, ask, obj
from .checks import normalise, run_check
from .config import RoleModel
from .llm import LLMError
from .models import Criterion, TestCase
from .store import Store


@dataclass
class GradeResult:
    criterion_id: str
    grader: str  # "code" or "llm"
    score: float  # 0/1 for pass/fail, 1-4 for the 1-4 scale
    passed: bool
    reasoning: str
    evidence: str | None = None
    attempt: int = 1
    cost_usd: float = 0.0
    error: bool = False


def judge_schema(scale: str) -> dict:
    scores = [0, 1] if scale == "pass_fail" else [1, 2, 3, 4]
    return obj({"evidence": NULLABLE_STR, "reasoning": STR, "score": {"type": "integer", "enum": scores}})


def judge_user_message(case: TestCase, response: str, criterion: Criterion) -> str:
    """Test case and response first, criterion last: the start of the message is identical for
    every criterion of a response, so providers bill it at the cheaper cached rate."""
    if case.is_multi_turn:
        convo = "\n".join(f"<{t.role}>\n{t.content}\n</{t.role}>" for t in case.input)
    else:
        convo = f"<user>\n{case.input}\n</user>"
    context = ("<context>\n" + "\n".join(f"{k}: {v}" for k, v in case.context.items()) + "\n</context>\n"
               if case.context else "")
    expected = "\n".join(f"- {b}" for b in case.expected_behaviours)
    if criterion.scale == "1-4":
        scale = "1-4. Anchors:\n" + "\n".join(f"{n}: {a}" for n, a in sorted(criterion.anchors.items()))
    else:
        scale = "pass/fail (1 = pass, 0 = fail)"
    return (f"<test_case>\n<input>\n{convo}\n</input>\n{context}"
            f"<expected_behaviours>\n{expected}\n</expected_behaviours>\n</test_case>\n\n"
            f"<response_to_grade>\n{response}\n</response_to_grade>\n\n"
            f"<criterion>\nname: {criterion.name}\ndescription: {criterion.description}\nscale: {scale}\n</criterion>")


def evidence_in_response(evidence: str | None, response: str) -> bool:
    """Is every quoted passage really in the response? (Typography and spacing are ignored.)"""
    if not evidence:
        return True
    hay = " ".join(normalise(response).lower().split())
    parts = [p for p in evidence.replace("…", "...").split("...") if p.strip()]
    return all(" ".join(normalise(p).lower().split()).strip(" \"'") in hay for p in parts)


def judge(case: TestCase, response: str, criterion: Criterion, role: RoleModel, store: Store | None = None,
          project_id: int | None = None, attempt: int = 1) -> GradeResult:
    try:
        r = ask(role, prompts.load("judge"), judge_user_message(case, response, criterion),
                schema=judge_schema(criterion.scale),
                # A second, independent grade must not be answered from the cache of the first.
                schema_name="grade" if attempt == 1 else f"grade_attempt_{attempt}",
                store=store, project_id=project_id, purpose="run: judge")
    except LLMError as e:
        return GradeResult(criterion.id, "llm", 0, False, f"Judge error: {e}", attempt=attempt, error=True)
    score = r.data["score"]
    evidence = (r.data["evidence"] or "").strip() or None
    reasoning = r.data["reasoning"].strip()
    if not evidence_in_response(evidence, response):
        reasoning = "[The quoted evidence was not found in the response.] " + reasoning
    passed = score == 1 if criterion.scale == "pass_fail" else score >= 3
    return GradeResult(criterion.id, "llm", score, passed, reasoning, evidence, attempt, r.cost_usd)


def code_grade(case: TestCase, response: str, criterion: Criterion) -> GradeResult:
    r = run_check(criterion.check, response, case)
    return GradeResult(criterion.id, "code", 1 if r.passed else 0, r.passed, r.detail, r.evidence)


# ---------------------------------------------------------------- scoring

def normalised(criterion: Criterion, grade: GradeResult) -> float:
    if criterion.scale == "1-4" and grade.grader == "llm":
        return (grade.score - 1) / 3
    return 1.0 if grade.passed else 0.0


def criterion_result(criterion: Criterion, grades: list[GradeResult]) -> tuple[float, bool]:
    """Combine a criterion's grades (code and/or judge, attempt 1 only) into (0-1 score, passed)."""
    first = [g for g in grades if g.attempt == 1]
    if not first:
        return 0.0, False
    return min(normalised(criterion, g) for g in first), all(g.passed for g in first)


def case_score(criteria: list[Criterion], grades: list[GradeResult], pass_mark: float) -> tuple[float, bool, list[str]]:
    """(weighted score 0-1, passed, ids of failed must-pass criteria)."""
    total = weight = 0.0
    failed_musts = []
    for c in criteria:
        score, passed = criterion_result(c, [g for g in grades if g.criterion_id == c.id])
        total += c.weight * score
        weight += c.weight
        if c.must_pass and not passed:
            failed_musts.append(c.id)
    score = total / weight if weight else 0.0
    return score, score >= pass_mark and not failed_musts, failed_musts


def disagreements(grades: list[GradeResult]) -> list[str]:
    """Criteria where two judge attempts gave different scores."""
    by_crit: dict[str, set] = {}
    for g in grades:
        if g.grader == "llm" and not g.error:
            by_crit.setdefault(g.criterion_id, set()).add(g.score)
    return sorted(c for c, scores in by_crit.items() if len(scores) > 1)
