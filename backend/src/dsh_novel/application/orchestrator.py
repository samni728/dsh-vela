"""Server-side autorun orchestrator: self-healing whole-book long runs.

One daemon thread per project executes chapters sequentially. Each chapter
reuses the ordinary run pipeline (quality gate + LLM review + threshold loop);
transient failures (FAILED_RETRYABLE) and quality blocks (QUALITY_BLOCKED) are
resumed automatically with backoff until the per-chapter attempt budget
(the effective policy's ``max_revisions``) is exhausted.

0.5.0 management/creation split: the orchestrator reads a per-project policy
object. With ``on_chapter_failure='skip_continue'`` (the default) a chapter
that exhausts its attempts is recorded into the rework queue (dynamically
derived as "attempted but uncommitted") and the run moves on, ending in
``completed_with_rework``; with ``'pause'`` the run stops at the failing
chapter. A systemic outage (>= 3 consecutive chapters failing with
MODEL_UNAVAILABLE) aborts the run regardless of the policy. A fresh autorun
POST retries rework-queue chapters first, then continues with new chapters.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dsh_novel.application.service import NovelService
from dsh_novel.errors import (
    ConfigInvalidError,
    InvalidRunStateError,
    ModelUnavailableError,
    OrchestratorBusyError,
    QualityGateBlockedError,
    ReportNotFoundError,
    VersionConflictError,
)
from dsh_novel.util import assert_management_payload, sha256_text, utc_now

# Backoff between automatic resume attempts; patched small in tests.
RESUME_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0, 10.0)

# Systemic-failure failsafe: this many consecutive chapters lost to
# MODEL_UNAVAILABLE abort the run even under skip_continue — when the model is
# gone entirely, continuing is pointless.
MODEL_FAILURE_FAILSAFE = 3


class _ChapterExecutionError(Exception):
    """Terminal per-chapter failure inside the orchestrator thread."""

    def __init__(
        self, message: str, *, chapter: int, model_unavailable: bool = False
    ) -> None:
        super().__init__(message)
        self.chapter = chapter
        self.model_unavailable = model_unavailable


class _SystemicModelFailure(Exception):
    """Failsafe abort: too many consecutive chapters lost to MODEL_UNAVAILABLE."""


@dataclass
class AutorunProgress:
    state: str = "idle"
    current_chapter: int | None = None
    failed_at_chapter: int | None = None
    last_error: str | None = None
    range_from: int | None = None
    range_to: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class AutorunManager:
    """Per-project single-orchestrator registry and runner."""

    def __init__(self, service: NovelService, *, max_revisions: int = 3) -> None:
        self.service = service
        self.max_revisions = max(1, int(max_revisions))
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._progress: dict[str, AutorunProgress] = {}
        self._threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ API

    def start(
        self,
        project_id: str,
        from_chapter: int | None,
        to_chapter: int | None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._registry_lock:
            lock = self._locks.setdefault(project_id, threading.Lock())
        with lock:
            db = self.service.database(project_id)
            project = db.project()  # raises ProjectNotFoundError when absent
            target = int(project["target_chapters"])
            start_from = 1 if from_chapter is None else int(from_chapter)
            end_to = target if to_chapter is None else int(to_chapter)
            if start_from < 1 or end_to < start_from or end_to > target:
                raise ConfigInvalidError(
                    f"invalid autorun range [{start_from}, {end_to}] "
                    f"for {target} target chapter(s)"
                )
            progress = self._progress.get(project_id)
            if progress is not None and progress.state == "running":
                raise OrchestratorBusyError(
                    f"an orchestrator is already running for project {project_id!r}"
                )
            # 0.5.0: resolve the effective policy (request > stored > settings)
            # and persist it on first set; the whole run reads this one object.
            effective_policy = self.service.resolve_policy(db, policy)
            # Self-healing plan: retry rework-queue chapters (attempted but not
            # committed) first, then continue with fresh chapters after the
            # last committed one. Already-committed chapters never re-run.
            committed = set(db.committed_chapter_numbers())
            attempted = set(db.attempted_chapter_numbers())
            pending = [
                chapter
                for chapter in range(start_from, end_to + 1)
                if chapter not in committed
            ]
            plan = [c for c in pending if c in attempted] + [
                c for c in pending if c not in attempted
            ]
            progress = AutorunProgress(
                state="running",
                range_from=start_from,
                range_to=end_to,
                started_at=utc_now(),
            )
            self._progress[project_id] = progress
            if not plan:
                progress.state = "completed"
                progress.finished_at = utc_now()
                try:
                    self._write_final_artifacts(project_id)
                except Exception as exc:
                    progress.last_error = f"artifact export failed: {exc}"
                return self.status(project_id)
            progress.current_chapter = plan[0]
            thread = threading.Thread(
                target=self._execute,
                args=(project_id, plan, effective_policy),
                name=f"autorun-{project_id}",
                daemon=True,
            )
            self._threads[project_id] = thread
            thread.start()
            return self.status(project_id)

    def status(self, project_id: str) -> dict[str, Any]:
        db = self.service.database(project_id)
        db.ensure_exists()
        progress = self._progress.get(project_id)
        committed = db.committed_chapter_numbers()
        reviews = db.latest_review_by_chapter()
        scores = [
            {
                "chapter": number,
                "scores": record.get("scores"),
                "overall": record.get("overall"),
                "verdict": record.get("verdict"),
                "attempt": record.get("attempt"),
            }
            for number, record in sorted(reviews.items())
        ]
        return {
            "state": progress.state if progress is not None else "idle",
            "current_chapter": progress.current_chapter if progress is not None else None,
            "failed_at_chapter": (
                progress.failed_at_chapter if progress is not None else None
            ),
            "range": (
                {"from": progress.range_from, "to": progress.range_to}
                if progress is not None
                else None
            ),
            "chapters_committed": len(committed),
            "committed_chapters": committed,
            "rework_queue": self.rework_queue(db, int(db.project()["target_chapters"])),
            "scores": scores,
            "last_error": progress.last_error if progress is not None else None,
        }

    # ------------------------------------------------- management plane (0.5.0)

    @staticmethod
    def rework_queue(db: Any, target_chapters: int | None = None) -> list[int]:
        """Chapters that were attempted but never committed, ascending.

        Derived dynamically from runs/chapters state — no extra table. Chapters
        that were never attempted are *pending*, not rework.
        """
        if target_chapters is None:
            target_chapters = int(db.project()["target_chapters"])
        committed = set(db.committed_chapter_numbers())
        attempted = set(db.attempted_chapter_numbers())
        return sorted(
            chapter
            for chapter in range(1, target_chapters + 1)
            if chapter in attempted and chapter not in committed
        )

    def pipeline(self, project_id: str) -> dict[str, Any]:
        """Zero-content management snapshot for the Master Agent.

        Numbers and status only: no content/digest/prose keys ever enter the
        payload, and every string value stays within the management length
        bound (enforced by ``assert_management_payload`` before returning).
        """
        db = self.service.database(project_id)
        db.ensure_exists()
        project = db.project()
        target = int(project["target_chapters"])
        statuses = {
            int(item["chapter_number"]): str(item["status"])
            for item in db.chapter_overview()
        }
        attempts = db.chapter_attempts()
        word_counts = db.chapter_word_counts()
        reviews = db.latest_review_by_chapter()
        issue_counts_by_chapter = {
            int(entry["chapter_number"]): dict(entry["types"])
            for entry in db.quality_event_summary()
        }

        chapters: list[dict[str, Any]] = []
        committed_count = 0
        for number in range(1, target + 1):
            status = statuses.get(number, "PENDING")
            if status == "COMMITTED":
                committed_count += 1
            record = reviews.get(number)
            chapters.append(
                {
                    "chapter_number": number,
                    "status": status,
                    "attempt": attempts.get(number, 0),
                    "overall_score": record.get("overall") if record else None,
                    "scores": record.get("scores") if record else None,
                    "verdict": record.get("verdict") if record else None,
                    "issue_counts": issue_counts_by_chapter.get(number, {}),
                    "word_count": word_counts.get(number, 0),
                }
            )
        rework = self.rework_queue(db, target)
        totals = {
            "committed": committed_count,
            "failed": len(rework),
            "pending": target - committed_count - len(rework),
        }

        progress = self._progress.get(project_id)
        raw_state = progress.state if progress is not None else "idle"
        if raw_state == "running":
            state = "running"
        elif raw_state == "failed":
            state = "failed"
        elif raw_state == "idle":
            state = "idle"
        else:  # completed / completed_with_rework terminal states
            state = "completed_with_rework" if rework else "completed"

        result = {
            "project_id": project_id,
            "state": state,
            "outline_generated": bool(project.get("story_spine")),
            "policy": self.service.effective_policy(db),
            "chapters": chapters,
            "rework_queue": rework,
            "totals": totals,
        }
        assert_management_payload(result)
        return result

    def report(self, project_id: str) -> dict[str, Any]:
        db = self.service.database(project_id)
        db.ensure_exists()
        path = db.project_dir / "README.md"
        if not path.is_file():
            raise ReportNotFoundError(
                f"project {project_id!r} has no generated report yet; "
                "complete an autorun first"
            )
        content = path.read_text(encoding="utf-8")
        return {"content": content, "path": str(path), "sha256": sha256_text(content)}

    # -------------------------------------------------------------- internals

    def _execute(
        self, project_id: str, plan: list[int], policy: dict[str, Any]
    ) -> None:
        progress = self._progress[project_id]
        skip_continue = str(policy.get("on_chapter_failure")) == "skip_continue"
        model_failure_streak = 0
        try:
            for chapter in plan:
                progress.current_chapter = chapter
                try:
                    self._execute_chapter(project_id, chapter, policy)
                    model_failure_streak = 0
                except _ChapterExecutionError as exc:
                    if exc.model_unavailable:
                        model_failure_streak += 1
                        if model_failure_streak >= MODEL_FAILURE_FAILSAFE:
                            raise _SystemicModelFailure(
                                f"systemic model failure: {model_failure_streak} "
                                "consecutive chapters (up to chapter "
                                f"{chapter}) ended MODEL_UNAVAILABLE; aborting the "
                                "run because the model endpoint is unreachable"
                            ) from exc
                    else:
                        model_failure_streak = 0
                    if not skip_continue:
                        raise
                    # skip_continue: chapter stays uncommitted (it lands in the
                    # dynamically derived rework queue); move on to the next.
                    continue
            progress.current_chapter = None
            rework = self.rework_queue(self.service.database(project_id))
            progress.state = "completed_with_rework" if rework else "completed"
            progress.finished_at = utc_now()
            self._write_final_artifacts(project_id)
        except _SystemicModelFailure as exc:
            progress.state = "failed"
            progress.failed_at_chapter = progress.current_chapter
            progress.last_error = str(exc)
            progress.finished_at = utc_now()
        except Exception as exc:
            progress.state = "failed"
            progress.failed_at_chapter = progress.current_chapter
            progress.last_error = str(exc)
            progress.finished_at = utc_now()

    def _execute_chapter(
        self, project_id: str, chapter: int, policy: dict[str, Any]
    ) -> dict[str, Any] | None:
        self._ensure_contracts(project_id, chapter, policy)
        try:
            return self.service.run_chapter(
                project_id=project_id,
                chapter_number=chapter,
                supplied_contract=None,
                idempotency_key=None,
            )
        except VersionConflictError:
            # Already committed concurrently; treat as done.
            return None
        except ModelUnavailableError as exc:
            return self._recover(project_id, chapter, exc, policy)
        except QualityGateBlockedError as exc:
            return self._recover(project_id, chapter, exc, policy)

    def _recover(
        self,
        project_id: str,
        chapter: int,
        initial: Exception,
        policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        run_id = initial.run_id
        if not run_id:
            raise _ChapterExecutionError(
                f"chapter {chapter}: {initial}",
                chapter=chapter,
                model_unavailable=isinstance(initial, ModelUnavailableError),
            ) from initial
        max_revisions = max(1, int(policy.get("max_revisions", self.max_revisions)))
        last_status = "UNKNOWN"
        for index in range(max_revisions):
            db = self.service.database(project_id)
            run = db.run(run_id)
            last_status = str(run["status"])
            if last_status == "COMMITTED":
                return run
            if last_status == "PAUSED":
                raise _ChapterExecutionError(
                    f"chapter {chapter} paused: "
                    f"{run.get('error_message') or 'retry budget exhausted'}",
                    chapter=chapter,
                    model_unavailable=isinstance(initial, ModelUnavailableError),
                ) from initial
            if last_status not in {"FAILED_RETRYABLE", "QUALITY_BLOCKED"}:
                raise _ChapterExecutionError(
                    f"chapter {chapter} stopped in status {last_status}",
                    chapter=chapter,
                    model_unavailable=isinstance(initial, ModelUnavailableError),
                ) from initial
            delay = RESUME_BACKOFF_SECONDS[min(index, len(RESUME_BACKOFF_SECONDS) - 1)]
            time.sleep(delay)
            try:
                return self.service.resume_run(run_id)
            except (ModelUnavailableError, QualityGateBlockedError, InvalidRunStateError):
                continue
        final_status = str(self.service.database(project_id).run(run_id)["status"])
        raise _ChapterExecutionError(
            f"chapter {chapter} exhausted {max_revisions} recovery attempts "
            f"(final status {final_status})",
            chapter=chapter,
            model_unavailable=isinstance(initial, ModelUnavailableError),
        ) from initial

    def _ensure_contracts(
        self, project_id: str, chapter: int, policy: dict[str, Any]
    ) -> None:
        db = self.service.database(project_id)
        if db.contract(chapter) is not None:
            return
        last_exc: Exception | None = None
        for _ in range(2):  # one retry on failure, then the chapter fails
            try:
                self.service.generate_outline(
                    project_id,
                    target_words=int(policy.get("target_words") or 0) or None,
                    persist_mode="missing",
                )
                if db.contract(chapter) is not None:
                    return
                last_exc = ConfigInvalidError(f"outline did not include chapter {chapter}")
            except Exception as exc:  # noqa: BLE001 - converted to terminal failure
                last_exc = exc
        raise _ChapterExecutionError(
            f"contract generation failed for chapter {chapter}: {last_exc}",
            chapter=chapter,
        ) from last_exc

    def _write_final_artifacts(self, project_id: str) -> None:
        db = self.service.database(project_id)
        manuscript = db.export("markdown")
        (db.project_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
        readme = self._build_report(db)
        (db.project_dir / "README.md").write_text(readme, encoding="utf-8")

    def _build_report(self, db: Any) -> str:
        project = db.project()
        committed = db.committed_chapter_numbers()
        reviews = db.latest_review_by_chapter()
        titles = {
            int(item["chapter_number"]): str(item["title"])
            for item in db.status()["chapters"]
        }
        target = int(project["target_chapters"])

        def fmt(value: Any) -> str:
            return f"{float(value):.1f}" if isinstance(value, (int, float)) else "-"

        lines: list[str] = [
            f"# 《{project['title']}》自动写作报告",
            "",
            "## 项目元数据",
            "",
            f"- 项目 ID：`{project['id']}`",
            f"- 前提：{project['premise'] or '（未提供）'}",
            f"- 目标章节数：{target}",
            f"- 已提交章节：{len(committed)}/{target}",
            f"- 蓝图版本：{project['blueprint_version']} / 正史版本：{project['canon_version']}",
            f"- 报告生成时间：{utc_now()}",
            "",
        ]
        hard_rules = project.get("hard_rules") or []
        if hard_rules:
            lines += ["## 硬性规则", "", *[f"- {rule}" for rule in hard_rules], ""]
        lines += [
            "## 每章分数表",
            "",
            "| 章节 | 标题 | contract_adherence | era_authenticity | flow | overall | 审稿结论 |",
            "|---|---|---|---|---|---|---|",
        ]
        for number in range(1, target + 1):
            title = titles.get(number, "")
            record = reviews.get(number)
            if record is None:
                lines.append(f"| {number} | {title} | - | - | - | - | 未审稿/未提交 |")
                continue
            scores = record.get("scores") or {}
            lines.append(
                f"| {number} | {title} "
                f"| {fmt(scores.get('contract_adherence'))} "
                f"| {fmt(scores.get('era_authenticity'))} "
                f"| {fmt(scores.get('flow'))} "
                f"| {fmt(record.get('overall'))} "
                f"| {record.get('verdict', '')} |"
            )
        lines.append("")
        rework = self.rework_queue(db, target)
        lines += ["## 补写队列", ""]
        if not rework:
            lines.append("- 无补写章节，全部目标章节均已定稿。")
        else:
            latest_runs = db.latest_run_by_chapter()
            lines += [
                "| 章节 | 最近状态 | 错误码 | 原因 |",
                "|---|---|---|---|",
            ]
            for number in rework:
                run = latest_runs.get(number, {})
                reason = str(
                    run.get("error_message") or run.get("error_code") or "未定稿"
                ).replace("\n", " ")[:200]
                lines.append(
                    f"| {number} | {run.get('status', '-')} "
                    f"| {run.get('error_code') or '-'} | {reason} |"
                )
            lines.append(
                "\n重新 POST autorun 会优先重试补写队列中的章节，"
                "再继续写 committed+1 之后的新章。"
            )
        lines.append("")
        summary = db.quality_event_summary()
        lines += ["## 质量事件摘要", ""]
        if not summary:
            lines.append("- 无质量事件记录。")
        else:
            lines += [
                "| 章节 | blocker | error | warning | 主要类型 |",
                "|---|---|---|---|---|",
            ]
            for entry in summary:
                top_types = sorted(
                    entry["types"].items(), key=lambda item: item[1], reverse=True
                )[:3]
                types_text = "、".join(
                    f"{name}×{count}" for name, count in top_types
                ) or "-"
                lines.append(
                    f"| {entry['chapter_number']} | {entry['blocker']} "
                    f"| {entry['error']} | {entry['warning']} | {types_text} |"
                )
        return "\n".join(lines) + "\n"
