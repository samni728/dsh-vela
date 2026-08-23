from __future__ import annotations

import re

from dsh_novel.domain import ChapterContract, QualityIssue
from dsh_novel.util import new_id, normalized_text, sha256_text

POLLUTION_PATTERNS = (
    re.compile(r"<\/?(?:think|analysis|system|assistant)>", re.IGNORECASE),
    re.compile(r"(?:system\s*prompt|系统提示词|作为(?:一个|AI|语言模型))", re.IGNORECASE),
    re.compile(r"^```(?:json|yaml)", re.IGNORECASE | re.MULTILINE),
)


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
    return issues
