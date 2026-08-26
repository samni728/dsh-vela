from __future__ import annotations

import re

from dsh_novel.domain import ChapterContract, QualityIssue
from dsh_novel.util import new_id, normalized_text, sha256_text

POLLUTION_PATTERNS = (
    re.compile(r"<\/?(?:think|analysis|system|assistant)>", re.IGNORECASE),
    re.compile(r"(?:system\s*prompt|系统提示词|作为(?:一个|AI|语言模型))", re.IGNORECASE),
    re.compile(r"^```(?:json|yaml)", re.IGNORECASE | re.MULTILINE),
)

# dense_short_line_repeat: a "short line" is a paragraph whose normalized text is
# 6..23 chars. When the same normalized short line recurs three times with the
# 1st and 3rd occurrence at most DENSE_SHORT_LINE_WINDOW_SLOTS apart AND the
# surrounding exchange repeats too (at least two of the three occurrences share
# the same normalized neighbour paragraph), the chapter is retelling the same
# scene (incident: ch8 rewrote its kiss scene three times, 237 slots apart).
# Cross-scene verbal tics — the same reply ("知道。") in unrelated scenes —
# have different neighbours and never trigger, whatever the distance.
SHORT_LINE_MIN_CHARS = 6
SHORT_LINE_MAX_CHARS = 23
DENSE_SHORT_LINE_WINDOW_SLOTS = 400

# cross_chapter_exact_repeat: paragraphs of at least this many normalized chars
# that appear verbatim in any recent chapter (incident: ch5 lamp description
# copied word-for-word into ch6).
CROSS_CHAPTER_MIN_CHARS = 40

# truncated_ending: the final non-empty paragraph must end with sentence-final
# punctuation (incident: ch10 stopped mid-sentence). Full-width closing quotes
# are included because Chinese dialogue routinely ends 说："……"。 with the
# period inside the quotes.
SENTENCE_ENDING_PUNCTUATION = ("。", "！", "？", "…", '"', "'", "」", "』", "!", "?", ".", "”", "’")

# required_event_keyword_missing: keywords are tokenized from each contract
# event description — >=2-char alnum tokens plus, for Chinese (which has no
# spaces), character bigrams of every CJK run as dependency-free "word tokens".
# When NONE of an event's keywords appears in the chapter the event was likely
# skipped; reported as a warning for the LLM reviewer and humans, never a
# blocker. Any single keyword hit counts as possible rewritten coverage.
KEYWORD_ALNUM_RE = re.compile(r"[A-Za-z0-9]{2,}")
KEYWORD_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _paragraphs(content: str) -> list[tuple[int, int, str]]:
    results: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", content):
        paragraph = match.group(0).strip()
        if paragraph and not paragraph.startswith("#"):
            results.append((match.start(), match.end(), paragraph))
    return results


def _shingles(value: str, size: int = 5) -> set[str]:
    normalized = normalized_text(value)
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}


def _similarity(left: str, right: str) -> float:
    left_set = _shingles(left)
    right_set = _shingles(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def inspect_dense_short_line_repeat(
    *, chapter_number: int, content: str
) -> list[QualityIssue]:
    """Blocker: the same short line recurs densely inside a sliding window.

    Short lines (normalized length 6..23) are grouped by normalized text. For
    each group with at least three occurrences, every consecutive triple is
    checked: if the 1st and 3rd occurrence of a triple are at most
    ``DENSE_SHORT_LINE_WINDOW_SLOTS`` short-line slots apart AND at least two
    of the three occurrences share the same normalized neighbour paragraph,
    the chapter is retelling the same scene — a true retell repeats the
    surrounding exchange as well. Cross-scene verbal tics keep their own
    neighbours and stay untouched.
    """
    all_slots = [
        (start, end, paragraph, normalized_text(paragraph))
        for start, end, paragraph in _paragraphs(content)
    ]
    norms = [normalized for _, _, _, normalized in all_slots]
    short_positions = [
        index
        for index, (_, _, _, normalized) in enumerate(all_slots)
        if SHORT_LINE_MIN_CHARS <= len(normalized) <= SHORT_LINE_MAX_CHARS
    ]

    def _context(slot_index: int) -> tuple[str, str]:
        previous = norms[slot_index - 1] if slot_index > 0 else ""
        following = (
            norms[slot_index + 1] if slot_index + 1 < len(norms) else ""
        )
        return (previous, following)

    groups: dict[str, list[int]] = {}
    for slot_index in short_positions:
        groups.setdefault(norms[slot_index], []).append(slot_index)

    issues: list[QualityIssue] = []
    for normalized, positions in groups.items():
        if len(positions) < 3:
            continue
        for first, second, third in zip(
            positions, positions[1:], positions[2:], strict=False
        ):
            if third - first > DENSE_SHORT_LINE_WINDOW_SLOTS:
                continue
            contexts = (_context(first), _context(second), _context(third))
            previous_neighbours = {context[0] for context in contexts}
            following_neighbours = {context[1] for context in contexts}
            if (
                len(previous_neighbours) == len(contexts)
                and len(following_neighbours) == len(contexts)
            ):
                continue
            sample_start, sample_end, sample_text, _ = all_slots[third]
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type="dense_short_line_repeat",
                    severity="blocker",
                    chapter_number=chapter_number,
                    span_start=sample_start,
                    span_end=sample_end,
                    source_hash=sha256_text(normalized),
                    instruction=(
                        "同一短行在小窗口内循环出现了三次以上，"
                        "删除或改写重复的对话循环，不要重写整章。"
                    ),
                    evidence=[
                        f"text:{sample_text}",
                        f"occurrence_slots:{first},{second},{third}",
                        f"window_slots:{third - first}",
                    ],
                )
            )
            break
    return issues


