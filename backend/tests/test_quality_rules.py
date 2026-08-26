"""Tests for the three new deterministic quality rules and the LLM reviewer.

The degenerate/truncated fixtures are verbatim chapters from the real incident
manuscript (/tmp/banzhang-full.md): chapter 8 looped the same short dialogue
three times, and chapter 10 stopped mid-sentence.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.application.quality import (
    inspect_chapter,
    inspect_cross_chapter_exact_repeat,
    inspect_dense_short_line_repeat,
    inspect_truncated_ending,
)
from dsh_novel.config import Settings
from dsh_novel.domain import (
    ChapterContract,
    QualityIssue,
    ReviewIssue,
    ReviewScores,
    ReviewVerdict,
)
from dsh_novel.providers import DeterministicFakeProvider, OpenAICompatibleProvider
from dsh_novel.providers.base import ReviewRequest, parse_review_payload
from dsh_novel.transports.http import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEGENERATE_CH8 = (FIXTURES_DIR / "degenerate_ch8.txt").read_text(encoding="utf-8")
TRUNCATED_CH10 = (FIXTURES_DIR / "truncated_ch10.txt").read_text(encoding="utf-8")


def _contract(chapter_number: int = 1) -> ChapterContract:
    return ChapterContract(
        chapter_number=chapter_number,
        title="测试",
        purpose="验证质量规则",
    )


def _issue_types(issues: list[QualityIssue]) -> set[str]:
    return {issue.issue_type for issue in issues}


# ---------------------------------------------------------------------------
# dense_short_line_repeat
# ---------------------------------------------------------------------------


def test_dense_short_line_repeat_hits_degenerate_ch8_fixture() -> None:
    issues = inspect_dense_short_line_repeat(chapter_number=8, content=DEGENERATE_CH8)
    assert issues, "第8章的三连对话循环必须命中 dense_short_line_repeat"
    assert all(issue.severity == "blocker" for issue in issues)
    hit = issues[0]
    assert any(entry.startswith("text:") for entry in hit.evidence)
    assert any(entry.startswith("occurrence_slots:") for entry in hit.evidence)

    # The full inspection pipeline reports the same blocker.
    all_issues = inspect_chapter(
        chapter_number=8,
        content=DEGENERATE_CH8,
        contract=_contract(8),
        recent_chapters=[],
    )
    assert "dense_short_line_repeat" in _issue_types(all_issues)


def _looping_content(target: str, span: int) -> str:
    """Looping exchange: the target line plus its fixed reply, placed so the
    target lands at short-line slots 0, span//2 and span; other fillers unique.

    A true loop repeats the surrounding exchange as well — the fixed reply
    gives every occurrence of the target the same normalized neighbour.
    """
    reply = "他说：“守着呢，你睡吧。”"
    mid = span // 2

    def fillers(count: int, start: int) -> list[str]:
        return [f"短句填充{start + i}号，内容各不相同。" for i in range(count)]

    parts = [target, reply]  # slots 0 (target), 1 (reply)
    parts += fillers(mid - 2, 0)  # slots 2 .. mid-1
    parts += [target, reply]  # slots mid (target), mid+1 (reply)
    parts += fillers(span - mid - 2, 100)  # slots mid+2 .. span-1
    parts += [target, reply]  # slots span (target), span+1 (reply)
    return "\n\n".join(parts)


@pytest.mark.parametrize(
    ("span", "should_hit"),
    [(400, True), (401, False)],
)
def test_dense_short_line_repeat_window_boundary(span: int, should_hit: bool) -> None:
    issues = inspect_dense_short_line_repeat(
        chapter_number=1, content=_looping_content("他说他今晚一定回来。", span)
    )
    assert bool(issues) is should_hit


def test_dense_short_line_repeat_ignores_lone_line_unique_neighbours() -> None:
    """The same lone line recurring far apart with a DIFFERENT neighbour each
    time is rhetorical rhythm, not a retell — never blocked, whatever the
    distance."""
    scenes = [
        (
            "周建疆蹲到枣红马旁边，先摸了摸马的脖子。",
            "林秀枝说：“知道。”",
            "她把毛巾拿开，看他额头。",
        ),
        ("周建疆说：“别倒太多。种子要留。”", "林秀枝说：“知道。”", "她把麻袋口捏紧。"),
        (
            "他想了一会儿，又说：“灯芯再剪一剪。”",
            "林秀枝说：“知道。”",
            "她转身走，走了几步，又回头。",
        ),
    ]
    parts = []
    filler = 0
    for before, tic, after in scenes:
        parts += [before, tic, after]
        parts += [f"场景过渡句{filler}号，描写各不相同。" for filler in range(filler, filler + 30)]
        filler += 30
    content = "\n\n".join(parts)
    issues = inspect_dense_short_line_repeat(chapter_number=3, content=content)
    assert issues == []


def test_dense_short_line_repeat_ignores_cross_scene_verbal_tic() -> None:
    """The same acknowledgement in three unrelated scenes (different
    neighbours each time) is a verbal tic, not a dialogue loop."""
    scenes = [
        (
            "周建疆蹲到枣红马旁边，先摸了摸马的脖子，又摸了摸马的眼睛。",
            "林秀枝说：“知道。”",
            "她把毛巾拿开，看他额头，汗又出来了。",
        ),
        (
            "周建疆说：“别倒太多。种子要留。”",
            "林秀枝说：“知道。”",
            "她蹲下去，用手把沙拨到一边，把露出的麻袋口捏紧。",
        ),
        (
            "他想了一会儿，又说：“灯芯再剪一剪。风大，火容易闷。”",
            "林秀枝说：“知道。”",
            "她转身走，走了几步，又回头。",
        ),
    ]
    parts = []
    filler = 0
    for before, tic, after in scenes:
        parts.append(before)
        parts.append(tic)
        parts.append(after)
        for _ in range(20):
            parts.append(f"场景过渡句{filler}号，描写各不相同。")
            filler += 1
    content = "\n\n".join(parts)
    issues = inspect_dense_short_line_repeat(
        chapter_number=3, content=content
    )
    assert issues == [], "跨场景的应答口头禅不是对话循环，不应命中"


def test_dense_short_line_repeat_hits_alternating_exchange() -> None:
    """A/B alternating lines (A-B-A-B-A) are a true loop: every occurrence of
    A shares the same neighbours."""
    lines = []
    for _ in range(12):
        lines.append("我说：“你守到天亮。”")
        lines.append("他说：“守到天亮。”")
    content = "\n\n".join(lines)
    issues = inspect_dense_short_line_repeat(
        chapter_number=8, content=content
    )
    assert issues, "A-B 交替循环必须命中 dense_short_line_repeat"


# ---------------------------------------------------------------------------
# truncated_ending
# ---------------------------------------------------------------------------


def test_truncated_ending_hits_truncated_ch10_fixture() -> None:
    issues = inspect_chapter(
        chapter_number=10,
        content=TRUNCATED_CH10,
        contract=_contract(10),
        recent_chapters=[],
    )
    assert "truncated_ending" in _issue_types(issues)
    issue = next(i for i in issues if i.issue_type == "truncated_ending")
    assert issue.severity == "blocker"
    assert "也不说不疼" in "".join(issue.evidence)


def test_truncated_ending_allows_sentence_final_punctuation() -> None:
    completed = TRUNCATED_CH10.rstrip() + "。"
    issues = inspect_truncated_ending(chapter_number=10, content=completed)
    assert issues is None


def test_truncated_ending_skips_empty_content() -> None:
    # Empty content is empty_content's job; no duplicate report.
    assert inspect_truncated_ending(chapter_number=1, content="   \n\n  ") is None


# ---------------------------------------------------------------------------
# cross_chapter_exact_repeat
# ---------------------------------------------------------------------------

LAMP_PARAGRAPH = (
    "马灯挂在马桩上，风一吹就轻轻摇晃，昏黄的灯影在地上荡来荡去，"
    "像一片被打碎之后又勉强拼起来的月光。"
)


def test_cross_chapter_exact_repeat_hits_shared_paragraph() -> None:
    recent = [{"chapter_number": 5, "content": f"第五章开头。\n\n{LAMP_PARAGRAPH}\n\n第五章结尾。"}]
    current = f"第六章开头。\n\n{LAMP_PARAGRAPH}\n\n第六章结尾。"

    issues = inspect_cross_chapter_exact_repeat(
        chapter_number=6, content=current, recent_chapters=recent
    )
    assert len(issues) == 1
    assert issues[0].severity == "blocker"
    assert any("source_chapter:5" in entry for entry in issues[0].evidence)

    all_issues = inspect_chapter(
        chapter_number=6,
        content=current,
        contract=_contract(6),
        recent_chapters=recent,
    )
    assert "cross_chapter_exact_repeat" in _issue_types(all_issues)


def test_cross_chapter_exact_repeat_ignores_different_paragraphs() -> None:
    recent = [{"chapter_number": 5, "content": LAMP_PARAGRAPH}]
    different = (
        "另一段完全不同的描写，写的是灶膛里的火光、窗纸上的霜花和雪夜里"
        "渐渐安静下来的营房脚步声。"
    )
    issues = inspect_cross_chapter_exact_repeat(
        chapter_number=6, content=different, recent_chapters=recent
    )
    assert issues == []


# ---------------------------------------------------------------------------
# LLM reviewer: parse helpers
# ---------------------------------------------------------------------------


def test_parse_review_payload_strips_think_blocks_and_fences() -> None:
    raw = (
        "<think>让我想想这章写得怎么样……</think>\n"
        "```json\n"
        '{"verdict":"pass","issues":[],'
        '"scores":{"contract_adherence":8,"era_authenticity":7,"flow":9}}\n'
        "```\n"
        "思考结束。"
    )
    verdict = parse_review_payload(raw)
    assert verdict.verdict == "pass"
    assert verdict.scores is not None
    assert verdict.scores.flow == 9.0


@pytest.mark.parametrize(
    "raw",
    [
        "<think>只有思考没有结论</think>",
        '{"verdict":"maybe","issues":[],"scores":{}}',
        "这不是 JSON",
        '{"verdict":"blocked","issues":[{"severity":"blocker","type":"t","description":"d"}],'
        '"scores":{"contract_adherence":11,"era_authenticity":5,"flow":5},"extra":1}',
    ],
)
def test_parse_review_payload_rejects_illegal_payloads(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_review_payload(raw)


# ---------------------------------------------------------------------------
# LLM reviewer: end-to-end through the run pipeline
# ---------------------------------------------------------------------------


class BlockedReviewProvider:
    name = "blocked_review_test"

    def __init__(self) -> None:
        self.fake = DeterministicFakeProvider()

    def generate_chapter(self, request):  # type: ignore[no-untyped-def]
        return self.fake.generate_chapter(request)

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        return ReviewVerdict(
            verdict="blocked",
            issues=[
                ReviewIssue(
                    severity="blocker",
                    type="contract_drift",
                    description="正文未覆盖合同事件且场景错位",
                ),
                ReviewIssue(severity="warning", type="pacing", description="节奏偏慢"),
            ],
            scores=ReviewScores(contract_adherence=2.0, era_authenticity=5.0, flow=4.0),
        )


class SlowReviewProvider(DeterministicFakeProvider):
    name = "slow_review_test"

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        time.sleep(0.5)
        return super().review_chapter(request)


class ExplodingReviewProvider(DeterministicFakeProvider):
    name = "exploding_review_test"

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        raise AssertionError("review must not run when disabled")


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"title": "审稿测试", "premise": "验证 LLM 审稿链路。", "target_chapters": 2},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["id"]


def test_fake_provider_pass_verdict_commits_normally(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        DeterministicFakeProvider(),
    )
    with TestClient(app) as client:
        project_id = _create_project(client)
        run = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert run.status_code == 200, run.text
        result = run.json()["result"]
        assert result["status"] == "COMMITTED"
        assert result["llm_review"]["verdict"] == "pass"
        assert result["llm_review"]["scores"]["contract_adherence"] == 8.0
        assert not [
            issue for issue in result["quality_issues"] if issue["source"] == "llm"
        ]


def test_blocked_llm_verdict_follows_quality_blocked_chain(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000),
        BlockedReviewProvider(),
    )
    with TestClient(app) as client:
        project_id = _create_project(client)
        failed = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert failed.status_code == 409
        assert failed.json()["error"]["code"] == "QUALITY_GATE_BLOCKED"
        run_id = failed.json()["run_id"]

        status = client.get(f"/api/v1/runs/{run_id}").json()["result"]
        assert status["status"] == "QUALITY_BLOCKED"
        assert status["stage"] == "REVIEWING"

        database = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT issue_json FROM review_issues").fetchall()
        payloads = [json.loads(row[0]) for row in rows]
        llm_payloads = [payload for payload in payloads if payload["source"] == "llm"]
        assert {payload["severity"] for payload in llm_payloads} == {"blocker", "warning"}
        assert any(payload["issue_type"] == "contract_drift" for payload in llm_payloads)

        # Resume regenerates and gets blocked again by the same reviewer.
        resumed = client.post(f"/api/v1/runs/{run_id}/resume", json={})
        assert resumed.status_code == 409
        assert resumed.json()["error"]["code"] == "QUALITY_GATE_BLOCKED"


def test_review_timeout_fails_open_with_warning(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            context_token_budget=5000,
            review_timeout_seconds=0.05,
        ),
        SlowReviewProvider(),
    )
    with TestClient(app) as client:
        project_id = _create_project(client)
        run = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert run.status_code == 200, run.text
        result = run.json()["result"]
        assert result["status"] == "COMMITTED"
        unavailable = [
            issue
            for issue in result["quality_issues"]
            if issue["source"] == "llm"
            and issue["issue_type"] == "review_unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0]["severity"] == "warning"


def test_disabled_review_never_calls_provider_and_reports_capability(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=5000, review_enabled=False),
        ExplodingReviewProvider(),
    )
    with TestClient(app) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["optional_capabilities"]["llm_review"] is False

        project_id = _create_project(client)
        run = client.post(f"/api/v1/projects/{project_id}/chapters/1/run", json={})
        assert run.status_code == 200, run.text
        assert run.json()["result"]["status"] == "COMMITTED"


def test_capabilities_report_llm_review_enabled_by_default(data_dir: Path) -> None:
    app = create_app(Settings(data_dir=data_dir, context_token_budget=5000))
    with TestClient(app) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["optional_capabilities"]["llm_review"] is True


# ---------------------------------------------------------------------------
# config keys
# ---------------------------------------------------------------------------


def _redirect_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / "config.yml"
    monkeypatch.setenv("DSH_NOVEL_CONFIG", str(config_path))
    for name in ("DSH_NOVEL_REVIEW_ENABLED", "DSH_NOVEL_REVIEW_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    return config_path


def test_config_accepts_review_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _redirect_config(tmp_path, monkeypatch)
    config_path.write_text(
        "review_enabled: false\nreview_timeout_seconds: 30\n", encoding="utf-8"
    )
    settings = Settings()
    assert settings.review_enabled is False
    assert settings.review_timeout_seconds == 30.0

    monkeypatch.setenv("DSH_NOVEL_REVIEW_ENABLED", "1")
    monkeypatch.setenv("DSH_NOVEL_REVIEW_TIMEOUT", "2.5")
    env_settings = Settings()
    assert env_settings.review_enabled is True  # env wins over file
    assert env_settings.review_timeout_seconds == 2.5


def test_config_defaults_enable_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_config(tmp_path, monkeypatch)
    settings = Settings()
    assert settings.review_enabled is True
    assert settings.review_timeout_seconds == 120.0


def test_config_rejects_wrong_review_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsh_novel.config import ConfigError

    config_path = _redirect_config(tmp_path, monkeypatch)
    config_path.write_text('review_enabled: "true"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "'review_enabled'" in message
    assert "boolean" in message

    config_path.write_text("review_timeout_seconds: soon\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Settings()


# ---------------------------------------------------------------------------
# openai_compatible provider wiring
# ---------------------------------------------------------------------------


def test_openai_review_chapter_raises_runtime_error_when_unreachable() -> None:
    provider = OpenAICompatibleProvider(
        endpoint="http://127.0.0.1:9/v1",
        model="local-writer",
        api_key=None,
        timeout_seconds=2,
        max_output_tokens=64,
        review_timeout_seconds=2,
    )
    request = ReviewRequest(
        project_title="雾港档案",
        contract=_contract(),
        content="第一章正文。",
        recent_chapters=[],
    )
    with pytest.raises(RuntimeError):
        provider.review_chapter(request)
