import pytest
from pydantic import ValidationError

from evalstudio.models import Criterion, EvalSet, Rubric, Target, TestCase


def case(**kw):
    return {"id": "c1", "input": "hi", "category": "x", "expected_behaviours": ["says hi"]} | kw


def test_multi_turn_input_must_end_with_user():
    ok = TestCase.model_validate(case(input=[{"role": "user", "content": "a"},
                                            {"role": "assistant", "content": "b"},
                                            {"role": "user", "content": "c"}]))
    assert ok.is_multi_turn
    with pytest.raises(ValidationError, match="end with a user message"):
        TestCase.model_validate(case(input=[{"role": "user", "content": "a"},
                                            {"role": "assistant", "content": "b"}]))


def test_duplicate_case_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate test case ids: c1"):
        EvalSet.model_validate({"cases": [case(), case()]})


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        TestCase.model_validate(case(expected="typo for expected_behaviours"))


def test_one_to_four_scale_needs_all_anchors():
    with pytest.raises(ValidationError, match="anchor for each"):
        Criterion(id="tone", name="Tone", description="d", scale="1-4", anchors={1: "a", 4: "d"})


def test_code_checks_are_pass_fail_only():
    with pytest.raises(ValidationError, match="pass/fail only"):
        Criterion(id="len", name="Len", description="d", scale="1-4",
                  anchors={1: "a", 2: "b", 3: "c", 4: "d"},
                  check={"type": "length", "max": 100})


def test_grader_follows_check():
    assert Criterion(id="a", name="A", description="d").grader == "llm"
    assert Criterion(id="b", name="B", description="d", check={"type": "regex", "pattern": "x"}).grader == "code"


def test_duplicate_criteria_rejected():
    c = {"id": "a", "name": "A", "description": "d"}
    with pytest.raises(ValidationError, match="duplicate criterion ids"):
        Rubric.model_validate({"test_case_id": "c1", "criteria": [c, c]})


def test_target_adapter_is_chosen_by_name():
    t = Target.model_validate({"config": {"adapter": "offline", "file": "responses.csv"}})
    assert t.config.adapter == "offline" and t.config.output_field == "output"
    with pytest.raises(ValidationError):
        Target.model_validate({"config": {"adapter": "carrier-pigeon"}})


def test_coverage_matrix_counts():
    es = EvalSet.model_validate({"cases": [
        case(id="a", category="refunds", type="typical", difficulty="easy"),
        case(id="b", category="refunds", type="edge", difficulty="easy"),
        case(id="c", category="delivery", type="adversarial", difficulty="hard"),
    ]})
    m = es.coverage()
    assert m["refunds"] == {"typical": 1, "edge": 1, "easy": 2}
    assert m["delivery"] == {"adversarial": 1, "hard": 1}


def test_code_check_plus_judge():
    c = Criterion(id="no-discount", name="N", description="d", also_judge=True,
                  check={"type": "not_contains", "values": ["% off"]})
    assert c.grader == "code+llm"
    with pytest.raises(ValidationError, match="also_judge needs a code check"):
        Criterion(id="x", name="X", description="d", also_judge=True)


def test_bad_regex_is_rejected():
    with pytest.raises(ValidationError, match="not a valid regular expression"):
        Criterion(id="x", name="X", description="d", check={"type": "regex", "pattern": "(unclosed"})


def test_demo_regexes_behave_in_multiline_mode(demo_dir):
    import re

    import yaml

    from evalstudio.models import REGEX_FLAGS, RubricSet

    rubrics = RubricSet.model_validate({k: v for k, v in yaml.safe_load(
        (demo_dir / "rubrics.yaml").read_text(encoding="utf-8")).items() if k != "format"})
    crit = {c.id: c.check.pattern for c in rubrics.for_case("lost-parcel-delivered").criteria
            if c.check is not None and c.check.type in ("regex", "not_regex")}
    good = "Hi Maria,\n\nSorry about that.\n\nWarm regards,\nThe Brightside Coffee team\n"

    def found(cid, text):
        return re.search(crit[cid], text, REGEX_FLAGS) is not None

    assert found("greeting", good) and found("sign-off", good) and not found("no-placeholders", good)
    assert not found("greeting", "Hello!\nHi Maria,")            # greeting must be at the very start
    assert not found("sign-off", good + "\nP.S. one more thing")  # sign-off must be at the very end
    assert found("no-placeholders", "Hi Maria,\n# Your order\nok")  # heading on a later line is caught
    assert found("no-placeholders", "Dear [Name],")
