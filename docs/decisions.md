# Design decisions

Each entry: what was decided, why, and what was given up. Newest at the bottom.

## 1. Python + FastAPI + SQLite + server-rendered pages

**Decision.** Python 3.12+, FastAPI for the web app, SQLite (Python's built-in `sqlite3`) for
storage, Pydantic for the data model, Jinja2 templates with HTMX and Chart.js loaded from a CDN
for the UI, Typer for the CLI.

**Why.** It matches the Lemon Bowl Sales Copilot, the first project it will grade, so its
`python` adapter can call the copilot directly. One `pip install` and one database file; no
JavaScript build step, so the UI code stays short and readable.

**Trade-off.** A server-rendered UI is less slick than a single-page app. That's fine for a
tool whose job is to show tables, text side by side and a chart.

## 2. The database is the source of truth; YAML bundles are for moving projects around

**Decision.** Everything lives in one SQLite file in `DATA_DIR`. A project can be exported to,
and imported from, a folder of YAML (or JSON) files: `project`, `usecase`, `evalset`, `rubrics`,
`target`.

**Why.** Runs, grades and human labels are many small linked rows, which a database handles
well. Eval sets and rubrics also need to live in other repos (e.g. `evals/` in the Sales Copilot)
and be reviewable in a pull request, which plain-text files handle well. `evalstudio run <folder>`
imports a bundle and runs it, so CI never needs the database ahead of time.

**Trade-off.** Two formats to keep in step. A round-trip test (export → import → identical)
guards this.

## 3. Versions are append-only snapshots

**Decision.** The use-case card, eval set, rubric set and target are each saved as a whole
snapshot with a version number. Approved versions never change: editing one makes a new draft
version (see 8 for how drafts are edited). A run
stores the four version numbers it used. Runs only use *approved* versions.

**Why.** "Did my prompt change help?" is only answerable if everything else stayed the same.
Snapshots make every run reproducible and every comparison fair, and are simple to reason about.

**Trade-off.** Some duplication in storage (an eval set of 30 cases is a few tens of KB per
version), which is irrelevant at this scale.

## 4. Strict data model with clear errors

**Decision.** Every object is a Pydantic model that rejects unknown fields. Bundle import errors
name the file and the exact field (`evalset.yaml: cases > 0 > category: Field required`).

**Why.** Eval sets and rubrics will be edited by hand in YAML. A typo like `expected:` instead of
`expected_behaviours:` must fail loudly, not silently drop a rule.

## 5. Code checks are pass/fail, and are used wherever possible

**Decision.** A criterion is graded by code if it has a `check` (contains / doesn't contain,
regex, JSON schema, number within tolerance, length, custom Python function); otherwise by the
LLM judge. Code checks are pass/fail only. A criterion can set `also_judge: true` to be graded by
both; it passes only if both pass.

Regex checks run in multiline mode (`^` and `$` match on every line); `\A` and `\Z` anchor to the
start and end of the whole response.

**Why.** Code is free, instant and never disagrees with itself. The LLM judge is kept for what
really needs judgement (tone, completeness, policy reasoning). For forbidden content ("no
discount"), a word list alone misses paraphrases ("15 percent off", "store credit"), so the word
list catches the obvious cases cheaply and the judge catches the rest.

## 6. Provider and model per role; OpenAI by default

*(Updated in Milestone 4: generator and improver now default to the cheaper `gpt-6.1-terra`; see 10.)*

**Decision.** The Studio talks to models through one function (`llm.complete`) that supports
OpenAI (Responses API) and Anthropic (Messages API). `EVALSTUDIO_PROVIDER` picks the default
(`openai`); each of the three roles (generator, judge, improver) can override its provider, model
and reasoning effort in `.env`. Structured output uses each provider's strict JSON-schema mode,
so the judge's score, reasoning and evidence always arrive in the expected shape.

| Role | Default (OpenAI) | Why |
|---|---|---|
| Generator | `gpt-6.1-sol`, medium effort | Strong at following a long brief and producing varied, well-formed cases, at $2 / $10 per 1M tokens |
| Judge | `gpt-6.1-sol`, medium effort | Clearly stronger than the demo target; `gpt-6-astra` is stronger still but 5x the price |
| Improver | `gpt-6.1-sol`, medium effort | Same reasoning as the generator |
| Demo target | `gpt-6-luna` | Cheap ($0.10 / $0.50) and leaves visible room for the improve loop |

**Why OpenAI first.** It's the key Yash already has, and the Sales Copilot runs on OpenAI too.
Anthropic stays a first-class option, and the defaults switch to Claude Opus 5.5 when
`EVALSTUDIO_PROVIDER=anthropic`.

**Known risk: same-family judging.** With OpenAI as the default, the judge and the system under
test will often come from the same provider, and sometimes be the same model (the Sales Copilot
also uses `gpt-6.1-sol`). A model can rate its own habits too kindly. Mitigations: the judge must
quote evidence; code checks don't depend on any model; the calibration step measures agreement
with hand grades; and `EVALSTUDIO_JUDGE_PROVIDER` / `EVALSTUDIO_JUDGE_MODEL` let a different
provider or a stronger model (e.g. `claude-opus-5-5` or `gpt-6-astra`) judge without changing
anything else. The README will report which judge produced each published number.

Prices live in `llm.PRICES` (checked 2026-10-29) and drive the cost estimate and spending cap. A
model with no price on file shows $0, and `evalstudio doctor` warns about it.

## 7. Coverage is planned by code; the model writes the cases

**Decision.** Before generating, code splits the requested total across the card's categories
(evenly), types (50% typical, 20% edge, 20% adversarial, 10% out-of-scope) and difficulties (30%
easy, 40% medium, 30% hard), counting existing cases, and assigns each new case a slot that
spreads types and difficulties across categories. The model writes one case per slot, in batches
of 10, seeing the cases written so far. Near-duplicates (more than half their 3-word phrases
shared with another case) are retried once, then dropped with a warning.

**Why.** Asking a model for "30 varied cases" gives clusters of similar typical cases and few
hard or adversarial ones. A coverage plan in code makes the mix predictable and testable, and the
coverage matrix shows exactly what was asked for. The phrase-overlap check needs no extra model
or embeddings service.

**Trade-off.** Phrase overlap catches reworded copies, not two different messages that test the
same rule. The matrix and the human review catch those.

## 8. Drafts are edited in place; approved versions are frozen

**Decision.** Editing a draft (card or eval set) updates that draft. Editing an approved version
starts a new draft version. Generation always works from the latest *approved* card.

**Why.** A review session can involve dozens of small edits; a new version for each would bury the
meaningful ones. What matters for reproducibility is that anything a run or a generation used
(approved versions) never changes.

## 9. Rubrics: common criteria once, specific criteria per case, checks tested on known-good answers

**Decision.** Rubrics are written in two steps. First, 2-6 *common* criteria that apply to every
response (mostly format: length, sign-off), written once and copied into every rubric. Then 3-7
criteria specific to each test case, written 5 cases per call, from the case's expected
behaviours, the card and the target's policy. Every code check the model proposes is run on the
case's reference answer; a check that fails a known-good answer is wrong, so that criterion is
graded by the LLM judge instead.

The model's check definitions are repaired where the intent is clear (a regex placed in the word
list field, a regex written as a JSON-schema "string pattern", fields the check type doesn't use)
and otherwise fall back to the judge, with a note. `evalstudio rubrics --common-only` rewrites
just the common criteria and swaps them into every rubric that uses them.

**Why.** Writing the common criteria once keeps format grading identical across cases and costs
one call instead of thirty. Testing checks against reference answers catches over-strict code
checks before they produce false failures in a real run. In the first live run, both repairs were
needed: the model put patterns in the wrong field and, after that was fixed, wrote them as JSON
schemas; the reference-answer test caught the second problem automatically.

**Trade-off.** Most case-specific criteria are judged by the LLM, because they test behaviour
("explains that exactly 3 days doesn't qualify") rather than exact text. That's the honest split,
and it is what makes grading cost money; the judge's agreement with hand grading is measured in
Milestone 5.

## 10. Cost-aware by design

**Decision.** The Studio treats API spend as something to design for, following a written cost
policy: an estimate before every paid action (`--dry-run`, and "estimate only" in the web app),
a $2 default cap per run, a result cache keyed by everything that affects a result, a canary
case before a batch and fail-fast after 3 errors in a row, the strong model only for the judge,
an output-token cap on every call, and a cost log with a `costs` command.

**Why.** An eval tool that is expensive to run doesn't get run. The cache matters most: the
improve loop changes one prompt and re-runs, and only the cases whose inputs changed are paid
for again. Estimates use the real prompt text and the average output of past calls, so they get
more accurate with use.

**Not yet:** the OpenAI Batch API (about half price, results within 24 hours) would suit
non-urgent full runs; it's a candidate for a later milestone.

## 11. Grading: code checks, then the judge one criterion at a time, with checked evidence

**Decision.** Code checks run first. The judge then grades one criterion per call, with a strict
schema: evidence (a quote from the response, or null), short reasoning, and a score limited to
the criterion's scale. The code verifies that each quote really appears in the response and
marks the grade when it doesn't. The test case and response come first in the judge's message
and the criterion last, so the shared start is billed at the provider's cached rate. Optional
double grading (`--double`) makes a second, independent call (never answered from the cache)
and flags criteria where the two scores differ.

**Why.** One criterion per call keeps each judgement focused and makes disagreements traceable
to a single criterion. Verified quotes make every grade checkable by a person in seconds.

## 12. The demo is self-contained and fictional

**Decision.** The public demo is a support-email assistant for *Brightside Coffee Co.*, an
invented company, with its own policy and system prompt. Prompt v1 is a reasonable first draft
that deliberately leaves some rules from the use-case card out (escalation, word limit,
embedded-instruction handling).

**Why.** Anyone can run the full loop with only an API key, no private data is involved, and the
improve loop has real, explainable gaps to find and fix.

## 13. Python environment outside OneDrive (Windows helper scripts)

**Decision.** `setup.bat` creates the virtual environment in `%USERPROFILE%\.venvs\eval-studio`.
The README also gives the standard `python -m venv .venv` steps for other systems.

**Why.** The project folder is synced by OneDrive; syncing thousands of package files is slow and
can lock files mid-install. Same approach as the Sales Copilot.
