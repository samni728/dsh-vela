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
    # 章节蓝图扩展：本章涉及的人物关系 + 本章反转/转折点（编排阶段产出）。
    characters: list[str] = Field(default_factory=list)
    twist: str = ""


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
    # 续写核心信息：本章人物关系的真实变化（抽取自正文，非合同回声）。
    character_changes: list[dict[str, Any]] = Field(default_factory=list)
    # 伏笔真实状态：{"hook", "status"(planted/advanced/resolved), "evidence"}。
    hooks_status: list[dict[str, Any]] = Field(default_factory=list)
    # 本章实际发生的反转/转折。
    twist: str = ""
    # 章末钩子：人物当前面对的状态/悬念（下一章续写锚点）。
    next_chapter_hook: str = ""


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
    # Where the issue came from: deterministic rules or the optional LLM
    # reviewer. Defaults to "rule" so pre-existing serialized issues stay valid.
    source: Literal["rule", "llm"] = "rule"


class ReviewIssue(StrictModel):
    severity: Literal["blocker", "warning"]
    type: str = Field(min_length=1)
    description: str


class ReviewScores(StrictModel):
    contract_adherence: float = Field(ge=0, le=10)
    era_authenticity: float = Field(ge=0, le=10)
    flow: float = Field(ge=0, le=10)


class ReviewVerdict(StrictModel):
    verdict: Literal["pass", "blocked"]
    issues: list[ReviewIssue] = Field(default_factory=list)
    scores: ReviewScores | None = None


class ChapterStateExtraction(StrictModel):
    """Structured core information extracted from one chapter's prose.

    Feeds the continuation mechanism: the next chapter's context reads this
    instead of only a 500-char digest, so character relationship changes and
    hook status survive across chapters.
    """

    # {"character": "赵峥", "before": "...", "after": "...", "relation": "..."}
    character_changes: list[dict[str, Any]] = Field(default_factory=list)
    # {"hook": "蜡烛上刻着日期", "status": "planted|advanced|resolved", "evidence": "..."}
    hooks_status: list[dict[str, Any]] = Field(default_factory=list)
    # 本章反转/转折（无则为空）。
    twist: str = ""
    # 章末衔接状态：人物接下来面对什么（下一章的续写锚点）。
    next_chapter_hook: str = ""


class OutlineChapter(StrictModel):
    """One chapter entry of a generated outline.

    章节蓝图：除了目的/事件/伏笔，还包含本章人物关系与反转点，全部
    结构化存库，供续写机制读取（人物核心变化 + 蓝图 + 钩子压缩续写）。
    """

    chapter_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1)
    required_events: list[str] = Field(default_factory=list)
    hooks_to_plant: list[str] = Field(default_factory=list)
    hooks_to_advance: list[str] = Field(default_factory=list)
    target_words: int = Field(default=3500, ge=100, le=20000)
    # 本章涉及的人物及其关系/状态（如 "赵峥（壮熊班长，外表钢铁直男）"）。
    characters: list[str] = Field(default_factory=list)
    # 本章反转/转折点（可为空）。
    twist: str = ""
    # 章末衔接：上一章结尾状态如何导向本章（续写锚点）。
    handoff: str = ""


class OutlineResult(StrictModel):
    """Structured whole-book outline produced by the outline agent."""

    story_spine: dict[str, Any]
    chapters: list[OutlineChapter] = Field(default_factory=list)

