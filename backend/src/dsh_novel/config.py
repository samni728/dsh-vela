from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILE_ENV = "DSH_NOVEL_CONFIG"

# Every key allowed in config.yml, mapped to a human-readable expected type.
# `host` is intentionally absent: it is fixed to 127.0.0.1 and not configurable.
CONFIG_FILE_FIELDS: dict[str, str] = {
    "auth_token": "string or null",
    "context_token_budget": "integer",
    "data_dir": "string (filesystem path)",
    "max_revisions": "integer",
    "model_api_key": "string or null",
    "model_endpoint": "string",
    "model_max_output_tokens": "integer",
    "model_name": "string",
    "model_provider": "string",
    "model_timeout_seconds": "number",
    "outline_timeout_seconds": "number",
    "port": "integer",
    "review_enabled": "boolean",
    "review_timeout_seconds": "number",
    "score_threshold": "number",
}

_OPTIONAL_STRING_KEYS = frozenset({"model_api_key", "auth_token"})
_INTEGER_KEYS = frozenset(
    {"model_max_output_tokens", "context_token_budget", "port", "max_revisions"}
)
_FLOAT_KEYS = frozenset(
    {
        "model_timeout_seconds",
        "review_timeout_seconds",
        "outline_timeout_seconds",
        "score_threshold",
    }
)
_BOOLEAN_KEYS = frozenset({"review_enabled"})


class ConfigError(ValueError):
    """Raised when the optional YAML config file exists but cannot be used."""


def default_config_path() -> Path:
    return Path.home() / ".dsh-novel" / "config.yml"


def config_file_path() -> Path:
    """Effective config file path; DSH_NOVEL_CONFIG overrides the default.

    The env override exists so custom DSH_NOVEL_DATA_DIR setups can still be
    found without a chicken-and-egg problem between the two variables.
    """
    override = os.getenv(CONFIG_FILE_ENV)
    if override:
        return Path(override)
    return default_config_path()


def _valid_keys_hint() -> str:
    return ", ".join(f"{key} ({kind})" for key, kind in sorted(CONFIG_FILE_FIELDS.items()))


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    """Load validated values from the optional YAML config file.

    Returns an empty dict when the file does not exist (silent skip), so the
    behavior is identical to running without a config file. Any present-but-
    broken file fails loudly with the offending field and the valid keys.
    """
    path = path if path is not None else config_file_path()
    if not path.is_file():
        return {}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"config file {path} is not valid YAML: {exc}\n"
            f"Valid keys: {_valid_keys_hint()}"
        ) from exc
    if parsed is None:  # empty file: nothing to merge
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"config file {path} must contain a YAML mapping at the top level, "
            f"got {type(parsed).__name__}.\nValid keys: {_valid_keys_hint()}"
        )
    unknown = sorted(str(key) for key in parsed if key not in CONFIG_FILE_FIELDS)
    if unknown:
        raise ConfigError(
            f"config file {path} has unknown key(s): {', '.join(unknown)}.\n"
            f"Valid keys: {_valid_keys_hint()}"
        )
    values: dict[str, Any] = {}
    for key, raw in parsed.items():
        if key in _OPTIONAL_STRING_KEYS:
            if raw is None or isinstance(raw, str):
                values[key] = raw
                continue
        elif key == "data_dir":
            if isinstance(raw, str):
                # Expand a leading `~` so the README example
                # `data_dir: ~/.dsh-novel` resolves to the real home directory.
                values[key] = Path(raw).expanduser()
                continue
        elif key in _BOOLEAN_KEYS:
            if isinstance(raw, bool):
                values[key] = raw
                continue
        elif key in _FLOAT_KEYS:
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                values[key] = float(raw)
                continue
        elif key in _INTEGER_KEYS:
            if isinstance(raw, int) and not isinstance(raw, bool):
                values[key] = raw
                continue
        elif isinstance(raw, str):  # remaining plain string keys
            values[key] = raw
            continue
        raise ConfigError(
            f"config file {path}: key '{key}' expects "
            f"{CONFIG_FILE_FIELDS[key]}, got {raw!r} ({type(raw).__name__}).\n"
            f"Valid keys: {_valid_keys_hint()}"
        )
    return values


def _resolve_data_dir(file_values: dict[str, Any]) -> Path:
    env = os.getenv("DSH_NOVEL_DATA_DIR")
    if env is not None:
        return Path(env)
    from_file = file_values.get("data_dir")
    if isinstance(from_file, Path):
        return from_file
    return Path.home() / ".dsh-novel"


def _resolve_str(env_name: str, key: str, file_values: dict[str, Any], default: str) -> str:
    env = os.getenv(env_name)
    if env is not None:
        return env
    from_file = file_values.get(key)
    if isinstance(from_file, str):
        return from_file
    return default


