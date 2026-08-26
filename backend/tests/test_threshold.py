"""Tests for the 0.4.0 score-threshold review loop and review history."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.application.quality import event_keywords, inspect_chapter
from dsh_novel.config import ConfigError, Settings
from dsh_novel.domain import ChapterContract
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


def _create_project(client: TestClient, project_id: str = "threshold_proj") -> str:
    response = client.post(
        "/api/v1/projects",
        json={
            "project_id": project_id,
            "title": "雾港档案",
            "premise": "一名档案员发现城市每晚都会忘记一个人。",
            "target_chapters": 2,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["id"]


# ---------------------------------------------------------------------------
# threshold loop: block on low score, commit after rewrite
# ---------------------------------------------------------------------------


def test_low_first_draft_blocks_then_rewrite_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_REVIEW_SCORES", "6.5,9")
    with _make_client(tmp_path) as client:
        project_id = _create_project(client)

        first = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "QUALITY_GATE_BLOCKED"
        run_id = first.json()["run_id"]

        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["status"] == "QUALITY_BLOCKED"
        assert status["stage"] == "REVIEWING"
        assert len(status["review"]) == 1
        assert status["review"][0]["overall"] == 6.5

        database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT issue_json FROM review_issues WHERE run_id = ?", (run_id,)
            ).fetchall()
        assert any("score_below_threshold" in row[0] for row in rows)

        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        result = resumed.json()["result"]
        assert result["status"] == "COMMITTED"
        assert result["attempt"] == 2
        assert result["llm_review"]["overall"] == 9.0

        # Two verdict records persisted for the same run.
        final = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert len(final["review"]) == 2
        assert [record["overall"] for record in final["review"]] == [6.5, 9.0]
        assert [record["attempt"] for record in final["review"]] == [1, 2]

        # Project recent_runs carries the review field too.
        project = client.get(f"/api/v1/projects/{project_id}").json()["result"]
        recent = project["recent_runs"][0]
        assert recent["id"] == run_id
        assert len(recent["review"]) == 2


def test_threshold_exhaustion_pauses_run_with_final_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_REVIEW_SCORES", "6.0")
    with _make_client(tmp_path) as client:
        project_id = _create_project(client)
        run_id = ""
        for expected_attempt in (1, 2, 3):
            if expected_attempt == 1:
                response = client.post(
                    f"/api/v1/projects/{project_id}/chapters/1/run", json={}
                )
            else:
                response = client.post(f"/api/v1/runs/{run_id}/resume", json={})
            assert response.status_code == 409, response.text
            run_id = response.json()["run_id"]

        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["status"] == "PAUSED"
        assert status["error_code"] == "SCORE_THRESHOLD_NOT_MET"
        assert "6.0" in status["error_message"]
        assert "final scores" in status["error_message"]
        assert len(status["review"]) == 3

        # A paused run cannot be resumed further.
        again = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "RUN_STATE_INVALID"


def test_score_threshold_setting_changes_the_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default fake scores are exactly 8.0: below a raised threshold of 9.
    monkeypatch.delenv("FAKE_REVIEW_SCORES", raising=False)
    with _make_client(tmp_path, score_threshold=9.0) as client:
        project_id = _create_project(client)
        blocked = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert blocked.status_code == 409
        run_id = blocked.json()["run_id"]
        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["status"] == "QUALITY_BLOCKED"

        # Second draft clears the raised bar via the scripted env hook.
        monkeypatch.setenv("FAKE_REVIEW_SCORES", "9.5")
        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["result"]["status"] == "COMMITTED"


def test_max_revisions_setting_unifies_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_REVIEW_SCORES", "5.0")
    # max_revisions=2: initial run + one resume; the second resume must refuse.
    with _make_client(tmp_path, max_revisions=2) as client:
        project_id = _create_project(client)
        first = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert first.status_code == 409
        run_id = first.json()["run_id"]

        second = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert second.status_code == 409  # attempt 2 still below threshold
        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["attempt"] == 2
        assert status["status"] == "PAUSED"  # budget of 2 exhausted -> paused
        assert status["error_code"] == "SCORE_THRESHOLD_NOT_MET"


def test_new_config_keys_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yml"
    monkeypatch.setenv("DSH_NOVEL_CONFIG", str(config_path))
    for name in (
        "DSH_NOVEL_SCORE_THRESHOLD",
        "DSH_NOVEL_MAX_REVISIONS",
        "DSH_NOVEL_OUTLINE_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path.write_text(
        "score_threshold: 7.5\nmax_revisions: 4\noutline_timeout_seconds: 90\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.score_threshold == 7.5
    assert settings.max_revisions == 4
    assert settings.outline_timeout_seconds == 90.0

    monkeypatch.setenv("DSH_NOVEL_SCORE_THRESHOLD", "6.0")
    monkeypatch.setenv("DSH_NOVEL_MAX_REVISIONS", "2")
    monkeypatch.setenv("DSH_NOVEL_OUTLINE_TIMEOUT", "33")
    env_settings = Settings()
    assert env_settings.score_threshold == 6.0  # env wins over file
    assert env_settings.max_revisions == 2
    assert env_settings.outline_timeout_seconds == 33.0

    config_path.write_text("score_threshold: high\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        Settings()
    assert "'score_threshold'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# runs.review_json schema migration stays backward compatible
# ---------------------------------------------------------------------------


def test_review_json_migration_upgrades_legacy_database(tmp_path: Path) -> None:
    """A 0.3.0-era database (schema version 1, no review_json column) must
    upgrade in place without losing data."""
    import sqlite3

    from dsh_novel.infrastructure.database import MIGRATIONS, ProjectDatabase

    projects_root = tmp_path / "projects"
    legacy_dir = projects_root / "legacy_project"
    legacy_dir.mkdir(parents=True)
    connection = sqlite3.connect(legacy_dir / "novel.sqlite3")
    try:
        connection.executescript(MIGRATIONS[1])
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
        )
        connection.execute(
            """
            INSERT INTO runs(
                id, chapter_number, status, stage, idempotency_key, contract_json,
                created_at, updated_at
            ) VALUES ('run_legacy', 1, 'COMMITTED', 'COMMITTED', 'legacy-key', '{}',
                      'legacy', 'legacy')
            """
        )
        connection.commit()
    finally:
        connection.close()

    db = ProjectDatabase(projects_root, "legacy_project")
    db.migrate()  # must apply migration 2 without touching existing rows

    raw = sqlite3.connect(legacy_dir / "novel.sqlite3")
    try:
        columns = {
            row[1] for row in raw.execute("PRAGMA table_info(runs)").fetchall()
        }
        row = raw.execute(
            "SELECT status, chapter_number FROM runs WHERE id = 'run_legacy'"
        ).fetchone()
    finally:
        raw.close()
    assert "review_json" in columns
    assert row == ("COMMITTED", 1)

    # New writes land in the added column and surface through run().
    db.append_review_verdict(
        "run_legacy",
        {"attempt": 1, "verdict": "pass", "scores": None, "overall": None},
    )
    loaded = db.run("run_legacy")
    assert len(loaded["review"]) == 1
    assert loaded["review"][0]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# blueprint-aware deterministic rule: required_event_keyword_missing
# ---------------------------------------------------------------------------


def _contract(events: list[str]) -> ChapterContract:
    return ChapterContract(
        chapter_number=1,
        title="测试",
        purpose="验证关键词规则",
        required_events=events,
    )


def test_event_keywords_tokenizes_multi_clause_events() -> None:
    tokens = event_keywords("主角在钟楼顶与线人交接，密码本被调包。")
    # Chinese word tokens are approximated by character bigrams of each run.
    assert "主角" in tokens
    assert "钟楼" in tokens
    assert "线人" in tokens
    assert "密码" in tokens
    # English/number tokens stay whole (>=2 chars).
    assert "USB" in event_keywords("插入USB加密狗")


def test_required_event_keyword_missing_warns_without_blocking() -> None:
    content = (
        "夜色里，档案员把灯拧暗了一格。\n\n"
        "他翻出一份没有署名的卷宗，纸页边缘已经脆化。\n\n"
        "窗外传来轮船的汽笛声，他把卷宗重新锁回抽屉。"
    )
    issues = inspect_chapter(
        chapter_number=1,
        content=content,
        contract=_contract(["主角在钟楼顶与线人交接密码本"]),
        recent_chapters=[],
    )
    keyword_issues = [
        issue
        for issue in issues
        if issue.issue_type == "required_event_keyword_missing"
    ]
    assert len(keyword_issues) == 1
    assert keyword_issues[0].severity == "warning"
    assert keyword_issues[0].source == "rule"
    # The warning never blocks on its own.
    blockers = [issue for issue in issues if issue.severity in {"blocker", "error"}]
    assert not blockers


def test_required_event_keyword_present_stays_silent() -> None:
    content = (
        "钟楼顶的风很大，主角终于见到了等在那里的线人。\n\n"
        "交接只用了几秒钟，密码本从一只手换到另一只手。\n\n"
        "楼下有人吹了一声口哨，两人随即朝不同方向离开。"
    )
    issues = inspect_chapter(
        chapter_number=1,
        content=content,
        contract=_contract(["主角在钟楼顶与线人交接密码本"]),
        recent_chapters=[],
    )
    assert not [
        issue
        for issue in issues
        if issue.issue_type == "required_event_keyword_missing"
    ]
