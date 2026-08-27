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
from dsh_novel.errors import ChapterNotFoundError, ProjectNotFoundError, RunNotFoundError
from dsh_novel.util import canonical_json, new_id, sha256_text, utc_now

SCHEMA_VERSION = 3
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
""",
    # 0.4.0: per-run LLM review history (list of ReviewVerdict records as JSON).
    # Backward compatible: old databases gain a nullable column via ALTER TABLE.
    2: """
ALTER TABLE runs ADD COLUMN review_json TEXT;
""",
    # 0.5.0: per-project writing policy (score_threshold / max_revisions /
    # target_words / on_chapter_failure) persisted as a JSON object.
    3: """
ALTER TABLE projects ADD COLUMN policy_json TEXT;
""",
}


def _parse_review_history(raw: str | None) -> list[dict[str, Any]]:
    """Decode a runs.review_json payload; tolerate legacy/garbage values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _parse_policy_json(raw: str | None) -> dict[str, Any]:
    """Decode a projects.policy_json payload; tolerate legacy/garbage values."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}

# Paths whose schema migrations have already run in this process (idempotence
# guard for the lazy migrate-on-connect of legacy project databases).
_migrated_paths: set[str] = set()


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
        # Legacy project databases created before newer schema versions are
        # migrated lazily on first access (idempotent per path; the re-entrant
        # connect() from migrate() skips the guard, so no recursion).
        if self.path not in _migrated_paths:
            _migrated_paths.add(self.path)
            self.migrate()
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
            result["policy"] = _parse_policy_json(result.pop("policy_json", None))
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
            result["recent_runs"] = []
            for row in connection.execute(
                "SELECT id, chapter_number, status, stage, attempt, error_code, "
                "review_json, updated_at FROM runs ORDER BY created_at DESC LIMIT 10"
            ):
                item = dict(row)
                item["review"] = _parse_review_history(item.pop("review_json"))
                result["recent_runs"].append(item)
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

    def recent_deltas(self, before_chapter: int, limit: int = 3) -> list[dict[str, Any]]:
        """Latest committed chapter deltas (structured core info) before N.

        Each delta carries character_changes / hooks_status / twist extracted
        from the actual prose — the continuation mechanism reads these instead
        of only a 500-char digest, so character relationship changes and hook
        state survive across chapters (the user's original design).
        """
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT delta_json FROM chapter_deltas
                WHERE chapter_number < ?
                ORDER BY chapter_number DESC LIMIT ?
                """,
                (before_chapter, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                data = json.loads(str(row["delta_json"]))
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            result.append(
                {
                    "chapter_number": data.get("chapter_number"),
                    "character_changes": data.get("character_changes") or [],
                    "hooks_status": data.get("hooks_status") or [],
                    "twist": data.get("twist") or "",
                    "next_chapter_hook": data.get("next_chapter_hook") or data.get("handoff") or "",
                }
            )
        return result

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
        result["review"] = _parse_review_history(result.pop("review_json", None))
        return result

    def append_review_verdict(self, run_id: str, record: dict[str, Any]) -> None:
        """Append one ReviewVerdict record to the run's review history."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT review_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            history = _parse_review_history(row["review_json"]) if row else []
            history.append(record)
            connection.execute(
                "UPDATE runs SET review_json = ?, updated_at = ? WHERE id = ?",
                (canonical_json(history), utc_now(), run_id),
            )

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

    def save_story_spine(self, story_spine: dict[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE projects SET story_spine_json = ?, updated_at = ?",
                (canonical_json(story_spine), utc_now()),
            )

    def project_policy(self) -> dict[str, Any]:
        """Stored raw policy dict; empty when the project never set one."""
        self.ensure_exists()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT policy_json FROM projects LIMIT 1"
            ).fetchone()
        return _parse_policy_json(row["policy_json"] if row else None)

    def save_policy(self, policy: dict[str, Any]) -> None:
        """Persist the effective policy object (first set or later override)."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE projects SET policy_json = ?, updated_at = ?",
                (canonical_json(policy), utc_now()),
            )

    def committed_chapter_numbers(self) -> list[int]:
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_number FROM chapters WHERE status = 'COMMITTED' "
                "ORDER BY chapter_number"
            ).fetchall()
        return [int(row["chapter_number"]) for row in rows]

    def uncommit_chapter(self, chapter_number: int) -> None:
        """Roll a COMMITTED chapter back to PREPARED so a later autorun will
        re-draft it. Revisions/deltas history is preserved; the chapter's
        canon_commit entry is removed because canon_commits has a UNIQUE
        constraint on chapter_number and the re-commit would otherwise fail.

        Used by the master agent's force-rewrite path: reverify may flag a
        committed chapter as below threshold, and since the orchestrator never
        re-runs committed chapters, the master agent uncommits it and submits
        a fresh autorun to get a rewrite.
        """
        self.ensure_exists()
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM chapters WHERE chapter_number = ?",
                (chapter_number,),
            ).fetchone()
            if row is None:
                raise ChapterNotFoundError(f"chapter {chapter_number} was not found")
            if row["status"] != "COMMITTED":
                return  # idempotent: nothing committed to roll back
            connection.execute(
                """
                UPDATE chapters SET status = 'PREPARED', digest = '',
                    finalized_revision_id = NULL, context_package_id = NULL,
                    updated_at = ?
                WHERE chapter_number = ?
                """,
                (now, chapter_number),
            )
            # Drop the old canon_commit row (UNIQUE on chapter_number) so the
            # fresh commit can insert a new one. History lives on in revisions
            # and chapter_deltas; this only frees the committed-slot.
            connection.execute(
                "DELETE FROM canon_commits WHERE chapter_number = ?",
                (chapter_number,),
            )

    def chapter_overview(self) -> list[dict[str, Any]]:
        """chapter_number/status/title rows only — no digest, no prose."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_number, status, title FROM chapters "
                "ORDER BY chapter_number"
            ).fetchall()
        return [dict(row) for row in rows]

    def attempted_chapter_numbers(self) -> list[int]:
        """Chapters with at least one run (committed or not)."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT chapter_number FROM runs ORDER BY chapter_number"
            ).fetchall()
        return [int(row["chapter_number"]) for row in rows]

    def chapter_attempts(self) -> dict[int, int]:
        """Highest run attempt per chapter (0 when never attempted)."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_number, MAX(attempt) AS max_attempt FROM runs "
                "GROUP BY chapter_number"
            ).fetchall()
        return {int(row["chapter_number"]): int(row["max_attempt"] or 0) for row in rows}

    def chapter_word_counts(self) -> dict[int, int]:
        """Character count of the finalized content per committed chapter."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chapter_number AS chapter_number, LENGTH(r.content) AS chars
                FROM chapters c
                JOIN revisions r ON r.id = c.finalized_revision_id
                WHERE c.status = 'COMMITTED'
                """
            ).fetchall()
        return {
            int(row["chapter_number"]): int(row["chars"] or 0)
            for row in rows
        }

    def latest_run_by_chapter(self) -> dict[int, dict[str, Any]]:
        """Most recent run summary per chapter (for rework-queue reasons)."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_number, status, attempt, error_code, error_message "
                "FROM runs ORDER BY rowid ASC"
            ).fetchall()
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            latest[int(row["chapter_number"])] = dict(row)
        return latest

    def blocking_issues_for_run(self, run_id: str) -> list[dict[str, str]]:
        """Blocking issues ({type, description, severity}) recorded for one run.

        Feeds the revision feedback loop: only blocker/error severity issues are
        returned, in insertion order, with descriptions bounded for prompting.
        """
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT issue_json FROM review_issues WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        result: list[dict[str, str]] = []
        for row in rows:
            try:
                payload = json.loads(row["issue_json"])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            severity = str(payload.get("severity", "warning"))
            if severity not in {"blocker", "error"}:
                continue
            result.append(
                {
                    "type": str(payload.get("issue_type", "unknown")),
                    "description": str(payload.get("instruction", ""))[:800],
                    "severity": severity,
                }
            )
        return result

    def latest_review_by_chapter(self) -> dict[int, dict[str, Any]]:
        """Most recent review record per chapter across all runs."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_number, review_json FROM runs "
                "WHERE review_json IS NOT NULL AND review_json != '' "
                "ORDER BY created_at ASC"
            ).fetchall()
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            history = _parse_review_history(row["review_json"])
            if history:
                latest[int(row["chapter_number"])] = history[-1]
        return latest

    def quality_event_summary(self) -> list[dict[str, Any]]:
        """Per-chapter quality issue counts grouped by severity and type."""
        self.ensure_exists()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT chapter_number, issue_json
                FROM review_issues ORDER BY chapter_number
                """
            ).fetchall()
        summary: dict[int, dict[str, Any]] = {}
        for row in rows:
            chapter = int(row["chapter_number"])
            entry = summary.setdefault(
                chapter,
                {"chapter_number": chapter, "blocker": 0, "error": 0, "warning": 0, "types": {}},
            )
            try:
                payload = json.loads(row["issue_json"])
                severity = str(payload.get("severity", "warning"))
                issue_type = str(payload.get("issue_type", "unknown"))
            except ValueError:
                severity, issue_type = "warning", "unknown"
            if severity in entry:
                entry[severity] += 1
            entry["types"][issue_type] = entry["types"].get(issue_type, 0) + 1
        return [summary[key] for key in sorted(summary)]

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
