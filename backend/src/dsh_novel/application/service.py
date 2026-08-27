from __future__ import annotations

from pathlib import Path
from typing import Any

from dsh_novel.api_models import ProjectCreateRequest
from dsh_novel.application.context import ContextCompiler
from dsh_novel.application.policy import (
    DEFAULT_ON_CHAPTER_FAILURE,
    DEFAULT_SCORE_THRESHOLD,
    merge_policy,
    normalize_policy,
)
from dsh_novel.application.quality import inspect_chapter
from dsh_novel.application.reviewer import (
    SCORE_BELOW_THRESHOLD_TYPE,
    ChapterReviewer,
    overall_score,
)
from dsh_novel.domain import ChapterContract, ChapterDelta, ContextPackage, OutlineResult
from dsh_novel.errors import (
    ConfigInvalidError,
    InvalidRunStateError,
    ModelUnavailableError,
    QualityGateBlockedError,
    RunNotFoundError,
    VersionConflictError,
)
from dsh_novel.infrastructure import ProjectDatabase
from dsh_novel.providers import ModelProvider, OutlineRequest, WriterRequest
from dsh_novel.util import new_id, sha256_text, utc_now

# Matches the ChapterContract default; used when no explicit target_words is
# supplied to outline generation.
DEFAULT_TARGET_WORDS = 3500

# Policy-level default per-chapter word budget (0.5.0 policy object).
POLICY_DEFAULT_TARGET_WORDS = 4000

# Upper bound on feedback items injected into a revision request so a pathological
# issue flood cannot blow up the writer prompt.
MAX_REVISION_FEEDBACK_ITEMS = 20


