from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def estimate_tokens(value: str) -> int:
    # Deliberately conservative for mixed Chinese/English prose.
    return max(1, (len(value) + 1) // 2)


# --- Management-plane zero-content guarantee (0.5.0) -------------------------
# Management endpoints (GET /pipeline) must never leak prose to the Master
# Agent: no content/digest/prose keys anywhere in the payload, and no string
# value longer than this bound (titles included; they are far shorter).
MANAGEMENT_FORBIDDEN_KEYS = frozenset({"content", "digest", "prose"})
MANAGEMENT_MAX_STRING_LEN = 200


def assert_management_payload(payload: Any) -> None:
    """Raise ValueError when a management payload would leak prose.

    Walks the whole structure recursively: every dict key must stay off the
    forbidden list and every string value must fit the length bound.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and key.lower() in MANAGEMENT_FORBIDDEN_KEYS:
                raise ValueError(
                    f"management payload contains forbidden key {key!r}"
                )
            assert_management_payload(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_management_payload(item)
    elif isinstance(payload, str):
        if len(payload) > MANAGEMENT_MAX_STRING_LEN:
            raise ValueError(
                "management payload string value exceeds "
                f"{MANAGEMENT_MAX_STRING_LEN} characters"
            )

