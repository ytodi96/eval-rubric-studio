import re
from collections import Counter

import pytest

from evalstudio import ai
from evalstudio.config import RoleModel
from evalstudio.generate import (DIFFICULTY_SHARE, TYPE_SHARE, apportion, generate_cases, generate_for_project,
                                 plan_slots, similarity)
from evalstudio.intake import Answer, Materials, ask_questions, card_from_json, write_card
from evalstudio.models import EvalSet, TestCase, UseCaseCard
from tests.fake_llm import FakeLLM

ROLE = RoleModel("openai", "gpt-6.1-sol", "medium")

CARD = UseCaseCard(title="T", goal="g", users="u", input_types=["email"], output_format="text",
                   dimensions=[{"key": "accuracy", "description": "d"}],
                   categories=["refunds", "delivery", "subscriptions"])

CARD_JSON = {"title": "Support", "goal": "g", "users": "u", "input_types": ["email"], "output_format": "text",
             "dimensions": [{"key": "Policy Compliance", "description": "d", "weight": 5, "custom": False},
                            {"key": "escalation", "description": "hand over when needed", "weight": 2, "custom": True},
                            {"key": "escalation", "description": "dupe", "weight": 1, "custom": True}],
             "categories": ["refunds", "refunds ", "delivery"], "failure_modes": [], "must": [], "must_not": [],
             "out_of_scope": [], "notes": None}


def case(i, category="refunds", type="typical", difficulty="medium", text=None):
    return TestCase(id=f"c{i}", input=text or f"unique message number {i} about {category}", category=category,
                    type=type, difficulty=difficulty, expected_behaviours=["x"])


# ---------------------------------------------------------------- intake

def test_questions_are_capped_at_five(monkeypatch):
    fake = FakeLLM({"clarifying_questions": {"understanding": "u", "questions": [
        {"question": f"q{i}", "why": "w", "options": []} for i in range(8)]}})
    monkeypatch.setattr(ai, "complete", fake)
    qs, cost = ask_questions(Materials(brief="A brief"), ROLE)
    assert len(qs.questions) == 5 and cost > 0
    assert "<brief>\nA brief\n</brief>" in fake.calls[0]["user"]
    assert "<target_system_prompt>" not in fake.calls[0]["user"]  # empty materials are left out


def test_card_gets_answers_and_library(monkeypatch):
    fake = FakeLLM({"use_case_card": CARD_JSON})
    monkeypatch.setattr(ai, "complete", fake)
    result = write_card(Materials(brief="b"), [Answer("Who reads it?", "Support agents")], ROLE)
    assert "Q: Who reads it?\nA: Support agents" in fake.calls[0]["user"]
    assert "- groundedness:" in fake.calls[0]["system"]  # dimension library was filled in
    assert result.card.title == "Support"


def test_card_cleanup():
    card, warnings = card_from_json(CARD_JSON)
    assert [d.key for d in card.dimensions] == ["policy_compliance", "escalation"]  # slugged, deduped
    assert card.dimensions[0].weight == 3 and not card.dimensions[0].custom
    assert card.dimensions[1].custom
    assert card.categories == ["refunds", "delivery"]
    assert any("weight 5" in w for w in warnings)


# ---------------------------------------------------------------- coverage plan

def test_apportion_adds_up():
    assert apportion(30, TYPE_SHARE) == {"typical": 15, "edge": 6, "adversarial": 6, "out_of_scope": 3}
    assert sum(apportion(7, DIFFICULTY_SHARE).values()) == 7


def test_plan_from_scratch_hits_the_mix():
    slots = plan_slots(CARD, 30, [])
    assert len(slots) == 30
    assert Counter(s.category for s in slots) == {"refunds": 10, "delivery": 10, "subscriptions": 10}
    assert Counter(s.type for s in slots) == apportion(30, TYPE_SHARE)
    assert Counter(s.difficulty for s in slots) == apportion(30, DIFFICULTY_SHARE)
    # Each category gets a spread of types, not all of one kind.
    for cat in CARD.categories:
        assert len({s.type for s in slots if s.category == cat}) >= 3


def test_plan_tops_up_around_existing_cases():
    existing = [case(i, "refunds", "typical", "easy") for i in range(6)]
    slots = plan_slots(CARD, 12, existing)
    assert len(slots) == 6
    assert "refunds" not in {s.category for s in slots}  # refunds already has its share
    assert plan_slots(CARD, 5, existing) == []


# ---------------------------------------------------------------- generating

