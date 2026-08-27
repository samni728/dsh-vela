#!/usr/bin/env python3
"""Master-agent friendly scheduler for the DSH Novel sidecar.

WHY THIS EXISTS
---------------
The novel pipeline (write -> review -> revise -> commit -> score) is long:
one chapter takes ~15-25 minutes of model time. The naive way to drive it —
calling `dsh-novel run-chapter` or `dsh-novel resume` synchronously — BLOCKS
for the whole generation, and any caller-side timeout (e.g. a 60s bash
deadline) aborts the request mid-flight, leaving the run stuck in
RUNNING/DRAFTING forever with the model idle.

The correct driver is the sidecar's *autorun orchestrator*: a daemon thread
per project that walks chapters sequentially, self-heals transient failures
with backoff, retries the rework queue first, and commits chapters that pass
the review gate. This script wraps that orchestrator in a non-blocking
submit -> poll -> wait -> export workflow so a master agent never blocks on a
single model call and never leaves a run wedged.

USAGE
-----
  python3 novel-agent.py ensure-server            # start sidecar if not up
  python3 novel-agent.py submit <project_id>      # start autorun (non-blocking)
  python3 novel-agent.py status <project_id>      # snapshot, no model calls
  python3 novel-agent.py wait <project_id>        # poll until terminal state
  python3 novel-agent.py export <project_id>      # write manuscript.md + README.md
  python3 novel-agent.py auto <title> <premise>   # create + outline + autorun

Every command prints one JSON object on stdout and exits 0 on success,
1 on failure — machine-readable for a master agent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- config

def _find_workspace() -> Path:
    """Locate the workspace root (the dir containing novel-data/ and dsh-vela/).

    The script may live at the workspace root or under dsh-vela/scripts/, so
    walk up from __file__ until both markers are present.
    """
    env_ws = os.environ.get("DSH_NOVEL_WORKSPACE")
    if env_ws:
        return Path(env_ws)
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "novel-data").is_dir() and (candidate / "dsh-vela").is_dir():
            return candidate
    return here


WORKSPACE = _find_workspace()
DEFAULT_DATA_DIR = WORKSPACE / "novel-data"
DEFAULT_PORT = 17861
BASE_URL = os.environ.get("DSH_NOVEL_BASE_URL", f"http://127.0.0.1:{DEFAULT_PORT}")

# Total wall-clock budget for `wait` (seconds). Autorun of N chapters at
# ~20 min/chapter can take hours; the default is deliberately generous.
WAIT_TIMEOUT_SECONDS = int(os.environ.get("DSH_NOVEL_WAIT_TIMEOUT", 6 * 3600))
# Poll interval for `wait`.
POLL_INTERVAL_SECONDS = float(os.environ.get("DSH_NOVEL_POLL_INTERVAL", 30))


def _venv_python() -> str:
    """Prefer the venv python that has dsh_novel installed."""
    candidates = [
        os.environ.get("DSH_NOVEL_VENV_PYTHON"),
        str(WORKSPACE / "dsh-vela" / "backend" / ".venv" / "bin" / "python3"),
        "python3",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return "python3"


def _http(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": {"code": f"HTTP_{exc.code}", "message": raw[:300]}}
        raise RuntimeError(
            f"HTTP {exc.code} on {method} {path}: "
            f"{payload.get('error', {}).get('message', payload)}"
        ) from exc


# --------------------------------------------------------------------------- server

def _server_up() -> bool:
    try:
        _http("GET", "/health", timeout=5)
        return True
    except Exception:
        return False


def ensure_server() -> dict:
    if _server_up():
        return {"ok": True, "action": "already_running", "base_url": BASE_URL}
    # Start the sidecar as a detached background process. It inherits the
    # workspace config (novel-config.yml) via DSH_NOVEL_CONFIG so data_dir,
    # model endpoint and port all resolve consistently.
    config_path = os.environ.get("DSH_NOVEL_CONFIG", str(WORKSPACE / "novel-config.yml"))
    if not Path(config_path).is_file():
        return {"ok": False, "error": f"config file not found: {config_path}"}
    log_path = WORKSPACE / "novel-server.log"
    env = dict(os.environ)
    env["DSH_NOVEL_CONFIG"] = config_path
    cmd = [_venv_python(), str(WORKSPACE / "dsh-vela" / "backend" / ".venv" / "bin" / "dsh-novel"), "serve"]
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            cmd, env=env, stdout=log, stderr=log,
            start_new_session=True,  # detach so the parent shell exit doesn't kill it
        )
    # Wait up to 15s for the health endpoint.
    deadline = time.time() + 15
    while time.time() < deadline:
        if _server_up():
            return {
                "ok": True,
                "action": "started",
                "pid": proc.pid,
                "base_url": BASE_URL,
                "log": str(log_path),
            }
        time.sleep(0.5)
    return {"ok": False, "error": "sidecar did not become healthy in 15s", "log": str(log_path)}


# --------------------------------------------------------------------------- autorun

def submit(project_id: str, from_chapter: int | None = None, to_chapter: int | None = None) -> dict:
    ensure_server()
    body = {}
    if from_chapter is not None:
        body["from_chapter"] = from_chapter
    if to_chapter is not None:
        body["to_chapter"] = to_chapter
    payload = _http("POST", f"/api/v1/projects/{project_id}/autorun", body, timeout=30)
    result = payload.get("result", {})
    return {
        "ok": True,
        "project_id": project_id,
        "state": result.get("state"),
        "current_chapter": result.get("current_chapter"),
        "chapters_committed": result.get("chapters_committed"),
        "rework_queue": result.get("rework_queue", []),
        "committed_chapters": result.get("committed_chapters", []),
    }


def status(project_id: str) -> dict:
    payload = _http("GET", f"/api/v1/projects/{project_id}/autorun", timeout=30)
    result = payload.get("result", {})
    return {
        "ok": True,
        "project_id": project_id,
        "state": result.get("state"),
        "current_chapter": result.get("current_chapter"),
        "chapters_committed": result.get("chapters_committed"),
        "committed_chapters": sorted(result.get("committed_chapters", [])),
        "rework_queue": result.get("rework_queue", []),
        "last_error": result.get("last_error"),
        "scores": result.get("scores", []),
    }


def wait(project_id: str, timeout: int | None = None) -> dict:
    """Poll until the autorun reaches a terminal state (completed / failed /
    completed_with_rework). Returns the final status; never blocks forever.

    CRITICAL serial discipline: this only *polls* — it never calls the model.
    Do NOT run reverify (or any other model call) while an autorun is running:
    the local model serves one request at a time, and concurrent calls make
    everything crawl.
    """
    ensure_server()
    budget = timeout if timeout is not None else WAIT_TIMEOUT_SECONDS
    deadline = time.time() + budget
    terminal = {"completed", "failed", "completed_with_rework"}
    while True:
        try:
            st = status(project_id)
        except RuntimeError as exc:
            st = {"ok": False, "state": "error", "last_error": str(exc)}
        state = st.get("state")
        if state in terminal or state == "error":
            st["wait_elapsed_seconds"] = round(time.time() - (deadline - budget), 1)
            return st
        if time.time() > deadline:
            st["wait_elapsed_seconds"] = round(budget, 1)
            st["state"] = "timeout"
            st["ok"] = False
            st["last_error"] = (
                f"timed out after {budget}s; run is still {state} — "
                "re-poll with `wait` or check `status`"
            )
            return st
        time.sleep(POLL_INTERVAL_SECONDS)


def export(project_id: str) -> dict:
    # The orchestrator writes manuscript.md + README.md into the project dir
    # when the run finishes. If it hasn't, export the current manuscript.
    payload = _http("POST", f"/api/v1/projects/{project_id}/export", {"format": "markdown"}, timeout=60)
    result = payload.get("result", {})
    return {"ok": True, "project_id": project_id, "export": result}


def auto_create(title: str, premise: str, target_chapters: int, target_words: int | None, hard_rules: list[str]) -> dict:
    ensure_server()
    body = {
        "title": title,
        "premise": premise,
        "target_chapters": target_chapters,
        "hard_rules": hard_rules,
    }
    if target_words:
        body["target_words"] = target_words
    payload = _http("POST", "/api/v1/auto", body, timeout=60)
    result = payload.get("result", {})
    return {
        "ok": True,
        "project_id": result.get("project_id"),
        "state": result.get("state"),
        "autorun": result.get("autorun", {}),
    }


def reverify(project_id: str, chapter_number: int | None = None) -> dict:
    """Master-agent final score-confirmation pass — STRICTLY SEQUENTIAL.

    Re-runs the LLM review gate against committed chapters ONE AT A TIME and
    reports the true scores plus concrete issues (repetition, filler, logic
    bugs, blueprint drift). The plugin's own review can fail open (scores=null)
    when the model times out or its output is truncated, so this is the
    authoritative gate the master agent uses before accepting a chapter.

    SERIAL DISCIPLINE: this calls the model, so it MUST only run when no
    autorun is in progress. The function refuses to start if the project's
    autorun state is 'running' — call `wait` first, then `reverify`.
    """
    import sys

    sys.path.insert(0, str(WORKSPACE / "dsh-vela" / "backend" / "src"))
    from dsh_novel.application import NovelService
    from dsh_novel.application.reviewer import overall_score
    from dsh_novel.config import Settings
    from dsh_novel.transports.http import build_provider, build_reviewer

    # Serial-discipline guard: never review while the orchestrator is writing.
    try:
        st = _http("GET", f"/api/v1/projects/{project_id}/autorun", timeout=15)
        if st.get("result", {}).get("state") == "running":
            return {
                "ok": False,
                "error": (
                    "autorun is still running — serial discipline forbids "
                    "concurrent model calls. Run `wait` first, then `reverify`."
                ),
            }
    except Exception:
        pass  # sidecar may be down; let the service call surface that

    settings = Settings()
    provider = build_provider(settings)
    reviewer = build_reviewer(settings, provider)
    service = NovelService(
        projects_root=settings.data_dir / "projects",
        provider=provider,
        context_token_budget=settings.context_token_budget,
        reviewer=reviewer,
        max_revisions=settings.max_revisions,
    )
    db = service.database(project_id)
    project = db.project()
    policy = service.effective_policy(db)
    threshold = float(policy.get("score_threshold", 8.0))
    target = int(project["target_chapters"])

    numbers = [chapter_number] if chapter_number is not None else range(1, target + 1)
    results = []
    needs_rewrite = []
    for number in numbers:
        content = db.chapter_content(number)
        contract = db.contract(number)
        if content is None or contract is None:
            results.append(
                {"chapter_number": number, "status": "no_content", "scores": None}
            )
            continue
        recent = db.recent_chapters(number, limit=3)
        try:
            issues, verdict = service.reviewer.review(
                project_title=project["title"],
                contract=contract,
                content=content,
                recent_chapters=recent,
                blueprint=project.get("story_spine") or None,
                attempt=1,
                score_threshold=threshold,
            )
            scores = verdict.scores.model_dump(mode="json") if verdict.scores else None
            overall = overall_score(verdict)
            passed = overall is not None and overall >= threshold
            record = {
                "chapter_number": number,
                "verdict": verdict.verdict,
                "scores": scores,
                "overall": overall,
                "threshold": threshold,
                "passed": passed,
                "issues": [
                    {"type": i.issue_type, "severity": i.severity, "instruction": i.instruction}
                    for i in issues
                    if i.severity in {"blocker", "error"}
                ],
            }
            results.append(record)
            if not passed:
                needs_rewrite.append(number)
        except Exception as exc:  # review unavailable -> mark unverified
            results.append(
                {"chapter_number": number, "status": "review_unavailable", "error": str(exc)}
            )
    return {
        "ok": True,
        "project_id": project_id,
        "threshold": threshold,
        "chapters_reviewed": len(results),
        "needs_rewrite": needs_rewrite,
        "chapters": results,
    }


def force_rewrite(project_id: str, chapter_number: int) -> dict:
    """Uncommit a chapter so a fresh autorun re-drafts it through the review gate.

    The orchestrator never re-runs committed chapters, so a chapter that
    committed with a fail-open (unscored) review — or that reverify later
    flagged below threshold — cannot be fixed by submitting autorun alone.
    This rolls the chapter back to PREPARED; the next `submit` picks it up
    and rewrites it. Serial discipline: refuses while autorun is running.
    """
    # Serial guard: no state surgery while the orchestrator is writing.
    try:
        st = _http("GET", f"/api/v1/projects/{project_id}/autorun", timeout=15)
        if st.get("result", {}).get("state") == "running":
            return {
                "ok": False,
                "error": (
                    "autorun is still running — serial discipline forbids "
                    "concurrent state changes. Run `wait` first, then "
                    "`force-rewrite`."
                ),
            }
    except Exception:
        pass  # sidecar down; let the CLI surface the real error

    import subprocess

    venv_py = _venv_python()
    dsh_novel = str(WORKSPACE / "dsh-vela" / "backend" / ".venv" / "bin" / "dsh-novel")
    env = dict(os.environ)
    env.setdefault("DSH_NOVEL_CONFIG", str(WORKSPACE / "novel-config.yml"))
    proc = subprocess.run(
        [venv_py, dsh_novel, "force-rewrite", project_id, str(chapter_number)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or proc.stdout.strip()}
    result = json.loads(proc.stdout)
    result["ok"] = True
    result["next_step"] = (
        f"chapter {chapter_number} is PREPARED now — run `submit` to rewrite it "
        "through the full write -> review -> score gate"
    )
    return result


def monitor(project_id: str, target_chapters: int | None = None) -> dict:
    """Lifecycle monitor: keep the autorun moving until the book is done.

    Runs a poll loop (never calling the model) that:
      - if autorun is running  -> just waits (serial discipline)
      - if completed_with_rework (chapters stuck in the rework queue)
        -> re-submits autorun so the orchestrator retries them
      - if completed (all chapters committed) -> done
      - if failed -> reports the error and stops
    Returns when all target chapters are committed, or after the timeout.
    This is the master agent's standing life-cycle supervision: instead of
    waiting for a human to ask, it keeps checking and re-driving the pipeline.
    """
    ensure_server()
    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    cycles = 0
    while time.time() < deadline:
        cycles += 1
        try:
            st = status(project_id)
        except Exception as exc:
            st = {"ok": False, "state": "error", "last_error": str(exc)}
        state = st.get("state")
        committed = sorted(st.get("committed_chapters", []) or [])
        if target_chapters is None:
            target_chapters = 10  # project default; status doesn't expose it
        if len(committed) >= target_chapters:
            return {
                "ok": True,
                "project_id": project_id,
                "state": "completed",
                "chapters_committed": committed,
                "cycles": cycles,
                "rework_queue": st.get("rework_queue", []),
            }
        if state == "failed":
            return {
                "ok": False,
                "project_id": project_id,
                "state": "failed",
                "last_error": st.get("last_error"),
                "chapters_committed": committed,
            }
        if state == "error":
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if state == "completed_with_rework":
            # Chapters are stuck in the rework queue: re-drive the pipeline.
            rework = st.get("rework_queue", [])
            print(f"[monitor] rework queue {rework} — re-submitting autorun", flush=True)
            submit(project_id)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        # state == "running": just wait; never interfere with the model.
        time.sleep(POLL_INTERVAL_SECONDS)
    return {
        "ok": False,
        "project_id": project_id,
        "state": "timeout",
        "chapters_committed": committed if "committed" in dir() else [],
        "last_error": f"monitor timed out after {WAIT_TIMEOUT_SECONDS}s",
    }


# --------------------------------------------------------------------------- cli

def main() -> int:
    parser = argparse.ArgumentParser(description="DSH Novel master-agent scheduler")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ensure-server", help="start the sidecar if it is not running")

    p_submit = sub.add_parser("submit", help="start autorun (non-blocking)")
    p_submit.add_argument("project_id")
    p_submit.add_argument("--from", dest="from_chapter", type=int)
    p_submit.add_argument("--to", dest="to_chapter", type=int)

    p_status = sub.add_parser("status", help="autorun snapshot (no model calls)")
    p_status.add_argument("project_id")

    p_wait = sub.add_parser("wait", help="poll until terminal state")
    p_wait.add_argument("project_id")
    p_wait.add_argument("--timeout", type=int, help="override WAIT_TIMEOUT_SECONDS")

    p_export = sub.add_parser("export", help="export manuscript")
    p_export.add_argument("project_id")

    p_auto = sub.add_parser("auto", help="create project + outline + autorun")
    p_auto.add_argument("title")
    p_auto.add_argument("premise")
    p_auto.add_argument("--target-chapters", type=int, default=10)
    p_auto.add_argument("--target-words", type=int)
    p_auto.add_argument("--hard-rule", action="append", default=[])

    p_reverify = sub.add_parser(
        "reverify", help="re-run LLM review on committed chapters, report true scores"
    )
    p_reverify.add_argument("project_id")
    p_reverify.add_argument("--chapter", type=int, help="only verify this chapter")

    p_force = sub.add_parser(
        "force-rewrite", help="uncommit a chapter so the next autorun re-drafts it"
    )
    p_force.add_argument("project_id")
    p_force.add_argument("chapter_number", type=int)

    p_monitor = sub.add_parser(
        "monitor",
        help="lifecycle supervision: keep autorun moving until the book is done",
    )
    p_monitor.add_argument("project_id")
    p_monitor.add_argument("--target-chapters", type=int, default=10)

    args = parser.parse_args()
    try:
        if args.command == "ensure-server":
            out = ensure_server()
        elif args.command == "submit":
            out = submit(args.project_id, args.from_chapter, args.to_chapter)
        elif args.command == "status":
            out = status(args.project_id)
        elif args.command == "wait":
            out = wait(args.project_id, args.timeout)
        elif args.command == "export":
            out = export(args.project_id)
        elif args.command == "auto":
            out = auto_create(args.title, args.premise, args.target_chapters, args.target_words, args.hard_rule)
        elif args.command == "reverify":
            out = reverify(args.project_id, args.chapter)
        elif args.command == "force-rewrite":
            out = force_rewrite(args.project_id, args.chapter_number)
        elif args.command == "monitor":
            out = monitor(args.project_id, args.target_chapters)
        else:
            parser.error(f"unknown command: {args.command}")
    except Exception as exc:  # noqa: BLE001 - report any failure as JSON
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
