"""Rubric generation, tested with a stand-in model."""

import re

import pytest

from evalstudio import ai
from evalstudio.bundle import import_bundle
from evalstudio.config import RoleModel
from evalstudio.models import Criterion, RubricSet, TestCase
from evalstudio.rubrics import criterion_from_json, generate_rubrics, merge, verify_on_reference
from tests.fake_llm import FakeLLM

ROLE = RoleModel("openai", "gpt-6.1-sol", "medium")
DIMS = {"policy_compliance", "tone", "format"}
NO_CHECK = {"type": "none", "values": None, "match": None, "case_sensitive": None, "pattern": None, "expected": None,
            "tolerance": None, "unit": None, "min": None, "max": None, "json_schema": None}


def crit(id="c", check=None, scale="pass_fail", anchors=(), **kw):
    return {"id": id, "name": "Name", "description": "What passes.", "dimension": "tone", "weight": 2,
            "must_pass": False, "scale": scale, "anchors": list(anchors), "check": NO_CHECK | (check or {}),
            "also_judge": False} | kw


def test_judge_criterion_with_anchors():
    c, w = criterion_from_json(crit(scale="1-4", anchors=["a", "b", "c", "d"]), DIMS, "x")
    assert c.grader == "llm" and c.anchors == {1: "a", 2: "b", 3: "c", 4: "d"} and not w


def test_code_check_is_built_and_forced_to_pass_fail():
    c, _ = criterion_from_json(crit(check={"type": "length", "unit": "words", "min": 60, "max": 180},
                                    scale="1-4", anchors=["a", "b", "c", "d"]), DIMS, "x")
    assert c.grader == "code" and c.scale == "pass_fail" and c.check.max == 180


def test_json_schema_check_arrives_as_a_string():
    c, _ = criterion_from_json(crit(check={"type": "json_schema", "json_schema": '{"type": "object"}'}), DIMS, "x")
    assert c.check.json_schema == {"type": "object"}


def test_invalid_check_falls_back_to_the_judge():
    c, w = criterion_from_json(crit(check={"type": "regex", "pattern": "(unclosed"}), DIMS, "x")
    assert c.grader == "llm" and "invalid" in w[0]


def test_repairs():
    c, w = criterion_from_json(crit(id="Offers Reship!", weight=9, dimension="made_up", scale="1-4", anchors=["a"]),
                               DIMS, "x")
    assert c.id == "offers-reship" and c.weight == 3 and c.dimension is None and c.scale == "pass_fail"
    assert "4 anchors" in w[0]
    _, _ = criterion_from_json(crit(also_judge=True), DIMS, "x")  # also_judge without a check is dropped quietly


def test_checks_that_fail_the_reference_answer_become_judge_criteria():
    case = TestCase(id="t", input="hi", category="c", expected_behaviours=["x"],
                    reference_answer="Hi Maria,\nWe'll reship it.\n\nWarm regards,\nThe Brightside Coffee team")
    good = Criterion(id="sign-off", name="n", description="d",
                     check={"type": "regex", "pattern": r"Warm regards,\s*\n\s*The Brightside Coffee team\s*\Z"})
    bad = Criterion(id="says-refund", name="n", description="d", also_judge=True,
                    check={"type": "contains", "values": ["refund"]})
    warnings = verify_on_reference([good, bad], case)
    assert good.grader == "code" and bad.grader == "llm" and "says-refund" in warnings[0]


def test_merge_renames_clashing_common_ids():
    a = Criterion(id="tone", name="n", description="d")
    merged = merge([a], [Criterion(id="tone", name="n", description="d"), Criterion(id="length", name="n", description="d")])
    assert [c.id for c in merged] == ["tone", "tone-common", "length"]


def _fake_rubrics(user):
    ids = re.findall(r'<test_case id="([^"]+)"', user)
    return {"rubrics": [{"test_case_id": i, "criteria": [
        crit(id="specific", must_pass=True),
        crit(id="greets", check={"type": "regex", "pattern": r"\AHi"}),
    ]} for i in ids]}


COMMON = {"criteria": [crit(id="length", dimension="format", check={"type": "length", "unit": "words", "max": 180}),
                       crit(id="no-markdown", dimension="format", check={"type": "not_regex", "pattern": r"\*\*|^#"})]}


