import { describe, expect, it, vi } from 'vitest'
import { NovelClient, routes } from '../src/client.js'
import { NovelAdapterError } from '../src/errors.js'

describe('NovelClient', () => {
  it('centralizes routes, sends auth, and preserves an envelope', async () => {
    const fetchMock = mockFetch(async (input, init) => {
      expect(String(input)).toBe(`http://127.0.0.1:17861${routes.projects}`)
      expect(new Headers(init?.headers).get('authorization')).toBe('Bearer local-secret')
      expect(JSON.parse(String(init?.body))).toEqual({ title: 'Vela' })
      return jsonResponse({
        ok: true,
        request_id: 'req_server',
        protocol_version: '1.0',
        result: { project_id: 'prj_1' },
        warnings: [],
        error: null,
      })
    })
    const client = new NovelClient({
      endpoint: 'http://127.0.0.1:17861/',
      timeoutMs: 1_000,
      token: 'local-secret',
      fetch: fetchMock,
    })

    const result = await client.createProject({ title: 'Vela' })
    expect(result.ok).toBe(true)
    expect(result.request_id).toBe('req_server')
    expect(result.result).toEqual({ project_id: 'prj_1' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('wraps a successful direct JSON response in the stable envelope', async () => {
    const client = new NovelClient({
      endpoint: 'http://localhost:17861',
      timeoutMs: 1_000,
      fetch: mockFetch(async () => jsonResponse({ status: 'ok' }, 200, { 'x-protocol-version': '1.2' })),
    })
    const result = await client.health()
    expect(result).toMatchObject({
      ok: true,
      protocol_version: '1.2',
      result: { status: 'ok' },
      warnings: [],
      error: null,
    })
  })

  it('maps cooperative timeout to a retryable structured error', async () => {
    const client = new NovelClient({
      endpoint: 'http://localhost:17861',
      timeoutMs: 5,
      fetch: abortOnlyFetch(),
    })
    await expect(client.health()).rejects.toMatchObject({
      name: 'NovelAdapterError',
      code: 'SIDECAR_TIMEOUT',
      retryable: true,
    })
  })

  it('maps caller cancellation independently from timeout', async () => {
    const client = new NovelClient({
      endpoint: 'http://localhost:17861',
      timeoutMs: 1_000,
      fetch: abortOnlyFetch(),
    })
    const controller = new AbortController()
    const pending = client.projectStatus('prj_1', controller.signal)
    controller.abort()
    await expect(pending).rejects.toMatchObject({ code: 'RUN_CANCELLED', retryable: false })
  })

  it('rejects non-json Sidecar responses with bounded diagnostic detail', async () => {
    const client = new NovelClient({
      endpoint: 'http://localhost:17861',
      timeoutMs: 1_000,
      fetch: mockFetch(async () => new Response('<html>bad gateway</html>', { status: 502 })),
    })
    await expect(client.health()).rejects.toMatchObject({
      code: 'SIDECAR_RESPONSE_INVALID',
      status: 502,
    })
  })

  it('aborts active requests when the plugin unloads', async () => {
    const client = new NovelClient({
      endpoint: 'http://localhost:17861',
      timeoutMs: 1_000,
      fetch: abortOnlyFetch(),
    })
    const pending = client.health()
    client.close()
    await expect(pending).rejects.toBeInstanceOf(NovelAdapterError)
    await expect(client.health()).rejects.toMatchObject({ code: 'ADAPTER_UNLOADED' })
  })
})

type FetchHandler = (input: string | URL | Request, init?: RequestInit) => Promise<Response>

function mockFetch(handler: FetchHandler): typeof fetch {
  return vi.fn(handler) as unknown as typeof fetch
}

function abortOnlyFetch(): typeof fetch {
  return mockFetch((_input, init) => new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal
    if (signal?.aborted === true) {
      reject(signal.reason)
      return
    }
    signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
  }))
}

function jsonResponse(value: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json', ...Object.fromEntries(new Headers(headers)) },
  })
}
