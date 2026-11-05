"""Step 1, intake: understand the use case, ask clarifying questions, write the use-case card."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from . import prompts
from .ai import NULLABLE_STR, STR, arr, ask, obj
from .config import RoleModel
from .library import DIMENSIONS
from .models import UseCaseCard
from .store import Store

MAX_QUESTIONS = 5


@dataclass
class Materials:
    """Everything the team gives us about the use case. Only the brief is required."""

    brief: str
    system_prompt: str = ""
    samples: str = ""
    docs: str = ""
    existing_cases: str = ""

    def render(self) -> str:
        parts = [("brief", self.brief), ("target_system_prompt", self.system_prompt),
                 ("sample_inputs_and_outputs", self.samples), ("docs_or_knowledge_base", self.docs),
                 ("existing_test_cases", self.existing_cases)]
        return "\n\n".join(f"<{tag}>\n{text.strip()}\n</{tag}>" for tag, text in parts if text.strip())


class Question(BaseModel):
    question: str
    why: str
    options: list[str] = []


class Questions(BaseModel):
    understanding: str
    questions: list[Question]


@dataclass
class Answer:
    question: str
    answer: str


@dataclass
class IntakeResult:
    card: UseCaseCard
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)


QUESTIONS_SCHEMA = obj({
    "understanding": STR,
    "questions": arr(obj({"question": STR, "why": STR, "options": arr(STR)})),
})

CARD_SCHEMA = obj({
    "title": STR,
    "goal": STR,
    "users": STR,
    "input_types": arr(STR),
    "output_format": STR,
    "dimensions": arr(obj({"key": STR, "description": STR, "weight": {"type": "number"},
                           "custom": {"type": "boolean"}})),
    "categories": arr(STR),
    "failure_modes": arr(STR),
    "must": arr(STR),
    "must_not": arr(STR),
    "out_of_scope": arr(STR),
    "notes": NULLABLE_STR,
})


def ask_questions(materials: Materials, role: RoleModel, store: Store | None = None,
                  project_id: int | None = None) -> tuple[Questions, float]:
    result = ask(role, prompts.load("intake_questions"), materials.render(),
                 schema=QUESTIONS_SCHEMA, schema_name="clarifying_questions",
                 store=store, project_id=project_id, purpose="intake: questions")
    qs = Questions.model_validate(result.data)
    qs.questions = qs.questions[:MAX_QUESTIONS]
    return qs, result.cost_usd


def write_card(materials: Materials, answers: list[Answer], role: RoleModel, store: Store | None = None,
               project_id: int | None = None) -> IntakeResult:
    library = "\n".join(f"- {key}: {desc}" for key, desc in DIMENSIONS.items())
    qa = "\n\n".join(f"Q: {a.question}\nA: {a.answer.strip() or '(no answer: make a reasonable assumption)'}"
                     for a in answers)
    user = materials.render() + (f"\n\n<answers_to_clarifying_questions>\n{qa}\n</answers_to_clarifying_questions>"
                                 if answers else "")
    result = ask(role, prompts.load("intake_card", dimension_library=library), user,
                 schema=CARD_SCHEMA, schema_name="use_case_card",
                 store=store, project_id=project_id, purpose="intake: use-case card")
    card, warnings = card_from_json(result.data)
    return IntakeResult(card=card, cost_usd=result.cost_usd, warnings=warnings)


def card_from_json(data: dict) -> tuple[UseCaseCard, list[str]]:
    """Turn the model's card into a valid UseCaseCard, fixing small things and saying what was fixed."""
    warnings = []
    dims, seen = [], set()
    for d in data["dimensions"]:
        key = slugify(d["key"], sep="_")
        if not key or key in seen:
            continue
        seen.add(key)
        weight = min(max(float(d["weight"]), 1.0), 3.0)
        if weight != d["weight"]:
            warnings.append(f"Dimension '{key}': weight {d['weight']} moved into the 1-3 range.")
        dims.append({"key": key, "description": d["description"], "weight": weight,
                     "custom": key not in DIMENSIONS})
    categories = list(dict.fromkeys(c.strip() for c in data["categories"] if c.strip()))
    card = UseCaseCard.model_validate({**data, "dimensions": dims, "categories": categories,
                                       "notes": data.get("notes") or ""})
    return card, warnings


def slugify(text: str, sep: str = "-") -> str:
    s = re.sub(r"[^a-z0-9]+", sep, text.lower()).strip(sep)
    return s[:64]
