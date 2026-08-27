from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from dsh_novel.domain import (
    ChapterContract,
    ContextPackage,
    OutlineResult,
    ReviewVerdict,
)


@dataclass(frozen=True, slots=True)
class WriterRequest:
    project_title: str
    contract: ChapterContract
    context: ContextPackage
    # 0.5.0 revision feedback: blocking issues ({type, description}) carried
    # from the previous intercepted draft, plus its review scores. Empty/None
    # on first drafts and on retries that were not quality-intercepted.
    revision_feedback: list[dict[str, str]] | None = None
    previous_scores: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """Structured review input: blueprint + contract + chapter + recent digests."""

    project_title: str
    contract: ChapterContract
    content: str
    recent_chapters: list[dict[str, str]] = field(default_factory=list)
    # Whole-book story spine summary for blueprint-aware review.
    blueprint: dict[str, Any] | None = None
    # 1-based draft attempt counter; lets deterministic providers vary scores.
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class OutlineRequest:
    """Input for the outline agent: whole-book structured outline generation."""

    title: str
    premise: str = ""
    hard_rules: list[str] = field(default_factory=list)
    target_chapters: int = 10
    target_words: int = 3500


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    """Input for the chapter-state extractor: turn one chapter's prose into the
    structured core information the continuation mechanism needs.

    This is the *real* delta: what changed for the characters, what happened
    to each hook, and whether the chapter contains a twist — extracted from
    the finalised prose, not echoed from the contract.
    """

    project_title: str
    contract: ChapterContract
    content: str
    # Previous chapters' compressed core info (character_changes/hooks_status)
    # so the extractor can state what *changed* relative to the last chapter.
    previous_delta: dict[str, Any] | None = None


class ModelProvider(Protocol):
    name: str

    def generate_chapter(self, request: WriterRequest) -> str: ...

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict: ...

    def generate_outline(self, request: OutlineRequest) -> OutlineResult: ...

    def extract_chapter_state(self, request: ExtractionRequest) -> dict[str, Any]: ...


def default_generate_outline(request: OutlineRequest) -> OutlineResult:
    """Default outline capability for providers that do not implement it."""
    raise NotImplementedError(
        "provider does not implement outline generation "
        f"(requested {request.target_chapters} chapters)"
    )


_THINK_BLOCK_RE = re.compile(
    r"<think\b[^>]*>.*?(?:</think\s*>|\Z)", re.IGNORECASE | re.DOTALL
)
_STRAY_TAG_RE = re.compile(r"</?(?:think|analysis|system|assistant)>", re.IGNORECASE)
_LEADING_FENCE_RE = re.compile(r"\A```[a-zA-Z0-9_-]*\s*")
_TRAILING_FENCE_RE = re.compile(r"\s*```\s*\Z")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks and stray protocol tags."""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _STRAY_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def _compact_validation_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"])
        parts.append(f"{location or '<root>'}: {error['msg']}")
    return "; ".join(parts)


def _strip_payload_fences(cleaned: str) -> str:
    cleaned = _LEADING_FENCE_RE.sub("", cleaned)
    return _TRAILING_FENCE_RE.sub("", cleaned).strip()


def parse_review_payload(raw: str) -> ReviewVerdict:
    """Parse a strict review JSON payload; raise ValueError when illegal.

    Mirrors the POLLUTION_PATTERNS idea: reasoning tags are stripped before any
    parsing is attempted, then the remaining text must be (or contain) one JSON
    object matching the ReviewVerdict schema.
    """
    cleaned = strip_think_blocks(raw)
    cleaned = _strip_payload_fences(cleaned)
    candidates = [cleaned]
    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            return ReviewVerdict.model_validate(data)
        except ValidationError:
            continue
    raise ValueError("review response did not contain a valid verdict JSON object")


def parse_extraction_payload(raw: str) -> dict[str, Any]:
    """Parse a strict chapter-state extraction JSON payload.

    The provider's extractor is fail-open, so a malformed response raises
    ValueError here and the caller falls back to empty extraction (the service
    then keeps contract-echo deltas rather than blocking the run).
    """
    cleaned = strip_think_blocks(raw)
    cleaned = _strip_payload_fences(cleaned)
    candidates = [cleaned]
    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        # Lenient: unknown keys ignored; only pull the fields we persist.
        result: dict[str, Any] = {
            "character_changes": data.get("character_changes") or [],
            "hooks_status": data.get("hooks_status") or [],
            "twist": str(data.get("twist") or ""),
            "next_chapter_hook": str(data.get("next_chapter_hook") or ""),
        }
        if not isinstance(result["character_changes"], list) or not isinstance(
            result["hooks_status"], list
        ):
            continue
        return result
    raise ValueError(
        "extraction response did not contain a valid extraction JSON object"
    )


def parse_outline_payload(raw: str, *, target_chapters: int) -> OutlineResult:
    """Parse a strict outline JSON payload; raise ValueError when illegal.

    After stripping think blocks and fences the text must be (or contain) one
    JSON object whose ``chapters`` array covers exactly chapter numbers
    1..target_chapters in order, with every field typed per OutlineChapter.
    """
    cleaned = strip_think_blocks(raw)
    cleaned = _strip_payload_fences(cleaned)
    candidates = [cleaned]
    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        candidates.append(match.group(0))
    problems: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError as exc:
            problems.append(f"invalid JSON: {exc}")
            continue
        if not isinstance(data, dict):
            problems.append(f"expected a JSON object, got {type(data).__name__}")
            continue
        try:
            outline = OutlineResult.model_validate(data)
        except ValidationError as exc:
            problems.append(f"schema mismatch: {_compact_validation_errors(exc)}")
            continue
        numbers = [chapter.chapter_number for chapter in outline.chapters]
        expected = list(range(1, target_chapters + 1))
        if numbers != expected:
            problems.append(
                f"chapter numbers must be consecutive 1..{target_chapters}, got {numbers}"
            )
            continue
        return outline
    raise ValueError("; ".join(problems) or "outline response was empty")


def default_review_verdict() -> ReviewVerdict:
    """Fail-open default used when a provider offers no review capability."""
    return ReviewVerdict(verdict="pass", issues=[], scores=None)
