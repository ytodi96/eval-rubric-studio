"""The web app's editing flows, against an in-memory database."""

import pytest
from fastapi.testclient import TestClient

from evalstudio import ai
from evalstudio.bundle import import_bundle
from evalstudio.store import Store
from evalstudio.web import app as web
from tests.fake_llm import FakeLLM
from tests.test_intake_generate import CARD_JSON, _fake_cases


@pytest.fixture
def client(monkeypatch, demo_dir):
    store = Store(":memory:")
    import_bundle(store, demo_dir)
    monkeypatch.setattr(web, "store", store)
    return TestClient(web.app), store


def test_project_page_renders(client):
    c, _ = client
    r = c.get("/p/support-email")
    assert r.status_code == 200 and "Coverage matrix" in r.text and "lost-parcel-delivered" in r.text


def test_edit_add_delete_case(client):
    c, store = client
    p = store.get_project("support-email")
    form = {"original_id": "lost-parcel-delivered", "id": "lost-parcel-delivered", "category": "order status and delivery",
            "type": "typical", "difficulty": "hard", "input": "Where is my parcel?", "context": "order: #1\nstatus: lost",
            "expected_behaviours": "Apologises\nOffers a reship", "reference_answer": ""}
    r = c.post("/p/support-email/cases/save", data=form)
    assert r.status_code == 200 and "Saved" in r.text
    case = next(x for x in store.load(p.id, "evalset").cases if x.id == "lost-parcel-delivered")
    assert case.difficulty == "hard" and case.context == {"order": "#1", "status": "lost"}
    assert store.get_version(p.id, "evalset").version == 1  # a draft is edited in place

    convo = "USER: Hi\n\nASSISTANT: Hello, how can I help?\n\nUSER: Refund please"
    r = c.post("/p/support-email/cases/save", data=form | {"original_id": "", "id": "new-one", "input": convo})
    new = next(x for x in store.load(p.id, "evalset").cases if x.id == "new-one")
    assert new.is_multi_turn and new.input[-1].content == "Refund please" and new.source == "manual"

    r = c.post("/p/support-email/cases/save", data=form | {"original_id": "", "id": "new-one"})
    assert "already has the id" in r.text  # duplicate id rejected, nothing saved

    c.post("/p/support-email/cases/new-one/delete")
    assert "new-one" not in {x.id for x in store.load(p.id, "evalset").cases}


def test_invalid_case_shows_errors(client):
    c, _ = client
    r = c.post("/p/support-email/cases/save", data={"original_id": "", "id": "x", "category": "c", "type": "typical",
                                                    "difficulty": "easy", "input": "hi", "expected_behaviours": ""})
    assert r.status_code == 200 and "expected_behaviours" in r.text


def test_approve_then_edit_makes_a_new_version(client):
    c, store = client
    p = store.get_project("support-email")
    c.post("/p/support-email/approve/usecase")
    assert store.get_version(p.id, "usecase").status == "approved"
    form = {"title": "New title", "goal": "g", "users": "u", "output_format": "o", "input_types": "email",
            "categories": "a\nb", "dimensions": "accuracy | 2 | right facts\nmy_custom | 1 | something",
            "failure_modes": "", "must": "", "must_not": "", "out_of_scope": "", "notes": ""}
    c.post("/p/support-email/card/edit", data=form)
    v = store.get_version(p.id, "usecase")
    assert v.version == 2 and v.status == "draft" and v.data["title"] == "New title"
    assert [d["custom"] for d in v.data["dimensions"]] == [False, True]


def test_intake_flow(client, monkeypatch):
    c, store = client
    monkeypatch.setattr(ai, "complete", FakeLLM({
        "clarifying_questions": {"understanding": "You draft replies.", "questions": [
            {"question": "Who reads it?", "why": "tone", "options": ["Agents", "Customers"]}]},
        "use_case_card": CARD_JSON}))
    r = c.post("/p/support-email/intake/questions", data={"brief": "We draft support replies."})
    assert "Who reads it?" in r.text and "You draft replies." in r.text
    r = c.post("/p/support-email/intake/card", data={"brief": "We draft support replies.", "n_questions": "1",
                                                     "question_0": "Who reads it?", "choice_0": "Agents",
                                                     "answer_0": "and team leads"})
    assert "Drafted use-case card" in r.text
    p = store.get_project("support-email")
    assert store.get_version(p.id, "usecase").data["title"] == "Support"


def test_generate_and_import(client, monkeypatch, tmp_path):
    c, store = client
    p = store.get_project("support-email")
    monkeypatch.setattr(ai, "complete", FakeLLM({"test_cases": _fake_cases}))
    before = len(store.load(p.id, "evalset").cases)
    c.post("/p/support-email/generate", data={"n": str(before + 4)})
    assert len(store.load(p.id, "evalset").cases) == before  # card not approved yet: nothing generated
    c.post("/p/support-email/approve/usecase")
    r = c.post("/p/support-email/generate", data={"n": str(before + 4)})
    assert "Generated 4 cases" in r.text and len(store.load(p.id, "evalset").cases) == before + 4

    r = c.post("/p/support-email/import", files={"file": ("x.yaml", b"- id: lost-parcel-delivered\n  input: hi\n"
                                                                    b"  expected_behaviours: [x]\n")})
    ids = [x.id for x in store.load(p.id, "evalset").cases]
    assert "lost-parcel-delivered-2" in ids and len(ids) == before + 5


def test_edit_a_rubric_as_yaml(client):
    c, store = client
    p = store.get_project("support-email")
    good = "- id: says-sorry\n  name: Says sorry\n  description: Apologises.\n  check: {type: contains, values: [sorry]}\n"
    r = c.post("/p/support-email/rubrics/opened-bag-refund-request/edit", data={"text": good})
    assert "Saved the rubric" in r.text
    assert [x.id for x in store.load(p.id, "rubrics").for_case("opened-bag-refund-request").criteria] == ["says-sorry"]
    r = c.post("/p/support-email/rubrics/opened-bag-refund-request/edit",
               data={"text": "- id: x\n  name: X\n  description: d\n  scale: '1-4'\n"})
    assert "anchor for each" in r.text  # invalid rubric is not saved
    r = c.post("/p/support-email/rubrics/nut-allergy-flavoured/edit", data={"text": good})  # a case with no rubric yet
    assert store.load(p.id, "rubrics").for_case("nut-allergy-flavoured") is not None


def test_estimate_only_buttons_call_nothing(client, monkeypatch):
    c, store = client
    import evalstudio.llm as llm

    monkeypatch.setattr(llm, "_openai", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no calls")))
    c.post("/p/support-email/approve/usecase")
    r = c.post("/p/support-email/generate", data={"n": "40", "dry_run": "true"})
    assert "Estimate only, nothing was called" in r.text
    r = c.post("/p/support-email/rubrics/generate", data={"dry_run": "true", "regenerate": "true"})
    assert "Approve the use-case card and the eval set first" in r.text or "Estimate only" in r.text
