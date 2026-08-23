import { randomUUID } from 'node:crypto'
import { ADAPTER_VERSION, isJsonObject, isJsonValue, isNovelEnvelope } from './protocol.js'
import type { JsonObject, JsonValue, NovelEnvelope } from './protocol.js'
import { NovelAdapterError } from './errors.js'

export interface NovelClientOptions {
  endpoint: string
  token?: string
  timeoutMs: number
  fetch?: typeof globalThis.fetch
}

interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: JsonValue
  signal?: AbortSignal | undefined
  timeoutMs?: number | undefined
}

/** All Sidecar paths live here so a protocol revision does not leak into tools. */
export const routes = {
  health: '/health',
  capabilities: '/api/v1/capabilities',
  projects: '/api/v1/projects',
  project: (projectId: string) => `/api/v1/projects/${encodeURIComponent(projectId)}`,
  chapterRun: (projectId: string, chapterNumber: number) =>
    `/api/v1/projects/${encodeURIComponent(projectId)}/chapters/${chapterNumber}/run`,
  run: (runId: string) => `/api/v1/runs/${encodeURIComponent(runId)}`,
  runResume: (runId: string) => `/api/v1/runs/${encodeURIComponent(runId)}/resume`,
  manuscriptExport: (projectId: string) => `/api/v1/projects/${encodeURIComponent(projectId)}/export`,
} as const

export class NovelClient {
  readonly endpoint: string
  readonly timeoutMs: number
  readonly #fetch: typeof globalThis.fetch
  readonly #token?: string
  readonly #active = new Set<AbortController>()
  #closed = false

  constructor(options: NovelClientOptions) {
    this.endpoint = normalizeEndpoint(options.endpoint)
    this.timeoutMs = positiveInteger('timeoutMs', options.timeoutMs)
    this.#fetch = options.fetch ?? globalThis.fetch
    if (typeof this.#fetch !== 'function') {
      throw new NovelAdapterError('CONFIG_INVALID', 'This Node.js runtime does not provide fetch().')
    }
    if (options.token !== undefined && options.token.length > 0) this.#token = options.token
  }

  close(): void {
    this.#closed = true
    for (const controller of this.#active) {
      controller.abort(new Error('DSH Novel adapter unloaded'))
    }
    this.#active.clear()
  }

  health(signal?: AbortSignal, timeoutMs?: number): Promise<NovelEnvelope> {
    return this.request(routes.health, { signal, timeoutMs })
  }

  capabilities(signal?: AbortSignal, timeoutMs?: number): Promise<NovelEnvelope> {
    return this.request(routes.capabilities, { signal, timeoutMs })
  }

  createProject(input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.projects, { method: 'POST', body: input, signal })
  }

  projectStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.project(projectId), { signal })
  }

  runChapter(projectId: string, chapterNumber: number, input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.chapterRun(projectId, chapterNumber), { method: 'POST', body: input, signal })
  }

  runStatus(runId: string, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.run(runId), { signal })
  }

  resumeRun(runId: string, forceRedraft: boolean, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.runResume(runId), {
      method: 'POST',
      body: { force_redraft: forceRedraft },
      signal,
    })
  }

  exportManuscript(projectId: string, format: string, signal?: AbortSignal): Promise<NovelEnvelope> {
    return this.request(routes.manuscriptExport(projectId), {
      method: 'POST',
      body: { format },
      signal,
    })
  }

  async request(path: string, options: RequestOptions = {}): Promise<NovelEnvelope> {
    if (this.#closed) {
      throw new NovelAdapterError('ADAPTER_UNLOADED', 'The DSH Novel adapter has been unloaded.')
    }
    const requestId = `adapter_${randomUUID()}`
    const controller = new AbortController()
    this.#active.add(controller)
    let callerCancelled = false
    let timedOut = false
    const onCallerAbort = (): void => {
      callerCancelled = true
      controller.abort(options.signal?.reason)
    }
    if (options.signal?.aborted === true) onCallerAbort()
    else options.signal?.addEventListener('abort', onCallerAbort, { once: true })

    const timeoutMs = positiveInteger('timeoutMs', options.timeoutMs ?? this.timeoutMs)
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort(new Error(`Sidecar request exceeded ${timeoutMs} ms`))
    }, timeoutMs)

    try {
      const headers: Record<string, string> = {
        accept: 'application/json',
        'content-type': 'application/json',
        'x-request-id': requestId,
        'x-dsh-novel-adapter-version': ADAPTER_VERSION,
      }
      if (this.#token !== undefined) headers.authorization = `Bearer ${this.#token}`
      const response = await this.#fetch(`${this.endpoint}${path}`, {
        method: options.method ?? 'GET',
        headers,
        ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
        signal: controller.signal,
      })
      const parsed = await parseResponse(response, requestId)
      if (isNovelEnvelope(parsed)) return normalizeEnvelope(parsed, requestId)
      if (!response.ok) {
        throw new NovelAdapterError('SIDECAR_HTTP_ERROR', `Sidecar returned HTTP ${response.status}.`, {
          status: response.status,
          requestId,
          retryable: response.status >= 500,
          details: isJsonValue(parsed) ? parsed : null,
        })
      }
      return successEnvelope(parsed, requestId, response.headers.get('x-protocol-version'))
    } catch (error) {
      if (callerCancelled) {
        throw new NovelAdapterError('RUN_CANCELLED', 'The Harness tool call was cancelled.', {
          requestId,
          cause: error,
        })
      }
      if (timedOut) {
        throw new NovelAdapterError('SIDECAR_TIMEOUT', `Sidecar request exceeded ${timeoutMs} ms.`, {
          requestId,
          retryable: true,
          cause: error,
        })
      }
      if (error instanceof NovelAdapterError) throw error
      throw new NovelAdapterError('SIDECAR_UNAVAILABLE', `Cannot reach DSH Novel Sidecar at ${this.endpoint}.`, {
        requestId,
        retryable: true,
        cause: error,
      })
    } finally {
      clearTimeout(timer)
      options.signal?.removeEventListener('abort', onCallerAbort)
      this.#active.delete(controller)
    }
  }
}

