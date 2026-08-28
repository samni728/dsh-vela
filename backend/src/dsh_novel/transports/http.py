from __future__ import annotations

import secrets
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from dsh_novel import PROTOCOL_VERSION, __version__
from dsh_novel.api_models import (
    AutoCreateRequest,
    AutorunRequest,
    ChapterPrepareRequest,
    ChapterRunRequest,
    ExportRequest,
    OutlineGenerateRequest,
    ProjectCreateRequest,
    ResumeRunRequest,
)
from dsh_novel.application import NovelService
from dsh_novel.application.orchestrator import AutorunManager
from dsh_novel.application.reviewer import ChapterReviewer
from dsh_novel.config import Settings
from dsh_novel.errors import NovelError, OrchestratorBusyError
from dsh_novel.providers import (
    DeterministicFakeProvider,
    ModelProvider,
    OpenAICompatibleProvider,
    serialize_provider,
)
from dsh_novel.util import canonical_json, new_id, sha256_text

CAPABILITIES = [
    "project.create",
    "project.status",
    "chapter.run",
    "run.status",
    "run.resume",
    "manuscript.export",
]


def build_provider(settings: Settings) -> ModelProvider:
    if settings.model_provider == "fake":
        return serialize_provider(DeterministicFakeProvider())
    if settings.model_provider == "openai_compatible":
        return serialize_provider(
            OpenAICompatibleProvider(
                endpoint=settings.model_endpoint,
                model=settings.model_name,
                api_key=settings.model_api_key,
                timeout_seconds=settings.model_timeout_seconds,
                max_output_tokens=settings.model_max_output_tokens,
                review_timeout_seconds=settings.review_timeout_seconds,
                outline_timeout_seconds=settings.outline_timeout_seconds,
                lock_path=settings.data_dir / "runtime" / "model-request.lock",
            )
        )
    raise ValueError(f"unsupported model provider: {settings.model_provider}")


def envelope(
    *,
    result: Any = None,
    project_id: str | None = None,
    run_id: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "request_id": new_id("req"),
        "project_id": project_id,
        "run_id": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "result": result,
        "warnings": warnings or [],
        "error": None,
    }


def error_envelope(
    code: str,
    message: str,
    details: Any = None,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": new_id("req"),
        "project_id": project_id,
        "run_id": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "result": None,
        "warnings": [],
        "error": {"code": code, "message": message, "details": details},
    }


def build_reviewer(settings: Settings, provider: ModelProvider) -> ChapterReviewer | None:
    """Optional LLM reviewer; disabled via config keeps the deterministic gate only."""
    if not settings.review_enabled:
        return None
    return ChapterReviewer(
        provider=provider,
        timeout_seconds=settings.review_timeout_seconds,
        score_threshold=settings.score_threshold,
    )


