from __future__ import annotations

from pathlib import Path
from typing import Any

from dsh_novel.api_models import ProjectCreateRequest
from dsh_novel.application.context import ContextCompiler
from dsh_novel.application.quality import inspect_chapter
from dsh_novel.domain import ChapterContract, ChapterDelta, ContextPackage
from dsh_novel.errors import (
    ConfigInvalidError,
    InvalidRunStateError,
    ModelUnavailableError,
    QualityGateBlockedError,
    RunNotFoundError,
    VersionConflictError,
)
from dsh_novel.infrastructure import ProjectDatabase
from dsh_novel.providers import ModelProvider, WriterRequest
from dsh_novel.util import new_id, sha256_text


class NovelService:
    def __init__(
        self,
        *,
        projects_root: Path,
        provider: ModelProvider,
        context_token_budget: int,
    ) -> None:
        self.projects_root = projects_root
        self.provider = provider
        self.compiler = ContextCompiler(context_token_budget)

    def database(self, project_id: str) -> ProjectDatabase:
        return ProjectDatabase(self.projects_root, project_id)

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
        if int(run["attempt"]) >= 3:
            db.update_run(
                run_id,
                status="PAUSED",
                stage=run["stage"],
                error_code="RETRY_BUDGET_EXHAUSTED",
                error_message="run reached the maximum of 3 draft attempts",
            )
            raise InvalidRunStateError(
                f"run {run_id!r} exhausted its retry budget"
            )
        if run["status"] not in {"FAILED_RETRYABLE", "QUALITY_BLOCKED", "RUNNING"}:
            raise InvalidRunStateError(
                f"run {run_id!r} cannot resume from {run['status']}"
            )
        return self._process_run(db, run_id)

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
        try:
            content = self.provider.generate_chapter(
                WriterRequest(
                    project_title=project["title"],
                    contract=contract,
                    context=package,
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
        db.save_issues(run_id, issues)
        if blocking:
            db.update_run(
                run_id,
                status="QUALITY_BLOCKED",
                stage="VALIDATING",
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
        result["context"] = {
            "package_id": package.package_id,
            "estimated_tokens": package.estimated_tokens,
            "token_budget": package.token_budget,
            "checksum": package.checksum,
        }
        return result

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
