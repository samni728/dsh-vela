"""Tests for the 0.5.0 per-project policy object: migration v3, persistence,
merge precedence (request > stored > settings) and effective behavior."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dsh_novel.application import orchestrator as orchestrator_module
from dsh_novel.application.policy import merge_policy, normalize_policy
from dsh_novel.config import Settings
from dsh_novel.errors import ConfigInvalidError
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


def _make_client(tmp_path: Path, **settings: object) -> TestClient:
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            context_token_budget=5000,
            **settings,  # type: ignore[arg-type]
        ),
        DeterministicFakeProvider(),
    )
    return TestClient(app)


def _create_project(client: TestClient, project_id: str, target: int = 1) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "title": "雾港档案", "target_chapters": target},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["id"]


def _wait_terminal(client: TestClient, project_id: str) -> dict[str, Any]:
    deadline = time.time() + 30.0
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/projects/{project_id}/autorun")
        assert response.status_code == 200, response.text
        last = response.json()["result"]
        if last["state"] in {"completed", "failed", "completed_with_rework"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"autorun did not terminate in time: {last}")


def _stored_policy(tmp_path: Path, project_id: str) -> dict[str, Any]:
    database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT policy_json FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row[0]) if row[0] else {}


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator_module, "RESUME_BACKOFF_SECONDS", (0.02, 0.04, 0.08)
    )


# ---------------------------------------------------------------------------
# migration v3: policy_json column, backward compatible
# ---------------------------------------------------------------------------


def test_policy_migration_upgrades_legacy_database(tmp_path: Path) -> None:
    """A pre-0.5.0 database (schema version 1) gains review_json AND policy_json
    in place without losing rows."""
    from dsh_novel.infrastructure.database import MIGRATIONS, ProjectDatabase

    projects_root = tmp_path / "projects"
    legacy_dir = projects_root / "legacy_policy"
    legacy_dir.mkdir(parents=True)
    connection = sqlite3.connect(legacy_dir / "novel.sqlite3")
    try:
        connection.executescript(MIGRATIONS[1])
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
        )
        connection.execute(
            """
            INSERT INTO projects(
                id, title, premise, target_chapters, hard_rules_json,
                story_spine_json, created_at, updated_at
            ) VALUES ('legacy_policy', '旧项目', '', 2, '[]', '{}', 'legacy', 'legacy')
            """
        )
        connection.commit()
    finally:
        connection.close()

    db = ProjectDatabase(projects_root, "legacy_policy")
    db.migrate()  # applies migrations 2 and 3

    raw = sqlite3.connect(legacy_dir / "novel.sqlite3")
    try:
        columns = {
            row[1] for row in raw.execute("PRAGMA table_info(projects)").fetchall()
        }
        row = raw.execute(
            "SELECT title FROM projects WHERE id = 'legacy_policy'"
        ).fetchone()
    finally:
        raw.close()
    assert "policy_json" in columns
    assert row == ("旧项目",)

    # Stored policy round-trips through the new column.
    assert db.project_policy() == {}
    db.save_policy({"score_threshold": 9.5, "on_chapter_failure": "pause"})
    assert db.project_policy()["score_threshold"] == 9.5
    assert db.project()["policy"]["on_chapter_failure"] == "pause"


# ---------------------------------------------------------------------------
# merge precedence: request > stored > settings defaults
# ---------------------------------------------------------------------------


def test_merge_and_normalize_policy_units() -> None:
    defaults = {
        "score_threshold": 7.5,
        "max_revisions": 3,
        "target_words": 4000,
        "on_chapter_failure": "skip_continue",
    }
    merged = merge_policy(
        request={"score_threshold": 9.9},
        stored={"score_threshold": 8.8, "max_revisions": 2},
        defaults=defaults,
    )
    assert merged == {
        "score_threshold": 9.9,  # request wins
        "max_revisions": 2,  # stored wins over settings default
        "target_words": 4000,
        "on_chapter_failure": "skip_continue",
    }
    assert merge_policy(request=None, stored=None, defaults=defaults) == defaults

    assert normalize_policy(None) == {}
    with pytest.raises(ConfigInvalidError):
        normalize_policy({"unknown_key": 1})
    with pytest.raises(ConfigInvalidError):
        normalize_policy({"on_chapter_failure": "bogus"})
    with pytest.raises(ConfigInvalidError):
        normalize_policy({"score_threshold": 11})


def test_settings_defaults_surface_in_pipeline_without_autorun(
    tmp_path: Path,
) -> None:
    with _make_client(tmp_path, score_threshold=7.5) as client:
        project_id = _create_project(client, "defaults_proj")
        result = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]
        assert result["policy"] == {
            "score_threshold": 7.5,
            "max_revisions": 3,
            "target_words": 4000,
            "on_chapter_failure": "skip_continue",
        }


def test_request_policy_persists_then_merges_with_stored(tmp_path: Path) -> None:
    with _make_client(tmp_path, score_threshold=7.5) as client:
        project_id = _create_project(client, "merge_proj")

        # First set: the request policy is persisted over settings defaults.
        started = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"score_threshold": 9.9}},
        )
        assert started.status_code == 200, started.text
        _wait_terminal(client, project_id)

        pipeline = (
            client.get(f"/api/v1/projects/{project_id}/pipeline").json()["result"]
        )
        assert pipeline["policy"]["score_threshold"] == 9.9
        assert _stored_policy(tmp_path, project_id)["score_threshold"] == 9.9

        # Second run adds one key: stored score_threshold survives the merge
        # and beats the settings default of 7.5.
        again = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"max_revisions": 2}},
        )
        assert again.status_code == 200, again.text
        _wait_terminal(client, project_id)
        policy = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]["policy"]
        assert policy["score_threshold"] == 9.9
        assert policy["max_revisions"] == 2

        # A new request value beats the stored one.
        third = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"score_threshold": 8.8}},
        )
        assert third.status_code == 200, third.text
        _wait_terminal(client, project_id)
        policy = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]["policy"]
        assert policy["score_threshold"] == 8.8
        assert policy["max_revisions"] == 2


def test_auto_create_persists_policy_and_feeds_outline(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={
                "title": "雾港档案",
                "premise": "策略对象测试。",
                "target_chapters": 1,
                "policy": {"score_threshold": 9.9, "target_words": 1200},
            },
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]
        _wait_terminal(client, project_id)

        policy = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]["policy"]
        assert policy["score_threshold"] == 9.9
        assert policy["target_words"] == 1200
        assert _stored_policy(tmp_path, project_id)["target_words"] == 1200

        # The outline agent received target_words from the policy.
        database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(database) as connection:
            contract_json = connection.execute(
                "SELECT contract_json FROM chapter_contracts WHERE chapter_number = 1"
            ).fetchone()[0]
        assert json.loads(contract_json)["target_words"] == 1200


# ---------------------------------------------------------------------------
# effective policy drives service/orchestrator/reviewer behavior
# ---------------------------------------------------------------------------


def test_effective_policy_drives_threshold_and_retry_budget(
    tmp_path: Path,
) -> None:
    """score_threshold=9.9 blocks the 8.0 fake drafts; max_revisions=2 exhausts
    after two attempts (the settings default of 3 would have allowed a third)."""
    with _make_client(tmp_path) as client:
        project_id = _create_project(client, "effective_proj")
        started = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={
                "policy": {
                    "score_threshold": 9.9,
                    "max_revisions": 2,
                    "on_chapter_failure": "pause",
                }
            },
        )
        assert started.status_code == 200, started.text

        terminal = _wait_terminal(client, project_id)
        assert terminal["state"] == "failed"
        assert terminal["failed_at_chapter"] == 1

        # Exactly two review attempts are recorded for the chapter's run.
        database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(database) as connection:
            review_json = connection.execute(
                "SELECT review_json FROM runs WHERE chapter_number = 1 "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
            attempt, status = connection.execute(
                "SELECT MAX(attempt), status FROM runs WHERE chapter_number = 1"
            ).fetchone()
        history = json.loads(review_json)
        assert [record["attempt"] for record in history] == [1, 2]
        assert attempt == 2
        assert status == "PAUSED"


def test_invalid_policy_requests_are_rejected(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        project_id = _create_project(client, "invalid_policy_proj")

        bad_mode = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"on_chapter_failure": "bogus"}},
        )
        assert bad_mode.status_code == 422

        bad_threshold = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"score_threshold": 12}},
        )
        assert bad_threshold.status_code == 422

        unknown_key = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"nonsense": True}},
        )
        assert unknown_key.status_code == 422

        auto_bad = client.post(
            "/api/v1/auto",
            json={"title": "x", "policy": {"max_revisions": 0}},
        )
        assert auto_bad.status_code == 422
