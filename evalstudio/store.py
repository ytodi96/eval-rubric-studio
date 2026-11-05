"""SQLite storage. One database file in DATA_DIR holds every project, version, run and grade.

Approved versions are frozen: editing an approved use-case card, eval set, rubric set or target
starts a new numbered draft (drafts are edited in place), so every past run can be reproduced exactly.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .models import ARTIFACT_MODELS, ArtifactKind, Grade, Project, ResponseRecord, Run, Version

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- Use-case cards, eval sets, rubric sets and targets, one row per saved version.
CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('usecase', 'evalset', 'rubrics', 'target')),
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
    note        TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, kind, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    usecase_version    INTEGER NOT NULL,
    evalset_version    INTEGER NOT NULL,
    rubrics_version    INTEGER NOT NULL,
    target_version     INTEGER NOT NULL,
    judge_model        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd    REAL NOT NULL DEFAULT 0,
    started_at         TEXT,
    finished_at        TEXT,
    summary            TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS responses (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_case_id  TEXT NOT NULL,
    output        TEXT,
    error         TEXT,
    latency_ms    INTEGER,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    model         TEXT,
    score         REAL,
    passed        INTEGER,
    UNIQUE (run_id, test_case_id)
);

CREATE TABLE IF NOT EXISTS grades (
    id           INTEGER PRIMARY KEY,
    response_id  INTEGER NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    grader       TEXT NOT NULL CHECK (grader IN ('code', 'llm')),
    attempt      INTEGER NOT NULL DEFAULT 1,
    score        REAL NOT NULL,
    passed       INTEGER NOT NULL,
    reasoning    TEXT NOT NULL DEFAULT '',
    evidence     TEXT,
    cost_usd     REAL NOT NULL DEFAULT 0,
    UNIQUE (response_id, criterion_id, grader, attempt)
);

-- Every LLM call the Studio itself makes (generation, judging, suggestions), for cost tracking.
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    purpose       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    created_at    TEXT NOT NULL
);

-- Saved model results, keyed by a hash of everything that affects them (see llm.cache_key).
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_labels (
    id           INTEGER PRIMARY KEY,
    response_id  INTEGER NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    score        REAL NOT NULL,
    passed       INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE (response_id, criterion_id)
);
"""


class NotFound(LookupError):
    pass


