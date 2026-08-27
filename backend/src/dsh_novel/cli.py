from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import uvicorn

from dsh_novel.api_models import ProjectCreateRequest
from dsh_novel.application import NovelService
from dsh_novel.application.orchestrator import AutorunManager
from dsh_novel.config import CONFIG_FILE_ENV, ConfigError, Settings, config_file_path
from dsh_novel.transports.http import build_provider, build_reviewer, create_app


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _service(settings: Settings) -> NovelService:
    settings.ensure_directories()
    provider = build_provider(settings)
    return NovelService(
        projects_root=settings.data_dir / "projects",
        provider=provider,
        context_token_budget=settings.context_token_budget,
        reviewer=build_reviewer(settings, provider),
        max_revisions=settings.max_revisions,
    )


def _orchestrator(settings: Settings) -> AutorunManager:
    """Build the autorun manager (daemon-thread whole-book runner).

    This is the *correct* way to drive long novel runs: it executes chapters
    sequentially in a background thread, self-heals FAILED_RETRYABLE /
    QUALITY_BLOCKED runs with backoff, retries the rework queue first, and
    never blocks the calling process on a single 20-minute model call.
    """
    service = _service(settings)
    return AutorunManager(service, max_revisions=settings.max_revisions)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="dsh-novel")
    root.add_argument("--data-dir", type=Path, help="override local data directory")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="start the local HTTP sidecar")

    create = commands.add_parser("project-create")
    create.add_argument("--title", required=True)
    create.add_argument("--premise", default="")
    create.add_argument("--target-chapters", type=int, default=10)

    status = commands.add_parser("project-status")
    status.add_argument("project_id")

    run = commands.add_parser("run-chapter")
    run.add_argument("project_id")
    run.add_argument("chapter_number", type=int)

    run_status = commands.add_parser("run-status")
    run_status.add_argument("run_id")

    resume = commands.add_parser("resume")
    resume.add_argument("run_id")

    force_rewrite = commands.add_parser(
        "force-rewrite",
        help="uncommit a chapter so a fresh autorun re-drafts it",
    )
    force_rewrite.add_argument("project_id")
    force_rewrite.add_argument("chapter_number", type=int)

    export = commands.add_parser("export")
    export.add_argument("project_id")
    export.add_argument("--format", choices=["markdown", "text"], default="markdown")

    # Non-blocking autorun driver: submit -> poll -> wait. This is the
    # master-agent friendly entry point; `run-chapter`/`resume` are
    # synchronous and will block for the whole chapter generation.
    autorun = commands.add_parser(
        "autorun",
        help="non-blocking whole-book orchestrator (start/status/pipeline/wait)",
    )
    autorun_sub = autorun.add_subparsers(dest="autorun_command", required=True)

    a_start = autorun_sub.add_parser("start", help="start autorun (returns immediately)")
    a_start.add_argument("project_id")
    a_start.add_argument("--from", dest="from_chapter", type=int)
    a_start.add_argument("--to", dest="to_chapter", type=int)

    a_status = autorun_sub.add_parser("status", help="autorun snapshot (no model calls)")
    a_status.add_argument("project_id")

    a_pipeline = autorun_sub.add_parser("pipeline", help="management snapshot (numbers only)")
    a_pipeline.add_argument("project_id")

    a_wait = autorun_sub.add_parser("wait", help="poll until terminal state")
    a_wait.add_argument("project_id")
    a_wait.add_argument("--timeout", type=int, default=6 * 3600,
                        help="wall-clock budget in seconds (default 6h)")
    a_wait.add_argument("--poll", type=float, default=30.0,
                        help="poll interval in seconds (default 30)")

    commands.add_parser(
        "config-path",
        help="print the effective config.yml path and whether it exists",
    )
    return root


def _print_config_path() -> None:
    path = config_file_path()
    source = CONFIG_FILE_ENV if os.getenv(CONFIG_FILE_ENV) else "default"
    _print({"config_path": str(path), "exists": path.is_file(), "source": source})


def main() -> None:
    args = parser().parse_args()
    if args.command == "config-path":
        _print_config_path()
        return
    try:
        settings = Settings()
        if args.data_dir:
            settings = Settings(data_dir=args.data_dir)
    except ConfigError as exc:
        raise SystemExit(f"dsh-novel: invalid configuration: {exc}") from exc
    if args.command == "serve":
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            reload=False,
        )
        return
    service = _service(settings)
    if args.command == "project-create":
        _print(
            service.create_project(
                ProjectCreateRequest(
                    title=args.title,
                    premise=args.premise,
                    target_chapters=args.target_chapters,
                )
            )
        )
    elif args.command == "project-status":
        _print(service.project_status(args.project_id))
    elif args.command == "run-chapter":
        _print(
            service.run_chapter(
                project_id=args.project_id,
                chapter_number=args.chapter_number,
                supplied_contract=None,
                idempotency_key=None,
            )
        )
    elif args.command == "run-status":
        _print(service.run_status(args.run_id))
    elif args.command == "resume":
        _print(service.resume_run(args.run_id))
    elif args.command == "force-rewrite":
        _print(service.force_rewrite(args.project_id, args.chapter_number))
    elif args.command == "autorun":
        orchestrator = _orchestrator(settings)
        if args.autorun_command == "start":
            _print(
                orchestrator.start(
                    args.project_id,
                    getattr(args, "from_chapter", None),
                    getattr(args, "to_chapter", None),
                )
            )
        elif args.autorun_command == "status":
            _print(orchestrator.status(args.project_id))
        elif args.autorun_command == "pipeline":
            _print(orchestrator.pipeline(args.project_id))
        elif args.autorun_command == "wait":
            import time as _time

            terminal = {"completed", "failed", "completed_with_rework"}
            deadline = _time.time() + args.timeout
            while True:
                st = orchestrator.status(args.project_id)
                if st["state"] in terminal:
                    _print(st)
                    break
                if _time.time() > deadline:
                    st["state"] = "timeout"
                    st["last_error"] = (
                        f"timed out after {args.timeout}s; run is still "
                        f"{st.get('state')} — re-run `autorun wait` to continue polling"
                    )
                    _print(st)
                    raise SystemExit(1)
                _time.sleep(args.poll)
    elif args.command == "export":
        _print(service.export(args.project_id, args.format))