def _fake_cases(user):
    slots = re.findall(r"^(\d+)\. category: (.+?) \|", user, re.M)
    return {"cases": [{
        "slot": int(n), "id": f"Case {cat} {n}",
        "input": [{"role": "user", "content": " ".join(f"w{n}{cat[:3]}{len(user) % 997}x{k}" for k in range(8))}],
        "context": [{"key": "order", "value": "#1"}], "expected_behaviours": ["does the thing"],
        "reference_answer": None} for n, cat in slots]}


def test_generate_fills_every_slot(monkeypatch):
    fake = FakeLLM({"test_cases": _fake_cases})
    monkeypatch.setattr(ai, "complete", fake)
    slots = plan_slots(CARD, 12, [])
    result = generate_cases(CARD, slots, [], ROLE)
    assert len(result.cases) == 12 and not result.warnings
    assert len(fake.calls) == 2  # batches of 10
    assert [(c.category, c.type, c.difficulty) for c in result.cases] == [(s.category, s.type, s.difficulty) for s in slots]
    assert all(c.source == "generated" and c.context == {"order": "#1"} for c in result.cases)
    assert len({c.id for c in result.cases}) == 12


def test_near_duplicates_are_retried_then_dropped(monkeypatch):
    same = "I want a refund for my order please, it arrived late and cold"
    fake = FakeLLM({"test_cases": lambda user: {"cases": [
        {"slot": int(n), "id": "dup", "input": [{"role": "user", "content": same}], "context": [],
         "expected_behaviours": ["x"], "reference_answer": None}
        for n in re.findall(r"^(\d+)\. category", user, re.M)]}})
    monkeypatch.setattr(ai, "complete", fake)
    existing = [case(0, text=same)]
    result = generate_cases(CARD, plan_slots(CARD, 3, existing), existing, ROLE)
    assert result.cases == [] and len(fake.calls) == 2
    assert sum("too similar" in w for w in result.warnings) == 2


def test_bad_items_are_retried(monkeypatch):
    replies = iter([{"cases": []}, _fake_cases("1. category: refunds | type: typical | difficulty: easy")])
    monkeypatch.setattr(ai, "complete", FakeLLM({"test_cases": lambda user: next(replies)}))
    result = generate_cases(CARD, plan_slots(CARD, 1, []), [], ROLE)
    assert len(result.cases) == 1


def test_generate_needs_an_approved_card(store, monkeypatch):
    p = store.create_project("demo", "Demo")
    store.save_version(p.id, "usecase", CARD)
    with pytest.raises(ValueError, match="Approve the use-case card first"):
        generate_for_project(store, p.id, 5, ROLE)
    store.approve(p.id, "usecase", 1)
    monkeypatch.setattr(ai, "complete", FakeLLM({"test_cases": _fake_cases}))
    result, version = generate_for_project(store, p.id, 5, ROLE)
    assert len(store.load(p.id, "evalset", version).cases) == 5
    assert store.usage_cost(p.id) > 0


def test_similarity():
    assert similarity("the cat sat on the mat", "the cat sat on the mat") == 1
    assert similarity("the cat sat on the mat", "a completely different sentence here") == 0


# ---------------------------------------------------------------- draft editing

def test_editing_a_draft_updates_it_but_approved_versions_are_frozen(store):
    p = store.create_project("demo", "Demo")
    store.save_version(p.id, "evalset", EvalSet(cases=[case(1)]))
    v = store.save_edit(p.id, "evalset", EvalSet(cases=[case(1), case(2)]))
    assert v.version == 1 and len(store.load(p.id, "evalset", 1).cases) == 2
    store.approve(p.id, "evalset", 1)
    v = store.save_edit(p.id, "evalset", EvalSet(cases=[case(1)]))
    assert v.version == 2 and v.status == "draft"
    assert len(store.load(p.id, "evalset", 1).cases) == 2  # approved v1 unchanged


def test_generator_sees_the_targets_policy(store, monkeypatch):
    p = store.create_project("demo", "Demo")
    store.save_version(p.id, "usecase", CARD, approved=True)
    store.save_version(p.id, "target", {"config": {"adapter": "prompt", "model": "m",
                                                   "system_prompt": "Bags come in 250 g and 1 kg."}})
    fake = FakeLLM({"test_cases": _fake_cases})
    monkeypatch.setattr(ai, "complete", fake)
    generate_for_project(store, p.id, 2, ROLE)
    assert "<reference_material>" in fake.calls[0]["user"] and "250 g and 1 kg" in fake.calls[0]["user"]
    assert "Consistent with the facts" in fake.calls[0]["system"]
