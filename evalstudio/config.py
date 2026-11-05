"""Settings, read from environment variables (and .env). Everything has a safe default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .llm import KEY_NAMES, PROVIDERS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROLES = ("generator", "judge", "improver")

# Default model per role for each provider. Only the judge gets the strong model; drafts (test
# cases, rubrics, suggestions) are reviewed by a person anyway. See docs/decisions.md.
DEFAULT_MODELS = {
    "openai": {"generator": "gpt-6.1-terra", "judge": "gpt-6.1-sol", "improver": "gpt-6.1-terra"},
    "anthropic": {"generator": "claude-sonnet-5-5", "judge": "claude-opus-5-5", "improver": "claude-sonnet-5-5"},
}
DEFAULT_EFFORT = {"generator": "medium", "judge": "medium", "improver": "medium"}
# Most output tokens one call may produce (reasoning included), per role.
DEFAULT_MAX_TOKENS = {"generator": 16000, "judge": 4000, "improver": 8000}


@dataclass(frozen=True)
class RoleModel:
    """Which provider and model does one job (generator, judge or improver)."""

    provider: str
    model: str
    effort: str | None
    max_tokens: int = 16000

    def __str__(self) -> str:
        return f"{self.provider} / {self.model}" + (f" (effort {self.effort})" if self.effort else "")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    provider: str
    roles: dict[str, RoleModel]
    spend_cap_usd: float
    pass_mark: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "evalstudio.sqlite3"

    @property
    def generator(self) -> RoleModel:
        return self.roles["generator"]

    @property
    def judge(self) -> RoleModel:
        return self.roles["judge"]

    @property
    def improver(self) -> RoleModel:
        return self.roles["improver"]

    def has_key(self, provider: str) -> bool:
        return bool(os.getenv(KEY_NAMES[provider]))


class ConfigError(ValueError):
    pass


def _provider(name: str, value: str) -> str:
    value = value.strip().lower()
    if value not in PROVIDERS:
        raise ConfigError(f"{name} must be one of {', '.join(PROVIDERS)}, not '{value}'.")
    return value


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()  # also a .env in the current folder, if any
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = (Path.cwd() / data_dir).resolve()
    provider = _provider("EVALSTUDIO_PROVIDER", os.getenv("EVALSTUDIO_PROVIDER", "openai"))
    roles = {}
    for role in ROLES:
        prefix = f"EVALSTUDIO_{role.upper()}"
        p = _provider(f"{prefix}_PROVIDER", os.getenv(f"{prefix}_PROVIDER", provider))
        roles[role] = RoleModel(
            provider=p,
            model=os.getenv(f"{prefix}_MODEL", DEFAULT_MODELS[p][role]),
            effort=os.getenv(f"{prefix}_EFFORT", DEFAULT_EFFORT[role]) or None,
            max_tokens=int(os.getenv(f"{prefix}_MAX_TOKENS", DEFAULT_MAX_TOKENS[role])),
        )
    return Settings(
        data_dir=data_dir,
        provider=provider,
        roles=roles,
        spend_cap_usd=float(os.getenv("EVALSTUDIO_SPEND_CAP_USD", "2")),
        pass_mark=float(os.getenv("EVALSTUDIO_PASS_MARK", "0.8")),
    )
