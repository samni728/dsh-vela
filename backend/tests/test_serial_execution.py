from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.domain import ChapterContract
from dsh_novel.providers import DeterministicFakeProvider, serialize_provider
from dsh_novel.providers.base import OutlineRequest
from dsh_novel.transports.http import create_app


class ConcurrencyProbeProvider(DeterministicFakeProvider):
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def generate_outline(self, request: OutlineRequest):  # type: ignore[no-untyped-def]
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.08)
            return super().generate_outline(request)
        finally:
            with self.guard:
                self.active -= 1


class BlockingOutlineProvider(DeterministicFakeProvider):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate_outline(self, request: OutlineRequest):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.entered.set()
        assert self.release.wait(3), "test did not release outline generation"
        return super().generate_outline(request)


class CountingProvider(DeterministicFakeProvider):
    def __init__(self) -> None:
        self.chapter_calls = 0

    def generate_chapter(self, request):  # type: ignore[no-untyped-def]
        self.chapter_calls += 1
        return super().generate_chapter(request)


def test_provider_proxy_allows_only_one_model_call() -> None:
    raw = ConcurrencyProbeProvider()
    provider = serialize_provider(raw)
    request = OutlineRequest(title="串行测试", target_chapters=1)
    threads = [
        threading.Thread(target=provider.generate_outline, args=(request,))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert raw.max_active == 1
    assert provider.snapshot() == {
        "mode": "serial",
        "max_concurrency": 1,
        "active_operation": None,
        "waiting_calls": 0,
    }


def test_auto_returns_fast_and_duplicate_submission_reuses_project(
    tmp_path: Path,
) -> None:
    raw = BlockingOutlineProvider()
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000), raw
    )
    body: dict[str, Any] = {
        "title": "同一请求只创建一次",
        "premise": "验证 Harness 超时重试不会复制任务。",
        "target_chapters": 1,
    }
    with TestClient(app) as client:
        started_at = time.monotonic()
        first = client.post("/api/v1/auto", json=body)
        elapsed = time.monotonic() - started_at
        assert first.status_code == 200, first.text
        assert elapsed < 0.5
        assert raw.entered.wait(1)

        duplicate = client.post("/api/v1/auto", json=body)
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["project_id"] == first.json()["project_id"]
        assert duplicate.json()["result"]["reused"] is True
        assert raw.calls == 1

        raw.release.set()


def test_only_one_project_can_own_autorun_lane(tmp_path: Path) -> None:
    raw = BlockingOutlineProvider()
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000), raw
    )
    with TestClient(app) as client:
        for project_id in ("serial_one", "serial_two"):
            created = client.post(
                "/api/v1/projects",
                json={
                    "project_id": project_id,
                    "title": project_id,
                    "target_chapters": 1,
                },
            )
            assert created.status_code == 200

        first = client.post("/api/v1/projects/serial_one/autorun", json={})
        assert first.status_code == 200
        assert raw.entered.wait(1)

        second = client.post("/api/v1/projects/serial_two/autorun", json={})
        assert second.status_code == 409
        error = second.json()["error"]
        assert error["code"] == "ORCHESTRATOR_BUSY"
        assert error["details"]["active_project_id"] == "serial_one"

        manual = client.post(
            "/api/v1/projects/serial_two/chapters/1/run", json={}
        )
        assert manual.status_code == 409
        assert manual.json()["error"]["details"]["action"] == "poll_status"

        raw.release.set()


def test_restart_marks_stale_running_run_as_recoverable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, context_token_budget=5000)
    first_app = create_app(settings, DeterministicFakeProvider())
    with TestClient(first_app) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "restart_state",
                "title": "恢复测试",
                "target_chapters": 1,
            },
        )
        assert created.status_code == 200

    service = first_app.state.service
    db = service.database("restart_state")
    contract, context = service.prepare_chapter(
        "restart_state",
        1,
        ChapterContract(chapter_number=1, title="中断章", purpose="验证恢复"),
    )
    run, _created = db.create_run(contract, context.package_id, "restart-run-once")
    db.update_run(run["id"], status="RUNNING", stage="DRAFTING")

    restarted_app = create_app(settings, DeterministicFakeProvider())
    with TestClient(restarted_app) as client:
        health = client.get("/health").json()
        assert health["recovered_interrupted_runs"] == 1
        status = client.get(f"/api/v1/runs/{run['id']}").json()["result"]
        assert status["status"] == "FAILED_RETRYABLE"
        assert status["error_code"] == "PROCESS_INTERRUPTED"


def test_resume_of_running_run_is_status_only(tmp_path: Path) -> None:
    raw = CountingProvider()
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000), raw
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "running_resume",
                "title": "运行态不可重入",
                "target_chapters": 1,
            },
        )
        assert created.status_code == 200
        service = app.state.service
        db = service.database("running_resume")
        contract, context = service.prepare_chapter(
            "running_resume",
            1,
            ChapterContract(chapter_number=1, title="运行章", purpose="不可重入"),
        )
        run, _created = db.create_run(
            contract, context.package_id, "running-resume-once"
        )
        db.update_run(run["id"], status="RUNNING", stage="DRAFTING")

        resumed = client.post(f"/api/v1/runs/{run['id']}/resume", json={})
        assert resumed.status_code == 200
        assert resumed.json()["result"]["status"] == "RUNNING"
        assert raw.chapter_calls == 0
