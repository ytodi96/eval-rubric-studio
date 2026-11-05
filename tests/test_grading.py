"""The judge call, evidence checking and the scoring maths."""

import pytest

from evalstudio import ai
from evalstudio.config import RoleModel
from evalstudio.grading import (GradeResult, case_score, criterion_result, disagreements, evidence_in_response,
                                judge, judge_schema, judge_user_message)
from evalstudio.models import Criterion, TestCase
from tests.fake_llm import FakeLLM

ROLE = RoleModel("openai", "gpt-6.1-sol", "medium", 4000)
CASE = TestCase(id="c", input="Where is my order?", category="x", expected_behaviours=["Gives the status"],
                context={"order": "#1"})
RESPONSE = "Hi Sam,\n\nYour order is in transit and should arrive within 3–5 business days.\n\nWarm regards,\nTeam"
PF = Criterion(id="status", name="Gives status", description="Says the order is in transit.", weight=2, must_pass=True)
SCALE = Criterion(id="tone", name="Tone", description="Warm.", scale="1-4",
                  anchors={1: "cold", 2: "polite", 3: "warm", 4: "very warm"})


def test_message_puts_the_criterion_last_for_caching():
    a, b = judge_user_message(CASE, RESPONSE, PF), judge_user_message(CASE, RESPONSE, SCALE)
    shared = a[:a.index("<criterion>")]
    assert b.startswith(shared) and "<response_to_grade>" in shared
    assert "1: cold" in b and "pass/fail" in a


def test_schemas_limit_scores_to_the_scale():
    assert judge_schema("pass_fail")["properties"]["score"]["enum"] == [0, 1]
    assert judge_schema("1-4")["properties"]["score"]["enum"] == [1, 2, 3, 4]


def test_evidence_check_ignores_typography_but_catches_inventions():
    assert evidence_in_response("should arrive within 3-5 business days", RESPONSE)
    assert evidence_in_response("Hi Sam, ... in transit", RESPONSE)
    assert evidence_in_response(None, RESPONSE)
    assert not evidence_in_response("we have refunded you", RESPONSE)


def test_judge_grade(monkeypatch):
    fake = FakeLLM({"grade": {"evidence": "in transit", "reasoning": "States it.", "score": 1}})
    monkeypatch.setattr(ai, "complete", fake)
    g = judge(CASE, RESPONSE, PF, ROLE)
    assert g.passed and g.score == 1 and g.evidence == "in transit" and g.cost_usd > 0
    assert "Grade only what the criterion asks" in fake.calls[0]["system"]


def test_judge_flags_made_up_evidence(monkeypatch):
    monkeypatch.setattr(ai, "complete", FakeLLM({"grade": {"evidence": "your refund is on its way", "reasoning": "r",
                                                           "score": 1}}))
    assert judge(CASE, RESPONSE, PF, ROLE).reasoning.startswith("[The quoted evidence was not found")


def test_second_attempt_is_a_separate_call(monkeypatch):
    fake = FakeLLM({"grade_attempt_2": {"evidence": None, "reasoning": "r", "score": 2}})
    monkeypatch.setattr(ai, "complete", fake)
    g = judge(CASE, RESPONSE, SCALE, ROLE, attempt=2)
    assert g.attempt == 2 and not g.passed  # a 2 on the 1-4 scale doesn't pass


def g(cid, score, passed, grader="llm", attempt=1):
    return GradeResult(cid, grader, score, passed, "", attempt=attempt)


def test_scoring_maths():
    # status (weight 2, must-pass) passes; tone (weight 1) scores 3 of 4 -> 2/3.
    score, passed, musts = case_score([PF, SCALE], [g("status", 1, True), g("tone", 3, True)], 0.8)
    assert score == pytest.approx((2 * 1 + 1 * 2 / 3) / 3) and passed and musts == []


def test_a_failed_must_pass_fails_the_case_whatever_the_score():
    score, passed, musts = case_score([PF, SCALE], [g("status", 0, False), g("tone", 4, True)], 0.3)
    assert score == pytest.approx(1 / 3) and not passed and musts == ["status"]


def test_below_the_pass_mark_fails():
    _, passed, _ = case_score([PF, SCALE], [g("status", 1, True), g("tone", 1, False)], 0.8)
    assert not passed  # (2 + 0) / 3 = 0.67 < 0.8


def test_code_plus_judge_needs_both():
    c = Criterion(id="d", name="n", description="d", also_judge=True, check={"type": "not_contains", "values": ["x"]})
    assert criterion_result(c, [g("d", 1, True, "code"), g("d", 0, False, "llm")]) == (0.0, False)
    assert criterion_result(c, [g("d", 1, True, "code"), g("d", 1, True, "llm")]) == (1.0, True)


def test_only_the_first_attempt_counts_and_disagreements_are_flagged():
    grades = [g("tone", 4, True), g("tone", 2, False, attempt=2)]
    assert criterion_result(SCALE, grades) == (1.0, True)
    assert disagreements(grades) == ["tone"]
    assert disagreements([g("tone", 3, True), g("tone", 3, True, attempt=2)]) == []


def test_missing_grade_counts_as_failed():
    assert criterion_result(PF, []) == (0.0, False)
