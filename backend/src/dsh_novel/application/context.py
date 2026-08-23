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