class _Result:
    """Rows fetched while holding the lock, so other threads can't interleave with the read."""

    def __init__(self, cur: sqlite3.Cursor):
        self.rows = cur.fetchall()
        self.lastrowid = cur.lastrowid

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _LockedConnection:
    """One SQLite connection shared by the web app and a run's worker threads. Every statement and
    every transaction holds the same lock, so they never interleave."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.RLock()

    def execute(self, sql: str, args=()) -> _Result:
        with self.lock:
            return _Result(self.conn.execute(sql, args))

    def __enter__(self):
        self.lock.acquire()
        self.conn.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self.conn.__exit__(*exc)
        finally:
            self.lock.release()

    def close(self) -> None:
        self.conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path | str):
        db_path = Path(db_path)
        self.csv_path = None
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.csv_path = db_path.parent / "cost-log.csv"
        self._csv_lock = threading.Lock()
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate(conn)
        conn.executescript(SCHEMA)
        self.db = _LockedConnection(conn)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ projects

    def create_project(self, slug: str, title: str, description: str = "") -> Project:
        project = Project(slug=slug, title=title, description=description)  # validates the slug
        try:
            with self.db:
                cur = self.db.execute(
                    "INSERT INTO projects (slug, title, description, created_at) VALUES (?, ?, ?, ?)",
                    (project.slug, project.title, project.description, _now()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"A project called '{slug}' already exists.") from None
        return self.get_project_by_id(cur.lastrowid)

    def get_project(self, slug: str) -> Project:
        row = self.db.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise NotFound(f"No project called '{slug}'. Run `evalstudio list` to see projects.")
        return Project(**dict(row))

    def get_project_by_id(self, project_id: int) -> Project:
        row = self.db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound(f"No project with id {project_id}.")
        return Project(**dict(row))

    def list_projects(self) -> list[Project]:
        return [Project(**dict(r)) for r in self.db.execute("SELECT * FROM projects ORDER BY slug")]

    def delete_project(self, slug: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM projects WHERE slug = ?", (slug,))

    # ------------------------------------------------------------ versioned artifacts

    def save_version(self, project_id: int, kind: ArtifactKind, data: BaseModel | dict,
                     note: str = "", approved: bool = False) -> Version:
        """Validate `data` and save it as the next version of `kind`."""
        model = ARTIFACT_MODELS[kind]
        obj = data if isinstance(data, model) else model.model_validate(data)
        payload = obj.model_dump(mode="json", exclude_none=True)
        with self.db:
            (latest,) = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM versions WHERE project_id = ? AND kind = ?",
                (project_id, kind),
            ).fetchone()
            self.db.execute(
                "INSERT INTO versions (project_id, kind, version, status, note, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, kind, latest + 1, "approved" if approved else "draft", note,
                 json.dumps(payload, ensure_ascii=False), _now()),
            )
        return self.get_version(project_id, kind, latest + 1)

    def get_version(self, project_id: int, kind: ArtifactKind, version: int | None = None) -> Version:
        """A specific version, or the latest one if `version` is None."""
        if version is None:
            row = self.db.execute(
                "SELECT * FROM versions WHERE project_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
                (project_id, kind),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM versions WHERE project_id = ? AND kind = ? AND version = ?",
                (project_id, kind, version),
            ).fetchone()
        if row is None:
            which = "any version" if version is None else f"version {version}"
            raise NotFound(f"This project has no {kind} ({which}).")
        return _version(row)

    def latest_approved(self, project_id: int, kind: ArtifactKind) -> Version | None:
        row = self.db.execute(
            "SELECT * FROM versions WHERE project_id = ? AND kind = ? AND status = 'approved' "
            "ORDER BY version DESC LIMIT 1",
            (project_id, kind),
        ).fetchone()
        return _version(row) if row else None

    def list_versions(self, project_id: int, kind: ArtifactKind) -> list[Version]:
        rows = self.db.execute(
            "SELECT * FROM versions WHERE project_id = ? AND kind = ? ORDER BY version",
            (project_id, kind),
        )
        return [_version(r) for r in rows]

    def approve(self, project_id: int, kind: ArtifactKind, version: int) -> Version:
        self.get_version(project_id, kind, version)  # raises if missing
        with self.db:
            self.db.execute(
                "UPDATE versions SET status = 'approved' WHERE project_id = ? AND kind = ? AND version = ?",
                (project_id, kind, version),
            )
        return self.get_version(project_id, kind, version)

    def load(self, project_id: int, kind: ArtifactKind, version: int | None = None) -> BaseModel:
        """The parsed object (UseCaseCard, EvalSet, RubricSet or Target) for a version."""
        return ARTIFACT_MODELS[kind].model_validate(self.get_version(project_id, kind, version).data)

    def save_edit(self, project_id: int, kind: ArtifactKind, data: BaseModel | dict, note: str = "") -> Version:
        """Save an edit. A draft is updated in place; an approved version is never changed, so
        editing it starts a new draft version instead."""
        try:
            latest = self.get_version(project_id, kind)
        except NotFound:
            return self.save_version(project_id, kind, data, note=note)
        if latest.status == "approved":
            return self.save_version(project_id, kind, data, note=note or f"edited from v{latest.version}")
        model = ARTIFACT_MODELS[kind]
        obj = data if isinstance(data, model) else model.model_validate(data)
        with self.db:
            self.db.execute(
                "UPDATE versions SET data = ?, note = COALESCE(NULLIF(?, ''), note) "
                "WHERE project_id = ? AND kind = ? AND version = ? AND status = 'draft'",
                (json.dumps(obj.model_dump(mode="json", exclude_none=True), ensure_ascii=False), note,
                 project_id, kind, latest.version),
            )
        return self.get_version(project_id, kind, latest.version)

    # ------------------------------------------------------------ LLM usage

    def log_usage(self, project_id: int | None, purpose: str, provider: str, model: str,
                  input_tokens: int, cached_tokens: int, output_tokens: int, cost_usd: float,
                  from_cache: bool = False) -> None:
        """Record one model call (or cache hit, at $0) in the database and in data/cost-log.csv."""
        if from_cache:
            purpose += " (cache hit)"
            input_tokens = cached_tokens = output_tokens = 0
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT INTO llm_usage (project_id, purpose, provider, model, input_tokens, cached_tokens, "
                "output_tokens, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, purpose, provider, model, input_tokens, cached_tokens, output_tokens, cost_usd, now),
            )
        if self.csv_path is not None:
            new = not self.csv_path.exists()
            slug = self.get_project_by_id(project_id).slug if project_id else ""
            with self._csv_lock, self.csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "project", "purpose", "provider", "model", "calls", "input_tokens",
                                "cached_tokens", "output_tokens", "cost_usd"])
                w.writerow([now, slug, purpose, provider, model, 0 if from_cache else 1, input_tokens,
                            cached_tokens, output_tokens, f"{cost_usd:.6f}"])

    def costs(self, project_id: int | None = None) -> list[sqlite3.Row]:
        """Spend by day and purpose, newest first."""
        where, args = ("WHERE project_id = ?", (project_id,)) if project_id else ("", ())
        return self.db.execute(
            f"SELECT substr(created_at, 1, 10) AS day, purpose, model, COUNT(*) AS calls, "
            f"SUM(input_tokens) AS input_tokens, SUM(cached_tokens) AS cached_tokens, "
            f"SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd "
            f"FROM llm_usage {where} GROUP BY day, purpose, model ORDER BY day DESC, cost_usd DESC", args).fetchall()

    def average_output_tokens(self, purpose: str, model: str) -> float | None:
        """Average output tokens of past real calls, used to make cost estimates realistic."""
        (avg,) = self.db.execute(
            "SELECT AVG(output_tokens) FROM llm_usage WHERE purpose = ? AND model = ? AND output_tokens > 0",
            (purpose, model)).fetchone()
        return avg

    # ------------------------------------------------------------ runs, responses, grades

    def create_run(self, run: Run) -> int:
        d = run.model_dump(mode="json", exclude={"id"})
        d["summary"] = json.dumps(d["summary"])
        with self.db:
            cur = self.db.execute(f"INSERT INTO runs ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                                  tuple(d.values()))
        return cur.lastrowid

    def update_run(self, run_id: int, **fields) -> None:
        if "summary" in fields:
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False)
        with self.db:
            self.db.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                            (*fields.values(), run_id))

    def get_run(self, run_id: int) -> Run:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"No run {run_id}.")
        return _run(row)

    def list_runs(self, project_id: int) -> list[Run]:
        return [_run(r) for r in self.db.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY id DESC",
                                                 (project_id,))]

    def add_response(self, r: ResponseRecord) -> int:
        d = r.model_dump(exclude={"id"})
        with self.db:
            cur = self.db.execute(f"INSERT INTO responses ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                                  tuple(d.values()))
        return cur.lastrowid

    def set_response_score(self, response_id: int, score: float, passed: bool) -> None:
        with self.db:
            self.db.execute("UPDATE responses SET score = ?, passed = ? WHERE id = ?", (score, passed, response_id))

    def responses(self, run_id: int) -> list[ResponseRecord]:
        return [ResponseRecord(**{**dict(r), "passed": None if r["passed"] is None else bool(r["passed"])})
                for r in self.db.execute("SELECT * FROM responses WHERE run_id = ? ORDER BY id", (run_id,))]

    def add_grade(self, g: Grade) -> None:
        d = g.model_dump(exclude={"id"})
        with self.db:
            self.db.execute(f"INSERT INTO grades ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                            tuple(d.values()))

    def grades(self, response_id: int) -> list[Grade]:
        return [Grade(**{**dict(r), "passed": bool(r["passed"])})
                for r in self.db.execute("SELECT * FROM grades WHERE response_id = ? ORDER BY id", (response_id,))]

    # ------------------------------------------------------------ result cache

    def cache_get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT result FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, result: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO llm_cache (key, result, created_at) VALUES (?, ?, ?)",
                            (key, json.dumps(result, ensure_ascii=False), _now()))

    @property
    def cache(self):
        """The cache in the shape llm.complete expects."""
        store = self

        class _Cache:
            get = staticmethod(store.cache_get)
            put = staticmethod(store.cache_put)

        return _Cache

    def usage_cost(self, project_id: int) -> float:
        (total,) = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE project_id = ?",
                                   (project_id,)).fetchone()
        return total


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older version up to date."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'grades'").fetchone()
    if row and "criterion_id, attempt)" in row[0]:  # before a criterion could have a code AND a judge grade
        with conn:
            conn.execute("ALTER TABLE grades RENAME TO grades_old")
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO grades SELECT * FROM grades_old")
            conn.execute("DROP TABLE grades_old")


def _run(row: sqlite3.Row) -> Run:
    d = dict(row)
    d["summary"] = json.loads(d["summary"])
    return Run(**d)


def _version(row: sqlite3.Row) -> Version:
    d = dict(row)
    return Version(kind=d["kind"], version=d["version"], status=d["status"], note=d["note"],
                   created_at=d["created_at"], data=json.loads(d["data"]))
