"""Step 3, rubrics: common criteria for every response, plus specific criteria for each test case.

Each code check the model proposes is tried on the test case's reference answer (a known good
response). A check that fails a good answer is wrong, so it is turned into an LLM-judge criterion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import yaml
from pydantic import TypeAdapter, ValidationError

from . import prompts
from .ai import NULLABLE_STR, STR, arr, ask, obj
from .checks import run_check
from .config import RoleModel
from .generate import _reference_material
from .intake import slugify
from .llm import LLMError
from .models import CodeCheck, Criterion, EvalSet, Rubric, RubricSet, TestCase, UseCaseCard
from .store import NotFound, Store

BATCH_SIZE = 5
CHECK_TYPES = ["none", "contains", "not_contains", "regex", "not_regex", "numeric", "length", "json_schema"]
# The fields each check type uses; anything else the model fills in is ignored.
CHECK_FIELDS = {
    "contains": {"values", "match", "case_sensitive"}, "not_contains": {"values", "match", "case_sensitive"},
    "regex": {"pattern"}, "not_regex": {"pattern"}, "numeric": {"pattern", "expected", "tolerance"},
    "length": {"unit", "min", "max"}, "json_schema": {"json_schema"},
}


def _nullable(schema: dict) -> dict:
    t = schema["type"]
    return {**schema, "type": [t, "null"]} | ({"enum": schema["enum"] + [None]} if "enum" in schema else {})


CHECK_SCHEMA = obj({
    "type": {"type": "string", "enum": CHECK_TYPES},
    "values": _nullable(arr(STR)),
    "match": _nullable({"type": "string", "enum": ["all", "any"]}),
    "case_sensitive": _nullable({"type": "boolean"}),
    "pattern": NULLABLE_STR,
    "expected": _nullable({"type": "number"}),
    "tolerance": _nullable({"type": "number"}),
    "unit": _nullable({"type": "string", "enum": ["words", "chars"]}),
    "min": _nullable({"type": "integer"}),
    "max": _nullable({"type": "integer"}),
    "json_schema": NULLABLE_STR,
})
CRITERION_SCHEMA = obj({
    "id": STR, "name": STR, "description": STR, "dimension": NULLABLE_STR,
    "weight": {"type": "number"}, "must_pass": {"type": "boolean"},
    "scale": {"type": "string", "enum": ["pass_fail", "1-4"]}, "anchors": arr(STR),
    "check": CHECK_SCHEMA, "also_judge": {"type": "boolean"},
})
COMMON_SCHEMA = obj({"criteria": arr(CRITERION_SCHEMA)})
CASES_SCHEMA = obj({"rubrics": arr(obj({"test_case_id": STR, "criteria": arr(CRITERION_SCHEMA)}))})

_check_adapter = TypeAdapter(CodeCheck)


@dataclass
class RubricResult:
    rubrics: list[Rubric]
    common: list[Criterion]
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- turning model output into criteria

def criterion_from_json(d: dict, dimensions: set[str], where: str) -> tuple[Criterion | None, list[str]]:
    """Build a valid Criterion from the model's JSON, repairing what can be repaired."""
    warnings = []
    check = None
    c = d["check"]
    if c["type"] != "none":
        spec = {k: v for k, v in c.items() if v is not None and k in CHECK_FIELDS[c["type"]] | {"type"}}
        if c["type"] in ("regex", "not_regex") and "pattern" not in spec and len(c["values"] or []) == 1:
            spec["pattern"] = c["values"][0]  # models often put the pattern in `values`
        try:
            if "json_schema" in spec:
                spec["json_schema"] = json.loads(spec["json_schema"])
                spec = _string_pattern_to_regex(spec)
            check = _check_adapter.validate_python(spec)
        except (ValueError, ValidationError) as e:
            warnings.append(f"{where} '{d['id']}': its {c['type']} check was invalid ({_first_error(e)}); "
                            "the LLM judge grades it instead.")
    scale, anchors = d["scale"], {}
    if check is not None:
        scale = "pass_fail"
    elif scale == "1-4":
        if len(d["anchors"]) == 4 and all(a.strip() for a in d["anchors"]):
            anchors = {i + 1: a.strip() for i, a in enumerate(d["anchors"])}
        else:
            scale = "pass_fail"
            warnings.append(f"{where} '{d['id']}': the 1-4 scale needs 4 anchors; switched to pass/fail.")
    dimension = d["dimension"] if d["dimension"] in dimensions else None
    try:
        crit = Criterion(
            id=slugify(d["id"]) or "criterion", name=d["name"].strip(), description=d["description"].strip(),
            dimension=dimension, weight=min(max(float(d["weight"]), 1.0), 3.0), must_pass=d["must_pass"],
            scale=scale, anchors=anchors, check=check, also_judge=bool(d["also_judge"] and check is not None),
        )
    except ValidationError as e:
        return None, warnings + [f"{where} '{d['id']}' dropped: {_first_error(e)}"]
    return crit, warnings


