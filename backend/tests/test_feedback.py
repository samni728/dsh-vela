"""Tests for the 0.5.0 revision feedback loop: blocking issues and previous
scores from an intercepted draft are injected into the next WriterRequest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.providers import DeterministicFakeProvider, WriterRequest
from dsh_novel.transports.http import create_app


class RecordingFake(DeterministicFakeProvider):
    """Fake provider that records every WriterRequest it receives."""

    name = "recording_fake_test"

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[WriterRequest] = []

    def generate_chapter(self, request: WriterRequest) -> str:
        self.requests.append(request)
        return super().generate_chapter(request)


class TruncatedOnceFake(DeterministicFakeProvider):
    """First draft lacks sentence-final punctuation (deterministic blocker)."""

    name = "truncated_once_test"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.requests: list[WriterRequest] = []

    def generate_chapter(self, request: WriterRequest) -> str:
        self.calls += 1
        self.requests.append(request)
        body = super().generate_chapter(request)
        if self.calls == 1:
            return body.rstrip("。！？…")
        return body


def _make_client(tmp_path: Path, provider: Any = None) -> TestClient:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        provider or DeterministicFakeProvider(),
    )
    return TestClient(app)


def _create_project(client: TestClient, project_id: str) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "title": "雾港档案", "target_chapters": 2},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["id"]


def test_score_blocked_resume_injects_feedback_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REVIEW_SCORES", "6.5,9")
    provider = RecordingFake()
    with _make_client(tmp_path, provider) as client:
        project_id = _create_project(client, "feedback_proj")

        blocked = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/run", json={}
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "QUALITY_GATE_BLOCKED"
        run_id = blocked.json()["run_id"]
        # First draft: no feedback at all.
        assert provider.requests[0].revision_feedback is None
        assert provider.requests[0].previous_scores is None

        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        result = resumed.json()["result"]
        assert result["status"] == "COMMITTED"

        # The rewrite request carried the blocking issue + previous scores.
        rewrite = provider.requests[1]
        assert rewrite.revision_feedback is not None
        assert [
            item["type"] for item in rewrite.revision_feedback
        ] == ["score_below_threshold"]
        assert all(
            set(item) == {"type", "description"} and item["description"]
            for item in rewrite.revision_feedback
        )
        assert rewrite.previous_scores == {
            "contract_adherence": 6.5,
            "era_authenticity": 6.5,
            "flow": 6.5,
        }

        # Visible marker at the end of the committed prose.
        content = result["content"]
        assert "[feedback:score_below_threshold]" in content
        assert content.rstrip().endswith("[feedback:score_below_threshold]。")


def test_deterministic_blocker_also_feeds_back_on_rewrite(tmp_path: Path) -> None:
    provider = TruncatedOnceFake()
    with _make_client(tmp_path, provider) as client:
        project_id = _create_project(client, "truncated_proj")
        blocked = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/run", json={}
        )
        assert blocked.status_code == 409
        run_id = blocked.json()["run_id"]

        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        content = resumed.json()["result"]["content"]
        assert "[feedback:truncated_ending]" in content
        rewrite = provider.requests[1]
        assert rewrite.revision_feedback is not None
        assert rewrite.revision_feedback[0]["type"] == "truncated_ending"


def test_clean_first_draft_carries_no_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAKE_REVIEW_SCORES", raising=False)
    provider = RecordingFake()
    with _make_client(tmp_path, provider) as client:
        project_id = _create_project(client, "clean_proj")
        ok = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert ok.status_code == 200, ok.text
        result = ok.json()["result"]
        assert "[feedback:" not in result["content"]
        assert provider.requests[0].revision_feedback is None
        assert provider.requests[0].previous_scores is None


def test_model_unavailable_retry_has_no_feedback(tmp_path: Path) -> None:
    """A plain transport retry is not a quality interception: no feedback."""

    class FlakyOnce(DeterministicFakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.requests: list[WriterRequest] = []

        def generate_chapter(self, request: WriterRequest) -> str:
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                raise RuntimeError("temporary outage")
            return super().generate_chapter(request)

    provider = FlakyOnce()
    with _make_client(tmp_path, provider) as client:
        project_id = _create_project(client, "flaky_proj")
        failed = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/run", json={}
        )
        assert failed.status_code == 503
        run_id = failed.json()["run_id"]

        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        assert "[feedback:" not in resumed.json()["result"]["content"]
        assert provider.requests[1].revision_feedback is None
