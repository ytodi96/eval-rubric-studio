"""The web app: project pages, intake, editing and approving the use-case card and eval set."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import yaml
from pydantic import ValidationError

from ..config import load_settings
from ..generate import generate_for_project, near_duplicate_pairs
from ..importers import CaseImportError, import_cases
from ..intake import Answer, Materials, ask_questions, write_card
from ..library import DIMENSIONS
from ..llm import LLMError
from ..models import EvalSet, Rubric, RubricSet, TestCase, UseCaseCard
from ..rubrics import generate_rubrics, missing_rubrics
from ..store import NotFound, Store

HERE = Path(__file__).parent
app = FastAPI(title="Eval & Rubric Studio")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
store = Store(load_settings().db_path)

KINDS = ("usecase", "evalset", "rubrics", "target")
COVERAGE_COLS = ["typical", "edge", "adversarial", "out_of_scope", "easy", "medium", "hard"]


def _project(slug: str):
    try:
        return store.get_project(slug)
    except NotFound:
        raise HTTPException(404, f"No project called '{slug}'")


def _back(slug: str, msg: str = "", anchor: str = "") -> RedirectResponse:
    url = f"/p/{slug}" + (f"?msg={quote(msg)}" if msg else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, status_code=303)


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{' > '.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------- projects

@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "index.html", {"projects": store.list_projects(), "msg": msg})


@app.post("/new")
def new_project(slug: str = Form(...), title: str = Form(""), description: str = Form("")):
    try:
        store.create_project(slug.strip().lower(), title.strip() or slug, description.strip())
    except ValueError as e:
        return RedirectResponse(f"/?msg={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/p/{slug.strip().lower()}/intake", status_code=303)


@app.get("/p/{slug}", response_class=HTMLResponse)
def project_page(request: Request, slug: str, msg: str = ""):
    project = _project(slug)
    parts, versions = {}, {}
    for kind in KINDS:
        try:
            versions[kind] = store.get_version(project.id, kind)
            parts[kind] = store.load(project.id, kind)
        except NotFound:
            pass
    evalset = parts.get("evalset")
    rubric_for = {r.test_case_id: r for r in parts["rubrics"].rubrics} if "rubrics" in parts else {}
    return templates.TemplateResponse(request, "project.html", {
        "project": project, "card": parts.get("usecase"), "evalset": evalset,
        "rubric_for": rubric_for, "target": parts.get("target"), "versions": versions,
        "approved_card": store.latest_approved(project.id, "usecase"),
        "approved_evalset": store.latest_approved(project.id, "evalset"),
        "rubricset": parts.get("rubrics"),
        "missing": missing_rubrics(evalset, parts["rubrics"]) if evalset and "rubrics" in parts else
                   ([c.id for c in evalset.cases] if evalset else []),
        "coverage_cols": COVERAGE_COLS, "msg": msg,
        "duplicates": near_duplicate_pairs(evalset) if evalset else [],
        "spent": store.usage_cost(project.id),
    })


@app.post("/p/{slug}/approve/{kind}")
def approve(slug: str, kind: str):
    project = _project(slug)
    if kind not in KINDS:
        raise HTTPException(400, "unknown kind")
    v = store.approve(project.id, kind, store.get_version(project.id, kind).version)
    return _back(slug, f"Approved {kind} v{v.version}.")


# ---------------------------------------------------------------- 1. intake

@app.get("/p/{slug}/intake", response_class=HTMLResponse)
def intake_form(request: Request, slug: str):
    return templates.TemplateResponse(request, "intake.html", {"project": _project(slug), "step": "materials"})


@app.post("/p/{slug}/intake/questions", response_class=HTMLResponse)
def intake_questions(request: Request, slug: str, brief: str = Form(...), system_prompt: str = Form(""),
                     samples: str = Form(""), docs: str = Form(""), existing_cases: str = Form("")):
    project = _project(slug)
    materials = Materials(brief, system_prompt, samples, docs, existing_cases)
    ctx = {"project": project, "materials": materials}
    try:
        qs, cost = ask_questions(materials, load_settings().generator, store, project.id)
    except LLMError as e:
        return templates.TemplateResponse(request, "intake.html", ctx | {"step": "materials", "error": str(e)})
    return templates.TemplateResponse(request, "intake.html", ctx | {"step": "questions", "qs": qs, "cost": cost})


@app.post("/p/{slug}/intake/card", response_class=HTMLResponse)
async def intake_card(request: Request, slug: str):
    project = _project(slug)
    form = await request.form()
    materials = Materials(form["brief"], form.get("system_prompt", ""), form.get("samples", ""),
                          form.get("docs", ""), form.get("existing_cases", ""))
    answers = []
    for i in range(int(form.get("n_questions", 0))):
        choice, typed = form.get(f"choice_{i}", ""), form.get(f"answer_{i}", "").strip()
        answers.append(Answer(form[f"question_{i}"], " ".join(filter(None, [choice, typed]))))
    try:
        result = write_card(materials, answers, load_settings().generator, store, project.id)
    except LLMError as e:
        return templates.TemplateResponse(request, "intake.html",
                                          {"project": project, "materials": materials, "step": "materials",
                                           "error": str(e)})
    v = store.save_version(project.id, "usecase", result.card, note="drafted by intake")
    note = " ".join(result.warnings)
    return _back(slug, f"Drafted use-case card v{v.version}. Review it, edit if needed, then approve. {note}", "card")


# ---------------------------------------------------------------- the use-case card

def _card_form(card: UseCaseCard | None) -> dict:
    if card is None:
        return {}
    return {
        "title": card.title, "goal": card.goal, "users": card.users, "output_format": card.output_format,
        "input_types": "\n".join(card.input_types), "categories": "\n".join(card.categories),
        "dimensions": "\n".join(f"{d.key} | {d.weight:g} | {d.description}" for d in card.dimensions),
        "failure_modes": "\n".join(card.failure_modes), "must": "\n".join(card.must),
        "must_not": "\n".join(card.must_not), "out_of_scope": "\n".join(card.out_of_scope), "notes": card.notes,
    }


@app.get("/p/{slug}/card/edit", response_class=HTMLResponse)
def card_edit(request: Request, slug: str):
    project = _project(slug)
    try:
        card = store.load(project.id, "usecase")
    except NotFound:
        card = None
    return templates.TemplateResponse(request, "card_edit.html", {"project": project, "f": _card_form(card),
                                                                  "library": DIMENSIONS})


@app.post("/p/{slug}/card/edit", response_class=HTMLResponse)
async def card_save(request: Request, slug: str):
    project = _project(slug)
    f = dict(await request.form())
    dims = []
    for line in _lines(f.get("dimensions", "")):
        key, weight, desc = (line.split("|", 2) + ["", ""])[:3]
        key = key.strip()
        try:
            w = float(weight.strip() or 1)
        except ValueError:
            w = 0  # rejected below with a clear message
        dims.append({"key": key, "weight": w, "description": desc.strip(), "custom": key not in DIMENSIONS})
    data = {
        "title": f.get("title", ""), "goal": f.get("goal", ""), "users": f.get("users", ""),
        "output_format": f.get("output_format", ""), "input_types": _lines(f.get("input_types", "")),
        "categories": _lines(f.get("categories", "")), "dimensions": dims,
        "failure_modes": _lines(f.get("failure_modes", "")), "must": _lines(f.get("must", "")),
        "must_not": _lines(f.get("must_not", "")), "out_of_scope": _lines(f.get("out_of_scope", "")),
        "notes": f.get("notes", ""),
    }
    try:
        v = store.save_edit(project.id, "usecase", data)
    except ValidationError as e:
        return templates.TemplateResponse(request, "card_edit.html",
                                          {"project": project, "f": f, "library": DIMENSIONS, "error": _errors(e)})
    return _back(slug, f"Saved use-case card v{v.version} (draft).", "card")


# ---------------------------------------------------------------- 2. the eval set

def _evalset(project) -> EvalSet:
    try:
        return store.load(project.id, "evalset")
    except NotFound:
        return EvalSet(cases=[])


def _estimate_msg(purpose_plan) -> str:
    from ..estimate import Estimate, estimate_calls

    total = Estimate()
    for purpose, system, users in purpose_plan:
        e = estimate_calls(store, load_settings().generator, purpose, system, users)
        total.lines += e.lines
        total.calls, total.cost_usd = total.calls + e.calls, total.cost_usd + e.cost_usd
    return (f"Estimate only, nothing was called: {total.calls} call(s), about ${total.cost_usd:.2f}. "
            + " ".join(total.lines))


@app.post("/p/{slug}/generate")
def generate(slug: str, n: int = Form(30), replace: bool = Form(False), dry_run: bool = Form(False)):
    project = _project(slug)
    if dry_run:
        from ..generate import dry_run_messages as gen_plan
        try:
            system, users = gen_plan(store, project.id, n, replace)
        except ValueError as e:
            return _back(slug, str(e), "evalset")
        return _back(slug, _estimate_msg([("generate: test cases", system, users)]), "evalset")
    try:
        result, version = generate_for_project(store, project.id, n, load_settings().generator, replace=replace)
    except (ValueError, LLMError) as e:
        return _back(slug, str(e), "evalset")
    msg = f"Generated {len(result.cases)} cases into eval set v{version} (${result.cost_usd:.3f}). "
    return _back(slug, msg + " ".join(result.warnings), "evalset")


@app.post("/p/{slug}/import")
async def import_file(slug: str, file: UploadFile = File(...)):
    project = _project(slug)
    suffix = Path(file.filename or "").suffix
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(await file.read())
        try:
            cases, warnings = import_cases(path)
        except CaseImportError as e:
            return _back(slug, str(e), "evalset")
    existing = _evalset(project).cases
    taken = {c.id for c in existing}
    for c in cases:
        base, k = c.id, 2
        while c.id in taken:
            c.id, k = f"{base}-{k}", k + 1
        taken.add(c.id)
    v = store.save_edit(project.id, "evalset", EvalSet(cases=existing + cases),
                        note=f"imported {len(cases)} from {file.filename}")
    return _back(slug, f"Imported {len(cases)} cases into eval set v{v.version}. " + " ".join(warnings), "evalset")


def input_to_text(case: TestCase | None) -> str:
    if case is None:
        return ""
    if isinstance(case.input, str):
        return case.input
    return "\n\n".join(f"{t.role.upper()}: {t.content}" for t in case.input)


def text_to_input(text: str) -> str | list[dict]:
    """Plain text is one message. Lines starting USER: / ASSISTANT: make a conversation."""
    text = text.strip().replace("\r\n", "\n")
    parts = re.split(r"^(USER|ASSISTANT):[ \t]*", text, flags=re.M)
    if len(parts) < 3 or parts[0].strip():
        return text
    return [{"role": role.lower(), "content": content.strip()} for role, content in zip(parts[1::2], parts[2::2])]


def _case_form(case: TestCase | None) -> dict:
    if case is None:
        return {"type": "typical", "difficulty": "medium"}
    return {"id": case.id, "category": case.category, "type": case.type, "difficulty": case.difficulty,
            "input": input_to_text(case), "context": "\n".join(f"{k}: {v}" for k, v in case.context.items()),
            "expected_behaviours": "\n".join(case.expected_behaviours),
            "reference_answer": case.reference_answer or "", "source": case.source}


def _case_page(request, project, f, original_id, error=""):
    try:
        categories = store.load(project.id, "usecase").categories
    except NotFound:
        categories = []
    return templates.TemplateResponse(request, "case_edit.html", {
        "project": project, "f": f, "original_id": original_id, "categories": categories, "error": error})


@app.get("/p/{slug}/cases/new", response_class=HTMLResponse)
def case_new(request: Request, slug: str):
    return _case_page(request, _project(slug), _case_form(None), "")


@app.get("/p/{slug}/cases/{case_id}/edit", response_class=HTMLResponse)
def case_edit(request: Request, slug: str, case_id: str):
    project = _project(slug)
    case = next((c for c in _evalset(project).cases if c.id == case_id), None)
    if case is None:
        raise HTTPException(404, f"No test case '{case_id}'")
    return _case_page(request, project, _case_form(case), case_id)


@app.post("/p/{slug}/cases/save", response_class=HTMLResponse)
async def case_save(request: Request, slug: str):
    project = _project(slug)
    f = dict(await request.form())
    original_id = f.get("original_id", "")
    cases = _evalset(project).cases
    old = next((c for c in cases if c.id == original_id), None)
    context = {}
    for line in _lines(f.get("context", "")):
        key, _, value = line.partition(":")
        context[key.strip()] = value.strip()
    try:
        case = TestCase.model_validate({
            "id": f.get("id", "").strip(), "category": f.get("category", "").strip(), "type": f.get("type"),
            "difficulty": f.get("difficulty"), "input": text_to_input(f.get("input", "")), "context": context,
            "expected_behaviours": _lines(f.get("expected_behaviours", "")),
            "reference_answer": f.get("reference_answer", "").strip() or None,
            "source": old.source if old else "manual",
        })
        if any(c.id == case.id for c in cases if c.id != original_id):
            raise ValueError(f"Another test case already has the id '{case.id}'.")
        new_cases = [case if c.id == original_id else c for c in cases] if old else cases + [case]
        v = store.save_edit(project.id, "evalset", EvalSet(cases=new_cases))
    except ValidationError as e:
        return _case_page(request, project, f, original_id, _errors(e))
    except ValueError as e:
        return _case_page(request, project, f, original_id, str(e))
    return _back(slug, f"Saved '{case.id}' in eval set v{v.version} (draft).", f"case-{case.id}")


# ---------------------------------------------------------------- 3. rubrics

def _rubrics(project) -> RubricSet:
    try:
        return store.load(project.id, "rubrics")
    except NotFound:
        return RubricSet(rubrics=[])


@app.post("/p/{slug}/rubrics/generate")
def rubrics_generate(slug: str, regenerate: bool = Form(False), dry_run: bool = Form(False)):
    project = _project(slug)
    if dry_run:
        from ..rubrics import dry_run_messages as rubric_plan
        try:
            plan = rubric_plan(store, project.id, regenerate)
        except ValueError as e:
            return _back(slug, str(e), "rubrics")
        return _back(slug, _estimate_msg(plan) if plan else "Nothing to write: every test case has a rubric.", "rubrics")
    try:
        result, version = generate_rubrics(store, project.id, load_settings().generator, regenerate=regenerate)
    except (ValueError, LLMError) as e:
        return _back(slug, str(e), "rubrics")
    msg = f"Wrote {len(result.rubrics)} rubrics into rubric set v{version} (${result.cost_usd:.3f})."
    if result.warnings:
        msg += f" {len(result.warnings)} note(s): " + " ".join(result.warnings)
    return _back(slug, msg, "rubrics")


@app.get("/p/{slug}/rubrics/{case_id}/edit", response_class=HTMLResponse)
def rubric_edit(request: Request, slug: str, case_id: str):
    project = _project(slug)
    rubric = _rubrics(project).for_case(case_id)
    criteria = rubric.model_dump(mode="json", exclude_none=True)["criteria"] if rubric else []
    text = yaml.safe_dump(criteria, sort_keys=False, allow_unicode=True, width=100)
    return templates.TemplateResponse(request, "rubric_edit.html", {"project": project, "case_id": case_id, "text": text})


@app.post("/p/{slug}/rubrics/{case_id}/edit", response_class=HTMLResponse)
def rubric_save(request: Request, slug: str, case_id: str, text: str = Form(...)):
    project = _project(slug)
    page = {"project": project, "case_id": case_id, "text": text}
    try:
        criteria = yaml.safe_load(text) or []
        rubric = Rubric.model_validate({"test_case_id": case_id, "criteria": criteria})
    except yaml.YAMLError as e:
        return templates.TemplateResponse(request, "rubric_edit.html", page | {"error": f"Not valid YAML: {e}"})
    except ValidationError as e:
        return templates.TemplateResponse(request, "rubric_edit.html", page | {"error": _errors(e)})
    rs = _rubrics(project)
    rs.rubrics = [rubric if r.test_case_id == case_id else r for r in rs.rubrics]
    if rs.for_case(case_id) is None:
        rs.rubrics.append(rubric)
    v = store.save_edit(project.id, "rubrics", rs)
    return _back(slug, f"Saved the rubric for '{case_id}' (rubric set v{v.version}, draft).", f"case-{case_id}")


@app.post("/p/{slug}/cases/{case_id}/delete")
def case_delete(slug: str, case_id: str):
    project = _project(slug)
    cases = [c for c in _evalset(project).cases if c.id != case_id]
    v = store.save_edit(project.id, "evalset", EvalSet(cases=cases))
    return _back(slug, f"Deleted '{case_id}' (eval set v{v.version}, draft).", "evalset")
