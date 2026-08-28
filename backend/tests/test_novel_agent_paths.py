import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "novel-agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("novel_agent_paths_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_root_follows_script_location(monkeypatch):
    monkeypatch.delenv("DSH_NOVEL_REPO", raising=False)
    monkeypatch.delenv("DSH_NOVEL_WORKSPACE", raising=False)

    agent = _load_agent()

    assert agent.REPO_ROOT == REPO_ROOT
    assert agent._dsh_novel_entrypoint() == (
        REPO_ROOT / "backend" / ".venv" / "bin" / "dsh-novel"
    )


def test_config_environment_override_is_expanded(monkeypatch):
    monkeypatch.setenv("DSH_NOVEL_CONFIG", "~/.config/dsh-novel-test.yml")

    agent = _load_agent()

    assert agent._config_path() == Path.home() / ".config" / "dsh-novel-test.yml"


def test_legacy_workspace_remains_compatible(monkeypatch, tmp_path):
    repo = tmp_path / "dsh-vela"
    (repo / "backend").mkdir(parents=True)
    monkeypatch.delenv("DSH_NOVEL_REPO", raising=False)
    monkeypatch.setenv("DSH_NOVEL_WORKSPACE", str(tmp_path))

    agent = _load_agent()

    assert agent.REPO_ROOT == repo
