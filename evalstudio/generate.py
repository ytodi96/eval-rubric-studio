"""Step 2, the eval set: plan a coverage matrix, write test cases to fill it, remove near-duplicates.

The *code* decides which cases are needed (category x type x difficulty), so coverage doesn't
depend on the model's whims. The model only writes the case for each slot.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import yaml

from . import prompts
from .ai import NULLABLE_STR, STR, arr, ask, obj
from .config import RoleModel
from .intake import slugify
from .llm import LLMError
from .models import EvalSet, TestCase, UseCaseCard
from .store import Store

# Target mix for a whole eval set. Shares are of the total, existing cases included.
TYPE_SHARE = {"typical": 0.5, "edge": 0.2, "adversarial": 0.2, "out_of_scope": 0.1}
DIFFICULTY_SHARE = {"easy": 0.3, "medium": 0.4, "hard": 0.3}
BATCH_SIZE = 10
DUPLICATE_THRESHOLD = 0.5  # share of overlapping 3-word phrases above which two inputs count as near-duplicates


@dataclass(frozen=True)
class Slot:
    category: str
    type: str
    difficulty: str


@dataclass
class GenerateResult:
    cases: list[TestCase]
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- the coverage plan

def apportion(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Split `total` by `shares` into whole numbers that add up to `total` (largest remainder)."""
    s = sum(shares.values())
    exact = {k: total * v / s for k, v in shares.items()}
    out = {k: int(x) for k, x in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def plan_slots(card: UseCaseCard, total: int, existing: list[TestCase]) -> list[Slot]:
    """The slots to fill so the whole set (existing + new) has `total` cases spread evenly across
    categories and close to the target type and difficulty mix."""
    n_new = total - len(existing)
    if n_new <= 0 or not card.categories:
        return []
    cats = card.categories
    deficit = {
        "category": apportion(total, {c: 1 for c in cats}),
        "type": apportion(total, TYPE_SHARE),
        "difficulty": apportion(total, DIFFICULTY_SHARE),
    }
    pairs: Counter = Counter()
    for c in existing:
        for axis, value in (("category", c.category), ("type", c.type), ("difficulty", c.difficulty)):
            if value in deficit[axis]:
                deficit[axis][value] -= 1
        pairs.update([(c.category, c.type), (c.category, c.difficulty), (c.type, c.difficulty)])

    slots = []
    for _ in range(n_new):
        # Most-needed category first; then the type and difficulty that are both needed overall and
        # least used so far in combination with what's already chosen, so the matrix fills evenly.
        # (A value that is still needed always beats one that has reached its target.)
        cat = max(cats, key=lambda c: deficit["category"][c])
        typ = max(TYPE_SHARE, key=lambda t: (deficit["type"][t] > 0, deficit["type"][t] - pairs[(cat, t)]))
        diff = max(DIFFICULTY_SHARE, key=lambda d: (deficit["difficulty"][d] > 0,
                                                    deficit["difficulty"][d] - pairs[(cat, d)] - pairs[(typ, d)]))
        for axis, value in (("category", cat), ("type", typ), ("difficulty", diff)):
            deficit[axis][value] -= 1
        pairs.update([(cat, typ), (cat, diff), (typ, diff)])
        slots.append(Slot(cat, typ, diff))
    return slots


# ---------------------------------------------------------------- writing cases

CASES_SCHEMA = obj({"cases": arr(obj({
    "slot": {"type": "integer"},
    "id": STR,
    "input": arr(obj({"role": {"type": "string", "enum": ["user", "assistant"]}, "content": STR})),
    "context": arr(obj({"key": STR, "value": STR})),
    "expected_behaviours": arr(STR),
    "reference_answer": NULLABLE_STR,
}))})


def generate_cases(card: UseCaseCard, slots: list[Slot], existing: list[TestCase], role: RoleModel,
                   store: Store | None = None, project_id: int | None = None,
                   reference: str = "") -> GenerateResult:
    """`reference` is source material the cases must agree with, e.g. the target's policy."""
    result = GenerateResult(cases=[])
    pending = list(slots)
    for attempt in range(2):  # second pass: retry slots that came back missing, invalid or duplicated
        retry: list[Slot] = []
        for i in range(0, len(pending), BATCH_SIZE):
            batch = pending[i:i + BATCH_SIZE]
            note = ("Some earlier attempts were too similar to existing cases; make these clearly different."
                    if attempt else "")
            try:
                written, cost = _write_batch(card, batch, existing + result.cases, role, store, project_id, note,
                                             reference)
            except LLMError as e:
                result.warnings.append(f"A batch of {len(batch)} failed: {e}")
                retry += batch
                continue
            result.cost_usd += cost
            for slot, case in zip(batch, written):
                if case is None:
                    retry.append(slot)
                    continue
                twin = _near_duplicate(case, existing + result.cases)
                if twin:
                    if attempt:
                        result.warnings.append(f"Dropped a {slot.type} case in '{slot.category}': too similar to '{twin}'.")
                    else:
                        retry.append(slot)
                    continue
                case.id = _unique_id(case.id, existing + result.cases)
                result.cases.append(case)
        pending = retry
        if not pending:
            break
    if pending:
        result.warnings.append(f"{len(pending)} slot(s) couldn't be filled; generate again to top up.")
    return result


def batch_message(card: UseCaseCard, batch: list[Slot], others: list[TestCase], note: str = "",
                  reference: str = "") -> str:
    """The user message for one batch of slots (also used by --dry-run)."""
    card_yaml = yaml.safe_dump(card.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100)
    existing = "\n".join(f"- {c.id} [{c.category} / {c.type} / {c.difficulty}]: {_short(_text(c))}"
                         for c in others) or "(none yet)"
    slot_lines = "\n".join(f"{n}. category: {s.category} | type: {s.type} | difficulty: {s.difficulty}"
                           for n, s in enumerate(batch, 1))
    ref = f"<reference_material>\n{reference.strip()}\n</reference_material>\n\n" if reference.strip() else ""
    user = (f"<use_case_card>\n{card_yaml}</use_case_card>\n\n{ref}"
            f"<existing_test_cases>\n{existing}\n</existing_test_cases>\n\n"
            f"<slots>\n{slot_lines}\n</slots>\n\n"
            f"Write exactly {len(batch)} test cases, one per slot, and set `slot` to the slot number. "
            f"Give each a short, descriptive id in kebab-case (e.g. 'late-order-angry-regular'). {note}").strip()
    return user


def _write_batch(card, batch, others, role, store, project_id, note,
                 reference="") -> tuple[list[TestCase | None], float]:
    user = batch_message(card, batch, others, note, reference)
    r = ask(role, prompts.load("generate_cases"), user, schema=CASES_SCHEMA, schema_name="test_cases",
            store=store, project_id=project_id, purpose="generate: test cases")
    by_slot = {c["slot"]: c for c in r.data["cases"]}
    return [_case_from_json(by_slot.get(n), s) for n, s in enumerate(batch, 1)], r.cost_usd


def _case_from_json(data: dict | None, slot: Slot) -> TestCase | None:
    if not data or not data["input"] or not data["expected_behaviours"]:
        return None
    turns = [t for t in data["input"] if t["content"].strip()]
    if not turns or turns[-1]["role"] != "user":
        return None
    try:
        return TestCase(
            id=slugify(data["id"]) or "case",
            input=turns[0]["content"] if len(turns) == 1 else turns,
            category=slot.category, type=slot.type, difficulty=slot.difficulty,
            expected_behaviours=[b for b in data["expected_behaviours"] if b.strip()],
            reference_answer=data["reference_answer"] or None,
            context={kv["key"]: kv["value"] for kv in data["context"] if kv["key"].strip()},
            source="generated",
        )
    except ValueError:
        return None


def _inputs(store: Store, project_id: int, replace: bool):
    card_version = store.latest_approved(project_id, "usecase")
    if card_version is None:
        raise ValueError("Approve the use-case card first: test cases are generated from it.")
    card = UseCaseCard.model_validate(card_version.data)
    try:
        existing = [] if replace else store.load(project_id, "evalset").cases
    except LookupError:
        existing = []
    return card_version, card, existing


def dry_run_messages(store: Store, project_id: int, total: int, replace: bool = False) -> tuple[str, list[str]]:
    """(system prompt, one user message per batch) that generating would send, without calling."""
    _, card, existing = _inputs(store, project_id, replace)
    slots = plan_slots(card, total, existing)
    reference = _reference_material(store, project_id)
    users = [batch_message(card, slots[i:i + BATCH_SIZE], existing, "", reference)
             for i in range(0, len(slots), BATCH_SIZE)]
    return prompts.load("generate_cases"), users


def generate_for_project(store: Store, project_id: int, total: int, role: RoleModel,
                         replace: bool = False) -> tuple[GenerateResult, int]:
    """Top up the project's eval set to `total` cases from its approved use-case card.
    Saves the result as an edit of the eval set and returns (result, eval set version)."""
    card_version, card, existing = _inputs(store, project_id, replace)
    result = generate_cases(card, plan_slots(card, total, existing), existing, role, store, project_id,
                            reference=_reference_material(store, project_id))
    v = store.save_edit(project_id, "evalset", EvalSet(cases=existing + result.cases),
                        note=f"generated {len(result.cases)} cases from card v{card_version.version}")
    return result, v.version


def _reference_material(store: Store, project_id: int) -> str:
    """The facts test cases must agree with: for a prompt target, its system prompt (where the policy
    usually lives). Other targets have no prompt to read, so the card alone is used."""
    try:
        target = store.load(project_id, "target")
    except LookupError:
        return ""
    if target.config.adapter != "prompt":
        return ""
    return f"The system prompt of the system under test:\n\n{target.config.system_prompt}"


# ---------------------------------------------------------------- near-duplicates

def _text(case: TestCase) -> str:
    return case.input if isinstance(case.input, str) else "\n".join(t.content for t in case.input)


def _short(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "..."


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i:i + 3]) for i in range(max(len(words) - 2, 1))}


def similarity(a: str, b: str) -> float:
    """Overlap of 3-word phrases between two texts (0 = nothing shared, 1 = identical)."""
    sa, sb = _shingles(a), _shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _near_duplicate(case: TestCase, others: list[TestCase]) -> str | None:
    text = _text(case)
    return next((o.id for o in others if similarity(text, _text(o)) >= DUPLICATE_THRESHOLD), None)


def _unique_id(case_id: str, others: list[TestCase]) -> str:
    taken = {o.id for o in others}
    if case_id not in taken:
        return case_id
    n = 2
    while f"{case_id}-{n}" in taken:
        n += 1
    return f"{case_id}-{n}"


def near_duplicate_pairs(evalset: EvalSet) -> list[tuple[str, str, float]]:
    """Pairs of cases in a set whose inputs are near-duplicates, for review."""
    cases = evalset.cases
    return [(a.id, b.id, round(s, 2)) for i, a in enumerate(cases) for b in cases[i + 1:]
            if (s := similarity(_text(a), _text(b))) >= DUPLICATE_THRESHOLD]