def _string_pattern_to_regex(spec: dict) -> dict:
    """Models sometimes express a regex as the JSON schema {"type": "string", "pattern": ...}. That
    would require the reply to be JSON, so turn it into the regex check it was meant to be.
    In JSON Schema, ^ and $ anchor the whole text, so they become \\A and \\Z."""
    schema = spec["json_schema"]
    if not (isinstance(schema, dict) and schema.get("type") == "string" and set(schema) <= {"type", "pattern"}
            and "pattern" in schema):
        return spec
    pattern = schema["pattern"]
    if pattern.startswith("^"):
        pattern = r"\A" + pattern[1:]
    if pattern.endswith("$") and not pattern.endswith(r"\$"):
        pattern = pattern[:-1] + r"\Z"
    return {"type": "regex", "pattern": pattern}


def _first_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        err = e.errors()[0]
        return f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
    return str(e)


def verify_on_reference(criteria: list[Criterion], case: TestCase) -> list[str]:
    """Run code checks on the case's reference answer. A check that fails a known-good answer is
    replaced by the LLM judge. Changes `criteria` in place; returns what was changed."""
    if not case.reference_answer:
        return []
    warnings = []
    for crit in criteria:
        if crit.check is None:
            continue
        result = run_check(crit.check, case.reference_answer, case)
        if not result.passed:
            warnings.append(f"{case.id} / {crit.id}: the {crit.check.type} check failed the reference answer "
                            f"({result.detail}); the LLM judge grades it instead.")
            crit.check, crit.also_judge = None, False
    return warnings


def merge(specific: list[Criterion], common: list[Criterion]) -> list[Criterion]:
    """Case-specific criteria first, then copies of the common ones (renamed if an id clashes)."""
    taken = {c.id for c in specific}
    out = list(specific)
    for c in common:
        copy = c.model_copy(deep=True, update={"shared": True})
        while copy.id in taken:
            copy.id = f"{copy.id}-common"
        taken.add(copy.id)
        out.append(copy)
    return out


# ---------------------------------------------------------------- calling the model

def _card_yaml(card: UseCaseCard) -> str:
    return yaml.safe_dump(card.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100)


def _guide() -> str:
    return prompts.load("criterion_guide")


def write_common(card: UseCaseCard, reference: str, role: RoleModel, store=None, project_id=None
                 ) -> tuple[list[Criterion], float, list[str]]:
    ref = f"\n\n<reference_material>\n{reference}\n</reference_material>" if reference.strip() else ""
    r = ask(role, prompts.load("rubric_common", criterion_guide=_guide()),
            f"<use_case_card>\n{_card_yaml(card)}</use_case_card>{ref}",
            schema=COMMON_SCHEMA, schema_name="common_criteria", store=store, project_id=project_id,
            purpose="rubrics: common criteria")
    dims = {d.key for d in card.dimensions}
    criteria, warnings = [], []
    for d in r.data["criteria"]:
        crit, w = criterion_from_json(d, dims, "Common criterion")
        warnings += w
        if crit and crit.id not in {c.id for c in criteria}:
            criteria.append(crit)
    return criteria, r.cost_usd, warnings


def _render_case(case: TestCase) -> str:
    parts = [f'<test_case id="{case.id}" category="{case.category}" type="{case.type}" difficulty="{case.difficulty}">']
    if case.is_multi_turn:
        parts += [f"<{t.role}>\n{t.content}\n</{t.role}>" for t in case.input]
    else:
        parts.append(f"<input>\n{case.input}\n</input>")
    if case.context:
        parts.append("<context>\n" + "\n".join(f"{k}: {v}" for k, v in case.context.items()) + "\n</context>")
    parts.append("<expected_behaviours>\n" + "\n".join(f"- {b}" for b in case.expected_behaviours)
                 + "\n</expected_behaviours>")
    if case.reference_answer:
        parts.append(f"<reference_answer>\n{case.reference_answer}\n</reference_answer>")
    return "\n".join(parts) + "\n</test_case>"


