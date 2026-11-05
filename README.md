# Eval & Rubric Studio

**Measure and improve the output quality of any AI use case.** Describe the use case; the Studio
builds the test cases and grading rubrics, runs your AI system, grades every answer with code
checks and an LLM judge, shows where it fails, suggests prompt fixes, and tracks quality across
versions.

> **Status: Milestone 4 of 7.** Intake, eval-set generation with a coverage matrix, import,
> rubric generation, the four target adapters, grading (code checks + an LLM judge with
> verified evidence), scoring and cost controls work end to end. Reports, calibration and the
> improve loop arrive in the next milestones; see [What I'd do next](#what-id-do-next).

## The problem

Teams ship AI features (agents, copilots, prompts) without a reliable way to tell whether the
outputs are good, or whether a prompt change made things better or worse. Writing good test
cases and grading rubrics by hand is slow, so it usually doesn't happen, and quality is judged
by reading a handful of answers and going with a feeling.

## What it does

Given any use case (a sales copilot, a trip planner, a support-email writer, a data extractor):

1. **Understands the use case.** Asks up to ~5 clarifying questions and writes a *use-case card*:
   goal, users, inputs, output format, the quality dimensions that matter, known failure modes
   and hard must / must-not rules. You edit and approve it.
2. **Builds an eval set.** ~30 varied test cases across a coverage matrix (category × difficulty
   × typical / edge / adversarial / out-of-scope), with near-duplicates removed. Or import your own.
3. **Writes a rubric per test case.** Weighted criteria, pass/fail or 1–4 with written anchors,
   must-pass flags, and a code check wherever one is possible.
4. **Runs your system.** A system prompt + model (OpenAI or Claude), an HTTP endpoint, a Python function in another
   project, or a file of already-collected answers. Shows the estimated cost first, with a cap.
5. **Grades every answer.** Code checks first, then an LLM judge, one criterion at a time, which
   must quote the answer as evidence.
6. **Reports.** Scores by criterion, category and difficulty, failure clusters, the 5 worst
   answers side by side with their rubric and the judge's reasoning, and what changed since the
   last run.
7. **Improves.** Suggests a specific prompt (or rubric) change per failure cluster; accept it,
   re-run, see the difference, and follow the score across versions.
8. **Calibrates the judge.** You grade a sample blind; it measures judge-vs-you agreement per
   criterion (Cohen's kappa) and flags criteria whose wording needs work.

## How it works

```
Brief ──► Use-case card ──► Eval set ──► Rubrics ──► Run target ──► Grade ──► Report ──► Improve
            (approve)       (approve)    (approve)      │                                   │
                                                        └──────── new target version ◄──────┘
```

Every artefact (card, eval set, rubrics, target) is **versioned**: once you approve a version it
never changes (editing it starts a new draft), and every run records exactly which versions it used. A whole
project can be exported as a folder of YAML files and kept in another repo (for example
`evals/` in the project being tested), then run in CI with `evalstudio run evals/`.

More detail: [docs/architecture.md](docs/architecture.md).

## Design decisions

The main choices and why are in [docs/decisions.md](docs/decisions.md). In short:

- **Python, FastAPI, SQLite, server-rendered pages.** One `pip install`, no JavaScript build,
  one database file.
- **Code checks before the LLM judge.** Anything a regex, schema or number check can decide is
  decided by code: cheaper, exact and repeatable.
- **The judge must show evidence.** Every LLM grade comes with a quote from the answer, or an
  explicit "no evidence found".
- **The judge is at least as strong as the system it grades**, and can run on a different
  provider. OpenAI by default, Anthropic optional; the model for each role is configurable.
- **Approved versions are frozen**, so any past result can be reproduced and compared.

## Evaluation

How do we know the Studio itself grades correctly? Two checks, with results to be filled in from
real runs:

| Check | What it measures | Result |
|---|---|---|
| Seeded-defect test | Inject known defects (wrong number, missing step, broken format, rule violation) into good answers; % caught by the grader | *Milestone 5* |
| Judge vs human | Agreement and Cohen's kappa between the LLM judge and hand grades, per criterion | *Milestone 5* |
| Demo baseline | Support-email demo, prompt v1 on `gpt-6-luna`, judged by `gpt-6.1-sol` | Score 65%, pass rate 3% (1/30). Main failures: wrong sign-off (28 cases), under 60 words (14), missing team-lead handovers |
| Demo before/after | Support-email demo score for prompt v1 vs the improved prompt | *Milestone 6* |
| Sales Copilot | Pass rate of the Lemon Bowl Sales Copilot on its 17 test cases (sample data) | *Milestone 7* |

## Run it yourself

You need Python 3.12+ and an API key for **OpenAI** (the default) or **Anthropic**. Set
`EVALSTUDIO_PROVIDER` in `.env`; each role (generator, judge, improver) can also use a
different provider, e.g. a Claude judge for an OpenAI system.

| Role | Default model (OpenAI) | Price per 1M tokens (in / out) | Default model (Anthropic) |
|---|---|---|---|
| Generator: questions, card, test cases, rubrics | `gpt-6.1-terra` | $1 / $5 | `claude-sonnet-5-5` |
| Judge | `gpt-6.1-sol` | $2 / $10 | `claude-opus-5-5` |
| Improver: prompt suggestions | `gpt-6.1-terra` | $1 / $5 | `claude-sonnet-5-5` |
| Demo system under test | `gpt-6-luna` | $0.10 / $0.50 | – |

Only the judge gets the strong model; everything the generator writes is reviewed by a person.

**What the demo costs** (measured): building the use-case card, 30 test cases and 30 rubrics
cost about $0.60 in total. A full run of the 30 cases cost **$0.47** and took under 2 minutes:
219 calls, almost all of it the judge grading 189 criteria one at a time; the `gpt-6-luna`
target costs under a cent. Re-running unchanged cases is free (cached).

### Cost controls

- **`--dry-run`** on every command that calls a model (and "estimate only" on the web buttons)
  prints the calls, estimated tokens and cost, and the first prompt, without calling anything.
- **Spending cap** (`EVALSTUDIO_SPEND_CAP_USD`, default $2): a run estimated above it is refused,
  and a run that reaches it while running stops cleanly. Override once with `--max-cost`.
- **No paying twice:** every model result is cached by everything that affects it (prompt,
  input, model, settings). Re-running after a small change only pays for what changed;
  `evalstudio --fresh ...` forces new calls.
- **Canary and fail-fast:** a run grades one case first and stops if it errors, and stops if 3
  cases in a row error.
- **`evalstudio costs`** shows spend by day and purpose; every call is also logged to
  `data/cost-log.csv` (gitignored).
- Tests use stand-in models; `pytest --live` adds the one test that makes a real call.

**Windows (double-click):** run `setup.bat`, paste your key into `.env`, then run `start.bat`
and open http://localhost:8000.

**Any OS (terminal):**

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

```bash
pip install -e ".[dev]"
```

```bash
cp .env.example .env             # then paste your OPENAI_API_KEY into .env
```

```bash
evalstudio import examples/support-email
```

```bash
evalstudio serve
```

Start your own project from the home page (**New project** → intake), or from the terminal:

```bash
evalstudio intake my-project --brief brief.md --system-prompt prompt.md
```

```bash
evalstudio approve my-project usecase
```

```bash
evalstudio generate my-project -n 30
```

```bash
evalstudio rubrics my-project
```

```bash
evalstudio run my-project --dry-run
```

```bash
evalstudio run my-project
```

In CI: `evalstudio run evals/ --yes --min-pass-rate 0.8` (imports the bundle folder, runs it and fails
the build if the pass rate drops).

Try a rubric's code checks on any reply, with no AI and no cost:
`evalstudio check my-project <test-case-id> reply.txt`.

Import existing test cases with `evalstudio import-cases my-project cases.md` (.md, .csv,
.json or .yaml). Other commands: `evalstudio --help`. Tests: `pytest`.

### Demo projects

| Folder | Use case | Target |
|---|---|---|
| [`examples/support-email`](examples/support-email) | Replies to customer support emails for *Brightside Coffee Co.*, a fictional online coffee roaster | A system prompt on `gpt-6-luna` |
| Lemon Bowl Sales Copilot | Converting catering enquiries into orders (separate repo, sample data only) | *Milestone 7* |

## What I'd do next

Build milestones:

- [x] 1. Scaffold: data model, project bundles, CLI, read-only web view, hand-written demo
- [x] 2. Intake + eval set generation, coverage matrix, review / edit / approve, import from Markdown / CSV / JSON
- [x] 3. Rubric generation and the code-check library
- [x] 4. Target adapters, cost estimate and cap, code checks + LLM judge, scoring
- [ ] 5. Report, failure clusters, run comparison, hand-grading UI, judge agreement, seeded-defect test
- [ ] 6. Improve loop, re-run diff, history chart
- [ ] 7. Dogfood on the Lemon Bowl Sales Copilot, real results in this README

## Data & privacy

- Your projects, runs and grades are stored in `DATA_DIR` (default `./data/`), which is
  gitignored. Only the demo projects in `examples/` are published.
- The demo company and every email in it are invented.
- Test inputs and your system's answers are sent to the AI provider you configure (OpenAI or
  Anthropic) for generation and judging. Don't put personal data in eval sets you don't want sent there.
- The API key lives in `.env`, which is gitignored. Never commit it.

## Licence

MIT