def _resolve_optional_str(
    env_name: str, key: str, file_values: dict[str, Any], *, empty_env_is_none: bool = False
) -> str | None:
    env = os.getenv(env_name)
    if empty_env_is_none:
        env = env or None
    if env is not None:
        return env
    from_file = file_values.get(key)
    if from_file is not None:
        return from_file
    return None


def _resolve_float(env_name: str, key: str, file_values: dict[str, Any], default: float) -> float:
    env = os.getenv(env_name)
    if env is not None:
        return float(env)
    from_file = file_values.get(key)
    if from_file is not None:
        return float(from_file)
    return default


def _resolve_int(env_name: str, key: str, file_values: dict[str, Any], default: int) -> int:
    env = os.getenv(env_name)
    if env is not None:
        return int(env)
    from_file = file_values.get(key)
    if from_file is not None:
        return int(from_file)
    return default


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def _resolve_bool(env_name: str, key: str, file_values: dict[str, Any], default: bool) -> bool:
    env = os.getenv(env_name)
    if env is not None:
        lowered = env.strip().lower()
        if lowered in _TRUE_ENV_VALUES:
            return True
        if lowered in _FALSE_ENV_VALUES:
            return False
        raise ValueError(
            f"environment variable {env_name} expects a boolean "
            f"(true/false/1/0), got {env!r}"
        )
    from_file = file_values.get(key)
    if isinstance(from_file, bool):
        return from_file
    return default


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: _resolve_data_dir(load_config_file()))
    model_provider: str = field(
        default_factory=lambda: _resolve_str(
            "DSH_NOVEL_MODEL_PROVIDER", "model_provider", load_config_file(), "fake"
        )
    )
    model_endpoint: str = field(
        default_factory=lambda: _resolve_str(
            "DSH_NOVEL_MODEL_ENDPOINT", "model_endpoint", load_config_file(),
            "http://127.0.0.1:1234/v1",
        )
    )
    model_name: str = field(
        default_factory=lambda: _resolve_str(
            "DSH_NOVEL_MODEL_NAME", "model_name", load_config_file(), "local-writer"
        )
    )
    model_api_key: str | None = field(
        default_factory=lambda: _resolve_optional_str(
            "DSH_NOVEL_MODEL_API_KEY", "model_api_key", load_config_file()
        )
    )
    model_timeout_seconds: float = field(
        default_factory=lambda: _resolve_float(
            "DSH_NOVEL_MODEL_TIMEOUT", "model_timeout_seconds", load_config_file(), 180.0
        )
    )
    model_max_output_tokens: int = field(
        default_factory=lambda: _resolve_int(
            "DSH_NOVEL_MODEL_MAX_OUTPUT_TOKENS", "model_max_output_tokens",
            load_config_file(), 8192,
        )
    )
    context_token_budget: int = field(
        default_factory=lambda: _resolve_int(
            "DSH_NOVEL_CONTEXT_TOKEN_BUDGET", "context_token_budget",
            load_config_file(), 20000,
        )
    )
    auth_token: str | None = field(
        default_factory=lambda: _resolve_optional_str(
            "DSH_NOVEL_TOKEN", "auth_token", load_config_file(), empty_env_is_none=True
        )
    )
    host: str = "127.0.0.1"
    port: int = field(
        default_factory=lambda: _resolve_int("DSH_NOVEL_PORT", "port", load_config_file(), 17861)
    )
    review_enabled: bool = field(
        default_factory=lambda: _resolve_bool(
            "DSH_NOVEL_REVIEW_ENABLED", "review_enabled", load_config_file(), True
        )
    )
    review_timeout_seconds: float = field(
        default_factory=lambda: _resolve_float(
            "DSH_NOVEL_REVIEW_TIMEOUT", "review_timeout_seconds", load_config_file(), 120.0
        )
    )
    # Minimum overall (= min of the three review scores) for a chapter to
    # commit; below it the run is blocked and rewritten (see reviewer.py).
    score_threshold: float = field(
        default_factory=lambda: _resolve_float(
            "DSH_NOVEL_SCORE_THRESHOLD", "score_threshold", load_config_file(), 8.0
        )
    )
    # Total attempts allowed per chapter; unifies the resume retry budget and
    # the threshold-loop exhaustion point.
    max_revisions: int = field(
        default_factory=lambda: _resolve_int(
            "DSH_NOVEL_MAX_REVISIONS", "max_revisions", load_config_file(), 3
        )
    )
    # Dedicated wall-clock budget for outline generation calls.
    outline_timeout_seconds: float = field(
        default_factory=lambda: _resolve_float(
            "DSH_NOVEL_OUTLINE_TIMEOUT", "outline_timeout_seconds", load_config_file(), 180.0
        )
    )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "projects").mkdir(exist_ok=True)
