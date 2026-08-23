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
        requireHandshake: true
```

Set `DSH_NOVEL_TOKEN` in the Harness process environment. `token` is also accepted for local development, but storing secrets in a committed profile patch is discouraged. `token` and a populated `tokenEnv` are mutually exclusive.

With `requireHandshake: true`, plugin loading fails if the Sidecar is unavailable, speaks a non-v1 protocol, or lacks one of the required coarse capabilities.

## Model-facing tools

- `novel_project_create`
- `novel_project_status`
- `novel_chapter_run`
- `novel_run_status`
- `novel_run_resume`
- `novel_manuscript_export`

Every call forwards Harness cancellation to `fetch`, has a cooperative timeout, and returns the stable Novel envelope. Large responses are bounded only in their model-facing rendering; the canonical JSON value is preserved by Harness.

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
