#!/usr/bin/env python3
"""方案 A 流水线驱动(同步逐章,共享模型端点时零争抢).

当 Master Agent 与写作 Sidecar 共用同一个本地推理端点时,不要用
异步长跑(autorun)并行占用模型,而应采用本脚本的同步逐章模式:

    第N章: 起草→审稿→修复→定稿  (写作模型独占,Master 模型空闲)
    第N章完成后才进入第N+1章     (两阶段严格交替,无并发)

用法:
    python3 scripts/pipeline_loop.py <project_id> [from_chapter] [to_chapter]

环境变量:
    DSH_NOVEL_ENDPOINT    Sidecar 地址(默认 http://127.0.0.1:17861)
    DSH_NOVEL_TOKEN       Sidecar Bearer token(可选)
    NOVEL_MAX_ATTEMPTS    单章最大尝试次数(默认 3)
    NOVEL_WATCH_MINUTES   客户端断连后的服务端状态监视时长(默认 25)

行为:
    - 严格顺序:一章完全定稿(COMMITTED)后才发起下一章;
    - 客户端断连不中断服务端生成(转监视模式);
    - FAILED_RETRYABLE / QUALITY_BLOCKED 自动恢复重写;
    - 每章打印状态与审稿分数,供 Master 在章间判定;
    - 全部完成后打印汇总表。
"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("DSH_NOVEL_ENDPOINT", "http://127.0.0.1:17861").rstrip("/")
API = f"{BASE}/api/v1"
TOKEN = os.environ.get("DSH_NOVEL_TOKEN")
MAX_ATTEMPTS = int(os.environ.get("NOVEL_MAX_ATTEMPTS", "3"))
WATCH_SECONDS = int(os.environ.get("NOVEL_WATCH_MINUTES", "25")) * 60


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def post(path: str, body: dict | None = None, timeout: int = 2700) -> dict:
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body or {}).encode(),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
            return {
                "ok": False,
                "run_id": detail.get("run_id"),
                "error": {
                    "message": f"HTTP {exc.code}: "
                    f"{(detail.get('error') or {}).get('message')}"
                },
            }
        except Exception:
            return {"ok": False, "run_id": None, "error": {"message": f"HTTP {exc.code}"}}
    except Exception as exc:  # noqa: BLE001 - client-side network errors
        return {
            "ok": False,
            "run_id": None,
            "error": {"message": f"CLIENT_ERROR: {type(exc).__name__}: {exc}"},
        }


def get(path: str) -> dict:
    try:
        with urllib.request.urlopen(API + path, timeout=30) as response:
            return json.loads(response.read().decode()).get("result") or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def project_state(project_id: str) -> dict:
    return get(f"/projects/{project_id}")


def chapter_committed(project_id: str, chapter_number: int) -> bool:
    project = project_state(project_id)
    return any(
        ch.get("chapter_number") == chapter_number
        and ch.get("status") == "COMMITTED"
        for ch in (project.get("chapters") or [])
    )


def run_chapter(project_id: str, chapter_number: int) -> bool:
    log(f"第{chapter_number}章 开始")
    run_id: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if run_id:
            resp = post(f"/runs/{run_id}/resume")
        else:
            resp = post(f"/projects/{project_id}/chapters/{chapter_number}/run")
        run_id = resp.get("run_id") or run_id
        if resp.get("ok") and (resp.get("result") or {}).get("status") == "COMMITTED":
            _report_chapter(project_id, chapter_number, run_id, attempt)
            return True
        err = (resp.get("error") or {}).get("message") or ""
        if "already committed" in err or chapter_committed(project_id, chapter_number):
            log(f"第{chapter_number}章 已在服务端完成")
            _report_chapter(project_id, chapter_number, run_id, attempt)
            return True
        log(f"第{chapter_number}章 尝试{attempt}: {err[:120]}")
        # 客户端可能断连但服务端仍在跑:转入监视模式
        deadline = time.time() + WATCH_SECONDS
        while time.time() < deadline:
            time.sleep(60)
            status, rid = latest_run(project_id, chapter_number)
            if status == "COMMITTED":
                log(f"第{chapter_number}章 定稿(监视发现)")
                _report_chapter(project_id, chapter_number, rid, attempt)
                return True
            if status in ("FAILED_RETRYABLE", "QUALITY_BLOCKED", "PAUSED") and rid:
                run_id = rid
                log(f"第{chapter_number}章 可恢复状态 {status},自动重写")
                break
            if status == "FAILED":
                if chapter_committed(project_id, chapter_number):
                    log(f"第{chapter_number}章 已定稿")
                    return True
                run_id = None
                log(f"第{chapter_number}章 终态失败,重新发起")
                break
        else:
            log(f"第{chapter_number}章 监视超时,放弃")
            return False
    log(f"第{chapter_number}章 尝试 {MAX_ATTEMPTS} 次后仍失败")
    return False


def latest_run(project_id: str, chapter_number: int) -> tuple[str | None, str | None]:
    project = project_state(project_id)
    for run in reversed(project.get("recent_runs") or []):
        if run.get("chapter_number") == chapter_number:
            return run.get("status"), run.get("id")
    return None, None


def _report_chapter(
    project_id: str, chapter_number: int, run_id: str | None, attempt: int
) -> None:
    score = None
    if run_id:
        run = get(f"/runs/{run_id}")
        review = run.get("review")
        if isinstance(review, list) and review:
            score = review[-1].get("overall_score")
    line = f"第{chapter_number}章 ✅ COMMITTED (attempt {attempt})"
    if score is not None:
        line += f" | overall={score}"
    log(line)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project_id = sys.argv[1]
    from_chapter = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    to_chapter = (
        int(sys.argv[3]) if len(sys.argv) > 3 else from_chapter
    )
    project = project_state(project_id)
    target = project.get("target_chapters") or to_chapter
    if to_chapter == from_chapter and len(sys.argv) < 4:
        to_chapter = target
    log(f"流水线启动: 项目 {project_id} 第{from_chapter}-{to_chapter}章(严格串行)")
    results = []
    for chapter in range(from_chapter, to_chapter + 1):
        ok = run_chapter(project_id, chapter)
        results.append((chapter, ok))
        if not ok:
            log(f"第{chapter}章失败,流水线停在断点(可修复后重新运行续跑)")
            break
    done = sum(1 for _, ok in results if ok)
    log(f"流水线结束: {done}/{len(results)} 章定稿")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
