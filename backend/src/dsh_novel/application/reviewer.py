from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from dsh_novel.domain import ChapterContract, QualityIssue, ReviewVerdict
from dsh_novel.providers.base import ReviewRequest, default_review_verdict
from dsh_novel.util import new_id, sha256_text

REVIEW_UNAVAILABLE_TYPE = "review_unavailable"
REVIEW_INVALID_RESPONSE_TYPE = "review_invalid_response"
SCORE_BELOW_THRESHOLD_TYPE = "score_below_threshold"


def overall_score(verdict: ReviewVerdict) -> float | None:
    """Overall review score = min of the three dimension scores; None if absent."""
    if verdict.scores is None:
        return None
    return min(verdict.scores.model_dump().values())


class ChapterReviewer:
    """Optional LLM review agent between the deterministic gate and COMMITTING.

    Failure semantics are strictly fail-open: timeouts, transport errors and
    unparseable verdicts all degrade to a single warning issue so a long run is
    never stuck by the reviewer. Blocking happens in exactly two cases:

    1. an explicit ``blocked`` verdict that contains at least one blocker-
       severity issue (same QUALITY_BLOCKED chain as deterministic rules), and
    2. the overall score (= min of the three scores) falling below
       ``score_threshold`` — reported as one ``score_below_threshold`` blocker.
    """

    def __init__(
        self,
        *,
        provider: Any,
        timeout_seconds: float,
        score_threshold: float = 8.0,
    ) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.score_threshold = score_threshold

    def review(
        self,
        *,
        project_title: str,
        contract: ChapterContract,
        content: str,
        recent_chapters: list[dict[str, object]],
        blueprint: dict[str, Any] | None = None,
        attempt: int = 0,
        score_threshold: float | None = None,
    ) -> tuple[list[QualityIssue], ReviewVerdict]:
        # 0.5.0: the effective per-project policy supplies the threshold; the
        # constructor value stays as the fallback for direct callers.
        threshold = (
            self.score_threshold if score_threshold is None else float(score_threshold)
        )
        request = ReviewRequest(
            project_title=project_title,
            contract=contract,
            content=content,
            recent_chapters=[
                {
                    "chapter_number": str(chapter.get("chapter_number", "")),
                    "digest": str(chapter.get("digest", ""))[:500],
                }
                for chapter in recent_chapters
            ],
            blueprint=blueprint,
            attempt=attempt,
        )
        content_hash = sha256_text(content)
        try:
            verdict = self._invoke(lambda: self._call_provider(request))
        except ValueError as exc:
            issue = self._warning_issue(
                issue_type=REVIEW_INVALID_RESPONSE_TYPE,
                description=f"LLM 审稿返回无法解析的结果，已按 fail-open 跳过：{exc}",
                chapter_number=contract.chapter_number,
                content_hash=content_hash,
            )
            return [issue], default_review_verdict()
        except Exception as exc:  # timeout / transport / provider failure
            issue = self._warning_issue(
                issue_type=REVIEW_UNAVAILABLE_TYPE,
                description=f"LLM 审稿不可用，已按 fail-open 跳过：{exc}",
                chapter_number=contract.chapter_number,
                content_hash=content_hash,
            )
            return [issue], default_review_verdict()
        return (
            self._issues_from_verdict(
                verdict,
                chapter_number=contract.chapter_number,
                content_hash=content_hash,
                score_threshold=threshold,
            ),
            verdict,
        )

    def _call_provider(self, request: ReviewRequest) -> ReviewVerdict:
        # Providers predating the review capability keep working: they simply
        # get the fail-open pass verdict.
        review_chapter = getattr(self.provider, "review_chapter", None)
        if not callable(review_chapter):
            return default_review_verdict()
        return review_chapter(request)

    def _invoke(self, call: Callable[[], ReviewVerdict]) -> ReviewVerdict:
        """Run the provider call under a hard wall-clock deadline.

        The worker thread is a daemon, so even a provider that ignores its own
        HTTP timeout can never wedge the run loop past this deadline.
        """
        outcome: dict[str, Any] = {}
        finished = threading.Event()

        def runner() -> None:
            try:
                outcome["verdict"] = call()
            except Exception as exc:  # converted to fail-open upstream
                outcome["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(target=runner, name="llm-chapter-review", daemon=True)
        worker.start()
        if not finished.wait(timeout=self.timeout_seconds):
            raise TimeoutError(
                f"llm review timed out after {self.timeout_seconds} seconds"
            )
        error = outcome.get("error")
        if error is not None:
            raise error
        verdict: ReviewVerdict = outcome["verdict"]
        return verdict

    def _issues_from_verdict(
        self,
        verdict: ReviewVerdict,
        *,
        chapter_number: int,
        content_hash: str,
        score_threshold: float | None = None,
    ) -> list[QualityIssue]:
        threshold = (
            self.score_threshold if score_threshold is None else float(score_threshold)
        )
        blocking_verdict = verdict.verdict == "blocked"
        issues: list[QualityIssue] = []
        for item in verdict.issues:
            is_blocker = blocking_verdict and item.severity == "blocker"
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type=item.type,
                    severity="blocker" if is_blocker else "warning",
                    chapter_number=chapter_number,
                    span_start=0,
                    span_end=0,
                    source_hash=content_hash,
                    instruction=item.description,
                    evidence=[f"review_verdict:{verdict.verdict}"],
                    source="llm",
                )
            )
        overall = overall_score(verdict)
        if overall is not None and overall < threshold:
            scores = verdict.scores
            assert scores is not None  # guaranteed by overall_score contract
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type=SCORE_BELOW_THRESHOLD_TYPE,
                    severity="blocker",
                    chapter_number=chapter_number,
                    span_start=0,
                    span_end=0,
                    source_hash=content_hash,
                    instruction=(
                        f"审稿总分 {overall:.1f} 低于阈值 {threshold:.1f}，需要重写："
                        f"contract_adherence={scores.contract_adherence:.1f}, "
                        f"era_authenticity={scores.era_authenticity:.1f}, "
                        f"flow={scores.flow:.1f}"
                    ),
                    evidence=[
                        f"review_verdict:{verdict.verdict}",
                        f"overall:{overall:.2f}",
                        f"threshold:{threshold}",
                    ],
                    source="llm",
                )
            )
        return issues

    def _warning_issue(
        self,
        *,
        issue_type: str,
        description: str,
        chapter_number: int,
        content_hash: str,
    ) -> QualityIssue:
        return QualityIssue(
            issue_id=new_id("issue"),
            issue_type=issue_type,
            severity="warning",
            chapter_number=chapter_number,
            span_start=0,
            span_end=0,
            source_hash=content_hash,
            instruction=description,
            evidence=[],
            source="llm",
        )
