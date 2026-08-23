from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dsh_novel.domain import ChapterContract, ContextPackage


@dataclass(frozen=True, slots=True)
class WriterRequest:
    project_title: str
    contract: ChapterContract
    context: ContextPackage


class ModelProvider(Protocol):
    name: str

    def generate_chapter(self, request: WriterRequest) -> str: ...

