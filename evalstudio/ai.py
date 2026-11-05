"""Calls a model for one of the Studio's roles and records what it cost."""

from __future__ import annotations

from .config import RoleModel
from .llm import LLMResult, complete
from .store import Store


def ask(role: RoleModel, system: str, user: str, *, schema: dict, schema_name: str,
        store: Store | None = None, project_id: int | None = None, purpose: str = "",
        max_tokens: int | None = None) -> LLMResult:
    """One structured call: `system` prompt + one user message, reply parsed against `schema`.
    With a store, identical earlier calls are answered from its cache, and every call is logged."""
    result = complete(role.provider, role.model, system, [{"role": "user", "content": user}],
                      schema=schema, schema_name=schema_name, effort=role.effort,
                      max_tokens=max_tokens or role.max_tokens, cache=store.cache if store is not None else None)
    if store is not None:
        store.log_usage(project_id, purpose or schema_name, result.provider, result.model,
                        result.input_tokens, result.cached_tokens, result.output_tokens, result.cost_usd,
                        from_cache=result.from_cache)
    return result


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """A strict JSON-schema object: every property required, nothing extra allowed."""
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
NULLABLE_STR = {"type": ["string", "null"]}
