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


class OutlineGenerateRequest(APIModel):
    """Optional body of POST /projects/{id}/outline."""

    target_words: int | None = Field(default=None, ge=100, le=20000)


class PolicyInput(APIModel):
    """Partial per-project writing policy; omitted keys fall through the merge
    chain (request policy > stored policy_json > settings defaults)."""

    score_threshold: float | None = Field(default=None, ge=0, le=10)
    max_revisions: int | None = Field(default=None, ge=1, le=20)
    target_words: int | None = Field(default=None, ge=100, le=20000)
    on_chapter_failure: Literal["skip_continue", "pause"] | None = None


class AutorunRequest(APIModel):
    """Optional body of POST /projects/{id}/autorun; defaults to 1..target.

    ``policy`` optionally overrides the per-project writing policy for this
    run; the merged effective policy is persisted on first set.
    """

    from_chapter: int | None = Field(default=None, ge=1)
    to_chapter: int | None = Field(default=None, ge=1)
    policy: PolicyInput | None = None


class AutoCreateRequest(APIModel):
    """One-shot entry: create project + outline + start autorun."""

    title: str = Field(min_length=1, max_length=200)
    premise: str = Field(default="", max_length=20000)
    target_chapters: int = Field(default=10, ge=1, le=3000)
    hard_rules: list[str] = Field(default_factory=list)
    target_words: int | None = Field(default=None, ge=100, le=20000)
    policy: PolicyInput | None = None

