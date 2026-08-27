from __future__ import annotations

from typing import Any

from dsh_novel.domain import ChapterContract, ContextBlock, ContextPackage
from dsh_novel.errors import ContextBudgetExceededError
from dsh_novel.util import canonical_json, estimate_tokens, sha256_text


class ContextCompiler:
    """Builds a bounded context from stable project state and a three-chapter bridge."""

    def __init__(self, token_budget: int) -> None:
        self.token_budget = token_budget

    def compile(
        self,
        *,
        project: dict[str, Any],
        contract: ChapterContract,
        recent_chapters: list[dict[str, Any]],
        recent_deltas: list[dict[str, Any]] | None = None,
    ) -> ContextPackage:
        blocks: list[ContextBlock] = []

        def add(kind: str, content: str, priority: int, required: bool) -> None:
            if not content.strip():
                return
            blocks.append(
                ContextBlock(
                    kind=kind,
                    content=content.strip(),
                    priority=priority,
                    required=required,
                    estimated_tokens=estimate_tokens(content),
                )
            )

        add(
            "hard_rules",
            canonical_json(
                {"premise": project["premise"], "hard_rules": project["hard_rules"]}
            ),
            100,
            True,
        )
        add("story_spine", canonical_json(project["story_spine"]), 95, True)
        add(
            "chapter_contract",
            canonical_json(contract.model_dump(mode="json")),
            100,
            True,
        )

        # 人物状态档案（entity_state）：从最近章节抽取的 character_changes
        # 累积成"人物关系当前发展到哪"，供续写参考——这是用户设计的核心。
        if recent_deltas:
            entity_rows: list[dict[str, Any]] = []
            for delta in recent_deltas:
                for change in delta.get("character_changes") or []:
                    entity_rows.append(
                        {
                            "chapter": delta.get("chapter_number"),
                            "character": change.get("character"),
                            "before": change.get("before"),
                            "after": change.get("after"),
                        }
                    )
            if entity_rows:
                add("entity_state", canonical_json(entity_rows), 92, False)

        if recent_chapters:
            bridge = []
            for chapter in recent_chapters:
                item = {
                    "chapter": chapter["chapter_number"],
                    "digest": chapter["digest"],
                }
                if chapter is recent_chapters[-1]:
                    item["ending"] = str(chapter["content"])[-1000:]
                bridge.append(item)
            add("continuity_bridge", canonical_json(bridge), 90, True)

        # 续写核心信息（arc_state）：上一章/最近两章的真实人物变化、伏笔状态、
        # 反转与章末钩子——压缩自 chapter_deltas 的结构化抽取，而非 500 字摘要。
        if recent_deltas:
            arc_items = [
                {
                    "chapter": delta.get("chapter_number"),
                    "character_changes": delta.get("character_changes") or [],
                    "hooks_status": delta.get("hooks_status") or [],
                    "twist": delta.get("twist") or "",
                    "next_chapter_hook": delta.get("next_chapter_hook") or "",
                }
                for delta in recent_deltas
            ]
            add("arc_state", canonical_json(arc_items), 93, False)

        kept: list[ContextBlock] = []
        omitted: list[dict[str, Any]] = []
        total = 0
        for block in sorted(blocks, key=lambda item: item.priority, reverse=True):
            if total + block.estimated_tokens <= self.token_budget:
                kept.append(block)
                total += block.estimated_tokens
            elif block.required:
                raise ContextBudgetExceededError(
                    f"required context exceeds token budget {self.token_budget}"
                )
            else:
                omitted.append(
                    {
                        "kind": block.kind,
                        "reason": "token_budget",
                        "estimated_tokens": block.estimated_tokens,
                    }
                )

        serializable = [block.model_dump(mode="json") for block in kept]
        checksum = sha256_text(canonical_json(serializable))
        return ContextPackage(
            package_id=f"ctx_{checksum[:24]}",
            project_id=project["id"],
            chapter_number=contract.chapter_number,
            task="draft",
            canon_version=project["canon_version"],
            blueprint_version=project["blueprint_version"],
            token_budget=self.token_budget,
            estimated_tokens=total,
            blocks=kept,
            omitted=omitted,
            provenance=[
                f"project:{project['id']}/canon:{project['canon_version']}",
                f"chapter-contract:{contract.chapter_number}",
                *[
                    f"chapter:{chapter['chapter_number']}/digest"
                    for chapter in recent_chapters
                ],
            ],
            checksum=checksum,
        )

