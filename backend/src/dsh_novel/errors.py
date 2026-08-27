from __future__ import annotations

from typing import Any


class NovelError(Exception):
    code = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.project_id = project_id
        self.run_id = run_id
        # Optional structured payload surfaced in the error envelope.
        self.details = details


class ProjectNotFoundError(NovelError):
    code = "PROJECT_NOT_FOUND"


class RunNotFoundError(NovelError):
    code = "RUN_NOT_FOUND"


class ChapterNotFoundError(NovelError):
    code = "CHAPTER_NOT_FOUND"


class ContextBudgetExceededError(NovelError):
    code = "CONTEXT_BUDGET_EXCEEDED"


class QualityGateBlockedError(NovelError):
    code = "QUALITY_GATE_BLOCKED"


class ModelUnavailableError(NovelError):
    code = "MODEL_UNAVAILABLE"


class VersionConflictError(NovelError):
    code = "VERSION_CONFLICT"


class InvalidRunStateError(NovelError):
    code = "RUN_STATE_INVALID"


class ConfigInvalidError(NovelError):
    code = "CONFIG_INVALID"


class OrchestratorBusyError(NovelError):
    code = "ORCHESTRATOR_BUSY"


class ReportNotFoundError(NovelError):
    code = "REPORT_NOT_FOUND"
