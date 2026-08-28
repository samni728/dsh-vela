from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.config import CONFIG_FILE_ENV, Settings
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


@pytest.fixture(autouse=True)
def isolate_user_config(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Never let tests consume the user's real config or credentials.

    ``test_config.py`` owns its config-path fixtures and deliberately exercises
    default-path behavior, so it remains in control of its environment.
    """
    if request.path.name != "test_config.py":
        monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "missing-config.yml"))


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
