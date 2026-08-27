from __future__ import annotations

import os

from dsh_novel.domain import OutlineChapter, OutlineResult, ReviewScores, ReviewVerdict
from dsh_novel.providers.base import (
    ExtractionRequest,
    OutlineRequest,
    ReviewRequest,
    WriterRequest,
)

# Test hook: comma-separated overall review scores, one per attempt. The i-th
# draft attempt (1-based) receives the i-th value; the last value repeats.
# Example: FAKE_REVIEW_SCORES="6.5,9" -> attempt 1 scores 6.5, attempt 2 scores 9.
FAKE_REVIEW_SCORES_ENV = "FAKE_REVIEW_SCORES"

_OUTLINE_ACTS = ("开局", "推进", "升级", "转折", "收束")


class DeterministicFakeProvider:
    """Deterministic provider for contract tests and offline smoke runs."""

    name = "deterministic_fake"

    def generate_chapter(self, request: WriterRequest) -> str:
        contract = request.contract
        events = contract.required_events or [contract.purpose]
        event_text = "、".join(events)
        variants = [
            (
                "晨雾沿着石阶缓慢退去，最先显露的是门前那道新鲜划痕。",
                "沉默被一声短促的询问打破，所有人的注意力随之改变。",
            ),
            (
                "雨点敲过窗棂，屋内的地图却比天气更令人不安。",
                "一份迟到的消息让原先稳固的判断出现了裂缝。",
            ),
            (
                "暮色压低了街巷的轮廓，远处灯火依次亮起。",
                "看似寻常的相遇很快暴露出双方完全不同的目的。",
            ),
            (
                "风从空旷站台穿过，卷起一张无人认领的车票。",
                "旧日承诺被重新提起时，回答者第一次选择了回避。",
            ),
            (
                "钟声落下之后，会议室里仍没有人率先离席。",
                "被忽略的细节重新排成线索，迫使众人修正计划。",
            ),
            (
                "河面反光刺得人睁不开眼，岸边脚印却异常清楚。",
                "追踪者在岔路前停下，因为证据指向了意料之外的人。",
            ),
            (
                "档案柜最底层传来轻响，一枚生锈钥匙落在地上。",
                "尘封记录补全了缺口，也让原有解释变得站不住脚。",
            ),
            (
                "夜班列车驶过城北，震动使杯中的水泛起细纹。",
                "行动开始前，主角主动改变了约定好的分工。",
            ),
        ]
        opening, turn = variants[(contract.chapter_number - 1) % len(variants)]
        body = "\n\n".join(
            [
                f"# {contract.title}",
                f"{opening}第{contract.chapter_number}章围绕{contract.purpose}展开。",
                turn,
                f"{turn}由此推动了{event_text}。每一步都改变了下一步的条件，"
                "人物没有依靠偶然跳过矛盾，而是在行动中承担了后果。",
                f"回望{opening}这一开端，冲突抵达临界点后，"
                f"第{contract.chapter_number}章的问题得到暂时回应，新的不确定性也随之出现。",
                contract.handoff
                or f"章节结束时，人物已经完成第{contract.chapter_number}章的选择，并准备继续前行。",
            ]
        )
        if request.revision_feedback:
            # Visible marker at the very end of the body so tests (and humans)
            # can assert the feedback loop reached the writer. It ends with a
            # sentence period on purpose: the truncated_ending quality gate
            # requires the final paragraph to close with sentence punctuation.
            types = ",".join(
                str(item.get("type", "unknown")) for item in request.revision_feedback
            )
            body = f"{body}\n\n[feedback:{types}]。"
        return body

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        """Fixed pass verdict so offline smoke runs never block on review.

        With ``FAKE_REVIEW_SCORES`` set, the score follows the draft attempt so
        threshold-loop tests can script a failing first draft that passes on
        rewrite.
        """
        score = 8.0
        raw = os.getenv(FAKE_REVIEW_SCORES_ENV)
        if raw:
            try:
                values = [float(item.strip()) for item in raw.split(",") if item.strip()]
            except ValueError:
                values = []
            if values:
                index = min(max(request.attempt - 1, 0), len(values) - 1)
                score = values[index]
        return ReviewVerdict(
            verdict="pass",
            issues=[],
            scores=ReviewScores(
                contract_adherence=score, era_authenticity=score, flow=score
            ),
        )

    def generate_outline(self, request: OutlineRequest) -> OutlineResult:
        """Deterministic canned outline covering exactly 1..target_chapters."""
        chapters = []
        for number in range(1, request.target_chapters + 1):
            act = _OUTLINE_ACTS[(number - 1) % len(_OUTLINE_ACTS)]
            plants = (
                [f"第{number}章埋下{act}阶段的悬念：未寄出的信"] if number % 2 == 1 else []
            )
            advances = (
                [] if number % 2 == 1 else [f"推进第{number - 1}章埋下的悬念"]
            )
            chapters.append(
                OutlineChapter(
                    chapter_number=number,
                    title=f"第{number}章·{act}",
                    purpose=(
                        f"第{number}章以「{act}」的姿态推进《{request.title}》的核心冲突，"
                        "并为下一章留下清晰的衔接。"
                    ),
                    required_events=[
                        f"第{number}章关键事件：主角在{act}阶段做出不可逆的选择"
                    ],
                    hooks_to_plant=plants,
                    hooks_to_advance=advances,
                    target_words=request.target_words,
                    characters=[f"主角（第{number}章推进{act}阶段）"],
                    twist=f"{act}阶段的转折：旧承诺被重新提起" if number % 3 == 0 else "",
                    handoff=(
                        f"第{number}章结尾留下衔接：主角面向下一阶段的选择。"
                    ),
                )
            )
        story_spine = {
            "central_conflict": (
                f"《{request.title}》：{request.premise[:60] or '核心冲突的展开与回收'}"
            ),
            "acts": list(_OUTLINE_ACTS),
            "hard_rules": list(request.hard_rules),
            "ending_constraint": "全部伏笔在末章前收束，结局呼应第一章的初始条件。",
        }
        return OutlineResult(story_spine=story_spine, chapters=chapters)

    def extract_chapter_state(self, request: ExtractionRequest) -> dict[str, Any]:
        """Deterministic extraction echoing contract + handoff, for tests."""
        return {
            "character_changes": [
                {
                    "character": item,
                    "before": "未知",
                    "after": f"在第{request.contract.chapter_number}章推进",
                }
                for item in (request.contract.characters or [])
            ],
            "hooks_status": [
                {"hook": hook, "status": "planted", "evidence": "按合同埋设"}
                for hook in request.contract.hooks_to_plant
            ]
            + [
                {"hook": hook, "status": "advanced", "evidence": "按合同推进"}
                for hook in request.contract.hooks_to_advance
            ],
            "twist": request.contract.twist,
            "next_chapter_hook": request.contract.handoff,
        }