def inspect_cross_chapter_exact_repeat(
    *,
    chapter_number: int,
    content: str,
    recent_chapters: list[dict[str, object]],
) -> list[QualityIssue]:
    """Blocker: a >=40-char normalized paragraph copied verbatim from a recent chapter."""
    historical: dict[str, int] = {}
    for chapter in recent_chapters:
        source_chapter = int(chapter["chapter_number"])
        for _, _, paragraph in _paragraphs(str(chapter["content"])):
            normalized = normalized_text(paragraph)
            if len(normalized) >= CROSS_CHAPTER_MIN_CHARS:
                historical.setdefault(normalized, source_chapter)

    issues: list[QualityIssue] = []
    reported: set[str] = set()
    for start, end, paragraph in _paragraphs(content):
        normalized = normalized_text(paragraph)
        if len(normalized) < CROSS_CHAPTER_MIN_CHARS or normalized in reported:
            continue
        source_chapter = historical.get(normalized)
        if source_chapter is None:
            continue
        reported.add(normalized)
        issues.append(
            QualityIssue(
                issue_id=new_id("issue"),
                issue_type="cross_chapter_exact_repeat",
                severity="blocker",
                chapter_number=chapter_number,
                span_start=start,
                span_end=end,
                source_hash=sha256_text(paragraph),
                instruction="整段与前文章节完全相同，删除或局部改写该段，不要复述已定稿内容。",
                evidence=[
                    f"source_chapter:{source_chapter}",
                    f"text:{paragraph[:120]}",
                ],
            )
        )
    return issues


def inspect_truncated_ending(*, chapter_number: int, content: str) -> QualityIssue | None:
    """Blocker: the final non-empty paragraph lacks sentence-final punctuation.

    Empty content is already reported as ``empty_content`` and is not re-flagged.
    """
    last_paragraph = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            last_paragraph = stripped
    if not last_paragraph or last_paragraph.endswith(SENTENCE_ENDING_PUNCTUATION):
        return None
    span_start = max(content.rfind(last_paragraph), 0)
    return QualityIssue(
        issue_id=new_id("issue"),
        issue_type="truncated_ending",
        severity="blocker",
        chapter_number=chapter_number,
        span_start=span_start,
        span_end=span_start + len(last_paragraph),
        source_hash=sha256_text(last_paragraph),
        instruction="结尾在半句处截断，请把最后一段补全到以句末标点收束。",
        evidence=[f"tail:{last_paragraph[-40:]}"],
    )


