"""Step 4, running the target: one adapter per kind of system under test.

    prompt   a system prompt + model (OpenAI or Anthropic)
    http     POST to an endpoint, with a request/response mapping
    python   call a function in another project
    offline  read already-collected responses from a CSV or JSON file

Every adapter turns a TestCase into a TargetResponse. Failures are recorded on the response
(never raised), so one broken case doesn't stop a run.
"""

from __future__ import annotations

import csv
import importlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .llm import LLMError, complete
from .models import HttpTarget, OfflineTarget, PromptTarget, PythonTarget, Target, TestCase


@dataclass
class TargetResponse:
    output: str | None
    error: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    from_cache: bool = False


def messages_for(case: TestCase) -> list[dict]:
    """The case as chat messages. Context (e.g. order details) goes in front of the last user
    message, the way an app would pass data it looked up alongside the customer's words."""
    turns = [{"role": "user", "content": case.input}] if isinstance(case.input, str) \
        else [{"role": t.role, "content": t.content} for t in case.input]
    if case.context:
        block = "\n".join(f"{k}: {v}" for k, v in case.context.items())
        last = turns[-1]
        turns[-1] = {"role": "user", "content": f"<context>\n{block}\n</context>\n\n{last['content']}"}
    return turns


def last_user_message(case: TestCase) -> str:
    return case.input if isinstance(case.input, str) else case.input[-1].content


def get_path(data, path: str | None):
    """Follow a dotted path like 'reply.text' or 'choices.0.message.content'."""
    if not path:
        return data
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)]
        elif isinstance(data, dict):
            data = data[part]
        else:
            data = getattr(data, part)
    return data


class Adapter:
    def __init__(self, target: Target):
        self.target = target

    def respond(self, case: TestCase) -> TargetResponse:
        start = time.perf_counter()
        try:
            r = self._respond(case)
        except Exception as e:  # recorded on the response; the run carries on
            r = TargetResponse(output=None, error=f"{type(e).__name__}: {e}")
        if not r.latency_ms:
            r.latency_ms = int((time.perf_counter() - start) * 1000)
        return r

    def _respond(self, case: TestCase) -> TargetResponse:
        raise NotImplementedError


class PromptAdapter(Adapter):
    def __init__(self, target: Target, client=None, cache=None):
        super().__init__(target)
        self.client, self.cache = client, cache

    def _respond(self, case):
        cfg: PromptTarget = self.target.config
        try:
            r = complete(cfg.provider, cfg.model, cfg.system_prompt, messages_for(case),
                         max_tokens=cfg.max_tokens, effort=cfg.effort, client=self.client, cache=self.cache)
        except LLMError as e:
            return TargetResponse(output=None, error=str(e), model=cfg.model)
        return TargetResponse(output=r.text, latency_ms=r.latency_ms, input_tokens=r.input_tokens,
                              cached_tokens=r.cached_tokens, output_tokens=r.output_tokens,
                              cost_usd=r.cost_usd, model=cfg.model, from_cache=r.from_cache)


_ENV = re.compile(r"\$\{(\w+)\}")
_PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def fill_template(template, case: TestCase):
    """Replace {{input}}, {{messages}}, {{context}}, {{context.<key>}} and {{id}} in a request body.
    A string that is exactly one placeholder becomes that value (so {{messages}} stays a list)."""
    values = {"input": last_user_message(case), "messages": messages_for(case), "context": case.context,
              "id": case.id}

    def lookup(name: str):
        if name.startswith("context."):
            return case.context.get(name.split(".", 1)[1], "")
        if name not in values:
            raise KeyError(f"Unknown placeholder {{{{{name}}}}} in the request body")
        return values[name]

    if isinstance(template, dict):
        return {k: fill_template(v, case) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_template(v, case) for v in template]
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template.strip())
        if whole:
            return lookup(whole.group(1))
        return _PLACEHOLDER.sub(lambda m: str(lookup(m.group(1))), template)
    return template


class HttpAdapter(Adapter):
    def __init__(self, target: Target, client=None):
        super().__init__(target)
        self.client = client

    def _respond(self, case):
        import httpx

        cfg: HttpTarget = self.target.config
        headers = {k: _ENV.sub(lambda m: os.getenv(m.group(1), ""), v) for k, v in cfg.headers.items()}
        body = fill_template(cfg.body, case)
        kwargs = {"json": body} if not isinstance(body, str) else {"content": body}
        client = self.client or httpx
        r = client.post(cfg.url, headers=headers, timeout=cfg.timeout_s, **kwargs)
        if r.status_code >= 400:
            return TargetResponse(output=None, error=f"HTTP {r.status_code}: {r.text[:300]}")
        output = get_path(r.json(), cfg.response_path)
        return TargetResponse(output=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


class PythonAdapter(Adapter):
    def __init__(self, target: Target):
        super().__init__(target)
        cfg: PythonTarget = target.config
        if cfg.project_dir and cfg.project_dir not in sys.path:
            sys.path.insert(0, cfg.project_dir)
        module, func = cfg.function.split(":")
        self.func = getattr(importlib.import_module(module), func)

    def _respond(self, case):
        cfg: PythonTarget = self.target.config
        result = self.func(case.input if isinstance(case.input, str) else messages_for(case), dict(case.context))
        output = get_path(result, cfg.response_path)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        return TargetResponse(output=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                              input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                              cost_usd=usage.get("cost_usd", 0.0), model=usage.get("model"))


class OfflineAdapter(Adapter):
    def __init__(self, target: Target):
        super().__init__(target)
        cfg: OfflineTarget = target.config
        path = Path(cfg.file)
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(text.splitlines()))
        else:
            data = json.loads(text)
            rows = data if isinstance(data, list) else data.get("responses", [])
        self.outputs = {str(r[cfg.id_field]): r[cfg.output_field] for r in rows if r.get(cfg.output_field)}

    def _respond(self, case):
        if case.id not in self.outputs:
            return TargetResponse(output=None, error=f"No response for '{case.id}' in {self.target.config.file}")
        return TargetResponse(output=self.outputs[case.id])


def make_adapter(target: Target, cache=None) -> Adapter:
    """`cache` (from Store.cache) lets a prompt target reuse earlier identical answers at no cost."""
    if target.config.adapter == "prompt":
        return PromptAdapter(target, cache=cache)
    return {"http": HttpAdapter, "python": PythonAdapter, "offline": OfflineAdapter}[target.config.adapter](target)
