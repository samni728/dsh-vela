from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "novel-data"


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    app = create_app(
        Settings(data_dir=data_dir, context_token_budget=5000),
        DeterministicFakeProvider(),
    )
    with TestClient(app) as test_client:
        yield test_client

