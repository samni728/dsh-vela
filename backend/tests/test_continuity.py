from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dsh_novel.config import Settings
from dsh_novel.providers import DeterministicFakeProvider
from dsh_novel.transports.http import create_app


def _make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings(data_dir=tmp_path / "data", context_token_budget=10000),
        DeterministicFakeProvider(),
    )
    return TestClient(app)


def _wait_terminal(client: TestClient, project_id: str) -> dict:
    terminal = {"completed", "failed", "completed_with_rework"}
    for _ in range(200):
        st = client.get(f"/api/v1/projects/{project_id}/autorun").json()["result"]
        if st["state"] in terminal:
            return st
        time.sleep(0.1)
    raise AssertionError("autorun did not reach a terminal state")


def test_outline_includes_character_twist_handoff(tmp_path: Path) -> None:
    """编排阶段：章节蓝图必须包含人物关系、反转、章末衔接并结构化存库。"""
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={"title": "雾港档案", "premise": "测试蓝图字段。", "target_chapters": 3},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]

        db_path = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(db_path) as conn:
            contract = conn.execute(
                "SELECT contract_json FROM chapter_contracts WHERE chapter_number = 1"
            ).fetchone()[0]
        contract_data = json.loads(contract)
        # 合同蓝图包含人物关系与反转字段
        assert "characters" in contract_data
        assert isinstance(contract_data["characters"], list)
        assert "twist" in contract_data
        assert "handoff" in contract_data


def test_chapter_deltas_carry_extracted_core_info(tmp_path: Path) -> None:
    """续写机制：每章定稿后，chapter_deltas 必须携带从正文抽取的人物核心
    变化 / 伏笔真实状态 / 反转 / 章末钩子（而非仅合同回声）。"""
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={"title": "雾港档案", "premise": "测试抽取层。", "target_chapters": 3},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]
        _wait_terminal(client, project_id)

        db_path = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT delta_json FROM chapter_deltas ORDER BY chapter_number"
            ).fetchall()
        assert len(rows) == 3, f"expected 3 deltas, got {len(rows)}"
        for row in rows:
            delta = json.loads(row[0])
            # 人物核心变化：抽取自正文（fake provider 从合同 characters 产出）
            assert "character_changes" in delta
            assert isinstance(delta["character_changes"], list)
            assert "hooks_status" in delta
            assert isinstance(delta["hooks_status"], list)
            assert "twist" in delta
            assert "next_chapter_hook" in delta


def test_context_includes_entity_state_and_arc_state(tmp_path: Path) -> None:
    """续写机制：写第 N 章时，上下文必须注入前章的人物状态档案（entity_state）
    与压缩核心信息（arc_state），而非只有 500 字摘要。"""
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={"title": "雾港档案", "premise": "测试上下文。", "target_chapters": 3},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]
        _wait_terminal(client, project_id)

        # 写第 2 章时的上下文包：此时第 1 章已定稿、delta 已抽取，故
        # arc_state（人物核心变化/伏笔状态/反转/钩子）与 entity_state
        # （人物状态档案）必须注入——这是用户设计的续写核心。
        db_path = tmp_path / "data" / "projects" / project_id / "novel.sqlite3"
        with sqlite3.connect(db_path) as conn:
            packages = conn.execute(
                "SELECT package_json FROM context_packages "
                "WHERE chapter_number = 2 ORDER BY created_at DESC LIMIT 1"
            ).fetchall()
        assert packages, "no context package found for chapter 2"
        package = json.loads(packages[0][0])
        kinds = [block["kind"] for block in package["blocks"]]
        assert "arc_state" in kinds, f"arc_state missing from chapter-2 context: {kinds}"
        assert "entity_state" in kinds, f"entity_state missing from chapter-2 context: {kinds}"
        assert "continuity_bridge" in kinds
        # arc_state 必须携带上一章的真实人物变化与伏笔状态
        arc_block = next(b for b in package["blocks"] if b["kind"] == "arc_state")
        arc = json.loads(arc_block["content"])
        assert arc, "arc_state content is empty"
        first = arc[0]
        assert first["chapter"] == 1
        assert first["character_changes"], "arc_state missing character_changes"
        assert first["hooks_status"], "arc_state missing hooks_status"


def test_recent_deltas_returns_structured_state(tmp_path: Path) -> None:
    """续写机制：recent_deltas 从数据库读出的必须是结构化的核心信息。"""
    with _make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/auto",
            json={"title": "雾港档案", "premise": "测试 delta 读取。", "target_chapters": 3},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["result"]["project_id"]
        _wait_terminal(client, project_id)

        from dsh_novel.infrastructure import ProjectDatabase

        db = ProjectDatabase(tmp_path / "data" / "projects", project_id)
        deltas = db.recent_deltas(4, limit=3)
        assert len(deltas) == 3
        for delta in deltas:
            assert "character_changes" in delta
            assert "hooks_status" in delta
            assert "next_chapter_hook" in delta
        # 最后一个 delta 是最新一章（第 3 章）
        assert deltas[-1]["chapter_number"] == 3
