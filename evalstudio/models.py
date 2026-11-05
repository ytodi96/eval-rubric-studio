"""The data model. Every object the Studio stores or exchanges is defined here.

Project → UseCaseCard → EvalSet (TestCases) → RubricSet (Rubric per test case) → Target
       → Run → Response → Grade (per criterion) → HumanLabel

UseCaseCard, EvalSet, RubricSet and Target are *versioned*: each save makes a new numbered
version, and a run records exactly which versions it used.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)]


class Strict(BaseModel):
    """Base for everything: unknown fields are an error, so typos in YAML files are caught."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 1. Use-case card

class Dimension(Strict):
    """A quality dimension that matters for this use case (from library.DIMENSIONS, or custom)."""

    key: Slug
    description: str
    weight: float = Field(1.0, gt=0)
    custom: bool = False


class UseCaseCard(Strict):
    title: str
    goal: str
    users: str
    input_types: list[str]
    output_format: str
    dimensions: list[Dimension]
    categories: list[str] = Field(description="Test categories, e.g. 'refund request'. Rows of the coverage matrix.")
    failure_modes: list[str] = []
    must: list[str] = Field([], description="Hard rules the output must always follow.")
    must_not: list[str] = Field([], description="Hard rules the output must never break.")
    out_of_scope: list[str] = []
    notes: str = ""


# ---------------------------------------------------------------- 2. Eval set

Difficulty = Literal["easy", "medium", "hard"]
CaseType = Literal["typical", "edge", "adversarial", "out_of_scope"]


class Turn(Strict):
    role: Literal["user", "assistant"]
    content: str


class TestCase(Strict):
    __test__ = False  # tell pytest this is not a test class

    id: Slug
    input: str | list[Turn] = Field(description="A single message, or a multi-turn conversation ending with the user.")
    category: str
    difficulty: Difficulty = "medium"
    type: CaseType = "typical"
    expected_behaviours: list[str] = Field(min_length=1)
    reference_answer: str | None = None
    context: dict[str, str] = Field({}, description="Extra data the target gets with the input, e.g. order details.")
    source: Literal["manual", "generated", "imported"] = "manual"

    @field_validator("input")
    @classmethod
    def _conversation_ends_with_user(cls, v):
        if isinstance(v, list) and (not v or v[-1].role != "user"):
            raise ValueError("a multi-turn input must end with a user message")
        return v

    @property
    def is_multi_turn(self) -> bool:
        return isinstance(self.input, list)


class EvalSet(Strict):
    cases: list[TestCase]

    @model_validator(mode="after")
    def _unique_ids(self):
        dupes = [i for i, n in Counter(c.id for c in self.cases).items() if n > 1]
        if dupes:
            raise ValueError(f"duplicate test case ids: {', '.join(dupes)}")
        return self

    def coverage(self) -> dict[str, dict[str, int]]:
        """Coverage matrix: category → {type or difficulty → number of cases}."""
        matrix: dict[str, dict[str, int]] = {}
        for c in self.cases:
            row = matrix.setdefault(c.category, {})
            for key in (c.type, c.difficulty):
                row[key] = row.get(key, 0) + 1
        return matrix


# ---------------------------------------------------------------- 3. Rubrics

class Contains(Strict):
    """contains: the response includes all (or, with match: any, at least one) of `values`.
    not_contains: none of `values` appears. Matching ignores curly-vs-straight quotes and dashes."""

    type: Literal["contains", "not_contains"]
    values: list[str] = Field(min_length=1)
    match: Literal["all", "any"] = "all"
    case_sensitive: bool = False


REGEX_FLAGS = re.MULTILINE


class Regex(Strict):
    """Patterns run in multiline mode: ^ and $ match at the start and end of every line.
    Use \\A and \\Z to anchor to the start and end of the whole response."""

    type: Literal["regex", "not_regex"]
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v):
        try:
            re.compile(v, REGEX_FLAGS)
        except re.error as e:
            raise ValueError(f"not a valid regular expression: {e}") from None
        return v


class JsonSchema(Strict):
    type: Literal["json_schema"]
    json_schema: dict


class Numeric(Strict):
    """Find a number in the response (via `pattern`, first capture group) and compare it."""

    type: Literal["numeric"]
    pattern: str
    expected: float
    tolerance: float = 0.0


class Length(Strict):
    type: Literal["length"]
    unit: Literal["words", "chars"] = "words"
    min: int | None = None
    max: int | None = None


class PythonCheck(Strict):
    """A custom function `module:function(response: str, case: TestCase) -> bool | (bool, str)`."""

    type: Literal["python"]
    function: str = Field(pattern=r"^[\w.]+:\w+$")


CodeCheck = Annotated[Contains | Regex | JsonSchema | Numeric | Length | PythonCheck, Field(discriminator="type")]


