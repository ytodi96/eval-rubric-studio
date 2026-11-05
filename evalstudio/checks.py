"""The code-check library: criteria that code can decide exactly, without a model.

Every check returns a CheckResult with pass/fail, a short explanation and, where there is one,
the piece of the response that decided it (the evidence).
"""

from __future__ import annotations

import importlib
import json
import re
import unicodedata
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .models import REGEX_FLAGS, Contains, JsonSchema, Length, Numeric, PythonCheck, Regex, TestCase


@dataclass
class CheckResult:
    passed: bool
    detail: str
    evidence: str | None = None


def run_check(check, response: str, case: TestCase | None = None) -> CheckResult:
    """Run one code check against a response."""
    runner = {Contains: _contains, Regex: _regex, JsonSchema: _json_schema, Numeric: _numeric,
              Length: _length, PythonCheck: _python}[type(check)]
    try:
        return runner(check, response, case)
    except Exception as e:  # a broken check must fail loudly in the report, not crash the run
        return CheckResult(False, f"The check itself failed: {type(e).__name__}: {e}")


def normalise(text: str) -> str:
    """Make matching forgiving about typography: curly quotes, dashes and repeated spaces."""
    text = unicodedata.normalize("NFKC", text)
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"), (" ", " ")):
        text = text.replace(a, b)
    return re.sub(r"[ \t]+", " ", text)


def _snippet(text: str, start: int, end: int, pad: int = 40) -> str:
    a, b = max(start - pad, 0), min(end + pad, len(text))
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def _contains(check: Contains, response: str, case) -> CheckResult:
    text = normalise(response)
    hay = text if check.case_sensitive else text.lower()
    found = {}
    for v in check.values:
        needle = normalise(v) if check.case_sensitive else normalise(v).lower()
        i = hay.find(needle)
        if i >= 0:
            found[v] = _snippet(text, i, i + len(needle))
    missing = [v for v in check.values if v not in found]
    if check.type == "contains":
        ok = not missing if check.match == "all" else bool(found)
        if ok:
            return CheckResult(True, "Contains " + ", ".join(f"'{v}'" for v in found), next(iter(found.values())))
        return CheckResult(False, "Missing " + ", ".join(f"'{v}'" for v in missing))
    # not_contains: any forbidden value appearing fails it (`match` doesn't apply).
    if found:
        return CheckResult(False, "Contains forbidden " + ", ".join(f"'{v}'" for v in found), next(iter(found.values())))
    return CheckResult(True, "None of the forbidden text appears")


def _regex(check: Regex, response: str, case) -> CheckResult:
    text = normalise(response)
    m = re.search(check.pattern, text, REGEX_FLAGS)
    if check.type == "regex":
        if m:
            return CheckResult(True, "Pattern found", _snippet(text, m.start(), m.end()))
        return CheckResult(False, "Pattern not found")
    if m:
        return CheckResult(False, "Forbidden pattern found", _snippet(text, m.start(), m.end()))
    return CheckResult(True, "Forbidden pattern not found")


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    return m.group(1) if m else text.strip()


def _json_schema(check: JsonSchema, response: str, case) -> CheckResult:
    try:
        data = json.loads(_strip_fences(response))
    except json.JSONDecodeError as e:
        return CheckResult(False, f"Not valid JSON: {e.msg} (line {e.lineno})")
    errors = sorted(Draft202012Validator(check.json_schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(map(str, e.path)) or "(top level)"
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        return CheckResult(False, f"Schema error at {where}: {e.message}{more}")
    return CheckResult(True, "Valid JSON matching the schema")


def _number(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _numeric(check: Numeric, response: str, case) -> CheckResult:
    """Passes if any number captured by the pattern is within tolerance of the expected value."""
    text = normalise(response)
    seen = []
    for m in re.finditer(check.pattern, text, REGEX_FLAGS):
        value = _number(m.group(1) if m.groups() else m.group(0))
        if value is None:
            continue
        seen.append(value)
        if abs(value - check.expected) <= check.tolerance:
            return CheckResult(True, f"Found {value:g} (expected {check.expected:g} ± {check.tolerance:g})",
                               _snippet(text, m.start(), m.end()))
    if seen:
        return CheckResult(False, f"Expected {check.expected:g} ± {check.tolerance:g}; found "
                           + ", ".join(f"{v:g}" for v in seen[:5]))
    return CheckResult(False, f"No number matching the pattern; expected {check.expected:g}")


def _length(check: Length, response: str, case) -> CheckResult:
    n = len(re.findall(r"\S+", response)) if check.unit == "words" else len(response.strip())
    too_short = check.min is not None and n < check.min
    too_long = check.max is not None and n > check.max
    bounds = f"{check.min if check.min is not None else 0}–{check.max if check.max is not None else '∞'}"
    if too_short or too_long:
        return CheckResult(False, f"{n} {check.unit}; expected {bounds}")
    return CheckResult(True, f"{n} {check.unit} (within {bounds})")


def _python(check: PythonCheck, response: str, case) -> CheckResult:
    """Call a custom function `module:function(response, case)` returning bool or (bool, detail).
    Only functions written by you run here; the rubric generator never writes Python checks."""
    module_name, func_name = check.function.split(":")
    func = getattr(importlib.import_module(module_name), func_name)
    out = func(response, case)
    if isinstance(out, tuple):
        passed, detail = bool(out[0]), str(out[1])
    else:
        passed, detail = bool(out), ""
    return CheckResult(passed, detail or ("Custom check passed" if passed else "Custom check failed"))