def case_batch_message(card: UseCaseCard, batch: list[TestCase], common_text: str, ref: str) -> str:
    return (f"<use_case_card>\n{_card_yaml(card)}</use_case_card>\n\n{ref}"
            f"<common_criteria>\n{common_text}\n</common_criteria>\n\n"
            + "\n\n".join(_render_case(c) for c in batch)
            + f"\n\nWrite one rubric for each of these {len(batch)} test cases, with test_case_id set to the case id.")


def dry_run_messages(store: Store, project_id: int, regenerate: bool = False) -> list[tuple[str, str, list[str]]]:
    """[(purpose, system prompt, user messages)] that writing rubrics would send, without calling."""
    card_v, evalset_v = store.latest_approved(project_id, "usecase"), store.latest_approved(project_id, "evalset")
    if card_v is None or evalset_v is None:
        raise ValueError("Approve the use-case card and the eval set first: rubrics are written from them.")
    card, evalset = UseCaseCard.model_validate(card_v.data), EvalSet.model_validate(evalset_v.data)
    try:
        existing = store.load(project_id, "rubrics")
    except NotFound:
        existing = RubricSet(rubrics=[])
    have = set() if regenerate else {r.test_case_id for r in existing.rubrics}
    todo = [c for c in evalset.cases if c.id not in have]
    reference = _reference_material(store, project_id)
    ref = f"<reference_material>\n{reference}\n</reference_material>\n\n" if reference.strip() else ""
    out = []
    common = [] if regenerate else existing.common
    if todo and not common:
        out.append(("rubrics: common criteria", prompts.load("rubric_common", criterion_guide=_guide()),
                    [f"<use_case_card>\n{_card_yaml(card)}</use_case_card>\n\n{ref}"]))
    common_text = "\n".join(f"- {c.id}: {c.description}" for c in common) or "(written in the first call)"
    users = [case_batch_message(card, todo[i:i + BATCH_SIZE], common_text, ref) for i in range(0, len(todo), BATCH_SIZE)]
    if users:
        out.append(("rubrics: test cases", prompts.load("rubric_case", criterion_guide=_guide()), users))
    return out


def write_case_rubrics(card: UseCaseCard, cases: list[TestCase], common: list[Criterion], reference: str,
                       role: RoleModel, store=None, project_id=None) -> RubricResult:
    result = RubricResult(rubrics=[], common=common)
    dims = {d.key for d in card.dimensions}
    common_text = "\n".join(f"- {c.id}: {c.description}" for c in common) or "(none)"
    ref = f"<reference_material>\n{reference}\n</reference_material>\n\n" if reference.strip() else ""
    system = prompts.load("rubric_case", criterion_guide=_guide())
    for i in range(0, len(cases), BATCH_SIZE):
        batch = cases[i:i + BATCH_SIZE]
        user = case_batch_message(card, batch, common_text, ref)
        try:
            r = ask(role, system, user, schema=CASES_SCHEMA, schema_name="rubrics", store=store,
                    project_id=project_id, purpose="rubrics: test cases")
        except LLMError as e:
            result.warnings.append(f"Rubrics for {', '.join(c.id for c in batch)} failed: {e}")
            continue
        result.cost_usd += r.cost_usd
        by_id = {x["test_case_id"]: x for x in r.data["rubrics"]}
        for case in batch:
            raw = by_id.get(case.id)
            if raw is None:
                result.warnings.append(f"No rubric came back for '{case.id}'; generate again to fill it.")
                continue
            specific = []
            for d in raw["criteria"]:
                crit, w = criterion_from_json(d, dims, case.id)
                result.warnings += w
                if crit and crit.id not in {c.id for c in specific}:
                    specific.append(crit)
            if not specific:
                result.warnings.append(f"'{case.id}' got no usable criteria; generate again to fill it.")
                continue
            criteria = merge(specific, common)
            result.warnings += verify_on_reference(criteria, case)
            result.rubrics.append(Rubric(test_case_id=case.id, criteria=criteria))
    return result