class NovelService:
    def __init__(
        self,
        *,
        projects_root: Path,
        provider: ModelProvider,
        context_token_budget: int,
        reviewer: ChapterReviewer | None = None,
        max_revisions: int = 3,
    ) -> None:
        self.projects_root = projects_root
        self.provider = provider
        self.compiler = ContextCompiler(context_token_budget)
        # Optional LLM reviewer; None keeps the purely deterministic pipeline.
        self.reviewer = reviewer
        # Unified per-chapter attempt budget: applies to resume retries and to
        # the score-threshold rewrite loop alike.
        self.max_revisions = max(1, int(max_revisions))

    def database(self, project_id: str) -> ProjectDatabase:
        return ProjectDatabase(self.projects_root, project_id)

    # ------------------------------------------------------------- policy

    @property
    def default_score_threshold(self) -> float:
        """Settings-level score threshold fallback (from the reviewer, if any)."""
        return float(getattr(self.reviewer, "score_threshold", DEFAULT_SCORE_THRESHOLD))

    def policy_defaults(self) -> dict[str, Any]:
        """Settings-level defaults used when request/stored policy omit a key."""
        return {
            "score_threshold": self.default_score_threshold,
            "max_revisions": self.max_revisions,
            "target_words": POLICY_DEFAULT_TARGET_WORDS,
            "on_chapter_failure": DEFAULT_ON_CHAPTER_FAILURE,
        }

    def effective_policy(
        self, db: ProjectDatabase, override: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Effective policy = request > stored policy_json > settings defaults."""
        return merge_policy(
            request=override, stored=db.project_policy(), defaults=self.policy_defaults()
        )

    def resolve_policy(
        self, db: ProjectDatabase, override: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Merge the effective policy and persist it on first set or override.

        The stored value is written whenever a request supplies a policy (the
        override becomes durable for later runs) or when the project has no
        stored policy yet (defaults become visible to the management plane).
        """
        stored = db.project_policy()
        request = normalize_policy(override)
        effective = merge_policy(
            request=request, stored=stored, defaults=self.policy_defaults()
        )
        if request or not stored:
            db.save_policy(effective)
        return effective

    def create_project(self, request: ProjectCreateRequest) -> dict[str, Any]:
        project_id = request.project_id or new_id("prj")
        try:
            db = ProjectDatabase.create(self.projects_root, project_id, request)
        except FileExistsError as exc:
            raise VersionConflictError(f"project {project_id!r} already exists") from exc
        return db.status()

    def project_status(self, project_id: str) -> dict[str, Any]:
        return self.database(project_id).status()

    def _default_contract(self, chapter_number: int, title: str) -> ChapterContract:
        return ChapterContract(
            chapter_number=chapter_number,
            title=f"第{chapter_number}章",
            purpose=f"推进《{title}》的核心冲突，并形成清晰的下一章衔接",
            required_events=[],
            handoff=f"第{chapter_number}章的选择为下一章造成新的条件。",
        )

    def prepare_chapter(
        self,
        project_id: str,
        chapter_number: int,
        supplied_contract: ChapterContract | None,
    ) -> tuple[ChapterContract, ContextPackage]:
        db = self.database(project_id)
        project = db.project()
        contract = supplied_contract or db.contract(chapter_number)
        if contract is None:
            contract = self._default_contract(chapter_number, project["title"])
        if contract.chapter_number != chapter_number:
            raise ConfigInvalidError("contract chapter_number does not match route")
        db.save_contract(contract)
        recent = db.recent_chapters(chapter_number, limit=3)
        package = self.compiler.compile(
            project=project,
            contract=contract,
            recent_chapters=recent,
        )
        db.save_context(package)
        return contract, package

    def generate_outline(
        self,
        project_id: str,
        *,
        target_words: int | None = None,
        persist_mode: str = "all",
    ) -> dict[str, Any]:
        """Generate a structured whole-book outline via the outline agent.

        persist_mode="all" rewrites story_spine and every chapter contract and
        refuses once any chapter is COMMITTED (outline regeneration guard).
        persist_mode="missing" (orchestrator self-healing) only fills contracts
        that do not exist yet, so committed chapters keep their history.
        """
        if persist_mode not in {"all", "missing"}:
            raise ConfigInvalidError(f"unknown outline persist mode: {persist_mode!r}")
        db = self.database(project_id)
        project = db.project()
        if persist_mode == "all":
            committed = db.committed_chapter_numbers()
            if committed:
                raise VersionConflictError(
                    f"project has {len(committed)} committed chapter(s); "
                    "outline regeneration is not allowed"
                )
        generator = getattr(self.provider, "generate_outline", None)
        if not callable(generator):
            raise ConfigInvalidError(
                f"provider {getattr(self.provider, 'name', '?')!r} does not support "
                "outline generation"
            )
        request = OutlineRequest(
            title=str(project["title"]),
            premise=str(project["premise"] or ""),
            hard_rules=[str(rule) for rule in (project.get("hard_rules") or [])],
            target_chapters=int(project["target_chapters"]),
            target_words=int(target_words or DEFAULT_TARGET_WORDS),
        )
        try:
            outline: OutlineResult = generator(request)
        except NotImplementedError as exc:
            raise ConfigInvalidError(f"outline generation unsupported: {exc}") from exc
        except (RuntimeError, ValueError) as exc:
            raise ConfigInvalidError(
                f"outline generation failed: {exc}",
                details={"provider": getattr(self.provider, "name", "?"), "errors": str(exc)},
            ) from exc
        numbers = [chapter.chapter_number for chapter in outline.chapters]
        expected = list(range(1, request.target_chapters + 1))
        if numbers != expected:
            raise ConfigInvalidError(
                "outline chapters must be numbered consecutively 1..N",
                details={"expected": expected, "got": numbers},
            )
        db.save_story_spine(outline.story_spine)
        upserted: list[int] = []
        for chapter in outline.chapters:
            if persist_mode == "missing" and db.contract(chapter.chapter_number) is not None:
                continue
            db.save_contract(self._contract_from_outline(chapter))
            upserted.append(chapter.chapter_number)
        return {
            "story_spine": outline.story_spine,
            "chapters": [chapter.model_dump(mode="json") for chapter in outline.chapters],
            "contracts_upserted": upserted,
        }

    @staticmethod
    def _contract_from_outline(chapter: Any) -> ChapterContract:
        return ChapterContract(
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            purpose=chapter.purpose,
            required_events=list(chapter.required_events),
            hooks_to_plant=list(chapter.hooks_to_plant),
            hooks_to_advance=list(chapter.hooks_to_advance),
            target_words=chapter.target_words,
        )

    def run_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
        supplied_contract: ChapterContract | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        db = self.database(project_id)
        contract, package = self.prepare_chapter(
            project_id, chapter_number, supplied_contract
        )
        key = idempotency_key or new_id("idem")
        run, created = db.create_run(contract, package.package_id, key)
        if not created:
            return db.run(run["id"])
        if db.chapter_content(chapter_number) is not None:
            db.update_run(
                run["id"],
                status="FAILED",
                stage="VERSION_CONFLICT",
                error_code=VersionConflictError.code,
                error_message=f"chapter {chapter_number} is already committed",
            )
            raise VersionConflictError(f"chapter {chapter_number} is already committed")
        return self._process_run(db, run["id"])

    def resume_run(self, run_id: str) -> dict[str, Any]:
        db = self.find_run(run_id)
        run = db.run(run_id)
        if run["status"] == "COMMITTED":
            return run
        # 0.5.0: the retry budget comes from the effective per-project policy.
        max_revisions = int(self.effective_policy(db)["max_revisions"])
        if run["status"] == "PAUSED" and run.get("error_code") == "RETRY_BUDGET_EXHAUSTED":
            # The retry budget may have been raised since the run paused
            # (e.g. a master agent bumped max_revisions). If there is still
            # headroom, roll the run back to FAILED_RETRYABLE so a resume
            # starts a fresh drafting attempt instead of erroring out.
            if int(run["attempt"]) < max_revisions:
                db.update_run(
                    run_id,
                    status="FAILED_RETRYABLE",
                    stage=run["stage"] or "DRAFTING",
                    error_code=None,
                    error_message=None,
                )
                run = db.run(run_id)
            else:
                raise InvalidRunStateError(
                    f"run {run_id!r} exhausted its retry budget of {max_revisions}"
                )
        if int(run["attempt"]) >= max_revisions:
            db.update_run(
                run_id,
                status="PAUSED",
                stage=run["stage"],
                error_code="RETRY_BUDGET_EXHAUSTED",
                error_message=(
                    f"run reached the maximum of {max_revisions} draft attempts"
                ),
            )
            raise InvalidRunStateError(
                f"run {run_id!r} exhausted its retry budget of {max_revisions}"
            )
        if run["status"] not in {"FAILED_RETRYABLE", "QUALITY_BLOCKED", "RUNNING"}:
            raise InvalidRunStateError(
                f"run {run_id!r} cannot resume from {run['status']}"
            )
        return self._process_run(db, run_id)

    def force_rewrite(self, project_id: str, chapter_number: int) -> dict[str, Any]:
        """Uncommit a chapter so a fresh autorun will re-draft it.

        The orchestrator never re-runs committed chapters, so a chapter that
        committed without a real score (fail-open review) or later fails a
        re-verification cannot be fixed by re-submitting autorun alone. This
        rolls the chapter back to PREPARED (history preserved); the master
        agent then submits a new autorun which picks it up from the pending
        plan and rewrites it through the full write->review->commit loop.
        """
        db = self.database(project_id)
        db.ensure_exists()
        db.uncommit_chapter(chapter_number)
        chapter = db.chapter_overview()
        row = next((c for c in chapter if int(c["chapter_number"]) == chapter_number), None)
        return {
            "project_id": project_id,
            "chapter_number": chapter_number,
            "status": row["status"] if row else "UNKNOWN",
            "message": (
                f"chapter {chapter_number} uncommitted; submit a fresh autorun "
                "to rewrite it through the full review gate"
            ),
        }

    def find_run(self, run_id: str) -> ProjectDatabase:
        if not run_id.startswith("run_"):
            raise ConfigInvalidError("invalid run id")
        if not self.projects_root.exists():
            raise RunNotFoundError(f"run {run_id!r} was not found")
        for project_dir in self.projects_root.iterdir():
            if not project_dir.is_dir():
                continue
            try:
                db = ProjectDatabase(self.projects_root, project_dir.name)
                db.run(run_id)
                return db
            except RunNotFoundError:
                continue
        raise RunNotFoundError(f"run {run_id!r} was not found")

    def run_status(self, run_id: str) -> dict[str, Any]:
        return self.find_run(run_id).run(run_id)

    def _process_run(self, db: ProjectDatabase, run_id: str) -> dict[str, Any]:
        run = db.run(run_id)
        contract = ChapterContract.model_validate(run["contract"])
        project = db.project()
        # 0.5.0: every stage of the chapter loop reads the effective policy.
        policy = self.effective_policy(db)
        max_revisions = int(policy["max_revisions"])
        recent = db.recent_chapters(contract.chapter_number, limit=3)
        package = self.compiler.compile(
            project=project, contract=contract, recent_chapters=recent
        )
        db.save_context(package)
        db.update_run(
            run_id,
            status="RUNNING",
            stage="DRAFTING",
            increment_attempt=True,
        )
        attempt = int(run["attempt"]) + 1
        feedback, previous_scores = self._revision_feedback(db, run)
        try:
            content = self.provider.generate_chapter(
                WriterRequest(
                    project_title=project["title"],
                    contract=contract,
                    context=package,
                    revision_feedback=feedback,
                    previous_scores=previous_scores,
                )
            )
        except Exception as exc:
            db.update_run(
                run_id,
                status="FAILED_RETRYABLE",
                stage="DRAFTING",
                error_code=ModelUnavailableError.code,
                error_message=str(exc),
            )
            raise ModelUnavailableError(
                str(exc), project_id=project["id"], run_id=run_id
            ) from exc

        revision_id = db.save_draft(run_id, contract.chapter_number, content)
        db.update_run(run_id, status="RUNNING", stage="VALIDATING")
        issues = inspect_chapter(
            chapter_number=contract.chapter_number,
            content=content,
            contract=contract,
            recent_chapters=recent,
        )
        blocking = [issue for issue in issues if issue.severity in {"blocker", "error"}]
        stage = "VALIDATING"
        review_summary: dict[str, Any] | None = None
        score_blocked = False
        if not blocking and self.reviewer is not None:
            # Deterministic gate passed: run the optional LLM review before
            # COMMITTING. It merges into the same issue list; llm blockers use
            # the identical QUALITY_BLOCKED chain and fail open on errors.
            stage = "REVIEWING"
            db.update_run(run_id, status="RUNNING", stage=stage)
            llm_issues, verdict = self.reviewer.review(
                project_title=project["title"],
                contract=contract,
                content=content,
                recent_chapters=recent,
                blueprint=project.get("story_spine") or None,
                attempt=attempt,
                score_threshold=float(policy["score_threshold"]),
            )
            issues.extend(llm_issues)
            blocking = [issue for issue in issues if issue.severity in {"blocker", "error"}]
            score_blocked = any(
                issue.issue_type == SCORE_BELOW_THRESHOLD_TYPE
                and issue.severity in {"blocker", "error"}
                for issue in llm_issues
            )
            review_summary = {
                "verdict": verdict.verdict,
                "scores": (
                    verdict.scores.model_dump(mode="json") if verdict.scores else None
                ),
                "overall": overall_score(verdict),
                "attempt": attempt,
            }
            db.append_review_verdict(
                run_id,
                {
                    "attempt": attempt,
                    "verdict": verdict.verdict,
                    "scores": review_summary["scores"],
                    "overall": review_summary["overall"],
                    "issues": [issue.model_dump(mode="json") for issue in verdict.issues],
                    "created_at": utc_now(),
                },
            )
        db.save_issues(run_id, issues)
        if blocking:
            if score_blocked and attempt >= max_revisions:
                message = self._threshold_exhausted_message(
                    review_summary, attempt, float(policy["score_threshold"])
                )
                db.update_run(
                    run_id,
                    status="PAUSED",
                    stage=stage,
                    error_code="SCORE_THRESHOLD_NOT_MET",
                    error_message=message,
                )
                raise QualityGateBlockedError(
                    message, project_id=project["id"], run_id=run_id
                )
            db.update_run(
                run_id,
                status="QUALITY_BLOCKED",
                stage=stage,
                error_code=QualityGateBlockedError.code,
                error_message=f"{len(blocking)} blocking quality issue(s)",
            )
            raise QualityGateBlockedError(
                f"{len(blocking)} blocking quality issue(s)",
                project_id=project["id"],
                run_id=run_id,
            )

        digest = content.replace("\n", " ").strip()[:500]
        delta = ChapterDelta(
            project_id=project["id"],
            chapter_number=contract.chapter_number,
            revision_id=revision_id,
            blueprint_version=project["blueprint_version"],
            events_added=[
                {"type": "required_event", "description": event}
                for event in contract.required_events
            ],
            state_changes=contract.required_state_changes,
            hooks_changed=[
                {"hook": hook, "transition": "planted"}
                for hook in contract.hooks_to_plant
            ]
            + [
                {"hook": hook, "transition": "advanced"}
                for hook in contract.hooks_to_advance
            ],
            blueprint_coverage=[
                {"requirement": event, "covered": True}
                for event in contract.required_events
            ],
            handoff=contract.handoff,
            digest=digest,
        )
        db.update_run(run_id, status="RUNNING", stage="COMMITTING")
        commit = db.finalize(
            run_id=run_id,
            revision_id=revision_id,
            contract=contract,
            delta=delta,
            idempotency_key=f"commit:{run['id']}:{sha256_text(content)}",
        )
        result = db.run(run_id)
        result["commit"] = commit
        result["content"] = content
        result["quality_issues"] = [issue.model_dump(mode="json") for issue in issues]
        if review_summary is not None:
            result["llm_review"] = review_summary
        result["context"] = {
            "package_id": package.package_id,
            "estimated_tokens": package.estimated_tokens,
            "token_budget": package.token_budget,
            "checksum": package.checksum,
        }
        return result

    @staticmethod
    def _revision_feedback(
        db: ProjectDatabase, run: dict[str, Any]
    ) -> tuple[list[dict[str, str]] | None, dict[str, float] | None]:
        """Blocking issues + scores from the previous intercepted draft.

        Returns ``(None, None)`` for first drafts and for retries that were not
        quality-intercepted (e.g. a plain model-unavailable retry), so providers
        never receive empty feedback noise.
        """
        if int(run.get("attempt") or 0) <= 0:
            return None, None
        blocking = db.blocking_issues_for_run(str(run["id"]))
        feedback: list[dict[str, str]] | None = None
        if blocking:
            feedback = [
                {"type": item["type"], "description": item["description"]}
                for item in blocking[:MAX_REVISION_FEEDBACK_ITEMS]
            ]
        history = run.get("review") or []
        previous_scores: dict[str, float] | None = None
        if history:
            scores = history[-1].get("scores")
            if isinstance(scores, dict):
                previous_scores = {str(k): float(v) for k, v in scores.items()}
        return feedback, previous_scores

    def _threshold_exhausted_message(
        self,
        review_summary: dict[str, Any] | None,
        attempt: int,
        threshold: float,
    ) -> str:
        """PAUSED reason when the score threshold survives max_revisions tries."""
        summary = review_summary or {}
        scores = summary.get("scores") or {}
        return (
            f"score threshold not met after {attempt} attempts "
            f"(threshold: {threshold}); final scores: "
            f"contract_adherence={scores.get('contract_adherence')}, "
            f"era_authenticity={scores.get('era_authenticity')}, "
            f"flow={scores.get('flow')}, overall={summary.get('overall')}"
        )

    def export(self, project_id: str, export_format: str) -> dict[str, Any]:
        db = self.database(project_id)
        content = db.export(export_format)
        extension = "md" if export_format == "markdown" else "txt"
        export_path = db.project_dir / "exports" / f"manuscript.{extension}"
        export_path.write_text(content, encoding="utf-8")
        return {
            "format": export_format,
            "content": content,
            "path": str(export_path),
            "sha256": sha256_text(content),
        }
