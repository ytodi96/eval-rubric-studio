"""Cost estimates before any paid call (what `--dry-run` prints, and what the spending cap checks).

Input tokens are estimated from the real prompt text (about 4 characters per token). Output
tokens come from the average of past real calls of the same kind and model, or a default until
there is history. Calls that would be answered from the cache are counted as free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import llm, prompts
from .adapters import messages_for
from .config import RoleModel
from .grading import judge_schema, judge_user_message
from .models import RubricSet, Target, TestCase
from .store import Store

# Typical output tokens per call (reasoning included), until there is history to average.
DEFAULT_OUTPUT = {
    "intake: questions": 1500, "intake: use-case card": 4000, "generate: test cases": 8000,
    "rubrics: common criteria": 2000, "rubrics: test cases": 6000, "run: target": 500, "run: judge": 600,
}


def tokens(text: str) -> int:
    return len(text) // 4 + 1


@dataclass
class Estimate:
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    lines: list[str] = field(default_factory=list)
    preview: str = ""
    unknown_cost: str = ""  # e.g. an http target whose price the Studio can't know

    def add(self, purpose: str, model: str, calls: int, input_tokens: int, output_tokens: int, cached: int = 0):
        cost = llm.cost_usd(model, input_tokens, 0, output_tokens)
        self.calls += calls
        self.cached_calls += cached
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost
        extra = f", {cached} from cache (free)" if cached else ""
        self.lines.append(f"{purpose}: {calls} call(s) to {model}{extra}, ~{input_tokens:,} in / "
                          f"~{output_tokens:,} out tokens, ~${cost:.3f}")

    def render(self) -> str:
        out = ["Estimate (no calls made):", *[f"  {line}" for line in self.lines],
               f"  Total: {self.calls} call(s), ~${self.cost_usd:.2f}"
               + (f" plus {self.unknown_cost}" if self.unknown_cost else "")]
        if self.preview:
            out += ["", "First prompt, shortened:", self.preview]
        return "\n".join(out)


def expected_output(store: Store | None, purpose: str, model: str) -> int:
    avg = store.average_output_tokens(purpose, model) if store is not None else None
    return int(avg) if avg else DEFAULT_OUTPUT[purpose]


def _cached(store: Store, provider, model, system, messages, schema, schema_name, max_tokens, effort):
    if llm.FRESH:
        return None
    return store.cache_get(llm.cache_key(provider, model, system, messages, schema, schema_name, max_tokens, effort))


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n[... {len(text) - n:,} more characters]"


def estimate_calls(store: Store | None, role: RoleModel, purpose: str, system: str, users: list[str]) -> Estimate:
    """Estimate for a list of generator-style calls whose prompts are known."""
    e = Estimate()
    out = expected_output(store, purpose, role.model)
    e.add(purpose, role.model, len(users), sum(tokens(system + u) for u in users), out * len(users))
    if users:
        e.preview = f"--- system ---\n{_short(system, 600)}\n--- user ---\n{_short(users[0], 1500)}"
    return e


def estimate_run(store: Store, cases: list[TestCase], rubrics: RubricSet, target: Target, judge: RoleModel,
                 double: bool = False) -> Estimate:
    e = Estimate()
    responses: dict[str, str | None] = {}
    cfg = target.config
    if cfg.adapter == "prompt":
        out = expected_output(store, "run: target", cfg.model)
        in_tok = out_tok = cached = 0
        for case in cases:
            msgs = messages_for(case)
            hit = _cached(store, cfg.provider, cfg.model, cfg.system_prompt, msgs, None, "answer",
                          cfg.max_tokens, cfg.effort)
            responses[case.id] = hit["text"] if hit else None
            if hit:
                cached += 1
            else:
                in_tok += tokens(cfg.system_prompt + "".join(m["content"] for m in msgs))
                out_tok += out
        e.add("Target (system under test)", cfg.model, len(cases), in_tok, out_tok, cached)
    else:
        responses = {c.id: None for c in cases}
        if cfg.adapter in ("http", "python"):
            e.unknown_cost = f"whatever the {cfg.adapter} target itself costs ({len(cases)} calls)"

    system = prompts.load("judge")
    out = expected_output(store, "run: judge", judge.model)
    attempts = 2 if double else 1
    calls = in_tok = out_tok = cached = 0
    first_user = ""
    for case in cases:
        rubric = rubrics.for_case(case.id)
        if rubric is None:
            continue
        reply = responses.get(case.id)
        for crit in rubric.criteria:
            if crit.grader == "code":
                continue
            # Before the target has answered, size the reply from past replies of this target.
            user = judge_user_message(case, reply or "x" * (expected_output(store, "run: target", cfg.model) * 4
                                                             if cfg.adapter == "prompt" else 2000), crit)
            first_user = first_user or judge_user_message(case, reply or "(the target's reply will go here)", crit)
            for attempt in range(1, attempts + 1):
                calls += 1
                name = "grade" if attempt == 1 else f"grade_attempt_{attempt}"
                if reply is not None and _cached(store, judge.provider, judge.model, system,
                                                 [{"role": "user", "content": user}], judge_schema(crit.scale),
                                                 name, judge.max_tokens, judge.effort):
                    cached += 1
                    continue
                in_tok += tokens(system + user)
                out_tok += out
    e.add("Judge" + (" (graded twice)" if double else ""), judge.model, calls, in_tok, out_tok, cached)
    if first_user:
        e.preview = f"--- judge system prompt ---\n{_short(system, 400)}\n--- first judge message ---\n{_short(first_user, 1500)}"
    return e
