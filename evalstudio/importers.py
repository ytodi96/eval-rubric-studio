"""Import existing test cases from Markdown, CSV, JSON or YAML.

Missing fields get safe defaults (category "imported", type "typical", difficulty "medium") that
can be edited afterwards in the web app. Imported cases are marked `source: imported`.

Markdown format (the one the Sales Copilot's test-cases.md uses; labels are case-insensitive):

    ## TC-01: Small gift breakfast
    **Customer:** "Hi! Looking for a breakfast platter for 4 people..."
    **Category:** gift orders            (optional; also Type:, Difficulty:)
    **Expected:**
    - Suggest: Desi Board or Nashte ka Chaak
    - Escalate: no

The input label can be Customer, User, Input, Prompt or Message. Notes in brackets outside the
quotes, like `(sent at 11am)`, go into the case's context as `situation`.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from .intake import slugify
from .models import TestCase

DEFAULTS = {"category": "imported", "type": "typical", "difficulty": "medium"}
INPUT_LABELS = ("customer", "user", "input", "prompt", "message")
TYPE_ALIASES = {"edge case": "edge", "edge-case": "edge", "out of scope": "out_of_scope",
                "out-of-scope": "out_of_scope"}


class CaseImportError(ValueError):
    """The file couldn't be read as test cases. The message says why."""


def import_cases(path: Path | str) -> tuple[list[TestCase], list[str]]:
    """Read test cases from a file. Returns (cases, warnings about skipped or fixed cases)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        raw, skipped = parse_markdown(text)
        cases, warnings = _to_cases(raw)
        return cases, skipped + warnings
    elif suffix == ".csv":
        raw = parse_csv(text)
    elif suffix in (".json", ".yaml", ".yml"):
        raw = parse_structured(text, suffix)
    else:
        raise CaseImportError(f"Can't import {path.name}: use a .md, .csv, .json or .yaml file.")
    return _to_cases(raw)


# ---------------------------------------------------------------- Markdown

_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.M)
_LABEL = re.compile(r"^\*\*([A-Za-z ]+?):?\*\*:?\s*(.*)$")


def parse_markdown(text: str) -> tuple[list[dict], list[str]]:
    """Returns (raw cases, warnings about sections that look like cases but couldn't be read)."""
    cases, warnings = [], []
    headings = list(_HEADING.finditer(text))
    for i, h in enumerate(headings):
        body = text[h.end(): headings[i + 1].start() if i + 1 < len(headings) else len(text)]
        fields = _labelled_fields(body)
        title = h.group(1)
        # The input is under Customer:/User:/..., or a note such as "Rep note:" or "Kitchen note:".
        input_label = next((lbl for lbl in INPUT_LABELS if lbl in fields), None) or \
            next((lbl for lbl in fields if lbl.endswith(" note")), None)
        if input_label is None:
            if "expected" in fields:
                warnings.append(f"'{title}' skipped: it has Expected: but no input label "
                                f"({', '.join(INPUT_LABELS)} or '... note').")
            continue  # a heading that isn't a test case
        id_match = re.match(r"([A-Za-z]+-?\d+)\s*[:.\-–]\s*(.*)", title)
        case_id, name = (id_match.group(1), id_match.group(2)) if id_match else (title, title)
        message, situation = _split_situation(fields[input_label]["text"])
        if input_label not in INPUT_LABELS:
            message = f"[{input_label.capitalize()}] {message}"
        case = {"id": slugify(case_id), "input": message,
                "expected_behaviours": fields.get("expected", {}).get("bullets", [])
                or _lines(fields.get("expected", {}).get("text", "")),
                "context": {"title": name} | ({"situation": situation} if situation else {})}
        for key in ("category", "type", "difficulty"):
            if key in fields and fields[key]["text"]:
                case[key] = fields[key]["text"].strip()
        if "reference" in fields or "reference answer" in fields:
            case["reference_answer"] = (fields.get("reference answer") or fields["reference"])["text"]
        cases.append(case)
    return cases, warnings


def _labelled_fields(body: str) -> dict[str, dict]:
    """Split a case body into **Label:** sections, each with its text and any bullet points."""
    fields: dict[str, dict] = {}
    current = None
    for line in body.splitlines():
        m = _LABEL.match(line.strip())
        if m:
            current = {"text": m.group(2).strip(), "bullets": []}
            fields[m.group(1).strip().lower()] = current
        elif current is not None and re.match(r"^\s*[-*]\s+", line):
            current["bullets"].append(re.sub(r"^\s*[-*]\s+", "", line).strip())
        elif current is not None and line.strip() and not line.strip().startswith("---"):
            current["text"] = (current["text"] + "\n" + line.strip()).strip()
    return fields


def _split_situation(text: str) -> tuple[str, str]:
    """'(after a quote) "That's a lot."' → ('That's a lot.', 'after a quote')."""
    quoted = re.findall(r'"([^"]+)"|“([^”]+)”', text)
    if not quoted:
        return text.strip(), ""
    message = "\n".join(a or b for a, b in quoted).strip()
    outside = re.sub(r'"[^"]+"|“[^”]+”', " ", text)
    notes = [n.strip() for n in re.findall(r"\(([^)]*)\)", outside) if n.strip()]
    return message, "; ".join(notes)


def _lines(text: str) -> list[str]:
    return [line.strip(" -*") for line in text.splitlines() if line.strip(" -*")]


# ---------------------------------------------------------------- CSV, JSON, YAML

def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "input" not in [f.strip().lower() for f in reader.fieldnames]:
        raise CaseImportError("The CSV needs at least an `input` column (and ideally `expected_behaviours`).")
    rows = []
    for row in reader:
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        expected = row.pop("expected_behaviours", "") or row.pop("expected", "")
        row["expected_behaviours"] = [p.strip() for p in re.split(r"\n|\s\|\s|;", expected) if p.strip()]
        rows.append({k: v for k, v in row.items() if v != ""})
    return rows


def parse_structured(text: str, suffix: str) -> list[dict]:
    try:
        data = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise CaseImportError(f"Not valid {suffix[1:].upper()}: {e}") from None
    if isinstance(data, dict):
        data = data.get("cases", data.get("test_cases"))
    if not isinstance(data, list):
        raise CaseImportError("Expected a list of test cases, or a mapping with a `cases` list.")
    return data


# ---------------------------------------------------------------- shared

def _to_cases(raw: list[dict]) -> tuple[list[TestCase], list[str]]:
    cases, warnings, seen = [], [], set()
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            warnings.append(f"Item {n}: not a test case, skipped.")
            continue
        item = {**DEFAULTS, **{k: v for k, v in item.items() if v not in (None, "")}}
        item["type"] = TYPE_ALIASES.get(str(item["type"]).lower(), str(item["type"]).lower())
        item["difficulty"] = str(item["difficulty"]).lower()
        if isinstance(item.get("expected_behaviours"), str):
            item["expected_behaviours"] = _lines(item["expected_behaviours"])
        base = slugify(str(item.get("id") or f"case-{n}")) or f"case-{n}"
        item["id"], k = base, 2
        while item["id"] in seen:
            item["id"], k = f"{base}-{k}", k + 1
        item["source"] = "imported"
        try:
            case = TestCase.model_validate(item)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            warnings.append(f"Case '{item['id']}' skipped: {problems}")
            continue
        seen.add(case.id)
        cases.append(case)
    if raw and not cases:
        raise CaseImportError("No test cases could be read. " + " ".join(warnings[:3]))
    return cases, warnings
