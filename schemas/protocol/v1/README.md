# DSH Novel Protocol v1

Protocol version: `1.0`.

The JSON schemas in this directory describe the stable boundary between transport
adapters and the independent Novel Sidecar. Additive optional fields are allowed
within protocol v1; consumers must ignore unknown optional response fields.

Frozen Sidecar routes:

- `GET /health`
- `GET /api/v1/capabilities`
- `POST /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `POST /api/v1/projects/{project_id}/chapters/{chapter_number}/run`
- `GET /api/v1/runs/{run_id}`
- `POST /api/v1/runs/{run_id}/resume`
- `POST /api/v1/projects/{project_id}/export`

The `prepare` route is an additional debugging API and is not a required Harness
capability.

