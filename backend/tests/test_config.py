from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dsh_novel.config import (
    CONFIG_FILE_ENV,
    ConfigError,
    Settings,
    config_file_path,
    default_config_path,
)
from dsh_novel.transports.http import create_app

DSH_ENV_VARS = [
    CONFIG_FILE_ENV,
    "DSH_NOVEL_DATA_DIR",
    "DSH_NOVEL_PORT",
    "DSH_NOVEL_TOKEN",
    "DSH_NOVEL_MODEL_PROVIDER",
    "DSH_NOVEL_MODEL_ENDPOINT",
    "DSH_NOVEL_MODEL_NAME",
    "DSH_NOVEL_MODEL_API_KEY",
    "DSH_NOVEL_MODEL_TIMEOUT",
    "DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS",
    "DSH_NOVEL_CONTEXT_TOKEN_BUDGET",
    "DSH_NOVEL_SCORE_THRESHOLD",
    "DSH_NOVEL_MAX_REVISIONS",
    "DSH_NOVEL_OUTLINE_TIMEOUT",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every DSH_NOVEL_* variable so tests are hermetic."""
    for name in DSH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the config file to a per-test path that does not exist yet."""
    path = tmp_path / "config.yml"
    monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
    return path


def write_config(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_missing_config_file_uses_defaults(
    clean_env: None, config_path: Path
) -> None:
    assert not config_path.exists()
    settings = Settings()
    assert settings.data_dir == Path.home() / ".dsh-novel"
    assert settings.model_provider == "fake"
    assert settings.model_endpoint == "http://127.0.0.1:1234/v1"
    assert settings.model_name == "local-writer"
    assert settings.model_api_key is None
    assert settings.model_timeout_seconds == 180.0
    assert settings.model_max_output_tokens == 8192
    assert settings.context_token_budget == 20000
    assert settings.auth_token is None
    assert settings.host == "127.0.0.1"
    assert settings.port == 17861


def test_config_file_values_apply(clean_env: None, config_path: Path) -> None:
    write_config(
        config_path,
        """
        model_provider: openai_compatible
        model_endpoint: http://127.0.0.1:9999/v1
        model_name: writer-x
        model_timeout_seconds: 42
        model_max_output_tokens: 1024
        context_token_budget: 1234
        port: 17863
        auth_token: file-secret
        """,
    )
    settings = Settings()
    assert settings.model_provider == "openai_compatible"
    assert settings.model_endpoint == "http://127.0.0.1:9999/v1"
    assert settings.model_name == "writer-x"
    assert settings.model_timeout_seconds == 42.0
    assert settings.model_max_output_tokens == 1024
    assert settings.context_token_budget == 1234
    assert settings.port == 17863
    assert settings.auth_token == "file-secret"


def test_env_overrides_file(
    clean_env: None, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(
        config_path,
        """
        model_provider: openai_compatible
        port: 17863
        context_token_budget: 1234
        """,
    )
    monkeypatch.setenv("DSH_NOVEL_MODEL_PROVIDER", "fake")
    monkeypatch.setenv("DSH_NOVEL_PORT", "17899")
    settings = Settings()
    assert settings.model_provider == "fake"  # env wins over file
    assert settings.port == 17899  # env wins over file
    assert settings.context_token_budget == 1234  # file still applies without env


def test_dsh_novel_config_redirects_file_path(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redirected = tmp_path / "elsewhere" / "custom.yml"
    redirected.parent.mkdir()
    write_config(redirected, "context_token_budget: 777\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(redirected))

    assert config_file_path() == redirected
    assert config_file_path() != default_config_path()
    assert Settings().context_token_budget == 777


def test_data_dir_from_file_applies(clean_env: None, config_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "novel-data"
    write_config(config_path, f'data_dir: "{target}"\n')
    settings = Settings()
    assert settings.data_dir == target
    settings.ensure_directories()
    assert (target / "projects").is_dir()


def test_data_dir_tilde_expands_to_home(
    clean_env: None, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data_dir: ~/.dsh-novel` (the README example) must resolve to the real
    home directory, not a literal `~` folder relative to the process cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config(config_path, "data_dir: ~/.dsh-novel\n")
    settings = Settings()
    assert settings.data_dir == tmp_path / ".dsh-novel"
    assert "~" not in str(settings.data_dir)


def test_invalid_yaml_fails_loudly_with_valid_keys(
    clean_env: None, config_path: Path
) -> None:
    write_config(config_path, "model_provider: [unclosed\n")
    with pytest.raises(ConfigError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert str(config_path) in message
    # legal keys are listed
    assert "model_provider" in message
    assert "context_token_budget" in message
    assert "port" in message


def test_unknown_key_fails_loudly_with_valid_keys(
    clean_env: None, config_path: Path
) -> None:
    write_config(config_path, "host: 0.0.0.0\n")
    with pytest.raises(ConfigError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "host" in message
    assert "unknown key" in message.lower()
    assert "data_dir (string (filesystem path))" in message  # valid keys with types


def test_type_error_names_field_and_expected_type(
    clean_env: None, config_path: Path
) -> None:
    write_config(config_path, 'port: "17862"\n')
    with pytest.raises(ConfigError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "'port'" in message
    assert "17862" in message
    assert "integer" in message


def test_top_level_must_be_mapping(clean_env: None, config_path: Path) -> None:
    write_config(config_path, "- fake\n- openai_compatible\n")
    with pytest.raises(ConfigError, match="mapping"):
        Settings()


def test_empty_config_file_is_silent(clean_env: None, config_path: Path) -> None:
    write_config(config_path, "")
    assert Settings().port == 17861


def test_explicit_kwargs_win_over_file(clean_env: None, config_path: Path) -> None:
    write_config(config_path, "port: 17863\nauth_token: from-file\n")
    settings = Settings(port=1, auth_token=None)
    assert settings.port == 1
    assert settings.auth_token is None


def test_health_reports_provider_from_config_file(clean_env: None, config_path: Path) -> None:
    write_config(config_path, "model_provider: openai_compatible\n")
    app = create_app(Settings())
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["provider"] == "openai_compatible"