function normalizeEndpoint(endpoint: string): string {
  let url: URL
  try {
    url = new URL(endpoint)
  } catch (error) {
    throw new NovelAdapterError('CONFIG_INVALID', `Invalid Sidecar endpoint: ${endpoint}`, { cause: error })
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new NovelAdapterError('CONFIG_INVALID', 'Sidecar endpoint must use http or https.')
  }
  if (url.username.length > 0 || url.password.length > 0 || url.search.length > 0 || url.hash.length > 0) {
    throw new NovelAdapterError('CONFIG_INVALID', 'Sidecar endpoint cannot contain credentials, query, or fragment.')
  }
  return url.toString().replace(/\/$/, '')
}

function positiveInteger(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new NovelAdapterError('CONFIG_INVALID', `${name} must be a positive safe integer.`)
  }
  return value
}

async function parseResponse(response: Response, requestId: string): Promise<unknown> {
  const text = await response.text()
  if (text.length === 0) return null
  try {
    return JSON.parse(text) as unknown
  } catch (error) {
    throw new NovelAdapterError('SIDECAR_RESPONSE_INVALID', 'Sidecar returned non-JSON content.', {
      requestId,
      status: response.status,
      details: text.slice(0, 500),
      cause: error,
    })
  }
}

function normalizeEnvelope(value: NovelEnvelope, requestId: string): NovelEnvelope {
  const protocolVersion = typeof value.protocol_version === 'string' ? value.protocol_version : '1.0'
  const warnings = Array.isArray(value.warnings) && value.warnings.every(isJsonValue) ? value.warnings : []
  const result = isJsonValue(value.result) ? value.result : null
  let error: NovelEnvelope['error'] = null
  if (isJsonObject(value.error) && typeof value.error.code === 'string' && typeof value.error.message === 'string') {
    error = {
      code: value.error.code,
      message: value.error.message,
      retryable: typeof value.error.retryable === 'boolean' ? value.error.retryable : false,
      details: isJsonValue(value.error.details) ? value.error.details : null,
    }
  }
  return {
    ...value,
    ok: value.ok,
    request_id: typeof value.request_id === 'string' ? value.request_id : requestId,
    protocol_version: protocolVersion,
    result,
    warnings,
    error,
  }
}

function successEnvelope(value: unknown, requestId: string, protocolHeader: string | null): NovelEnvelope {
  if (!isJsonValue(value)) {
    throw new NovelAdapterError('SIDECAR_RESPONSE_INVALID', 'Sidecar response is not lossless JSON.', { requestId })
  }
  return {
    ok: true,
    request_id: requestId,
    protocol_version: protocolHeader ?? '1.0',
    result: value,
    warnings: [],
    error: null,
  }
}
