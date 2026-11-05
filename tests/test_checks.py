"""The code-check library."""

import pytest
from pydantic import TypeAdapter

from evalstudio.checks import run_check
from evalstudio.models import CodeCheck

check = TypeAdapter(CodeCheck).validate_python

EMAIL = """Hi Maria,

I’m sorry your order #BC-20417 hasn’t arrived. We’ll reship it free; standard shipping takes 3–5 business days.
The total was $43.95.

Warm regards,
The Brightside Coffee team"""


@pytest.mark.parametrize("spec, passed", [
    ({"type": "contains", "values": ["reship", "3-5 business days"]}, True),     # en dash in text, hyphen in value
    ({"type": "contains", "values": ["I'm sorry"]}, True),                       # curly vs straight apostrophe
    ({"type": "contains", "values": ["reship", "refund"]}, False),
    ({"type": "contains", "values": ["reship", "refund"], "match": "any"}, True),
    ({"type": "contains", "values": ["REShip"], "case_sensitive": True}, False),
    ({"type": "not_contains", "values": ["discount", "% off"]}, True),
    ({"type": "not_contains", "values": ["discount", "free"]}, False),           # any forbidden word fails
])
def test_contains(spec, passed):
    assert run_check(check(spec), EMAIL).passed is passed


def test_contains_gives_evidence_and_names_whats_missing():
    r = run_check(check({"type": "contains", "values": ["reship"]}), EMAIL)
    assert "reship" in r.evidence
    r = run_check(check({"type": "contains", "values": ["refund", "photo"]}), EMAIL)
    assert "'refund'" in r.detail and "'photo'" in r.detail


@pytest.mark.parametrize("spec, passed", [
    ({"type": "regex", "pattern": r"\A\s*Hi Maria,"}, True),
    ({"type": "regex", "pattern": r"Warm regards,\s*\n\s*The Brightside Coffee team\s*\Z"}, True),
    ({"type": "regex", "pattern": r"^The Brightside"}, True),                    # ^ matches any line
    ({"type": "not_regex", "pattern": r"\[[A-Za-z ]+\]|^#"}, True),
    ({"type": "not_regex", "pattern": r"#BC-\d+"}, False),
])
def test_regex(spec, passed):
    assert run_check(check(spec), EMAIL).passed is passed


@pytest.mark.parametrize("expected, tolerance, passed", [(43.95, 0, True), (44, 0.1, True), (40, 1, False)])
def test_numeric(expected, tolerance, passed):
    spec = {"type": "numeric", "pattern": r"\$\s?([\d,]+(?:\.\d+)?)", "expected": expected, "tolerance": tolerance}
    r = run_check(check(spec), EMAIL)
    assert r.passed is passed
    if not passed:
        assert "found 43.95" in r.detail


def test_numeric_handles_thousands_and_no_match():
    spec = {"type": "numeric", "pattern": r"₹\s?([\d,]+)", "expected": 13450}
    assert run_check(check(spec), "Your total is ₹13,450 including GST.").passed
    assert "No number" in run_check(check(spec), "No price here").detail


@pytest.mark.parametrize("spec, passed", [
    ({"type": "length", "unit": "words", "min": 20, "max": 60}, True),
    ({"type": "length", "unit": "words", "max": 10}, False),
    ({"type": "length", "unit": "chars", "min": 1000}, False),
])
def test_length(spec, passed):
    assert run_check(check(spec), EMAIL).passed is passed


SCHEMA = {"type": "object", "properties": {"total": {"type": "number"}, "items": {"type": "array"}},
          "required": ["total", "items"]}


def test_json_schema():
    c = check({"type": "json_schema", "json_schema": SCHEMA})
    assert run_check(c, '{"total": 10, "items": []}').passed
    assert run_check(c, 'Here you go:\n```json\n{"total": 10, "items": [1]}\n```').passed  # fenced JSON
    r = run_check(c, '{"total": "ten", "items": []}')
    assert not r.passed and "total" in r.detail
    assert "Not valid JSON" in run_check(c, "total: 10").detail


def test_python_check():
    c = check({"type": "python", "function": "tests.custom_checks:mentions_order_number"})
    assert run_check(c, EMAIL).passed
    r = run_check(c, "Hi there")
    assert not r.passed and r.detail == "no order number"


def test_a_broken_check_fails_instead_of_crashing():
    r = run_check(check({"type": "python", "function": "tests.custom_checks:explodes"}), EMAIL)
    assert not r.passed and "ZeroDivisionError" in r.detail
