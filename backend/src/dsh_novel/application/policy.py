"""Per-project writing policy (0.5.0): thresholds and failure behavior.

A policy is a small dict with exactly four keys:

- ``score_threshold``: minimum overall review score (float, 0..10).
- ``max_revisions``: total draft attempts per chapter (int, >= 1).
- ``target_words``: default per-chapter word budget (int).
- ``on_chapter_failure``: ``skip_continue`` (record rework and go on) or
  ``pause`` (stop the whole run at the failing chapter).

Merge precedence for the *effective* policy is always:
request policy > project-stored policy (``projects.policy_json``) > settings
defaults. The first time a policy is resolved for a project it is persisted so
the management plane can report a stable effective policy.
"""

from __future__ import annotations

from typing import Any

from dsh_novel.errors import ConfigInvalidError

POLICY_KEYS = ("score_threshold", "max_revisions", "target_words", "on_chapter_failure")
ON_CHAPTER_FAILURE_MODES = ("skip_continue", "pause")

# Built-in fallbacks when neither the request nor the stored policy supplies a
# key and settings do not carry one either.
DEFAULT_SCORE_THRESHOLD = 8.0
DEFAULT_MAX_REVISIONS = 3
DEFAULT_TARGET_WORDS = 4000
DEFAULT_ON_CHAPTER_FAILURE = "skip_continue"

MAX_POLICY_TARGET_WORDS = 20000
MAX_POLICY_REVISIONS = 20


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_value(key: str, value: Any) -> bool:
    if key == "score_threshold":
        return _is_number(value) and 0 <= float(value) <= 10
    if key == "max_revisions":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= MAX_POLICY_REVISIONS
        )
    if key == "target_words":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 100 <= value <= MAX_POLICY_TARGET_WORDS
        )
    if key == "on_chapter_failure":
        return value in ON_CHAPTER_FAILURE_MODES
    return False


def normalize_policy(raw: Any) -> dict[str, Any]:
    """Validate a client-supplied partial policy; keep only known valid keys.

    Raises :class:`ConfigInvalidError` for unknown keys or out-of-range values
    so bad input fails loudly at the API boundary instead of silently falling
    back to defaults.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigInvalidError(
            "policy must be a JSON object with keys: " + ", ".join(POLICY_KEYS)
        )
    unknown = sorted(str(key) for key in raw if key not in POLICY_KEYS)
    if unknown:
        raise ConfigInvalidError(
            f"unknown policy key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(POLICY_KEYS)}"
        )
    result: dict[str, Any] = {}
    for key in POLICY_KEYS:
        if key not in raw or raw[key] is None:
            continue
        if not _valid_value(key, raw[key]):
            raise ConfigInvalidError(
                f"policy.{key} has an invalid value: {raw[key]!r}"
            )
        result[key] = raw[key]
    return result


def sanitize_stored_policy(raw: Any) -> dict[str, Any]:
    """Leniently parse a persisted policy; drop anything invalid or unknown."""
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in POLICY_KEYS if _valid_value(key, raw.get(key))}


def merge_policy(
    *,
    request: dict[str, Any] | None,
    stored: dict[str, Any] | None,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Effective policy = request > stored > defaults, merged per key."""
    merged: dict[str, Any] = {}
    for key in POLICY_KEYS:
        value = defaults.get(key)
        if value is None:
            value = _fallback_default(key)
        merged[key] = value
    for key, value in sanitize_stored_policy(stored).items():
        merged[key] = value
    for key, value in normalize_policy(request).items():
        merged[key] = value
    return merged


def _fallback_default(key: str) -> Any:
    return {
        "score_threshold": DEFAULT_SCORE_THRESHOLD,
        "max_revisions": DEFAULT_MAX_REVISIONS,
        "target_words": DEFAULT_TARGET_WORDS,
        "on_chapter_failure": DEFAULT_ON_CHAPTER_FAILURE,
    }[key]
