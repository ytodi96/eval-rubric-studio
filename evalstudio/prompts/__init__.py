"""System prompts, kept as plain Markdown files next to this one so they're easy to read and edit."""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent


def load(name: str, **values: str) -> str:
    """The prompt in `<name>.md`, with each `{key}` replaced by its value."""
    text = (HERE / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
