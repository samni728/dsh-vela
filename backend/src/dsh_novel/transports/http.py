from __future__ import annotations

import secrets
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
from dsh_novel.errors import NovelError
from dsh_novel.providers import (
    DeterministicFakeProvider,
    ModelProvider,
    OpenAICompatibleProvider,
)
from dsh_novel.util import new_id

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
        return DeterministicFakeProvider()
    if settings.model_provider == "openai_compatible":
        return OpenAICompatibleProvider(
            endpoint=settings.model_endpoint,
            model=settings.model_name,
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            review_timeout_seconds=settings.review_timeout_seconds,
            outline_timeout_seconds=settings.outline_timeout_seconds,
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
    selected_provider = provider or build_provider(settings)
    service = NovelService(
        projects_root=settings.data_dir / "projects",
        provider=selected_provider,
        context_token_budget=settings.context_token_budget,
        reviewer=build_reviewer(settings, selected_provider),
        max_revisions=settings.max_revisions,
    )
    orchestrator = AutorunManager(service, max_revisions=settings.max_revisions)
    app = FastAPI(
        title="DSH Novel Sidecar",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.autorun = orchestrator

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
        created = service.create_project(
            ProjectCreateRequest(
                title=body.title,
                premise=body.premise,
                target_chapters=body.target_chapters,
                hard_rules=body.hard_rules,
            )
        )
        project_id = created["id"]
        # Persist the requested policy (or the defaults on first set) before
        # outline + autorun so the whole run reads one effective policy.
        policy = service.resolve_policy(
            service.database(project_id),
            body.policy.model_dump(exclude_none=True) if body.policy else None,
        )
        service.generate_outline(
            project_id,
            target_words=body.target_words or int(policy["target_words"]),
        )
        status = orchestrator.start(project_id, None, None)
        return envelope(
            result={
                "project_id": project_id,
                "state": status["state"],
                "autorun": status,
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
