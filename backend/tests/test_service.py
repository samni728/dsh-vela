from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dsh_novel.api_models import ProjectCreateRequest
from dsh_novel.application import NovelService
from dsh_novel.config import Settings
from dsh_novel.providers.fake import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


def make_service(tmp_path: Path, *, budget: int = 20_000) -> NovelService:
    return NovelService(
        projects_root=tmp_path / "projects",
        provider=DeterministicFakeProvider(),
        context_token_budget=budget,
    )


def test_service_can_create_prepare_run_and_export(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    project = service.create_project(
        ProjectCreateRequest(
            title="雾港档案",
            premise="一名档案员发现城市每晚都会忘记一个人。",
            target_chapters=5,
            hard_rules=["每章必须推进一次调查"],
            story_spine={"acts": ["开端", "升级", "回收"]},
        )
    )

    contract, package = service.prepare_chapter(project["id"], 1, None)
    assert contract.chapter_number == 1
    assert package.estimated_tokens <= package.token_budget
    assert package.blocks

    run = service.run_chapter(
        project_id=project["id"],
        chapter_number=1,
        supplied_contract=None,
        idempotency_key="chapter-1-test-key",
    )
    assert run["status"] == "COMMITTED"
    assert run["commit"]["chapter_number"] == 1
    assert run["content"]
    assert run["content"].startswith("# 第1章")

    status = service.project_status(project["id"])
    assert status["chapters_committed"] == 1
    assert status["chapters"][0]["status"] == "COMMITTED"

    exported = service.export(project["id"], "markdown")
    assert "# 第1章" in exported["content"]
    assert exported["sha256"]


def test_http_app_exposes_the_same_flow(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    client = TestClient(create_app(settings, DeterministicFakeProvider()))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    created = client.post(
        "/api/v1/projects",
        json={
            "title": "夜航图书馆",
            "premise": "每当钟声敲响，借阅记录就会改写一次。",
            "target_chapters": 3,
        },
    )
    assert created.status_code == 200
    project_id = created.json()["result"]["id"]

    status = client.get(f"/api/v1/projects/{project_id}")
    assert status.status_code == 200
    assert status.json()["result"]["title"] == "夜航图书馆"

    run = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
    assert run.status_code == 200
    assert run.json()["ok"] is True
    assert run.json()["result"]["status"] == "COMMITTED"

    export = client.post(f"/api/v1/projects/{project_id}/export", json={"format": "markdown"})
    assert export.status_code == 200
    assert export.json()["result"]["content"].startswith("# 第1章")
