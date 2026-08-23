from __future__ import annotations

import json
from pathlib import Path


def test_protocol_schema_files_are_valid_json() -> None:
    schema_root = Path(__file__).parents[2] / "schemas" / "protocol" / "v1"
    files = sorted(schema_root.glob("*.schema.json"))
    assert files
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert data["type"] == "object"
