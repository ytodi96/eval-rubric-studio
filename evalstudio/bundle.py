"""Project bundles: a folder of YAML files that holds a whole project, so it can live in git.

    my-project/
      project.yaml   slug, title, description
      usecase.yaml   the use-case card
      evalset.yaml   the test cases
      rubrics.yaml   one rubric per test case
      target.yaml    the system under test (a prompt target may point at a .md file)

`import_bundle` loads a folder into the database (each file becomes a new version);
`export_bundle` writes the latest versions back out. JSON files (.json) work in place of YAML.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import ARTIFACT_MODELS, Project
from .store import NotFound, Store

FORMAT = "evalstudio/v1"
FILES = {"usecase": "usecase", "evalset": "evalset", "rubrics": "rubrics", "target": "target"}


class BundleError(ValueError):
    """A bundle file is missing or invalid. The message says which file and what to fix."""


def _find(folder: Path, stem: str) -> Path | None:
    for ext in (".yaml", ".yml", ".json"):
        p = folder / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def read_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise BundleError(f"{path.name} is not valid {path.suffix[1:].upper()}: {e}") from None
    if not isinstance(data, dict):
        raise BundleError(f"{path.name} should contain a mapping (key: value pairs) at the top level.")
    data.pop("format", None)
    return data


def _resolve_target_files(data: dict, folder: Path) -> dict:
    """A prompt target may say `system_prompt_file: prompt_v1.md` instead of inlining the prompt."""
    cfg = data.get("config", {})
    if "system_prompt_file" in cfg:
        cfg["system_prompt"] = (folder / cfg.pop("system_prompt_file")).read_text(encoding="utf-8")
    # Files and folders named in the bundle are relative to the bundle folder.
    for key in ("file", "project_dir"):
        if cfg.get(key) and not Path(cfg[key]).is_absolute():
            cfg[key] = str((folder / cfg[key]).resolve())
    return data


def import_bundle(store: Store, folder: Path | str, approve: bool = False) -> Project:
    folder = Path(folder)
    meta_path = _find(folder, "project")
    if meta_path is None:
        raise BundleError(f"No project.yaml in {folder}.")
    meta = read_file(meta_path)
    try:
        project = store.get_project(meta["slug"])
    except NotFound:
        project = store.create_project(meta["slug"], meta.get("title", meta["slug"]), meta.get("description", ""))
    except KeyError:
        raise BundleError("project.yaml needs a `slug` (short lowercase name, e.g. support-email).") from None

    for kind, stem in FILES.items():
        path = _find(folder, stem)
        if path is None:
            continue
        data = read_file(path)
        if kind == "target":
            data = _resolve_target_files(data, folder)
        try:
            obj = ARTIFACT_MODELS[kind].model_validate(data)
        except ValidationError as e:
            raise BundleError(f"{path.name} has problems:\n{_explain(e)}") from None
        try:
            latest = store.get_version(project.id, kind)
        except NotFound:
            latest = None
        if latest is not None and ARTIFACT_MODELS[kind].model_validate(latest.data) == obj:
            if approve and latest.status != "approved":  # unchanged: no new version, just approve it
                store.approve(project.id, kind, latest.version)
            continue
        store.save_version(project.id, kind, obj, note=f"imported from {path.name}", approved=approve)
    return project


def export_bundle(store: Store, slug: str, folder: Path | str, fmt: str = "yaml") -> list[Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    project = store.get_project(slug)
    written = [_write(folder / f"project.{fmt}",
                      {"slug": project.slug, "title": project.title, "description": project.description})]
    for kind, stem in FILES.items():
        try:
            v = store.get_version(project.id, kind)
        except NotFound:
            continue
        data = v.data
        if kind == "target":  # keep file paths relative to the bundle, so no local paths are exported
            cfg = dict(data["config"])
            for key in ("file", "project_dir"):
                if cfg.get(key):
                    try:
                        cfg[key] = Path(os.path.relpath(cfg[key], folder.resolve())).as_posix()
                    except ValueError:  # on another drive: leave as is
                        pass
            data = {**data, "config": cfg}
        if kind == "target" and data["config"].get("adapter") == "prompt" and fmt == "yaml":
            prompt_file = folder / f"system_prompt_v{v.version}.md"
            prompt_file.write_text(data["config"]["system_prompt"], encoding="utf-8")
            written.append(prompt_file)
            data = {**data, "config": {k: val for k, val in data["config"].items() if k != "system_prompt"}
                    | {"system_prompt_file": prompt_file.name}}
        written.append(_write(folder / f"{stem}.{fmt}", data))
    return written


class _Dumper(yaml.SafeDumper):
    """Writes multi-line text as readable `|` blocks instead of one long quoted line."""


def _str(dumper: yaml.SafeDumper, s: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


_Dumper.add_representer(str, _str)


def _write(path: Path, data: dict) -> Path:
    data = {"format": FORMAT, **data}
    if path.suffix == ".json":
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        path.write_text(yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100),
                        encoding="utf-8")
    return path


def _explain(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        where = " > ".join(str(p) for p in err["loc"])
        lines.append(f"  - {where}: {err['msg']}")
    return "\n".join(lines)
