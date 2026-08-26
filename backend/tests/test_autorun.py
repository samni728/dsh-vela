"""Tests for the 0.4.0 autorun orchestrator and the one-shot /auto entry."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dsh_novel.application import orchestrator as orchestrator_module
from dsh_novel.config import Settings
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


class SlowChapterProvider(DeterministicFakeProvider):
    name = "slow_chapter_test"

    def __init__(self, delay: float = 0.25) -> None:
        super().__init__()
        self.delay = delay

    def generate_chapter(self, request):  # type: ignore[no-untyped-def]
        time.sleep(self.delay)
        return super().generate_chapter(request)


class GatewayProvider(DeterministicFakeProvider):
    """Healthy for chapter 1, dead from chapter 2 on until re-enabled."""

    name = "gateway_test"

    def __init__(self) -> None:
        super().__init__()
        self.dead = True

    def generate_chapter(self, request):  # type: ignore[no-untyped-def]
        if self.dead and request.contract.chapter_number >= 2:
            raise RuntimeError("simulated endpoint outage")
        return super().generate_chapter(request)


def _make_client(
    tmp_path: Path, provider: Any = None
) -> tuple[TestClient, Any]:
    selected = provider or DeterministicFakeProvider()
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        selected,
    )
    return TestClient(app), selected


def _wait_for_terminal(
    client: TestClient, project_id: str, timeout: float = 30.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/projects/{project_id}/autorun")
        assert response.status_code == 200, response.text
        last = response.json()["result"]
        if last["state"] in {"completed", "failed", "completed_with_rework"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"autorun did not terminate in time: {last}")


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator_module, "RESUME_BACKOFF_SECONDS", (0.02, 0.04, 0.08)
    )


# ---------------------------------------------------------------------------
# happy path via POST /api/v1/auto
# ---------------------------------------------------------------------------


def test_auto_entry_runs_three_chapters_and_writes_artifacts(tmp_path: Path) -> None:
    with _make_client(tmp_path)[0] as client:
        created = client.post(
            "/api/v1/auto",
            json={
                "title": "雾港档案",
                "premise": "一名档案员发现城市每晚都会忘记一个人。",
                "target_chapters": 3,
                "hard_rules": ["每章必须推进一次调查"],
                "target_words": 1500,
            },
        )
        assert created.status_code == 200, created.text
        payload = created.json()["result"]
        project_id = payload["project_id"]
        assert payload["state"] in {"running", "completed"}

        final = _wait_for_terminal(client, project_id)
        assert final["state"] == "completed"
        assert final["chapters_committed"] == 3
        assert [score["chapter"] for score in final["scores"]] == [1, 2, 3]
        for score in final["scores"]:
            assert score["verdict"] == "pass"
            assert score["scores"]["contract_adherence"] >= 8.0

        project_dir = tmp_path / "data" / "projects" / project_id
        manuscript = (project_dir / "manuscript.md").read_text(encoding="utf-8")
        for marker in ("第1章", "第2章", "第3章"):
            assert marker in manuscript

        readme = (project_dir / "README.md").read_text(encoding="utf-8")
        assert "每章分数表" in readme
        assert "质量事件摘要" in readme
        for row_marker in ("| 1 |", "| 2 |", "| 3 |"):
            assert row_marker in readme

        report = client.get(f"/api/v1/projects/{project_id}/report")
        assert report.status_code == 200
        assert "每章分数表" in report.json()["result"]["content"]

        # Re-POST after completion stays completed (idempotent self-heal).
        again = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert again.status_code == 200
        assert again.json()["result"]["state"] == "completed"


# ---------------------------------------------------------------------------
# concurrency guard and range validation
# ---------------------------------------------------------------------------


def test_second_autorun_while_running_returns_409(tmp_path: Path) -> None:
    client, _provider = _make_client(tmp_path, SlowChapterProvider())
    with client:
        created = client.post(
            "/api/v1/projects",
            json={"project_id": "busy_proj", "title": "雾港档案", "target_chapters": 3},
        )
        project_id = created.json()["result"]["id"]
        outlined = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert outlined.status_code == 200

        started = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert started.status_code == 200
        busy = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert busy.status_code == 409
        assert busy.json()["error"]["code"] == "ORCHESTRATOR_BUSY"

        _wait_for_terminal(client, project_id)


def test_autorun_range_validation(tmp_path: Path) -> None:
    with _make_client(tmp_path)[0] as client:
        created = client.post(
            "/api/v1/projects",
            json={"project_id": "range_proj", "title": "雾港档案", "target_chapters": 3},
        )
        project_id = created.json()["result"]["id"]
        bad = client.post(
            f"/api/v1/projects/{project_id}/autorun", json={"to_chapter": 9}
        )
        assert bad.status_code == 409
        assert bad.json()["error"]["code"] == "CONFIG_INVALID"

        inverted = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"from_chapter": 3, "to_chapter": 2},
        )
        assert inverted.status_code == 409
        assert inverted.json()["error"]["code"] == "CONFIG_INVALID"


def test_report_before_completion_returns_404(tmp_path: Path) -> None:
    with _make_client(tmp_path)[0] as client:
        created = client.post(
            "/api/v1/projects",
            json={"project_id": "reportless", "title": "雾港档案", "target_chapters": 2},
        )
        project_id = created.json()["result"]["id"]
        missing = client.get(f"/api/v1/projects/{project_id}/report")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "REPORT_NOT_FOUND"


# ---------------------------------------------------------------------------
# failure breakpoint + self-healing resume
# ---------------------------------------------------------------------------


def test_dead_endpoint_skips_to_rework_then_self_heals(tmp_path: Path) -> None:
    """0.5.0 default policy (skip_continue): chapters lost to a dead endpoint
    land in the rework queue, the run finishes completed_with_rework, and a
    re-POST retries the rework queue first before any new chapter."""
    provider = GatewayProvider()
    client, gateway = _make_client(tmp_path, provider)
    with client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "heal_proj",
                "title": "雾港档案",
                "premise": "断点续跑测试。",
                "target_chapters": 3,
            },
        )
        project_id = created.json()["result"]["id"]
        outlined = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert outlined.status_code == 200, outlined.text

        started = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert started.status_code == 200

        terminal = _wait_for_terminal(client, project_id)
        assert terminal["state"] == "completed_with_rework"
        assert terminal["rework_queue"] == [2, 3]
        assert terminal["chapters_committed"] == 1
        assert terminal["last_error"] is None

        # The committed first chapter is intact.
        project = client.get(f"/api/v1/projects/{project_id}").json()["result"]
        assert project["chapters_committed"] == 1

        # Service recovers: re-POST retries the rework queue first and completes.
        gateway.dead = False
        resumed = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert resumed.status_code == 200, resumed.text

        completed = _wait_for_terminal(client, project_id)
        assert completed["state"] == "completed"
        assert completed["rework_queue"] == []
        assert completed["chapters_committed"] == 3

        project_dir = tmp_path / "data" / "projects" / project_id
        manuscript = (project_dir / "manuscript.md").read_text(encoding="utf-8")
        assert "第3章" in manuscript
        readme = (project_dir / "README.md").read_text(encoding="utf-8")
        assert "| 3 |" in readme
        assert "补写队列" in readme


def test_missing_contract_is_generated_midrun(tmp_path: Path) -> None:
    """No outline up front: the orchestrator must LLM-generate contracts."""
    with _make_client(tmp_path)[0] as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "cold_start",
                "title": "夜航图书馆",
                "premise": "每当钟声敲响，借阅记录就会改写一次。",
                "target_chapters": 2,
            },
        )
        project_id = created.json()["result"]["id"]
        started = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert started.status_code == 200, started.text

        final = _wait_for_terminal(client, project_id)
        assert final["state"] == "completed"
        assert final["chapters_committed"] == 2
