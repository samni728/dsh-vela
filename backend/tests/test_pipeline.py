"""Tests for the 0.5.0 zero-content management plane (GET /pipeline),
skip_continue rework queue, pause mode and the systemic-failure failsafe."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dsh_novel.application import orchestrator as orchestrator_module
from dsh_novel.config import Settings
from dsh_novel.domain import ReviewScores, ReviewVerdict
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app

# Management-plane hard guarantees.
FORBIDDEN_KEYS = {"content", "digest", "prose"}
MAX_STRING_LEN = 200


class ChapterTwoLowScorer(DeterministicFakeProvider):
    """Chapter 2 never reaches the threshold; every other chapter passes."""

    name = "chapter_two_low_test"

    def review_chapter(self, request):  # type: ignore[no-untyped-def]
        verdict = super().review_chapter(request)
        if request.contract.chapter_number == 2:
            return ReviewVerdict(
                verdict="pass",
                issues=[],
                scores=ReviewScores(
                    contract_adherence=5.0, era_authenticity=5.0, flow=5.0
                ),
            )
        return verdict


class DeadProvider(DeterministicFakeProvider):
    """Every drafting call fails with a transport-level error."""

    name = "dead_test"

    def __init__(self) -> None:
        super().__init__()
        self.chapters_attempted: list[int] = []

    def generate_chapter(self, request):  # type: ignore[no-untyped-def]
        self.chapters_attempted.append(request.contract.chapter_number)
        raise RuntimeError("simulated total endpoint outage")


class HealableChapterTwo(DeterministicFakeProvider):
    """Chapter 2 reviews low until healed; records the review visit order."""

    name = "healable_chapter_two_test"

    def __init__(self) -> None:
        super().__init__()
        self.healed = False
        self.review_order: list[int] = []

    def review_chapter(self, request):  # type: ignore[no-untyped-def]
        verdict = super().review_chapter(request)
        chapter = request.contract.chapter_number
        if chapter == 2 and not self.healed:
            return ReviewVerdict(
                verdict="pass",
                issues=[],
                scores=ReviewScores(
                    contract_adherence=5.0, era_authenticity=5.0, flow=5.0
                ),
            )
        self.review_order.append(chapter)
        return verdict


def _make_client(tmp_path: Path, provider: Any = None) -> TestClient:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        provider or DeterministicFakeProvider(),
    )
    return TestClient(app)


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


def _assert_zero_content(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            assert key not in FORBIDDEN_KEYS, f"forbidden management key: {key}"
            _assert_zero_content(value)
    elif isinstance(node, list):
        for item in node:
            _assert_zero_content(item)
    elif isinstance(node, str):
        assert len(node) <= MAX_STRING_LEN, f"string too long: {node[:80]}..."


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator_module, "RESUME_BACKOFF_SECONDS", (0.02, 0.04, 0.08)
    )


# ---------------------------------------------------------------------------
# A. zero-content pipeline endpoint
# ---------------------------------------------------------------------------


def test_pipeline_is_zero_content_and_structurally_complete(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={"title": "雾港档案", "premise": "测试零正文。", "target_chapters": 3},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]
        _wait_terminal(client, project_id)

        response = client.get(f"/api/v1/projects/{project_id}/pipeline")
        assert response.status_code == 200, response.text
        payload = response.json()

        # Hard guarantee: serialized JSON carries no prose keys and no long
        # string values anywhere (walks envelope + result recursively).
        _assert_zero_content(payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert '"content"' not in serialized
        assert '"digest"' not in serialized

        result = payload["result"]
        assert result["project_id"] == project_id
        assert result["state"] == "completed"
        assert result["outline_generated"] is True
        assert set(result["policy"]) == {
            "score_threshold",
            "max_revisions",
            "target_words",
            "on_chapter_failure",
        }
        assert [c["chapter_number"] for c in result["chapters"]] == [1, 2, 3]
        for chapter in result["chapters"]:
            assert chapter["status"] == "COMMITTED"
            assert chapter["attempt"] == 1
            assert chapter["verdict"] == "pass"
            assert chapter["overall_score"] == pytest.approx(8.0)
            assert set(chapter["scores"]) == {
                "contract_adherence",
                "era_authenticity",
                "flow",
            }
            assert chapter["word_count"] > 0
        assert result["rework_queue"] == []
        assert result["totals"] == {"committed": 3, "failed": 0, "pending": 0}


def test_pipeline_unknown_project_returns_404(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        missing = client.get("/api/v1/projects/nope_missing/pipeline")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "PROJECT_NOT_FOUND"


# ---------------------------------------------------------------------------
# D. skip_continue + rework queue vs pause mode
# ---------------------------------------------------------------------------


def test_skip_continue_records_rework_queue_and_finishes(tmp_path: Path) -> None:
    provider = ChapterTwoLowScorer()
    with _make_client(tmp_path, provider) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "rework_proj",
                "title": "雾港档案",
                "target_chapters": 3,
            },
        )
        project_id = created.json()["result"]["id"]
        started = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert started.status_code == 200, started.text

        terminal = _wait_terminal(client, project_id)
        # Chapter 2 is permanently below threshold: skipped, run completes.
        assert terminal["state"] == "completed_with_rework"

        pipeline = client.get(f"/api/v1/projects/{project_id}/pipeline").json()
        _assert_zero_content(pipeline)
        result = pipeline["result"]
        assert result["state"] == "completed_with_rework"
        assert result["rework_queue"] == [2]
        assert result["totals"] == {"committed": 2, "failed": 1, "pending": 0}
        chapters = {c["chapter_number"]: c for c in result["chapters"]}
        assert chapters[1]["status"] == "COMMITTED"
        assert chapters[3]["status"] == "COMMITTED"
        assert chapters[2]["status"] != "COMMITTED"
        assert chapters[2]["overall_score"] == pytest.approx(5.0)
        assert chapters[2]["attempt"] == 3  # exhausted the default budget
        assert (
            chapters[2]["issue_counts"].get("score_below_threshold", 0) >= 1
        )
        assert chapters[1]["word_count"] > 0
        assert chapters[3]["word_count"] > 0
        assert chapters[2]["word_count"] == 0


def test_pause_mode_stops_failed_at_the_blocking_chapter(tmp_path: Path) -> None:
    provider = ChapterTwoLowScorer()
    with _make_client(tmp_path, provider) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "pause_proj",
                "title": "雾港档案",
                "target_chapters": 3,
            },
        )
        project_id = created.json()["result"]["id"]
        started = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"on_chapter_failure": "pause"}},
        )
        assert started.status_code == 200, started.text

        terminal = _wait_terminal(client, project_id)
        assert terminal["state"] == "failed"
        assert terminal["failed_at_chapter"] == 2
        assert terminal["last_error"]

        result = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]
        assert result["state"] == "failed"
        assert result["rework_queue"] == [2]
        assert result["totals"] == {"committed": 1, "failed": 1, "pending": 1}


def test_rework_queue_is_retried_before_new_chapters(tmp_path: Path) -> None:
    """Resume plan order: rework chapters first, then fresh committed+1 ones."""
    provider = HealableChapterTwo()
    with _make_client(tmp_path, provider) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "order_proj",
                "title": "雾港档案",
                "target_chapters": 3,
            },
        )
        project_id = created.json()["result"]["id"]

        # Pause at chapter 2: committed={1}, attempted={1,2}, 3 untouched.
        started = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"on_chapter_failure": "pause"}},
        )
        assert started.status_code == 200, started.text
        terminal = _wait_terminal(client, project_id)
        assert terminal["state"] == "failed"
        assert terminal["failed_at_chapter"] == 2

        # Heal and resume under skip_continue: chapter 2 (rework) must be
        # revisited before chapter 3 (fresh).
        provider.healed = True
        provider.review_order.clear()
        resumed = client.post(
            f"/api/v1/projects/{project_id}/autorun",
            json={"policy": {"on_chapter_failure": "skip_continue"}},
        )
        assert resumed.status_code == 200, resumed.text

        completed = _wait_terminal(client, project_id)
        assert completed["state"] == "completed"
        assert provider.review_order == [2, 3]
        result = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]
        assert result["totals"] == {"committed": 3, "failed": 0, "pending": 0}


def test_consecutive_model_failures_abort_even_with_skip_continue(
    tmp_path: Path,
) -> None:
    provider = DeadProvider()
    with _make_client(tmp_path, provider) as client:
        created = client.post(
            "/api/v1/projects",
            json={
                "project_id": "dead_proj",
                "title": "雾港档案",
                "target_chapters": 5,
            },
        )
        project_id = created.json()["result"]["id"]
        started = client.post(f"/api/v1/projects/{project_id}/autorun", json={})
        assert started.status_code == 200, started.text

        terminal = _wait_terminal(client, project_id)
        # Failsafe: 3 consecutive MODEL_UNAVAILABLE chapters abort the run even
        # though skip_continue would otherwise keep going through all five.
        assert terminal["state"] == "failed"
        assert "MODEL_UNAVAILABLE" in str(terminal["last_error"])
        assert set(provider.chapters_attempted) == {1, 2, 3}

        result = client.get(
            f"/api/v1/projects/{project_id}/pipeline"
        ).json()["result"]
        assert result["state"] == "failed"
        assert result["totals"]["committed"] == 0