@pytest.fixture
def demo(store, demo_dir):
    p = import_bundle(store, demo_dir)
    store.approve(p.id, "usecase", 1)
    # Start from just the two hand-written rubrics, as before any were generated.
    hand = [r for r in store.load(p.id, "rubrics").rubrics
            if r.test_case_id in ("lost-parcel-delivered", "opened-bag-refund-request")]
    store.save_edit(p.id, "rubrics", RubricSet(rubrics=hand))
    return p


def test_generate_needs_approved_card_and_eval_set(store, demo):
    with pytest.raises(ValueError, match="Approve the use-case card and the eval set"):
        generate_rubrics(store, demo.id, ROLE)


def test_generate_keeps_hand_written_rubrics_and_fills_the_rest(store, demo, monkeypatch):
    store.approve(demo.id, "evalset", 1)
    fake = FakeLLM({"common_criteria": COMMON, "rubrics": _fake_rubrics})
    monkeypatch.setattr(ai, "complete", fake)
    n_cases = len(store.load(demo.id, "evalset").cases)
    result, version = generate_rubrics(store, demo.id, ROLE)

    rs = store.load(demo.id, "rubrics", version)
    assert len(rs.rubrics) == n_cases and len(result.rubrics) == n_cases - 2  # 2 hand-written rubrics kept
    assert rs.for_case("lost-parcel-delivered").criteria[0].id == "offers-reship"  # untouched
    new = rs.for_case("angry-discount-demand")
    assert [c.id for c in new.criteria] == ["specific", "greets", "length", "no-markdown"]
    assert [c.id for c in rs.common] == ["length", "no-markdown"] and rs.evalset_version == 1
    calls = [c["schema_name"] for c in fake.calls]
    assert calls.count("common_criteria") == 1 and calls.count("rubrics") == -(-(n_cases - 2) // 5)
    assert "<reference_material>" in fake.calls[1]["user"]  # the policy goes to the rubric writer too
    assert "Criterion guide" in fake.calls[0]["system"]
    # Generating again only fills gaps: nothing is missing, so no calls.
    fake.calls.clear()
    generate_rubrics(store, demo.id, ROLE)
    assert fake.calls == []


def test_regenerate_rewrites_everything(store, demo, monkeypatch):
    store.approve(demo.id, "evalset", 1)
    monkeypatch.setattr(ai, "complete", FakeLLM({"common_criteria": COMMON, "rubrics": _fake_rubrics}))
    generate_rubrics(store, demo.id, ROLE, regenerate=True)
    assert store.load(demo.id, "rubrics").for_case("lost-parcel-delivered").criteria[0].id == "specific"


def test_regex_in_the_wrong_field_and_stray_fields_are_repaired():
    c, w = criterion_from_json(crit(check={"type": "regex", "values": [r"\AHi"], "case_sensitive": True}), DIMS, "x")
    assert c.grader == "code" and c.check.pattern == r"\AHi" and not w


def test_refresh_common_swaps_only_the_shared_criteria(store, demo, monkeypatch):
    store.approve(demo.id, "evalset", 1)
    monkeypatch.setattr(ai, "complete", FakeLLM({"common_criteria": COMMON, "rubrics": _fake_rubrics}))
    generate_rubrics(store, demo.id, ROLE)
    new_common = {"criteria": [crit(id="sign-off", dimension="format",
                                    check={"type": "regex", "values": [r"team\s*\Z"]})]}
    fake = FakeLLM({"common_criteria": new_common})
    monkeypatch.setattr(ai, "complete", fake)
    from evalstudio.rubrics import refresh_common
    result, version = refresh_common(store, demo.id, ROLE)
    rs = store.load(demo.id, "rubrics", version)
    assert [c.id for c in rs.for_case("angry-discount-demand").criteria] == ["specific", "greets", "sign-off"]
    assert rs.for_case("angry-discount-demand").criteria[-1].shared
    assert rs.for_case("lost-parcel-delivered").criteria[0].id == "offers-reship"  # hand-written: untouched
    assert len(fake.calls) == 1


def test_a_string_pattern_schema_becomes_a_regex():
    schema = r'{"type": "string", "pattern": "^Hi [^,\\r\\n]+,"}'
    c, _ = criterion_from_json(crit(check={"type": "json_schema", "json_schema": schema}), DIMS, "x")
    assert c.check.type == "regex" and c.check.pattern == r"\AHi [^,\r\n]+,"
    real, _ = criterion_from_json(crit(check={"type": "json_schema", "json_schema": '{"type": "object"}'}), DIMS, "x")
    assert real.check.type == "json_schema"  # a real JSON schema is left alone
