"""One way to call a language model, whichever provider it is (OpenAI or Anthropic).

Every LLM call in the Studio (generator, judge, improver, and `prompt` targets) goes through
`complete()`. Pass a JSON schema to get structured output that is guaranteed to match it.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Literal

Provider = Literal["openai", "anthropic"]
PROVIDERS: tuple[Provider, ...] = ("openai", "anthropic")
KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

# USD per million tokens: (input, cached input, output). Used for cost estimates and the spending
# cap. Checked against the providers' pricing pages on 2026-10-29; update if prices change.
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 0.20, 10.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}


class LLMError(Exception):
    """A call failed. The message is written for the user and says what to do."""


@dataclass
class LLMResult:
    text: str
    data: dict | None  # parsed JSON when a schema was given
    provider: str
    model: str
    input_tokens: int = 0  # not cached
    cached_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    from_cache: bool = False  # True when the Studio reused a saved result instead of calling

    @property
    def cost_usd(self) -> float:
        if self.from_cache:
            return 0.0
        return cost_usd(self.model, self.input_tokens, self.cached_tokens, self.output_tokens)


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    p_in, p_cached, p_out = PRICES.get(model, (0.0, 0.0, 0.0))
    return (input_tokens * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1e6


def has_price(model: str) -> bool:
    return model in PRICES


# "Don't pay twice": results are cached by everything that affects them. Set by `--fresh`.
FRESH = False


def set_fresh(value: bool) -> None:
    global FRESH
    FRESH = value


def cache_key(provider: str, model: str, system: str, messages: list[dict], schema: dict | None,
              schema_name: str, max_tokens: int, effort: str | None) -> str:
    payload = json.dumps([provider, model, system, messages, schema, schema_name, max_tokens, effort],
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def complete(provider: Provider, model: str, system: str, messages: list[dict], *,
             schema: dict | None = None, schema_name: str = "answer", max_tokens: int = 16000,
             effort: str | None = None, client=None, cache=None) -> LLMResult:
    """Send `system` + `messages` ([{"role": "user"|"assistant", "content": str}]) and return the reply.

    With `schema` (a JSON schema with every property required and additionalProperties false),
    the reply is constrained to valid JSON matching it and parsed into `result.data`.
    With `cache` (an object with get(key) and put(key, dict)), an identical earlier request is
    answered from the cache at no cost, unless FRESH is set.
    """
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    key = cache_key(provider, model, system, messages, schema, schema_name, max_tokens, effort)
    if cache is not None and not FRESH:
        hit = cache.get(key)
        if hit is not None:
            return LLMResult(**hit, from_cache=True)
    call = _openai if provider == "openai" else _anthropic
    start = time.perf_counter()
    result = call(model, system, messages, schema, schema_name, max_tokens, effort, client)
    result.latency_ms = int((time.perf_counter() - start) * 1000)
    if schema is not None:
        try:
            result.data = json.loads(result.text)
        except json.JSONDecodeError:
            raise LLMError(f"{model} returned text that isn't valid JSON. Re-run; if it repeats, "
                           "raise max_tokens.") from None
    if cache is not None:
        cache.put(key, {k: v for k, v in result.__dict__.items() if k != "from_cache"})
    return result


# ---------------------------------------------------------------- OpenAI (Responses API)

def _openai(model, system, messages, schema, schema_name, max_tokens, effort, client) -> LLMResult:
    import openai

    client = client or openai.OpenAI()  # reads OPENAI_API_KEY
    request = dict(model=model, instructions=system, input=messages, max_output_tokens=max_tokens, store=False)
    if effort:
        request["reasoning"] = {"effort": effort}
    if schema is not None:
        request["text"] = {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}}
    try:
        r = client.responses.create(**request)
    except openai.AuthenticationError:
        raise LLMError("The OpenAI API key is missing or wrong. Check OPENAI_API_KEY in .env.") from None
    except openai.RateLimitError as e:
        if "insufficient_quota" in str(getattr(e, "body", "")) or getattr(e, "code", None) == "insufficient_quota":
            raise LLMError("The OpenAI account has no credit left. Add credit at platform.openai.com → "
                           "Settings → Billing, then re-run.") from None
        raise LLMError("OpenAI says too many requests right now. Wait a minute and re-run.") from None
    except openai.APIConnectionError:
        raise LLMError("Couldn't reach OpenAI. Check the internet connection and try again.") from None
    except openai.APIStatusError as e:
        raise LLMError(f"OpenAI API error {e.status_code}: {e.message}") from None

    if r.status == "incomplete":
        reason = getattr(r.incomplete_details, "reason", "")
        raise LLMError(f"{model} stopped early ({reason or 'incomplete'})."
                       + (" Raise max_tokens and re-run." if reason == "max_output_tokens" else ""))
    for item in r.output:
        if item.type == "message" and any(getattr(c, "type", "") == "refusal" for c in item.content or []):
            raise LLMError(f"{model} declined to answer.")
    u = r.usage
    cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
    return LLMResult(text=r.output_text, data=None, provider="openai", model=model,
                     input_tokens=max((u.input_tokens or 0) - cached, 0), cached_tokens=cached,
                     output_tokens=u.output_tokens or 0)


# ---------------------------------------------------------------- Anthropic (Messages API)

def _anthropic(model, system, messages, schema, schema_name, max_tokens, effort, client) -> LLMResult:
    import anthropic

    client = client or anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    output_config: dict = {}
    if effort:
        output_config["effort"] = effort
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    request = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
    if output_config:
        request["output_config"] = output_config
    try:
        with client.messages.stream(**request) as stream:  # streaming avoids timeouts on long outputs
            r = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError("The Anthropic API key is missing or wrong. Check ANTHROPIC_API_KEY in .env.") from None
    except anthropic.RateLimitError:
        raise LLMError("Anthropic says too many requests right now. Wait a minute and re-run.") from None
    except anthropic.APIConnectionError:
        raise LLMError("Couldn't reach Anthropic. Check the internet connection and try again.") from None
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from None

    if r.stop_reason == "refusal":
        raise LLMError(f"{model} declined to answer.")
    if r.stop_reason == "max_tokens":
        raise LLMError(f"{model} stopped early (max_tokens). Raise max_tokens and re-run.")
    u = r.usage
    return LLMResult(text="".join(b.text for b in r.content if b.type == "text"), data=None,
                     provider="anthropic", model=model,
                     input_tokens=(u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0),
                     cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                     output_tokens=u.output_tokens or 0)
