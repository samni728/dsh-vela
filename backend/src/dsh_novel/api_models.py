from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dsh_novel.domain import ChapterContract


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreateRequest(APIModel):
    title: str = Field(min_length=1, max_length=200)
    premise: str = Field(default="", max_length=20000)
    target_chapters: int = Field(default=10, ge=1, le=3000)
    hard_rules: list[str] = Field(default_factory=list)
    story_spine: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{3,64}$")


class ChapterPrepareRequest(APIModel):
    contract: ChapterContract | None = None


class ChapterRunRequest(APIModel):
    contract: ChapterContract | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class ResumeRunRequest(APIModel):
    force_redraft: bool = True


class ExportRequest(APIModel):
    format: Literal["markdown", "text"] = "markdown"

