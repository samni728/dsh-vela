# DSH Novel DeepSeek Harness Adapter

This directory contains the thin TypeScript adapter between DeepSeek Harness and the local DSH Novel Sidecar. It owns no SQLite tables, model providers, retrieval logic, or chapter workflow state.

## Configuration

The bundle patch defaults to:

```yaml
- insert:
    - id: dsh-novel
      name: dsh-novel-plugin
      config:
        endpoint: http://127.0.0.1:17861
        tokenEnv: DSH_NOVEL_TOKEN
        requestTimeoutMs: 30000
        handshakeTimeoutMs: 5000
        maxRenderChars: 20000
        handshakeMode: lazy
```

Set `DSH_NOVEL_TOKEN` in the Harness process environment. `token` is also accepted for local development, but storing secrets in a committed profile patch is discouraged. `token` and a populated `tokenEnv` are mutually exclusive.

`handshakeMode` controls when the adapter performs the Sidecar health/capability handshake:

- `lazy` (default): mounting performs no network request, so a missing Sidecar can never fail the Harness profile bootstrap. The first tool call shakes hands once (bounded by `handshakeTimeoutMs`) and remembers success; while the Sidecar is unreachable, calls return an `ADAPTER_SIDECAR_UNAVAILABLE` envelope (`retryable: true`) with startup guidance (`uv run dsh-novel serve`). Failures are not cached — the next call retries the handshake.
- `boot`: strict legacy behaviour — shake hands at mount and throw on failure (unreachable Sidecar, non-v1 protocol, or missing required capabilities).
- `off`: never handshake.

The deprecated `requireHandshake` flag is still honored when `handshakeMode` is unset: only an explicit `false` maps to `'off'`; every other value (including absence) maps to the new default `'lazy'`. When both are set, `handshakeMode` wins.

## Model-facing tools

- `novel_project_create`
- `novel_project_status`
- `novel_outline_generate`
- `novel_chapter_run`
- `novel_run_status`
- `novel_run_resume`
- `novel_manuscript_export`
- `novel_auto_create` — one-shot fully automatic mode: create project + outline + autorun long haul; optional `policy` override (`score_threshold` / `max_revisions` / `target_words` / `on_chapter_failure`)
- `novel_autorun_start` — optional `from_chapter` / `to_chapter` plus the same `policy` override; rework-queue chapters are retried first
- `novel_autorun_status` — poll until `state` is `completed` or `failed`
- `novel_pipeline_status` — zero-prose management snapshot: scores and status only, never chapter content
- `novel_report`

Every call forwards Harness cancellation to `fetch`, has a cooperative timeout, and returns the stable Novel envelope. Large responses are bounded only in their model-facing rendering; the canonical JSON value is preserved by Harness.

`policy` objects are partial: any field you omit (or set to `null`) is stripped by the adapter before the request leaves, so the Sidecar falls back to stored project policy and its own defaults.

## Master Agent protocol

The Sidecar splits into a **creation plane** (writes, reviews, stores prose) and a **management plane** (read-only numbers and status). This adapter keeps the split visible to the model:

| Plane | Tools | What they return | Who consumes it |
|---|---|---|---|
| Management | `novel_pipeline_status`, `novel_autorun_status`, `novel_project_status` | Run state, effective policy, per-chapter scores/statuses/word counts, rework queue, totals — numbers and status only | Master Agent polls and decides |
| Management (commands) | `novel_auto_create`, `novel_autorun_start` | Commands plus an optional partial `policy`; no prose in either direction | Master Agent starts / resumes runs |
| Creation | `novel_chapter_run`, `novel_run_resume`, `novel_manuscript_export`, `novel_report` | Chapter content, digests, report text | Human review / export flows only |

**Zero-prose guarantee.** `novel_pipeline_status` maps to `GET /api/v1/projects/{id}/pipeline`. The Sidecar validates that payload (`assert_management_payload`) before returning it: no `content` / `digest` / `prose` keys at any depth, every string value capped at 200 characters, and prose expressed only as numeric `word_count`. The adapter forwards the payload untouched, so manuscript content cannot leak to the Master Agent through it.

**The Master Agent never reads or rewrites chapter content.** It starts runs, watches numbers, and adjusts policy; prose leaves the Sidecar only when a human explicitly asks for an export or a report.

**Recommended loop** (management calls are read-only and concurrency-safe):

```text
# start once; policy fields you omit are dropped by the adapter
res = novel_auto_create {title, premise, target_chapters, policy?}
project_id = res.result.project_id

# poll (interval >= 5s)
loop:
  p = novel_pipeline_status {project_id}
  switch p.result.state:
    "running"                -> report p.result.totals, keep polling
    "completed"              -> break            # everything committed
    "completed_with_rework"  -> break            # rework queue non-empty
    "failed"                 -> inspect last_error via novel_autorun_status;
                                fix the cause, then retry novel_autorun_start
                                (it resumes from the last safe checkpoint)

# completed_with_rework: send autorun again for the rework chapters.
# A fresh call retries queued chapters first, then continues with new ones;
# adjust policy in the same call if wanted (e.g. lower score_threshold).
if p.result.rework_queue is not empty:
    novel_autorun_start {project_id, policy?}   # loop back to polling

# finish (human actions)
novel_report {project_id}               # generated README report
novel_manuscript_export {project_id}    # only when prose is actually wanted
```

## Development

```sh
npm install
npm run check
npm test
npm run build
npm pack --dry-run
```

The repository root is the installable Git bundle. The root manifest points to `adapter-dsh/lib/index.js` and `adapter-dsh/cordis.patch.yml`, so users install the repository rather than this subdirectory:

```sh
dsh plugin --profile novel add github:samni728/dsh-vela#<commit>
```

For a local adapter-only tarball, run `npm pack` in this directory and install the generated `.tgz` with `dsh plugin --profile novel add <tarball>`.
