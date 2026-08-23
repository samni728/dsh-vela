from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterContract(StrictModel):
    chapter_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1)
    required_events: list[str] = Field(default_factory=list)
    required_state_changes: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_changes: list[dict[str, Any]] = Field(default_factory=list)
    hooks_to_plant: list[str] = Field(default_factory=list)
    hooks_to_advance: list[str] = Field(default_factory=list)
    handoff: str = ""
    target_words: int = Field(default=3500, ge=100, le=20000)


class ContextBlock(StrictModel):
    kind: Literal[
        "hard_rules",
        "story_spine",
        "arc_state",
        "chapter_contract",
        "entity_state",
        "continuity_bridge",
        "retrieved_evidence",
        "style",
    ]
    priority: int = Field(ge=0, le=100)
    required: bool
    estimated_tokens: int = Field(ge=0)
    content: str


class ContextPackage(StrictModel):
    package_id: str
    project_id: str
    chapter_number: int = Field(ge=1)
    task: str
    canon_version: int = Field(ge=0)
    blueprint_version: int = Field(ge=1)
    token_budget: int = Field(gt=0)
    estimated_tokens: int = Field(ge=0)
    blocks: list[ContextBlock]
    omitted: list[dict[str, Any]] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    checksum: str


class ChapterDelta(StrictModel):
    project_id: str
    chapter_number: int = Field(ge=1)
    revision_id: str
    blueprint_version: int = Field(ge=1)
    events_added: list[dict[str, Any]] = Field(default_factory=list)
    state_changes: list[dict[str, Any]] = Field(default_factory=list)
    hooks_changed: list[dict[str, Any]] = Field(default_factory=list)
    blueprint_coverage: list[dict[str, Any]] = Field(default_factory=list)
    handoff: str = ""
    digest: str
    extraction_confidence: float = Field(default=1.0, ge=0, le=1)


class QualityIssue(StrictModel):
    issue_id: str
    issue_type: str
    severity: Literal["blocker", "error", "warning"]
    chapter_number: int
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    source_hash: str
    instruction: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)

