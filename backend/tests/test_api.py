from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.transports.http import create_app


def create_project(client: TestClient, project_id: str = "test_project") -> str:
    response = client.post(
        "/api/v1/projects",
        json={
            "project_id": project_id,
            "title": "雾港来信",
            "premise": "一封迟到十年的信改变了港城的秩序。",
            "target_chapters": 5,
            "hard_rules": ["秘密必须通过行动揭示"],
            "story_spine": {
                "central_conflict": "追查旧案与保护当下之间的冲突",
                "ending_constraint": "主角必须公开真相",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    return response.json()["result"]["id"]


def test_health_and_capability_handshake(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["protocol_version"] == "1.0"

    capabilities = client.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["capabilities"] == [
        "project.create",
        "project.status",
        "chapter.run",
        "run.status",
        "run.resume",
        "manuscript.export",
    ]
    assert capabilities.json()["optional_capabilities"] == {
        "embedding": False,
        "rerank": False,
        "llm_review": True,
        "outline": True,
        "autorun": True,
    }


def test_project_run_status_and_export(client: TestClient, data_dir) -> None:
    project_id = create_project(client)
    run = client.post(
        f"/api/v1/projects/{project_id}/chapters/1/run",
        json={
            "idempotency_key": "chapter-1-once",
            "contract": {
                "chapter_number": 1,
                "title": "潮痕",
                "purpose": "主角收到迟到的信",
                "required_events": ["发现信封上的旧印章"],
                "handoff": "主角决定前往旧码头。",
            },
        },
    )
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["ok"] is True
    assert payload["result"]["status"] == "COMMITTED"
    assert payload["result"]["commit"]["canon_version"] == 1
    assert "发现信封上的旧印章" in payload["result"]["content"]
    run_id = payload["run_id"]

    replay = client.post(
        f"/api/v1/projects/{project_id}/chapters/1/run",
        json={"idempotency_key": "chapter-1-once"},
    )
    assert replay.status_code == 200
    assert replay.json()["run_id"] == run_id

    status = client.get(f"/api/v1/runs/{run_id}")
    assert status.status_code == 200
    assert status.json()["result"]["stage"] == "COMMITTED"

    project = client.get(f"/api/v1/projects/{project_id}")
    assert project.json()["result"]["chapters_committed"] == 1

    exported = client.post(
        f"/api/v1/projects/{project_id}/export", json={"format": "markdown"}
    )
    assert exported.status_code == 200
    assert exported.json()["result"]["format"] == "markdown"
    assert "潮痕" in exported.json()["result"]["content"]

    database = data_dir / "projects" / project_id / "novel.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM revisions WHERE status = 'FINALIZED'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM chapter_deltas WHERE status = 'CONFIRMED'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM canon_commits").fetchone()[0] == 1


def test_context_uses_only_three_recent_chapters(client: TestClient) -> None:
    project_id = create_project(client, "bounded_context")
    for chapter in range(1, 6):
        response = client.post(
            f"/api/v1/projects/{project_id}/chapters/{chapter}/run",
            json={"idempotency_key": f"bounded-chapter-{chapter}"},
        )
        assert response.status_code == 200, response.text

    prepared = client.post(
        f"/api/v1/projects/{project_id}/chapters/6/prepare", json={}
    )
    assert prepared.status_code == 200
    context = prepared.json()["result"]["context"]
    assert context["estimated_tokens"] <= context["token_budget"]
    assert "chapter:2/digest" not in context["provenance"]
    assert context["provenance"][-3:] == [
        "chapter:3/digest",
        "chapter:4/digest",
        "chapter:5/digest",
    ]


def test_validation_error_uses_protocol_envelope(client: TestClient) -> None:
    response = client.post("/api/v1/projects", json={"title": ""})
    assert response.status_code == 422
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_optional_bearer_token_protects_api(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "auth", auth_token="local-secret"))
    with TestClient(app) as protected:
        assert protected.get("/health").status_code == 200

        denied = protected.get("/api/v1/capabilities")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_REQUIRED"

        allowed = protected.get(
            "/api/v1/capabilities",
            headers={"Authorization": "Bearer local-secret"},
        )
        assert allowed.status_code == 200
        assert "chapter.run" in allowed.json()["capabilities"]