class Criterion(Strict):
    id: Slug
    name: str
    description: str
    dimension: str | None = None
    weight: float = Field(1.0, gt=0)
    scale: Literal["pass_fail", "1-4"] = "pass_fail"
    anchors: dict[int, str] = Field({}, description="For the 1-4 scale: what each level looks like.")
    must_pass: bool = False
    check: CodeCheck | None = Field(None, description="A code check. Leave empty to grade with the LLM judge.")
    also_judge: bool = Field(False, description="Grade with the code check AND the LLM judge; both must pass. "
                                                "Catches what a word list or pattern misses.")
    shared: bool | None = Field(None, description="True for a copy of one of the rubric set's common criteria.")

    @model_validator(mode="after")
    def _scale_rules(self):
        if self.scale == "1-4" and sorted(self.anchors) != [1, 2, 3, 4]:
            raise ValueError(f"criterion '{self.id}': the 1-4 scale needs an anchor for each of 1, 2, 3 and 4")
        if self.check is not None and self.scale != "pass_fail":
            raise ValueError(f"criterion '{self.id}': code checks are pass/fail only")
        if self.also_judge and self.check is None:
            raise ValueError(f"criterion '{self.id}': also_judge needs a code check (without one, the judge grades it anyway)")
        return self

    @property
    def grader(self) -> Literal["code", "llm", "code+llm"]:
        if self.check is None:
            return "llm"
        return "code+llm" if self.also_judge else "code"


class Rubric(Strict):
    test_case_id: Slug
    criteria: list[Criterion] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_criteria(self):
        dupes = [i for i, n in Counter(c.id for c in self.criteria).items() if n > 1]
        if dupes:
            raise ValueError(f"rubric '{self.test_case_id}': duplicate criterion ids: {', '.join(dupes)}")
        return self


class RubricSet(Strict):
    rubrics: list[Rubric]
    common: list[Criterion] = Field([], description="Criteria shared by every rubric (already copied into "
                                                    "each one); kept here so new rubrics reuse them.")
    evalset_version: int | None = Field(None, description="The eval set version these rubrics were written for.")

    def for_case(self, test_case_id: str) -> Rubric | None:
        return next((r for r in self.rubrics if r.test_case_id == test_case_id), None)


# ---------------------------------------------------------------- 4. Targets (the system under test)

class PromptTarget(Strict):
    adapter: Literal["prompt"]
    provider: Literal["openai", "anthropic"] = "openai"
    model: str
    system_prompt: str
    max_tokens: int = 4000
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class HttpTarget(Strict):
    """POST to an endpoint. `body` may use {{input}}, {{messages}} and {{context.<key>}} placeholders."""

    adapter: Literal["http"]
    url: str
    headers: dict[str, str] = Field({}, description="Values like ${MY_TOKEN} are read from the environment.")
    body: dict | str
    response_path: str = Field(description="Where the reply is in the JSON response, e.g. 'reply.text'.")
    timeout_s: float = 120


class PythonTarget(Strict):
    """Call `module:function(input, context)` from another project folder."""

    adapter: Literal["python"]
    function: str = Field(pattern=r"^[\w.]+:\w+$")
    project_dir: str | None = Field(None, description="Folder added to the import path. Relative to the bundle.")
    response_path: str | None = Field(None, description="If the function returns a dict/object, where the reply is.")


class OfflineTarget(Strict):
    """Read already-collected responses from a CSV or JSON file (no live calls)."""

    adapter: Literal["offline"]
    file: str
    id_field: str = "test_case_id"
    output_field: str = "output"


TargetConfig = Annotated[PromptTarget | HttpTarget | PythonTarget | OfflineTarget, Field(discriminator="adapter")]


class Target(Strict):
    name: str = Field("default", description="Shown in reports, e.g. 'prompt v3'.")
    config: TargetConfig


# ---------------------------------------------------------------- Projects and versions

class Project(Strict):
    id: int | None = None
    slug: Slug
    title: str
    description: str = ""
    created_at: datetime | None = None


ArtifactKind = Literal["usecase", "evalset", "rubrics", "target"]
ARTIFACT_MODELS: dict[str, type[BaseModel]] = {
    "usecase": UseCaseCard,
    "evalset": EvalSet,
    "rubrics": RubricSet,
    "target": Target,
}


class Version(Strict):
    """One saved version of a versioned artifact."""

    kind: ArtifactKind
    version: int
    status: Literal["draft", "approved"] = "draft"
    note: str = ""
    created_at: datetime
    data: dict


# ---------------------------------------------------------------- 5-8. Runs, grades, human labels

class Run(Strict):
    id: int | None = None
    project_id: int
    usecase_version: int
    evalset_version: int
    rubrics_version: int
    target_version: int
    judge_model: str
    status: Literal["pending", "running", "done", "failed", "stopped"] = "pending"
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: dict = {}


class ResponseRecord(Strict):
    id: int | None = None
    run_id: int
    test_case_id: str
    output: str | None
    error: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    score: float | None = None
    passed: bool | None = None


class Grade(Strict):
    id: int | None = None
    response_id: int
    criterion_id: str
    grader: Literal["code", "llm"]
    attempt: int = 1
    score: float = Field(description="1 or 0 for pass/fail; 1-4 for the 1-4 scale.")
    passed: bool
    reasoning: str = ""
    evidence: str | None = Field(None, description="A quote from the response, or None if the judge couldn't find one.")
    cost_usd: float = 0.0


class HumanLabel(Strict):
    id: int | None = None
    response_id: int
    criterion_id: str
    score: float
    passed: bool
    note: str = ""
    created_at: datetime | None = None
