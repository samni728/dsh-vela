import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apply } from '../src/index.js'
import { REQUIRED_CAPABILITIES } from '../src/protocol.js'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apply handshakeMode', () => {
  it('mounts lazily without any network request and registers every tool', async () => {
    const { definitions, ctx } = collectingContext()
    const counters = installSidecar()
    await expect(apply(ctx, {})).resolves.toBeUndefined()
    expect(definitions.map(tool => tool.name)).toHaveLength(12)
    expect(counters.health).toBe(0)
    expect(counters.capabilities).toBe(0)
  })

  it('returns a retryable ADAPTER_SIDECAR_UNAVAILABLE envelope when the Sidecar is down', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await apply(ctx, {})
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({
      ok: false,
      error: { code: 'ADAPTER_SIDECAR_UNAVAILABLE', retryable: true },
    })
    const message = readErrorMessage(result)
    expect(message).toContain('uv run dsh-novel serve')
  })

  it('retries the handshake after a failure and performs it exactly once on success', async () => {
    const { definitions, ctx } = collectingContext()
    // The first /health attempt fails; everything afterwards succeeds.
    const counters = installSidecar({ healthFailures: 1 })
    await apply(ctx, {})

    expect(await callProjectStatus(definitions)).toMatchObject({
      ok: false,
      error: { code: 'ADAPTER_SIDECAR_UNAVAILABLE' },
    })
    expect(await callProjectStatus(definitions)).toMatchObject({ ok: true })
    expect(await callProjectStatus(definitions)).toMatchObject({ ok: true })

    // One failed attempt (health only) plus exactly one successful handshake.
    expect(counters.health).toBe(2)
    expect(counters.capabilities).toBe(1)
  })

  it('shares one handshake across concurrent first calls', async () => {
    const { definitions, ctx } = collectingContext()
    const counters = installSidecar({ healthFailures: 0 })
    await apply(ctx, {})
    const results = await Promise.all([
      callProjectStatus(definitions),
      callProjectStatus(definitions),
      callProjectStatus(definitions),
    ])
    expect(results).toHaveLength(3)
    for (const result of results) expect(result).toMatchObject({ ok: true })
    expect(counters.health).toBe(1)
    expect(counters.capabilities).toBe(1)
  })

  it('keeps boot mode failing at mount when the Sidecar is unreachable', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await expect(apply(ctx, { handshakeMode: 'boot' })).rejects.toMatchObject({
      name: 'NovelAdapterError',
      code: 'SIDECAR_UNAVAILABLE',
    })
    expect(definitions).toHaveLength(0)
  })

  it('keeps boot mode handshaking at mount before any tool call', async () => {
    const { definitions, ctx } = collectingContext()
    const counters = installSidecar({ healthFailures: 0 })
    await apply(ctx, { handshakeMode: 'boot' })
    expect(definitions.map(tool => tool.name)).toHaveLength(12)
    expect(counters.health).toBe(1)
    expect(counters.capabilities).toBe(1)

    await callProjectStatus(definitions)
    expect(counters.health).toBe(1)
    expect(counters.capabilities).toBe(1)
  })

  it('maps legacy requireHandshake:false to off with raw client errors', async () => {
    const { definitions, ctx } = collectingContext()
    const counters = installSidecar({ unreachable: true })
    await apply(ctx, { requireHandshake: false })
    expect(counters.health).toBe(0)
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({ ok: false, error: { code: 'SIDECAR_UNAVAILABLE' } })
    const message = readErrorMessage(result)
    expect(message).not.toContain('ADAPTER_SIDECAR_UNAVAILABLE')
  })

  it('treats legacy requireHandshake:true like lazy mode', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await apply(ctx, { requireHandshake: true })
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({
      ok: false,
      error: { code: 'ADAPTER_SIDECAR_UNAVAILABLE', retryable: true },
    })
  })

  it('defaults an empty config to lazy mode', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await apply(ctx, {})
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({ ok: false, error: { code: 'ADAPTER_SIDECAR_UNAVAILABLE' } })
  })

  it('prefers handshakeMode over a legacy requireHandshake:false', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await apply(ctx, { requireHandshake: false, handshakeMode: 'lazy' })
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({ ok: false, error: { code: 'ADAPTER_SIDECAR_UNAVAILABLE' } })
  })

  it('honors an explicit handshakeMode:off', async () => {
    const { definitions, ctx } = collectingContext()
    installSidecar({ unreachable: true })
    await apply(ctx, { handshakeMode: 'off' })
    const result = await callProjectStatus(definitions)
    expect(result).toMatchObject({ ok: false, error: { code: 'SIDECAR_UNAVAILABLE' } })
  })
})

interface SidecarCounters {
  health: number
  capabilities: number
  projectReads: number
}

function installSidecar(options: { healthFailures?: number; unreachable?: boolean } = {}): SidecarCounters {
  const counters: SidecarCounters = { health: 0, capabilities: 0, projectReads: 0 }
  const fetchMock = vi.fn(async (input: string | URL | Request): Promise<Response> => {
    const url = String(input)
    if (url.endsWith('/health')) {
      counters.health += 1
      if (options.unreachable === true || counters.health <= (options.healthFailures ?? Number.POSITIVE_INFINITY)) {
        throw new Error('connect ECONNREFUSED 127.0.0.1:17861')
      }
      return jsonResponse({ status: 'ok' })
    }
    if (options.unreachable === true) throw new Error('connect ECONNREFUSED 127.0.0.1:17861')
    if (url.endsWith('/capabilities')) {
      counters.capabilities += 1
      return jsonResponse({
        protocol_version: '1.0',
        core_version: '0.1.0',
        capabilities: [...REQUIRED_CAPABILITIES],
      })
    }
    if (url.includes('/api/v1/projects/prj_1')) {
      counters.projectReads += 1
      return jsonResponse({ project_id: 'prj_1', title: '雾港档案', chapters: [] })
    }
    throw new Error(`Unexpected Sidecar request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return counters
}

async function callProjectStatus(definitions: ToolDefinition[], projectId = 'prj_1'): Promise<unknown> {
  const tool = definitions.find(item => item.name === 'novel_project_status')
  return tool?.execute({ project_id: projectId }, { signal: new AbortController().signal } as never)
}

function readErrorMessage(result: unknown): string {
  const error = (result as { error?: { message?: unknown } } | undefined)?.error
  return typeof error?.message === 'string' ? error.message : ''
}

function collectingContext(): { definitions: ToolDefinition[]; disposers: (() => void)[]; ctx: Context } {
  const definitions: ToolDefinition[] = []
  const disposers: (() => void)[] = []
  const ctx = {
    effect(dispose: () => () => void) {
      disposers.push(dispose())
    },
    tools: {
      register(tool: ToolDefinition) {
        definitions.push(tool)
        return () => undefined
      },
    },
  } as unknown as Context
  return { definitions, disposers, ctx }
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json', 'x-protocol-version': '1.0' },
  })
}
