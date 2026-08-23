from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dsh_novel.api_models import ProjectCreateRequest
from dsh_novel.domain import ChapterContract, ChapterDelta, ContextPackage, QualityIssue
from dsh_novel.errors import ProjectNotFoundError, RunNotFoundError
from dsh_novel.util import canonical_json, new_id, sha256_text, utc_now

SCHEMA_VERSION = 1
PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")

MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    premise TEXT NOT NULL,
    target_chapters INTEGER NOT NULL,
    hard_rules_json TEXT NOT NULL,
    story_spine_json TEXT NOT NULL,
    blueprint_version INTEGER NOT NULL DEFAULT 1,
    canon_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapter_contracts (
    chapter_number INTEGER PRIMARY KEY,
    contract_json TEXT NOT NULL,
    blueprint_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    chapter_number INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    digest TEXT NOT NULL DEFAULT '',
    finalized_revision_id TEXT,
    context_package_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_revisions_chapter ON revisions(chapter_number, created_at);
CREATE TABLE IF NOT EXISTS context_packages (
    id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL,
    task TEXT NOT NULL,
    package_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapter_deltas (
    id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL,
    revision_id TEXT NOT NULL UNIQUE,
    delta_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_issues (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    issue_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canon_commits (
    id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL UNIQUE,
    revision_id TEXT NOT NULL UNIQUE,
    canon_version INTEGER NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    contract_json TEXT NOT NULL,
    context_package_id TEXT,
    current_revision_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_chapter ON runs(chapter_number, created_at);
"""
}


class ProjectDatabase:
    def __init__(self, projects_root: Path, project_id: str) -> None:
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ProjectNotFoundError("invalid project id")
        self.project_id = project_id
        self.project_dir = projects_root / project_id
        self.path = self.project_dir / "novel.sqlite3"

    @classmethod
    def create(
        cls, projects_root: Path, project_id: str, request: ProjectCreateRequest
    ) -> ProjectDatabase:
        db = cls(projects_root, project_id)
        db.project_dir.mkdir(parents=True, exist_ok=False)
        (db.project_dir / "manuscript").mkdir()
        (db.project_dir / "exports").mkdir()
        (db.project_dir / "runtime").mkdir()
        db.migrate()
        now = utc_now()
        with db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, title, premise, target_chapters, hard_rules_json,
                    story_spine_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    request.title,
                    request.premise,
                    request.target_chapters,
                    canonical_json(request.hard_rules),
                    canonical_json(request.story_spine),
                    now,
                    now,
                ),
            )
        return db

    def ensure_exists(self) -> None:
        if not self.path.is_file():
            raise ProjectNotFoundError(f"project {self.project_id!r} was not found")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version in sorted(MIGRATIONS):
                if version in applied:
                    continue
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )

    def project(self) -> dict[str, Any]:
        self.ensure_exists()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM projects LIMIT 1").fetchone()
            if row is None:
                raise ProjectNotFoundError(f"project {self.project_id!r} is not initialized")
            result = dict(row)
            result["hard_rules"] = json.loads(result.pop("hard_rules_json"))
            result["story_spine"] = json.loads(result.pop("story_spine_json"))
            counts = connection.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status = 'COMMITTED' THEN 1 ELSE 0 END) AS committed
                FROM chapters
                """
            ).fetchone()
            result["chapters_total"] = int(counts["total"] or 0)
            result["chapters_committed"] = int(counts["committed"] or 0)
            return result

    def status(self) -> dict[str, Any]:
        result = self.project()
        with self.connect() as connection:
            result["chapters"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT chapter_number, status, title, digest, finalized_revision_id, "
                    "updated_at FROM chapters ORDER BY chapter_number"
                )
            ]
            result["recent_runs"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, chapter_number, status, stage, attempt, error_code, updated_at "
                    "FROM runs ORDER BY created_at DESC LIMIT 10"
                )
            ]
        return result

    def save_contract(self, contract: ChapterContract) -> None:
        project = self.project()
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO chapter_contracts(
                    chapter_number, contract_json, blueprint_version, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chapter_number) DO UPDATE SET
                    contract_json = excluded.contract_json,
                    blueprint_version = excluded.blueprint_version,
                    updated_at = excluded.updated_at
                """,
                (
                    contract.chapter_number,
                    canonical_json(contract.model_dump(mode="json")),
                    project["blueprint_version"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO chapters(chapter_number, status, title, updated_at)
                VALUES (?, 'PREPARED', ?, ?)
                ON CONFLICT(chapter_number) DO UPDATE SET
                    title = excluded.title,
                    status = CASE
                      WHEN chapters.status = 'COMMITTED' THEN chapters.status
                      ELSE 'PREPARED'
                    END,
                    updated_at = excluded.updated_at
                """,
                (contract.chapter_number, contract.title, now),
            )

    def contract(self, chapter_number: int) -> ChapterContract | None:
        self.ensure_exists()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT contract_json FROM chapter_contracts WHERE chapter_number = ?",
                (chapter_number,),
            ).fetchone()
        return ChapterContract.model_validate_json(row["contract_json"]) if row else None

    def recent_chapters(self, before_chapter: int, limit: int = 3) -> list[dict[str, Any]]:
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chapter_number, c.digest, r.content
                FROM chapters c
                JOIN revisions r ON r.id = c.finalized_revision_id
                WHERE c.status = 'COMMITTED' AND c.chapter_number < ?
                ORDER BY c.chapter_number DESC LIMIT ?
                """,
                (before_chapter, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_context(self, package: ContextPackage) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO context_packages(
                    id, chapter_number, task, package_json, checksum, estimated_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package.package_id,
                    package.chapter_number,
                    package.task,
                    canonical_json(package.model_dump(mode="json")),
                    package.checksum,
                    package.estimated_tokens,
                    utc_now(),
                ),
            )

    def create_run(
        self, contract: ChapterContract, context_package_id: str, idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        self.ensure_exists()
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing), False
            run_id = new_id("run")
            connection.execute(
                """
                INSERT INTO runs(
                    id, chapter_number, status, stage, idempotency_key, contract_json,
                    context_package_id, created_at, updated_at
                ) VALUES (?, ?, 'RUNNING', 'CONTEXT_READY', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    contract.chapter_number,
                    idempotency_key,
                    canonical_json(contract.model_dump(mode="json")),
                    context_package_id,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row), True

    def run(self, run_id: str) -> dict[str, Any]:
        self.ensure_exists()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id!r} was not found")
        result = dict(row)
        result["contract"] = json.loads(result.pop("contract_json"))
        return result

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        stage: str,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE runs SET status = ?, stage = ?, error_code = ?, error_message = ?,
                    attempt = attempt + ?, updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    stage,
                    error_code,
                    error_message,
                    1 if increment_attempt else 0,
                    utc_now(),
                    run_id,
                ),
            )

    def save_draft(self, run_id: str, chapter_number: int, content: str) -> str:
        revision_id = new_id("rev")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO revisions(id, chapter_number, content, content_hash, status, created_at)
                VALUES (?, ?, ?, ?, 'DRAFT', ?)
                """,
                (revision_id, chapter_number, content, sha256_text(content), now),
            )
            connection.execute(
                """
                UPDATE runs SET current_revision_id = ?, stage = 'DRAFT_SAVED', updated_at = ?
                WHERE id = ?
                """,
                (revision_id, now, run_id),
            )
        return revision_id

    def save_issues(self, run_id: str, issues: list[QualityIssue]) -> None:
        if not issues:
            return
        with self.transaction() as connection:
            for issue in issues:
                connection.execute(
                    """
                    INSERT INTO review_issues(
                        id, run_id, chapter_number, issue_json, status, created_at
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?)
                    """,
                    (
                        issue.issue_id,
                        run_id,
                        issue.chapter_number,
                        canonical_json(issue.model_dump(mode="json")),
                        utc_now(),
                    ),
                )

    def finalize(
        self,
        *,
        run_id: str,
        revision_id: str,
        contract: ChapterContract,
        delta: ChapterDelta,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically exposes revision, delta, digest, canon version and commit."""
        now = utc_now()
        commit_id = new_id("commit")
        delta_id = new_id("delta")
        with self.transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFoundError(f"run {run_id!r} was not found")
            existing = connection.execute(
                "SELECT * FROM canon_commits WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing)
            project = connection.execute("SELECT * FROM projects LIMIT 1").fetchone()
            canon_version = int(project["canon_version"]) + 1
            connection.execute(
                "UPDATE revisions SET status = 'FINALIZED', finalized_at = ? WHERE id = ?",
                (now, revision_id),
            )
            connection.execute(
                """
                INSERT INTO chapter_deltas(
                    id, chapter_number, revision_id, delta_json, status, created_at
                ) VALUES (?, ?, ?, ?, 'CONFIRMED', ?)
                """,
                (
                    delta_id,
                    contract.chapter_number,
                    revision_id,
                    canonical_json(delta.model_dump(mode="json")),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE chapters SET status = 'COMMITTED', digest = ?,
                    finalized_revision_id = ?, context_package_id = ?, updated_at = ?
                WHERE chapter_number = ?
                """,
                (
                    delta.digest,
                    revision_id,
                    run["context_package_id"],
                    now,
                    contract.chapter_number,
                ),
            )
            connection.execute(
                "UPDATE projects SET canon_version = ?, updated_at = ?",
                (canon_version, now),
            )
            connection.execute(
                """
                INSERT INTO canon_commits(
                    id, chapter_number, revision_id, canon_version, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    contract.chapter_number,
                    revision_id,
                    canon_version,
                    idempotency_key,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE runs SET status = 'COMMITTED', stage = 'COMMITTED',
                    error_code = NULL, error_message = NULL, updated_at = ? WHERE id = ?
                """,
                (now, run_id),
            )
        return {
            "id": commit_id,
            "chapter_number": contract.chapter_number,
            "revision_id": revision_id,
            "canon_version": canon_version,
            "idempotency_key": idempotency_key,
            "created_at": now,
        }

    def chapter_content(self, chapter_number: int) -> str | None:
        self.ensure_exists()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.content FROM chapters c
                JOIN revisions r ON r.id = c.finalized_revision_id
                WHERE c.chapter_number = ? AND c.status = 'COMMITTED'
                """,
                (chapter_number,),
            ).fetchone()
        return str(row["content"]) if row else None

    def export(self, export_format: str) -> str:
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chapter_number, c.title, r.content FROM chapters c
                JOIN revisions r ON r.id = c.finalized_revision_id
                WHERE c.status = 'COMMITTED' ORDER BY c.chapter_number
                """
            ).fetchall()
        separator = "\n\n" if export_format == "markdown" else "\n\n"
        parts: list[str] = []
        for row in rows:
            content = str(row["content"]).strip()
            if export_format == "markdown" and not content.startswith("#"):
                parts.append(f"# 第{row['chapter_number']}章 {row['title']}\n\n{content}")
            else:
                parts.append(content)
        return separator.join(parts)
