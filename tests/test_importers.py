import json

import pytest

from evalstudio.importers import CaseImportError, import_cases

# Same layout as the Sales Copilot's test-cases.md, with invented content.
MARKDOWN = '''# Test cases

Use these to check the assistant.

---
## TC-01: Small gift breakfast
**Customer:** "Hi! Breakfast platter for 4 this Friday please, it's a birthday gift."
**Expected:**
- Intent: new-order (gift)
- Suggest: one board that serves 4
- Escalate: no

## TC-02: Price objection
**Customer:** (after a quote of 13,450) "That's quite a lot."
**Category:** pricing
**Type:** edge case
**Expected:**
- No discount
- Offers to trim an item and shows the new total

## TC-03: Running late
**Rep note:** Order is running 40 minutes late; customer hasn't asked yet.
**Expected:**
- Proactively tells the customer with an apology

## TC-04: Broken case
**Expected:**
- Has no input at all

## Notes
Not a test case, no input label.
'''


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_markdown(tmp_path):
    cases, warnings = import_cases(write(tmp_path, "cases.md", MARKDOWN))
    assert [c.id for c in cases] == ["tc-01", "tc-02", "tc-03"]
    assert len(warnings) == 1 and "TC-04" in warnings[0]  # the "Notes" heading is silently not a case
    one, two, three = cases
    assert three.input.startswith("[Rep note] Order is running 40 minutes late")
    assert one.input.startswith("Hi! Breakfast platter") and not one.input.startswith('"')
    assert one.expected_behaviours == ["Intent: new-order (gift)", "Suggest: one board that serves 4", "Escalate: no"]
    assert one.context == {"title": "Small gift breakfast"}
    assert (one.category, one.type, one.source) == ("imported", "typical", "imported")
    assert two.input == "That's quite a lot."
    assert two.context["situation"] == "after a quote of 13,450"
    assert (two.category, two.type) == ("pricing", "edge")


def test_csv(tmp_path):
    text = ("id,input,category,type,difficulty,expected_behaviours\n"
            'r1,"Where is my order?",delivery,typical,easy,"Gives status | No exact date"\n'
            'r1,"Second with same id",delivery,,,"Something"\n')
    cases, _ = import_cases(write(tmp_path, "cases.csv", text))
    assert [c.id for c in cases] == ["r1", "r1-2"]
    assert cases[0].expected_behaviours == ["Gives status", "No exact date"]
    assert cases[1].difficulty == "medium"


def test_csv_needs_an_input_column(tmp_path):
    with pytest.raises(CaseImportError, match="input"):
        import_cases(write(tmp_path, "cases.csv", "question,answer\nq,a\n"))


def test_json_and_yaml(tmp_path):
    data = [{"id": "j1", "input": [{"role": "user", "content": "hi"}], "expected_behaviours": ["greets"]}]
    cases, _ = import_cases(write(tmp_path, "c.json", json.dumps({"cases": data})))
    assert cases[0].is_multi_turn
    cases, _ = import_cases(write(tmp_path, "c.yaml", "- id: y1\n  input: hi\n  expected_behaviours: [greets]\n"))
    assert cases[0].id == "y1"


def test_bad_cases_are_skipped_with_a_reason(tmp_path):
    text = "- id: ok\n  input: hi\n  expected_behaviours: [x]\n- id: bad\n  input: hi\n"
    cases, warnings = import_cases(write(tmp_path, "c.yaml", text))
    assert [c.id for c in cases] == ["ok"]
    assert "bad" in warnings[0] and "expected_behaviours" in warnings[0]


def test_demo_bundle_evalset_imports(demo_dir):
    cases, warnings = import_cases(demo_dir / "evalset.yaml")
    assert len(cases) >= 8 and not warnings
