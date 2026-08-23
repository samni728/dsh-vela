from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("DSH_NOVEL_DATA_DIR", Path.home() / ".dsh-novel")
        )
    )
    model_provider: str = field(
        default_factory=lambda: os.getenv("DSH_NOVEL_MODEL_PROVIDER", "fake")
    )
    model_endpoint: str = field(
        default_factory=lambda: os.getenv(
            "DSH_NOVEL_MODEL_ENDPOINT", "http://127.0.0.1:1234/v1"
        )
    )
    model_name: str = field(
        default_factory=lambda: os.getenv("DSH_NOVEL_MODEL_NAME", "local-writer")
    )
    model_api_key: str | None = field(
        default_factory=lambda: os.getenv("DSH_NOVEL_MODEL_API_KEY")
    )
    model_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("DSH_NOVEL_MODEL_TIMEOUT", "180"))
    )
    model_max_output_tokens: int = field(
        default_factory=lambda: int(os.getenv("DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS", "8192"))
    )
    context_token_budget: int = field(
        default_factory=lambda: int(os.getenv("DSH_NOVEL_CONTEXT_TOKEN_BUDGET", "20000"))
    )
    auth_token: str | None = field(
        default_factory=lambda: os.getenv("DSH_NOVEL_TOKEN") or None
    )
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: int(os.getenv("DSH_NOVEL_PORT", "17861")))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "projects").mkdir(exist_ok=True)
