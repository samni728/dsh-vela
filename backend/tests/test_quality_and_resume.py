from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dsh_novel.application.quality import inspect_chapter
from dsh_novel.config import Settings
from dsh_novel.domain import ChapterContract
from dsh_novel.providers import DeterministicFakeProvider, WriterRequest
from dsh_novel.transports.http import create_app


class FlakyProvider:
    name = "flaky_test"

    def __init__(self) -> None:
        self.calls = 0
        self.fake = DeterministicFakeProvider()

    def generate_chapter(self, request: WriterRequest) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary local model outage")
        return self.fake.generate_chapter(request)


def test_resume_after_model_failure(tmp_path: Path) -> None:
    provider = FlakyProvider()
    app = create_app(
        Settings(data_dir=tmp_path / "resume", context_token_budget=5000),
        provider,
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/projects",
            json={"project_id": "resume_project", "title": "断点", "target_chapters": 2},
        ).json()
        project_id = created["result"]["id"]
        failed = client.post(
            f"/api/v1/projects/{project_id}/chapters/1/run", json={}
        )
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "MODEL_UNAVAILABLE"
        run_id = failed.json()["run_id"]
        assert run_id

        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["status"] == "FAILED_RETRYABLE"
        assert status["attempt"] == 1

        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["result"]["status"] == "COMMITTED"
        assert resumed.json()["result"]["attempt"] == 2


def test_quality_gate_finds_pollution_and_exact_repeat() -> None:
    paragraph = "这个段落包含足够多的文字，用来验证同一章节中的完整重复不会被模型自检遗漏。"
    content = f"<think>分析</think>\n\n{paragraph}\n\n{paragraph}"
    issues = inspect_chapter(
        chapter_number=1,
        content=content,
        contract=ChapterContract(
            chapter_number=1,
            title="测试",
            purpose="验证质量门禁",
            required_events=["不存在的同义事件"],
        ),
        recent_chapters=[],
    )
    types = {issue.issue_type: issue.severity for issue in issues}
    assert types["prompt_pollution"] == "blocker"
    assert types["exact_paragraph_repeat"] == "blocker"
    assert types["required_event_missing"] == "warning"