def create_app(
    settings: Settings | None = None,
    provider: ModelProvider | None = None,
) -> FastAPI:
    settings = settings or Settings()
    selected_provider = serialize_provider(provider or build_provider(settings))
    service = NovelService(
        projects_root=settings.data_dir / "projects",
        provider=selected_provider,
        context_token_budget=settings.context_token_budget,
        reviewer=build_reviewer(settings, selected_provider),
        max_revisions=settings.max_revisions,
    )
    orchestrator = AutorunManager(service, max_revisions=settings.max_revisions)
    auto_request_lock = threading.Lock()
    app = FastAPI(
        title="DSH Novel Sidecar",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.autorun = orchestrator

    def reject_manual_model_work_while_autorun(action: str) -> None:
        active_project = orchestrator.active_project_id()
        if active_project is None:
            return
        raise OrchestratorBusyError(
            f"cannot {action} while the serial autorun lane is running; poll status",
            project_id=active_project,
            details={
                "active_project_id": active_project,
                "action": "poll_status",
            },
        )

    @app.middleware("http")
    async def authenticate_api(request: Request, call_next):  # type: ignore[no-untyped-def]
        if settings.auth_token is not None and request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {settings.auth_token}"
            if not secrets.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content=error_envelope(
                        "AUTH_REQUIRED",
                        "a valid DSH Novel bearer token is required",
                    ),
                )
        return await call_next(request)

    @app.exception_handler(NovelError)
    def handle_novel_error(_request: Request, exc: NovelError) -> JSONResponse:
        status = 409
        if exc.code in {"PROJECT_NOT_FOUND", "RUN_NOT_FOUND", "REPORT_NOT_FOUND"}:
            status = 404
        elif exc.code in {"MODEL_UNAVAILABLE", "INTERNAL_ERROR"}:
            status = 503
        return JSONResponse(
            status_code=status,
            content=error_envelope(
                exc.code,
                str(exc),
                getattr(exc, "details", None),
                project_id=exc.project_id,
                run_id=exc.run_id,
            ),
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                "CONFIG_INVALID",
                "request validation failed",
                exc.errors(),
            ),
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "core_version": __version__,
            "provider": selected_provider.name,
            "model_execution": selected_provider.snapshot(),
            "recovered_interrupted_runs": service.recovered_interrupted_runs,
        }

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "core_version": __version__,
            "capabilities": CAPABILITIES,
            "optional_capabilities": {
                "embedding": False,
                "rerank": False,
                "llm_review": settings.review_enabled,
                "outline": True,
                "autorun": True,
                "serial_model_execution": True,
            },
        }

    @app.post("/api/v1/projects")
    def create_project(body: ProjectCreateRequest) -> dict[str, Any]:
        result = service.create_project(body)
        return envelope(result=result, project_id=result["id"])

    @app.get("/api/v1/projects/{project_id}")
    def project_status(project_id: str) -> dict[str, Any]:
        result = service.project_status(project_id)
        return envelope(result=result, project_id=project_id)

    @app.post("/api/v1/projects/{project_id}/outline")
    def generate_outline(
        project_id: str, body: OutlineGenerateRequest | None = None
    ) -> dict[str, Any]:
        reject_manual_model_work_while_autorun("generate an outline")
        body = body or OutlineGenerateRequest()
        result = service.generate_outline(
            project_id, target_words=body.target_words, persist_mode="all"
        )
        return envelope(result=result, project_id=project_id)

    @app.post("/api/v1/projects/{project_id}/chapters/{chapter_number}/prepare")
    def prepare_chapter(
        project_id: str,
        chapter_number: int,
        body: ChapterPrepareRequest | None = None,
    ) -> dict[str, Any]:
        contract, package = service.prepare_chapter(
            project_id,
            chapter_number,
            body.contract if body else None,
        )
        return envelope(
            result={
                "contract": contract.model_dump(mode="json"),
                "context": package.model_dump(mode="json"),
            },
            project_id=project_id,
        )

    @app.post("/api/v1/projects/{project_id}/chapters/{chapter_number}/run")
    def run_chapter(
        project_id: str,
        chapter_number: int,
        body: ChapterRunRequest | None = None,
    ) -> dict[str, Any]:
        reject_manual_model_work_while_autorun("run a chapter manually")
        body = body or ChapterRunRequest()
        result = service.run_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
            supplied_contract=body.contract,
            idempotency_key=body.idempotency_key,
        )
        return envelope(
            result=result,
            project_id=project_id,
            run_id=result["id"],
        )

    @app.get("/api/v1/runs/{run_id}")
    def run_status(run_id: str) -> dict[str, Any]:
        result = service.run_status(run_id)
        return envelope(result=result, run_id=run_id)

    @app.post("/api/v1/runs/{run_id}/resume")
    def resume_run(
        run_id: str, _body: ResumeRunRequest | None = None
    ) -> dict[str, Any]:
        reject_manual_model_work_while_autorun("resume a run manually")
        result = service.resume_run(run_id)
        return envelope(result=result, run_id=run_id)

    @app.post("/api/v1/projects/{project_id}/export")
    def export_project(
        project_id: str, body: ExportRequest | None = None
    ) -> dict[str, Any]:
        body = body or ExportRequest()
        result = service.export(project_id, body.format)
        return envelope(result=result, project_id=project_id)

    # ------------------------------------------------------------- autorun

    @app.post("/api/v1/projects/{project_id}/autorun")
    def start_autorun(
        project_id: str, body: AutorunRequest | None = None
    ) -> dict[str, Any]:
        body = body or AutorunRequest()
        result = orchestrator.start(
            project_id,
            body.from_chapter,
            body.to_chapter,
            policy=body.policy.model_dump(exclude_none=True) if body.policy else None,
        )
        return envelope(result=result, project_id=project_id)

    @app.get("/api/v1/projects/{project_id}/autorun")
    def autorun_status(project_id: str) -> dict[str, Any]:
        return envelope(result=orchestrator.status(project_id), project_id=project_id)

    @app.get("/api/v1/projects/{project_id}/pipeline")
    def project_pipeline(project_id: str) -> dict[str, Any]:
        # Management plane: numbers and status only — zero prose by contract.
        return envelope(result=orchestrator.pipeline(project_id), project_id=project_id)

    @app.get("/api/v1/projects/{project_id}/report")
    def project_report(project_id: str) -> dict[str, Any]:
        return envelope(result=orchestrator.report(project_id), project_id=project_id)

    @app.post("/api/v1/auto")
    def auto_create(body: AutoCreateRequest) -> dict[str, Any]:
        # Stable even when an older Adapter does not send an explicit key.
        # Identical retries therefore resolve to one durable project instead
        # of starting several books after a client-side timeout.
        fingerprint_body = body.model_dump(
            mode="json", exclude={"idempotency_key"}, exclude_none=True
        )
        fingerprint_seed = body.idempotency_key or canonical_json(fingerprint_body)
        project_id = f"auto_{sha256_text(fingerprint_seed)[:24]}"

        # Keep check/create/start atomic across simultaneous Harness retries.
        # This lock is held for milliseconds: outline generation now belongs
        # to the background autorun thread and the endpoint returns quickly.
        with auto_request_lock:
            active_project = orchestrator.active_project_id()
            if active_project is not None and active_project != project_id:
                raise OrchestratorBusyError(
                    "the serial autorun lane is already running another project; "
                    "poll its status instead of submitting new work",
                    project_id=active_project,
                    details={
                        "active_project_id": active_project,
                        "requested_project_id": project_id,
                        "action": "poll_status",
                    },
                )

            db = service.database(project_id)
            reused = db.path.is_file()
            if not reused:
                service.create_project(
                    ProjectCreateRequest(
                        project_id=project_id,
                        title=body.title,
                        premise=body.premise,
                        target_chapters=body.target_chapters,
                        hard_rules=body.hard_rules,
                    )
                )

            # Persist the requested policy (or defaults) before autorun.  The
            # first chapter's background preparation generates the outline.
            policy = service.resolve_policy(
                service.database(project_id),
                body.policy.model_dump(exclude_none=True) if body.policy else None,
            )
            if body.target_words is not None:
                policy["target_words"] = body.target_words
                service.database(project_id).save_policy(policy)

            if active_project == project_id:
                status = orchestrator.status(project_id)
            else:
                status = orchestrator.start(
                    project_id, None, None, policy=policy
                )
        return envelope(
            result={
                "project_id": project_id,
                "state": status["state"],
                "autorun": status,
                "reused": reused,
                "next_action": "poll_status",
            },
            project_id=project_id,
        )

    return app


def __getattr__(name: str) -> Any:
    # Lazily build the default app (PEP 562) so importing this module has no
    # config-file side effects; `uvicorn dsh_novel.transports.http:app` still
    # resolves, and an invalid config file fails loudly at that point.
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