def event_keywords(event: str) -> list[str]:
    """Tokenize an event description into deduplicated >=2-char word tokens."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for token in KEYWORD_ALNUM_RE.findall(event):
        add(token)
    for run in KEYWORD_CJK_RUN_RE.findall(event):
        if len(run) < 2:
            continue  # a lone hanzi has no reliable >=2-char token
        for index in range(len(run) - 1):
            add(run[index : index + 2])
    return tokens


def inspect_required_event_keywords(
    *,
    chapter_number: int,
    content: str,
    contract: ChapterContract,
) -> list[QualityIssue]:
    """Warning: every keyword of a required event is absent from the chapter.

    Deliberately looser than ``required_event_missing`` (which demands the full
    normalized event as a substring): if ANY token of the event shows up, the
    event is considered possibly covered in rewritten form and stays silent.
    """
    issues: list[QualityIssue] = []
    for event in contract.required_events:
        keywords = event_keywords(event)
        if not keywords:
            continue
        if any(keyword in content for keyword in keywords):
            continue
        issues.append(
            QualityIssue(
                issue_id=new_id("issue"),
                issue_type="required_event_keyword_missing",
                severity="warning",
                chapter_number=chapter_number,
                span_start=0,
                span_end=0,
                source_hash=sha256_text(content),
                instruction=(
                    f"合同事件「{event}」的关键词均未在正文出现，"
                    "请核对是否以改写方式覆盖"
                    f"（关键词示例：{'、'.join(keywords[:8])}）。"
                ),
                evidence=[f"event:{event}", f"keywords:{','.join(keywords)}"],
            )
        )
    return issues


def inspect_chapter(
    *,
    chapter_number: int,
    content: str,
    contract: ChapterContract,
    recent_chapters: list[dict[str, object]],
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    if not content.strip():
        issues.append(
            QualityIssue(
                issue_id=new_id("issue"),
                issue_type="empty_content",
                severity="blocker",
                chapter_number=chapter_number,
                span_start=0,
                span_end=0,
                source_hash=sha256_text(content),
                instruction="生成非空小说正文。",
            )
        )
        return issues

    for pattern in POLLUTION_PATTERNS:
        match = pattern.search(content)
        if match:
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type="prompt_pollution",
                    severity="blocker",
                    chapter_number=chapter_number,
                    span_start=match.start(),
                    span_end=match.end(),
                    source_hash=sha256_text(content[match.start() : match.end()]),
                    instruction="删除分析、系统说明或格式标签，只保留小说正文。",
                    evidence=[match.group(0)],
                )
            )

    paragraphs = _paragraphs(content)
    normalized_seen: dict[str, tuple[int, int, str]] = {}
    for start, end, paragraph in paragraphs:
        normalized = normalized_text(paragraph)
        if len(normalized) < 24:
            continue
        if normalized in normalized_seen:
            previous = normalized_seen[normalized]
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type="exact_paragraph_repeat",
                    severity="blocker",
                    chapter_number=chapter_number,
                    span_start=start,
                    span_end=end,
                    source_hash=sha256_text(paragraph),
                    instruction="删除或局部改写重复段落，不要重写整章。",
                    evidence=[f"same chapter span {previous[0]}-{previous[1]}"],
                )
            )
        else:
            normalized_seen[normalized] = (start, end, paragraph)

    historical: list[tuple[int, str]] = []
    for chapter in recent_chapters:
        for _, _, paragraph in _paragraphs(str(chapter["content"])):
            if len(normalized_text(paragraph)) >= 40:
                historical.append((int(chapter["chapter_number"]), paragraph))
    for start, end, paragraph in paragraphs:
        if len(normalized_text(paragraph)) < 40:
            continue
        for source_chapter, historical_paragraph in historical:
            similarity = _similarity(paragraph, historical_paragraph)
            if similarity >= 0.88:
                issues.append(
                    QualityIssue(
                        issue_id=new_id("issue"),
                        issue_type="near_paragraph_repeat",
                        severity="error",
                        chapter_number=chapter_number,
                        span_start=start,
                        span_end=end,
                        source_hash=sha256_text(paragraph),
                        instruction="局部改写与历史章节高度相似的段落。",
                        evidence=[
                            f"chapter:{source_chapter}",
                            f"shingle_jaccard:{similarity:.3f}",
                        ],
                    )
                )
                break

    normalized_content = normalized_text(content)
    for event in contract.required_events:
        if normalized_text(event) not in normalized_content:
            issues.append(
                QualityIssue(
                    issue_id=new_id("issue"),
                    issue_type="required_event_missing",
                    severity="warning",
                    chapter_number=chapter_number,
                    span_start=0,
                    span_end=min(len(content), 1),
                    source_hash=sha256_text(content),
                    instruction=f"请确认正文是否以改写方式覆盖了章节合同事件：{event}",
                    evidence=[event],
                )
            )

    issues.extend(inspect_dense_short_line_repeat(chapter_number=chapter_number, content=content))
    issues.extend(
        inspect_required_event_keywords(
            chapter_number=chapter_number,
            content=content,
            contract=contract,
        )
    )
    issues.extend(
        inspect_cross_chapter_exact_repeat(
            chapter_number=chapter_number,
            content=content,
            recent_chapters=recent_chapters,
        )
    )
    truncated = inspect_truncated_ending(chapter_number=chapter_number, content=content)
    if truncated is not None:
        issues.append(truncated)
    return issues
