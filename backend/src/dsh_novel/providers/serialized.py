from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from dsh_novel.domain import OutlineResult, ReviewVerdict
from dsh_novel.providers.base import (
    ExtractionRequest,
    ModelProvider,
    OutlineRequest,
    ReviewRequest,
    WriterRequest,
)

T = TypeVar("T")


class SerializedModelProvider:
    """Process-local, single-lane proxy for one local model endpoint.

    The Sidecar exposes several model-backed operations (outline, drafting,
    review and state extraction).  They may be reached from different HTTP
    requests and autorun threads, so chapter-level sequencing alone is not a
    sufficient concurrency guard.  This proxy is the final safety boundary:
    at most one call can reach the configured model provider at a time.
    """

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self._lane = threading.Lock()
        self._state_lock = threading.Lock()
        self._active_operation: str | None = None
        self._waiting = 0

    @property
    def name(self) -> str:
        return self.provider.name

    def snapshot(self) -> dict[str, Any]:
        """Small management-only snapshot; never includes prompts or prose."""
        with self._state_lock:
            return {
                "mode": "serial",
                "max_concurrency": 1,
                "active_operation": self._active_operation,
                "waiting_calls": self._waiting,
            }

    def _call(self, operation: str, call: Callable[[], T]) -> T:
        with self._state_lock:
            self._waiting += 1
        self._lane.acquire()
        with self._state_lock:
            self._waiting -= 1
            self._active_operation = operation
        try:
            return call()
        finally:
            with self._state_lock:
                self._active_operation = None
            self._lane.release()

    def generate_chapter(self, request: WriterRequest) -> str:
        return self._call(
            "chapter.generate", lambda: self.provider.generate_chapter(request)
        )

    def review_chapter(self, request: ReviewRequest) -> ReviewVerdict:
        method = getattr(self.provider, "review_chapter", None)
        if not callable(method):
            raise NotImplementedError("provider does not support chapter review")
        return self._call(
            "chapter.review", lambda: method(request)
        )

    def generate_outline(self, request: OutlineRequest) -> OutlineResult:
        method = getattr(self.provider, "generate_outline", None)
        if not callable(method):
            raise NotImplementedError("provider does not support outline generation")
        return self._call(
            "outline.generate", lambda: method(request)
        )

    def extract_chapter_state(self, request: ExtractionRequest) -> dict[str, Any]:
        method = getattr(self.provider, "extract_chapter_state", None)
        if not callable(method):
            return {}
        return self._call(
            "chapter.extract", lambda: method(request)
        )


def serialize_provider(provider: ModelProvider) -> SerializedModelProvider:
    if isinstance(provider, SerializedModelProvider):
        return provider
    return SerializedModelProvider(provider)
