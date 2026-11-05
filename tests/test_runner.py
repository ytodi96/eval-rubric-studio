"""Whole runs on the demo project, with stand-in models (no network, no cost)."""

import re

import pytest

from evalstudio import adapters, ai, llm
from evalstudio.bundle import import_bundle
from evalstudio.config import load_settings
from evalstudio.llm import LLMError, LLMResult
from evalstudio.runner import RunError, run_project
from tests.fake_llm import FakeLLM

GOOD = ("Hi there,\n\nThanks for getting in touch. I'm sorry about the trouble with your order. "
        + "We'll look into it right away and send you an update with everything you need. " * 3
        + "\n\nWarm regards,\nThe Brightside Coffee team")


@pytest.fixture
def demo(store, demo_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EVALSTUDIO_SPEND_CAP_USD", "2")
    p = import_bundle(store, demo_dir, approve=True)
    return p, load_settings()


def fake_target(text=GOOD, error_for=()):
    def complete(provider, model, system, messages, **kw):
        if any(e in messages[-1]["content"] for e in error_for):
            raise LLMError("boom")
        return LLMResult(text=text, data=None, provider=provider, model=model, input_tokens=500, output_tokens=200)
    return complete


def judge_says(score_pf=1, score_14=4):
    def reply(user):
        is_scale = "scale: 1-4" in user
        return {"evidence": None, "reasoning": "ok", "score": score_14 if is_scale else score_pf}
    return FakeLLM({"grade": reply, "grade_attempt_2": reply})


def test_a_full_run(store, demo, monkeypatch):
    p, settings = demo
    monkeypatch.setattr(adapters, "complete", fake_target())
    fake = judge_says()
    monkeypatch.setattr(ai, "complete", fake)
    seen = []
    out = run_project(store, p.id, settings, limit=5, progress=lambda o, done, total: seen.append(done))
    assert out.status == "done" and out.summary["graded"] == 5 and sorted(seen) == [1, 2, 3, 4, 5]
    responses = store.responses(out.run_id)
    assert len(responses) == 5 and all(r.score is not None for r in responses)
    grades = store.grades(responses[0].id)
    assert {x.grader for x in grades} == {"code", "llm"}
    run = store.get_run(out.run_id)
    assert run.actual_cost_usd > 0 and run.estimated_cost_usd > 0 and run.rubrics_version == 1
    assert set(out.summary["by_category"]) <= {c for c in store.load(p.id, "usecase").categories}


def test_must_pass_failures_fail_cases(store, demo, monkeypatch):
    p, settings = demo
    monkeypatch.setattr(adapters, "complete", fake_target())
    monkeypatch.setattr(ai, "complete", judge_says(score_pf=0))
    out = run_project(store, p.id, settings, limit=3)
    assert out.summary["pass_rate"] == 0


def test_the_canary_stops_a_broken_run(store, demo, monkeypatch):
    p, settings = demo
    monkeypatch.setattr(adapters, "complete", fake_target(error_for=["tracking says"]))  # the first case
    fake = judge_says()
    monkeypatch.setattr(ai, "complete", fake)
    out = run_project(store, p.id, settings)
    assert out.status == "stopped" and "first case" in out.stopped_reason
    assert len(store.responses(out.run_id)) == 1 and fake.calls == []


def test_three_errors_in_a_row_stop_the_run(store, demo, monkeypatch):
    p, settings = demo
    first = store.load(p.id, "evalset").cases[0].input[:20]

    def complete(provider, model, system, messages, **kw):
        if first not in messages[-1]["content"]:
            raise LLMError("rate limited")
        return LLMResult(text=GOOD, data=None, provider=provider, model=model)

    monkeypatch.setattr(adapters, "complete", complete)
    monkeypatch.setattr(ai, "complete", judge_says())
    out = run_project(store, p.id, settings)
    assert out.status == "stopped" and "3 cases in a row" in out.stopped_reason
    assert len(store.responses(out.run_id)) < 30


def test_runs_over_the_cap_are_refused(store, demo, monkeypatch):
    p, settings = demo
    with pytest.raises(RunError, match="above the \\$0.01 cap"):
        run_project(store, p.id, settings, max_cost=0.01)


def test_only_approved_versions_run(store, demo_dir):
    p = import_bundle(store, demo_dir)
    with pytest.raises(RunError, match="Approve these first: usecase, evalset, rubrics, target"):
        run_project(store, p.id, load_settings())


def test_double_grading_flags_disagreements(store, demo, monkeypatch):
    p, settings = demo
    monkeypatch.setattr(adapters, "complete", fake_target())
    def reply(score_on_scale):  # attempt 1 and attempt 2 disagree on every 1-4 criterion
        return lambda user: {"evidence": None, "reasoning": "r", "score": score_on_scale if "scale: 1-4" in user else 1}

    monkeypatch.setattr(ai, "complete", FakeLLM({"grade": reply(4), "grade_attempt_2": reply(2)}))
    out = run_project(store, p.id, settings, only=["nut-allergy-flavoured"], double=True)
    assert out.summary["double_graded"] and "nut-allergy-flavoured" in out.summary["disagreements"]


def test_cache_makes_a_repeat_run_free(store, demo, monkeypatch):
    """Through the real llm.complete with a stand-in API client: the second identical run costs $0."""
    p, settings = demo
    calls = {"n": 0}

    def fake_openai(model, system, messages, schema, schema_name, max_tokens, effort, client):
        calls["n"] += 1
        if schema is None:
            return LLMResult(text=GOOD, data=None, provider="openai", model=model, input_tokens=400, output_tokens=150)
        scale = "scale: 1-4" in messages[-1]["content"]
        text = '{"evidence": null, "reasoning": "ok", "score": %d}' % (4 if scale else 1)
        return LLMResult(text=text, data=None, provider="openai", model=model, input_tokens=900, output_tokens=300)

    monkeypatch.setattr(llm, "_openai", fake_openai)
    first = run_project(store, p.id, settings, only=["lost-parcel-delivered", "nut-allergy-flavoured"])
    n = calls["n"]
    second = run_project(store, p.id, settings, only=["lost-parcel-delivered", "nut-allergy-flavoured"])
    assert n > 0 and calls["n"] == n  # no new calls
    assert store.get_run(second.run_id).actual_cost_usd == 0 and store.get_run(first.run_id).actual_cost_usd > 0
    assert second.summary["score"] == first.summary["score"]
    llm.set_fresh(True)
    try:
        run_project(store, p.id, settings, only=["lost-parcel-delivered"])
        assert calls["n"] > n  # --fresh calls again
    finally:
        llm.set_fresh(False)
    assert any("cache hit" in r["purpose"] for r in store.costs(p.id))
    log = (store.csv_path or None)
    assert log is None or log.exists()
