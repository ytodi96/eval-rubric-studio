"""The `evalstudio` command. Run `evalstudio --help` to see every command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from . import __version__
from .bundle import BundleError, export_bundle, import_bundle
from .config import ConfigError, load_settings
from .llm import KEY_NAMES, has_price
from .models import EvalSet, RubricSet, Target, UseCaseCard
from .store import NotFound, Store

app = typer.Typer(help="Eval & Rubric Studio: measure and improve the output quality of any AI use case.",
                  no_args_is_help=True, add_completion=False)

KINDS = ("usecase", "evalset", "rubrics", "target")


def _store() -> Store:
    return Store(load_settings().db_path)


def _fail(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(1)


def _project_from(ref: str, store: Store):
    """`ref` is a project slug, or a bundle folder. A folder is imported and approved first: its
    files are the reviewed source (e.g. `evals/` in another repo, run in CI)."""
    path = Path(ref)
    if path.is_dir():
        return import_bundle(store, path, approve=True)
    return store.get_project(ref)


@app.callback()
def main(fresh: Annotated[bool, typer.Option("--fresh", help="Ignore saved results and call the models again")] = False) -> None:
    """Eval & Rubric Studio: measure and improve the output quality of any AI use case."""
    from .llm import set_fresh

    set_fresh(fresh)


def _print_estimate(est, cap: float | None = None) -> None:
    typer.echo(est.render())
    if cap is not None and est.cost_usd > cap:
        typer.secho(f"Above the ${cap:.2f} spending cap.", fg="yellow")


@app.command()
def init(slug: str, title: Annotated[str, typer.Option(help="Readable name")] = "",
         description: str = "") -> None:
    """Create an empty project."""
    try:
        p = _store().create_project(slug, title or slug, description)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Created project '{p.slug}'. Next: `evalstudio intake {p.slug}` (Milestone 2).")


@app.command("import")
def import_(folder: Path, approve: Annotated[bool, typer.Option(help="Mark imported versions approved")] = False) -> None:
    """Import a project bundle folder (YAML/JSON files). Each file becomes a new version."""
    try:
        p = import_bundle(_store(), folder, approve=approve)
    except (BundleError, FileNotFoundError) as e:
        _fail(str(e))
    typer.echo(f"Imported '{p.slug}'. See it with `evalstudio show {p.slug}`.")


@app.command()
def export(slug: str, folder: Path,
           format: Annotated[str, typer.Option("--format", help="yaml or json")] = "yaml") -> None:
    """Export the latest versions of a project to a bundle folder (e.g. evals/ in another repo)."""
    if format not in ("yaml", "json"):
        _fail("--format must be yaml or json")
    try:
        files = export_bundle(_store(), slug, folder, fmt=format)
    except NotFound as e:
        _fail(str(e))
    for f in files:
        typer.echo(f"  wrote {f}")


@app.command("list")
def list_() -> None:
    """List projects."""
    projects = _store().list_projects()
    if not projects:
        typer.echo("No projects yet. Try: evalstudio import examples/support-email")
    for p in projects:
        typer.echo(f"{p.slug:24} {p.title}")


@app.command()
def show(slug: str) -> None:
    """Summarise a project: card, eval set coverage, rubrics, target and versions."""
    store = _store()
    try:
        p = store.get_project(slug)
    except NotFound as e:
        _fail(str(e))
    typer.secho(f"{p.title}  ({p.slug})", bold=True)
    for kind in KINDS:
        versions = store.list_versions(p.id, kind)
        if not versions:
            typer.echo(f"\n{kind}: none yet")
            continue
        v = versions[-1]
        typer.secho(f"\n{kind}: v{v.version} ({v.status}), {len(versions)} version(s)", bold=True)
        obj = store.load(p.id, kind, v.version)
        if isinstance(obj, UseCaseCard):
            typer.echo(f"  goal: {obj.goal}")
            typer.echo(f"  dimensions: {', '.join(d.key for d in obj.dimensions)}")
            typer.echo(f"  categories: {', '.join(obj.categories)}")
            typer.echo(f"  rules: {len(obj.must)} must, {len(obj.must_not)} must-not")
        elif isinstance(obj, EvalSet):
            typer.echo(f"  {len(obj.cases)} test cases")
            _print_coverage(obj)
        elif isinstance(obj, RubricSet):
            graders = [c.grader for r in obj.rubrics for c in r.criteria]
            typer.echo(f"  {len(obj.rubrics)} rubrics, {len(graders)} criteria ({graders.count('code')} code check, "
                       f"{graders.count('llm')} LLM judge, {graders.count('code+llm')} both)")
        elif isinstance(obj, Target):
            extra = f", {obj.config.provider} / {obj.config.model}" if obj.config.adapter == "prompt" else ""
            typer.echo(f"  {obj.name}: {obj.config.adapter} adapter{extra}")


def _print_coverage(es: EvalSet) -> None:
    cols = {"typical": "typical", "edge": "edge", "adversarial": "advers.", "out_of_scope": "out-of-sc",
            "easy": "easy", "medium": "medium", "hard": "hard"}
    matrix = es.coverage()
    width = max([len(c) for c in matrix] + [8])
    typer.echo("  " + "category".ljust(width) + "".join(label.rjust(10) for label in cols.values()))
    for cat, row in sorted(matrix.items()):
        typer.echo("  " + cat.ljust(width) + "".join(str(row.get(c, "·")).rjust(10) for c in cols))


@app.command()
def approve(slug: str, kind: Annotated[str, typer.Argument(help="usecase, evalset, rubrics or target")],
            version: Annotated[int | None, typer.Option(help="Default: the latest")] = None) -> None:
    """Approve a version (the latest by default). Runs only use approved versions."""
    if kind not in KINDS:
        _fail(f"kind must be one of: {', '.join(KINDS)}")
    store = _store()
    try:
        p = store.get_project(slug)
        v = store.approve(p.id, kind, version or store.get_version(p.id, kind).version)
    except NotFound as e:
        _fail(str(e))
    typer.echo(f"Approved {kind} v{v.version} for '{slug}'.")


@app.command()
def serve(port: int = 8000, host: str = "127.0.0.1") -> None:
    """Start the web app (http://localhost:8000)."""
    import uvicorn

    typer.echo(f"Open http://localhost:{port}  (Ctrl+C to stop)")
    uvicorn.run("evalstudio.web.app:app", host=host, port=port)


@app.command()
def doctor() -> None:
    """Check the setup: API key, data folder and models."""
    try:
        s = load_settings()
    except ConfigError as e:
        _fail(f"Problem in .env: {e}")
    typer.echo(f"evalstudio {__version__}")
    typer.echo(f"data folder: {s.data_dir}")
    typer.echo(f"provider:    {s.provider}")
    for role, rm in s.roles.items():
        price = "" if has_price(rm.model) else "  (no price on file: costs will show as $0)"
        typer.echo(f"{role + ':':12} {rm}{price}")
    typer.echo(f"spend cap:   ${s.spend_cap_usd:.2f} per run")
    typer.echo(f"pass mark:   {s.pass_mark:.0%}")
    ok = True
    for provider in sorted({rm.provider for rm in s.roles.values()}):
        key = KEY_NAMES[provider]
        if s.has_key(provider):
            typer.secho(f"{key}: found", fg="green")  # never print the key itself
        else:
            ok = False
            typer.secho(f"{key}: missing. Copy .env.example to .env and add it.", fg="yellow")
    if not ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------- arriving in later milestones

def _later(milestone: int, what: str) -> None:
    typer.secho(f"{what} arrives in Milestone {milestone}.", fg="yellow")
    raise typer.Exit(2)


def _read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8-sig") if path else ""


def _get_or_create(store: Store, slug: str):
    try:
        return store.get_project(slug)
    except NotFound:
        p = store.create_project(slug, slug.replace("-", " ").capitalize())
        typer.echo(f"Created project '{slug}'.")
        return p


@app.command()
def intake(
    slug: str,
    brief: Annotated[Path, typer.Option(help="Free-text description of the use case (.md or .txt)", exists=True)],
    system_prompt: Annotated[Path | None, typer.Option(help="The target's system prompt", exists=True)] = None,
    samples: Annotated[Path | None, typer.Option(help="Sample inputs and outputs", exists=True)] = None,
    docs: Annotated[Path | None, typer.Option(help="Docs or knowledge base", exists=True)] = None,
    cases: Annotated[Path | None, typer.Option(help="Existing test cases", exists=True)] = None,
    answers: Annotated[Path | None, typer.Option(help="YAML list of answers, in question order (skips the prompts)",
                                                exists=True)] = None,
    dry_run: Annotated[bool, typer.Option(help="Show the calls and estimated cost; call nothing")] = False,
) -> None:
    """Ask clarifying questions about the use case, then draft the use-case card."""
    from . import prompts
    from .estimate import estimate_calls
    from .intake import Answer, Materials, ask_questions, write_card
    from .llm import LLMError

    settings, store = load_settings(), _store()
    materials = Materials(brief=_read(brief), system_prompt=_read(system_prompt), samples=_read(samples),
                          docs=_read(docs), existing_cases=_read(cases))
    if dry_run:
        est = estimate_calls(store, settings.generator, "intake: questions", prompts.load("intake_questions"),
                             [materials.render()])
        card = estimate_calls(store, settings.generator, "intake: use-case card", prompts.load("intake_card"),
                              [materials.render()])
        est.lines += card.lines
        est.calls, est.cost_usd = est.calls + card.calls, est.cost_usd + card.cost_usd
        _print_estimate(est)
        return
    project = _get_or_create(store, slug)
    try:
        qs, cost = ask_questions(materials, settings.generator, store, project.id)
        typer.secho("\nMy understanding:", bold=True)
        typer.echo(qs.understanding)
        given = yaml.safe_load(_read(answers)) or [] if answers else None
        collected = []
        for n, q in enumerate(qs.questions, 1):
            typer.secho(f"\n{n}. {q.question}", bold=True)
            typer.echo(f"   (why: {q.why})")
            for k, option in enumerate(q.options, 1):
                typer.echo(f"   {k}) {option}")
            if given is not None:
                reply = str(given[n - 1]) if n <= len(given) else ""
                typer.echo(f"   > {reply}")
            else:
                reply = typer.prompt("   Your answer (a number picks an option; Enter to skip)", default="",
                                     show_default=False)
            if reply.strip().isdigit() and 1 <= int(reply) <= len(q.options):
                reply = q.options[int(reply) - 1]
            collected.append(Answer(q.question, reply))
        typer.echo("\nWriting the use-case card...")
        result = write_card(materials, collected, settings.generator, store, project.id)
    except LLMError as e:
        _fail(str(e))
    v = store.save_version(project.id, "usecase", result.card, note="drafted by intake")
    for w in result.warnings:
        typer.secho(f"  note: {w}", fg="yellow")
    typer.echo(f"\nSaved use-case card v{v.version} (draft). Cost: ${cost + result.cost_usd:.3f}.")
    typer.echo(f"Review and edit it at http://localhost:8000/p/{slug}, then `evalstudio approve {slug} usecase`.")


@app.command()
def generate(slug: str, n: Annotated[int, typer.Option("-n", help="Total test cases wanted, including existing ones")] = 30,
             replace: Annotated[bool, typer.Option(help="Discard existing cases instead of topping up")] = False,
             dry_run: Annotated[bool, typer.Option(help="Show the calls and estimated cost; call nothing")] = False) -> None:
    """Generate test cases from the approved use-case card, filling the coverage matrix up to N."""
    from .estimate import estimate_calls
    from .generate import dry_run_messages, generate_for_project, near_duplicate_pairs
    from .llm import LLMError

    settings, store = load_settings(), _store()
    try:
        p = store.get_project(slug)
        if dry_run:
            system, users = dry_run_messages(store, p.id, n, replace)
            _print_estimate(estimate_calls(store, settings.generator, "generate: test cases", system, users))
            return
        typer.echo(f"Generating with {settings.generator}...")
        result, version = generate_for_project(store, p.id, n, settings.generator, replace=replace)
    except (NotFound, ValueError, LLMError) as e:
        _fail(str(e))
    for w in result.warnings:
        typer.secho(f"  note: {w}", fg="yellow")
    evalset = store.load(p.id, "evalset", version)
    typer.echo(f"Added {len(result.cases)} cases: eval set v{version} now has {len(evalset.cases)}. "
               f"Cost: ${result.cost_usd:.3f}.")
    _print_coverage(evalset)
    for a, b, s in near_duplicate_pairs(evalset):
        typer.secho(f"  possible near-duplicates: {a} and {b} (similarity {s})", fg="yellow")
    typer.echo(f"Review at http://localhost:8000/p/{slug}, then `evalstudio approve {slug} evalset`.")


@app.command("import-cases")
def import_cases_cmd(slug: str, file: Annotated[Path, typer.Argument(exists=True, help=".md, .csv, .json or .yaml")]) -> None:
    """Add existing test cases from a file to a project's eval set."""
    from .importers import CaseImportError, import_cases
    from .models import EvalSet as ES

    store = _store()
    project = _get_or_create(store, slug)
    try:
        cases, warnings = import_cases(file)
    except CaseImportError as e:
        _fail(str(e))
    try:
        existing = store.load(project.id, "evalset").cases
    except NotFound:
        existing = []
    taken = {c.id for c in existing}
    for c in cases:
        base, k = c.id, 2
        while c.id in taken:
            c.id, k = f"{base}-{k}", k + 1
        taken.add(c.id)
    v = store.save_edit(project.id, "evalset", ES(cases=existing + cases), note=f"imported {len(cases)} from {file.name}")
    for w in warnings:
        typer.secho(f"  note: {w}", fg="yellow")
    typer.echo(f"Imported {len(cases)} cases into eval set v{v.version} ({len(existing) + len(cases)} in total).")


@app.command()
def rubrics(slug: str, regenerate: Annotated[bool, typer.Option(help="Rewrite all rubrics, not just missing ones")] = False,
            common_only: Annotated[bool, typer.Option(help="Only rewrite the common criteria shared by every rubric")] = False,
            dry_run: Annotated[bool, typer.Option(help="Show the calls and estimated cost; call nothing")] = False) -> None:
    """Write a rubric for each test case in the approved eval set that doesn't have one yet."""
    from .estimate import Estimate, estimate_calls
    from .llm import LLMError
    from .rubrics import dry_run_messages, generate_rubrics, refresh_common

    settings, store = load_settings(), _store()
    try:
        p = store.get_project(slug)
        if dry_run:
            total = Estimate()
            plan = dry_run_messages(store, p.id, regenerate)
            if common_only:
                plan = [x for x in plan if x[0] == "rubrics: common criteria"] or plan[:0]
            for purpose, system, users in plan:
                e = estimate_calls(store, settings.generator, purpose, system, users)
                total.lines += e.lines
                total.calls, total.cost_usd = total.calls + e.calls, total.cost_usd + e.cost_usd
                total.preview = total.preview or e.preview
            if not plan:
                total.lines.append("Nothing to write: every test case has a rubric.")
            _print_estimate(total)
            return
        typer.echo(f"Writing {'common criteria' if common_only else 'rubrics'} with {settings.generator}...")
        if common_only:
            result, version = refresh_common(store, p.id, settings.generator)
        else:
            result, version = generate_rubrics(store, p.id, settings.generator, regenerate=regenerate)
    except (NotFound, ValueError, LLMError) as e:
        _fail(str(e))
    for w in result.warnings:
        typer.secho(f"  note: {w}", fg="yellow")
    rs = store.load(p.id, "rubrics", version)
    graders = [c.grader for r in rs.rubrics for c in r.criteria]
    typer.echo(f"Wrote {len(result.rubrics)} rubrics: rubric set v{version} now covers {len(rs.rubrics)} cases, "
               f"{len(graders)} criteria ({graders.count('code')} code, {graders.count('llm')} LLM judge, "
               f"{graders.count('code+llm')} both). Cost: ${result.cost_usd:.3f}.")
    typer.echo(f"Review at http://localhost:8000/p/{slug}, then `evalstudio approve {slug} rubrics`.")


@app.command()
def check(slug: str, case_id: str, response: Annotated[Path, typer.Argument(exists=True, help="A text file with a response")]) -> None:
    """Run a test case's code checks on a response, to try a rubric out (no AI, no cost)."""
    from .checks import run_check

    store = _store()
    try:
        p = store.get_project(slug)
        rubric = store.load(p.id, "rubrics").for_case(case_id)
        case = next((c for c in store.load(p.id, "evalset").cases if c.id == case_id), None)
    except NotFound as e:
        _fail(str(e))
    if rubric is None:
        _fail(f"No rubric for '{case_id}'.")
    text = response.read_text(encoding="utf-8-sig")
    for c in rubric.criteria:
        if c.check is None:
            typer.echo(f"  ·    {c.id}: LLM judge (graded in a run)")
            continue
        r = run_check(c.check, text, case)
        typer.secho(f"  {'PASS' if r.passed else 'FAIL'} {c.id}: {r.detail}", fg="green" if r.passed else "red")


@app.command()
def run(
    project: Annotated[str, typer.Argument(help="Project slug or bundle folder")],
    dry_run: Annotated[bool, typer.Option(help="Show the calls and estimated cost; call nothing")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation (for CI)")] = False,
    case: Annotated[list[str] | None, typer.Option("--case", help="Run only this test case (repeatable)")] = None,
    limit: Annotated[int | None, typer.Option(help="Run only the first N test cases")] = None,
    double: Annotated[bool, typer.Option(help="Grade every judged criterion twice and flag disagreements")] = False,
    max_cost: Annotated[float | None, typer.Option(help="Spending cap for this run, in USD (default from .env)")] = None,
    min_pass_rate: Annotated[float | None, typer.Option(help="Exit with an error if the pass rate is below this (0-1), for CI")] = None,
) -> None:
    """Run the target on the eval set, grade every response and print the scores. Use in CI."""
    from .runner import RunError, prepare, run_project

    settings, store = load_settings(), _store()
    try:
        p = _project_from(project, store)
        _versions, cases, _rubrics, target, est = prepare(store, p.id, settings, double, limit, case)
    except (NotFound, BundleError, RunError) as e:
        _fail(str(e))
    cap = max_cost if max_cost is not None else settings.spend_cap_usd
    typer.echo(f"{len(cases)} test case(s) · target: {target.name} ({target.config.adapter}) · judge: {settings.judge}")
    _print_estimate(est, cap)
    if dry_run:
        return
    if not yes and not typer.confirm(f"Run it (cap ${cap:.2f})?", default=False):
        raise typer.Exit(0)

    def progress(o, done, total):
        mark = "ERROR" if o.error else ("PASS " if o.passed else "FAIL ")
        extra = f"  ({o.error})" if o.error else (f"  must-pass failed: {', '.join(o.failed_musts)}" if o.failed_musts else "")
        typer.secho(f"  [{done:>2}/{total}] {mark} {o.score:5.0%}  {o.case_id}{extra}",
                    fg="red" if o.error or not o.passed else "green")

    try:
        outcome = run_project(store, p.id, settings, double=double, limit=limit, only=case, max_cost=max_cost,
                              progress=progress)
    except RunError as e:
        _fail(str(e))
    s = outcome.summary
    r = store.get_run(outcome.run_id)
    typer.secho(f"\nRun {outcome.run_id}: score {s['score']:.0%}, pass rate {s['pass_rate']:.0%} "
                f"({s['graded']}/{s['cases']} graded, {s['errors']} errors). Cost ${r.actual_cost_usd:.3f} "
                f"(estimated ${r.estimated_cost_usd:.3f}).", bold=True)
    for cat, g in s["by_category"].items():
        typer.echo(f"  {cat:32} score {g['score']:4.0%}  pass {g['pass_rate']:4.0%}  ({g['cases']})")
    if s["disagreements"]:
        typer.secho(f"  Judge disagreed with itself on {len(s['disagreements'])} case(s).", fg="yellow")
    if outcome.stopped_reason:
        typer.secho(f"Stopped early: {outcome.stopped_reason}.", fg="red")
        raise typer.Exit(1)
    if min_pass_rate is not None and s["pass_rate"] < min_pass_rate:
        typer.secho(f"Pass rate {s['pass_rate']:.0%} is below the required {min_pass_rate:.0%}.", fg="red")
        raise typer.Exit(1)


@app.command()
def costs(slug: Annotated[str | None, typer.Argument(help="Only this project")] = None) -> None:
    """Show AI spend by day and purpose (also in data/cost-log.csv)."""
    store = _store()
    try:
        project_id = store.get_project(slug).id if slug else None
    except NotFound as e:
        _fail(str(e))
    rows = store.costs(project_id)
    if not rows:
        typer.echo("No AI calls recorded yet.")
        return
    typer.echo(f"{'day':10}  {'purpose':38} {'model':16} {'calls':>5} {'in tok':>9} {'out tok':>8} {'cost':>8}")
    for r in rows:
        typer.echo(f"{r['day']:10}  {r['purpose'][:38]:38} {r['model'][:16]:16} {r['calls']:>5} "
                   f"{r['input_tokens'] + r['cached_tokens']:>9,} {r['output_tokens']:>8,} ${r['cost_usd']:>7.3f}")
    typer.secho(f"Total: ${sum(r['cost_usd'] for r in rows):.3f}", bold=True)


@app.command()
def report(slug: str) -> None:
    """Show the latest report for a project."""
    _later(5, "Reports")


if __name__ == "__main__":
    app()
