"""Tests for the 0.4.0 outline agent: endpoint, persistence and parsing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.providers import (
    DeterministicFakeProvider,
    OpenAICompatibleProvider,
    OutlineRequest,
    parse_outline_payload,
)
from dsh_novel.transports.http import create_app


def _make_client(tmp_path: Path, provider=None) -> TestClient:  # type: ignore[no-untyped-def]
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        provider or DeterministicFakeProvider(),
    )
    return TestClient(app)


def _create_project(client: TestClient, project_id: str = "outline_proj") -> str:
    response = client.post(
        "/api/v1/projects",
        json={
            "project_id": project_id,
            "title": "雾港档案",
            "premise": "一名档案员发现城市每晚都会忘记一个人。",
            "target_chapters": 4,
            "hard_rules": ["每章必须推进一次调查"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["id"]


# ---------------------------------------------------------------------------
# endpoint + persistence
# ---------------------------------------------------------------------------


def test_outline_endpoint_generates_and_persists_contracts(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        project_id = _create_project(client)
        response = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert response.status_code == 200, response.text
        result = response.json()["result"]

        numbers = [chapter["chapter_number"] for chapter in result["chapters"]]
        assert numbers == [1, 2, 3, 4]
        assert result["story_spine"]["central_conflict"]
        for chapter in result["chapters"]:
            assert chapter["title"]
            assert chapter["purpose"]
            assert isinstance(chapter["required_events"], list)
            assert chapter["target_words"] >= 100

        database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT chapter_number FROM chapter_contracts ORDER BY chapter_number"
            ).fetchall()
        assert [row[0] for row in rows] == [1, 2, 3, 4]

        status = client.get(f"/api/v1/projects/{project_id}")
        assert status.status_code == 200
        assert status.json()["result"]["story_spine"]["central_conflict"]
        assert status.json()["result"]["chapters_committed"] == 0


def test_outline_target_words_override_reaches_contracts(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        project_id = _create_project(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/outline", json={"target_words": 2500}
        )
        assert response.status_code == 200, response.text
        assert all(
            chapter["target_words"] == 2500
            for chapter in response.json()["result"]["chapters"]
        )
        prepared = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/prepare", json={}
        )
        assert prepared.status_code == 200
        assert prepared.json()["result"]["contract"]["target_words"] == 2500


def test_outline_regeneration_rejected_after_any_commit(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        project_id = _create_project(client)
        committed = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/run",
            json={
                "idempotency_key": "outline-guard-once",
                "contract": {
                    "chapter_number": 1,
                    "title": "潮痕",
                    "purpose": "主角收到迟到的信",
                },
            },
        )
        assert committed.status_code == 200, committed.text

        rejected = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "VERSION_CONFLICT"


def test_outline_missing_project_returns_404(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        response = client.post("/api/v1/projects/ghost_project/outline", json={})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


# ---------------------------------------------------------------------------
# fail-loud behaviour on broken providers
# ---------------------------------------------------------------------------


class GarbageOutlineProvider(DeterministicFakeProvider):
    name = "garbage_outline_test"

    def generate_outline(self, request: OutlineRequest):  # type: ignore[no-untyped-def]
        raise ValueError("model returned unparsable outline JSON")


class NoOutlineProvider(DeterministicFakeProvider):
    name = "no_outline_test"

    generate_outline = None  # type: ignore[assignment]


def test_outline_parse_failure_is_fail_loud_config_invalid(tmp_path: Path) -> None:
    with _make_client(tmp_path, GarbageOutlineProvider()) as client:
        project_id = _create_project(client)
        response = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert response.status_code == 409
        payload = response.json()
        assert payload["error"]["code"] == "CONFIG_INVALID"
        assert "unparsable" in str(payload["error"]["details"])
        # The project stays usable for manual runs.
        run = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert run.status_code == 200


def test_outline_unsupported_provider_reports_config_invalid(tmp_path: Path) -> None:
    with _make_client(tmp_path, NoOutlineProvider()) as client:
        project_id = _create_project(client)
        response = client.post(f"/api/v1/projects/{project_id}/outline", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFIG_INVALID"
        assert "does not support outline" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# strict payload parsing (openai path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "<think>只输出思考</think>",
        "这不是 JSON",
        '{"story_spine":{},"chapters":[{"chapter_number":"一","title":"t","purpose":"p"}]}',
        '{"story_spine":{},"chapters":[{"chapter_number":2,"title":"t","purpose":"p"}]}',
        '{"story_spine":{},"chapters":[{"chapter_number":1,"title":"","purpose":"p"}]}',
    ],
)
def test_parse_outline_payload_rejects_illegal_payloads(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_outline_payload(raw, target_chapters=1)


def test_parse_outline_payload_accepts_think_wrapped_valid_json() -> None:
    raw = (
        "<think>先想一下结构</think>\n"
        "```json\n"
        '{"story_spine":{"central_conflict":"c"},'
        '"chapters":[{"chapter_number":1,"title":"第1章","purpose":"开局",'
        '"required_events":["收到信"],"hooks_to_plant":["旧印章"],'
        '"hooks_to_advance":[],"target_words":3000}]}\n'
        "```\n"
    )
    outline = parse_outline_payload(raw, target_chapters=1)
    assert [chapter.chapter_number for chapter in outline.chapters] == [1]
    assert outline.story_spine["central_conflict"] == "c"


def test_openai_provider_retries_outline_once_then_fails() -> None:
    class BrokenChatProvider(OpenAICompatibleProvider):
        def __init__(self) -> None:
            super().__init__(
                endpoint="http://127.0.0.1:9/v1",
                model="local-writer",
                api_key=None,
                timeout_seconds=2,
                max_output_tokens=64,
            )
            self.calls = 0

        def _chat_completion(self, payload, timeout_seconds):  # type: ignore[no-untyped-def]
            self.calls += 1
            return "<think>想想</think>抱歉，我无法输出 JSON。"

    provider = BrokenChatProvider()
    request = OutlineRequest(title="雾港档案", target_chapters=3)
    with pytest.raises(RuntimeError) as excinfo:
        provider.generate_outline(request)
    assert provider.calls == 2, "outline generation must retry exactly once"
    assert "attempt 2" in str(excinfo.value)


def test_capabilities_report_outline_and_autorun(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["optional_capabilities"]["outline"] is True
        assert capabilities["optional_capabilities"]["autorun"] is True