def generate_rubrics(store: Store, project_id: int, role: RoleModel, regenerate: bool = False
                     ) -> tuple[RubricResult, int]:
    """Write rubrics for every test case in the approved eval set that doesn't have one yet
    (or for all of them, with `regenerate`). Saves an edit of the rubric set."""
    card_v = store.latest_approved(project_id, "usecase")
    evalset_v = store.latest_approved(project_id, "evalset")
    if card_v is None or evalset_v is None:
        raise ValueError("Approve the use-case card and the eval set first: rubrics are written from them.")
    card, evalset = UseCaseCard.model_validate(card_v.data), EvalSet.model_validate(evalset_v.data)
    try:
        existing = store.load(project_id, "rubrics")
    except NotFound:
        existing = RubricSet(rubrics=[])
    case_ids = {c.id for c in evalset.cases}
    keep = [] if regenerate else [r for r in existing.rubrics if r.test_case_id in case_ids]
    todo = [c for c in evalset.cases if c.id not in {r.test_case_id for r in keep}]
    reference = _reference_material(store, project_id)

    cost, warnings = 0.0, []
    common = [] if regenerate else existing.common
    if todo and not common:
        common, cost, warnings = write_common(card, reference, role, store, project_id)
    result = write_case_rubrics(card, todo, common, reference, role, store, project_id) if todo \
        else RubricResult(rubrics=[], common=common)
    result.cost_usd += cost
    result.warnings = warnings + result.warnings

    order = {c.id: n for n, c in enumerate(evalset.cases)}
    rubrics = sorted(keep + result.rubrics, key=lambda r: order[r.test_case_id])
    v = store.save_edit(project_id, "rubrics",
                        RubricSet(rubrics=rubrics, common=common, evalset_version=evalset_v.version),
                        note=f"{len(result.rubrics)} rubrics written for eval set v{evalset_v.version}")
    return result, v.version


def _without_common(rubric: Rubric, old_common: list[Criterion]) -> list[Criterion] | None:
    """The rubric's own criteria, or None if it doesn't use the common criteria (e.g. hand-written)."""
    if any(c.shared for c in rubric.criteria):
        return [c for c in rubric.criteria if not c.shared]
    # Rubrics written before copies were marked: recognise the copies by id.
    ids = {c.id for c in rubric.criteria}
    drop = set()
    for c in old_common:
        if f"{c.id}-common" in ids:
            drop.add(f"{c.id}-common")
        elif c.id in ids:
            drop.add(c.id)
    if not old_common or len(drop) < len(old_common):
        return None
    return [c for c in rubric.criteria if c.id not in drop]


def refresh_common(store: Store, project_id: int, role: RoleModel) -> tuple[RubricResult, int]:
    """Rewrite only the common criteria and swap them into every rubric that uses them; the
    case-specific criteria are kept, so this costs one call."""
    card_v = store.latest_approved(project_id, "usecase")
    evalset_v = store.latest_approved(project_id, "evalset")
    if card_v is None or evalset_v is None:
        raise ValueError("Approve the use-case card and the eval set first: rubrics are written from them.")
    card, evalset = UseCaseCard.model_validate(card_v.data), EvalSet.model_validate(evalset_v.data)
    rs = store.load(project_id, "rubrics")
    common, cost, warnings = write_common(card, _reference_material(store, project_id), role, store, project_id)
    cases = {c.id: c for c in evalset.cases}
    updated = []
    for rubric in rs.rubrics:
        own = _without_common(rubric, rs.common)
        if own is None:
            updated.append(rubric)  # leave rubrics that never used the common criteria alone
            continue
        criteria = merge(own, common)
        if rubric.test_case_id in cases:
            warnings += verify_on_reference(criteria, cases[rubric.test_case_id])
        updated.append(Rubric(test_case_id=rubric.test_case_id, criteria=criteria))
    v = store.save_edit(project_id, "rubrics", RubricSet(rubrics=updated, common=common,
                                                         evalset_version=rs.evalset_version),
                        note="common criteria rewritten")
    return RubricResult(rubrics=[], common=common, cost_usd=cost, warnings=warnings), v.version


def missing_rubrics(evalset: EvalSet, rubrics: RubricSet) -> list[str]:
    have = {r.test_case_id for r in rubrics.rubrics}
    return [c.id for c in evalset.cases if c.id not in have]

