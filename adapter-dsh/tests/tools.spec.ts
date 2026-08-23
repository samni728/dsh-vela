import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { describe, expect, it, vi } from 'vitest'
import type { NovelClient } from '../src/client.js'
import type { NovelEnvelope } from '../src/protocol.js'
import { registerNovelTools } from '../src/tools.js'

describe('registerNovelTools', () => {
  it('registers only the six coarse-grained public tools', () => {
    const { definitions, ctx } = collectingContext()
    registerNovelTools(ctx, fakeClient(), 30_000, 20_000)
    expect(definitions.map(tool => tool.name)).toEqual([
      'novel_project_create',
      'novel_project_status',
      'novel_chapter_run',
      'novel_run_status',
      'novel_run_resume',
      'novel_manuscript_export',
    ])
    expect(definitions.every(tool => tool.timeoutMs === 30_000)).toBe(true)
  })

  it('translates chapter arguments and forwards the Harness cancellation signal', async () => {
    const { definitions, ctx } = collectingContext()
    const client = fakeClient()
    registerNovelTools(ctx, client, 30_000, 20_000)
    const tool = definitions.find(item => item.name === 'novel_chapter_run')
    const controller = new AbortController()
    const result = await tool?.execute({
      project_id: 'prj_1',
      chapter_number: 7,
      contract: { title: 'Threshold', purpose: 'Increase tension.' },
      idempotency_key: 'chapter-7-attempt-1',
    }, { signal: controller.signal } as never)

    expect(client.runChapter).toHaveBeenCalledWith(
      'prj_1',
      7,
      {
        contract: {
          chapter_number: 7,
          title: 'Threshold',
          purpose: 'Increase tension.',
        },
        idempotency_key: 'chapter-7-attempt-1',
      },
      controller.signal,
    )
    expect(result).toMatchObject({ ok: true })
  })

  it('returns network failures as a structured model-readable envelope', async () => {
    const { definitions, ctx } = collectingContext()
    const client = fakeClient()
    vi.mocked(client.projectStatus).mockRejectedValueOnce(new Error('connection refused'))
    registerNovelTools(ctx, client, 30_000, 20_000)
    const tool = definitions.find(item => item.name === 'novel_project_status')
    const result = await tool?.execute({ project_id: 'prj_1' }, { signal: new AbortController().signal } as never)
    expect(result).toMatchObject({
      ok: false,
      error: { code: 'ADAPTER_INTERNAL_ERROR', retryable: false },
    })
  })

  it('bounds the rendered manuscript so exports do not flood model context', () => {
    const { definitions, ctx } = collectingContext()
    registerNovelTools(ctx, fakeClient(), 30_000, 1_000)
    const tool = definitions.find(item => item.name === 'novel_manuscript_export')
    const content = tool?.output.render({}, { content: 'x'.repeat(2_000) })
    expect(content?.[0]).toMatchObject({ type: 'text' })
    expect(content?.[0]?.type === 'text' ? content[0].text : '').toContain('Adapter truncated')
  })
})

function collectingContext(): { definitions: ToolDefinition[]; ctx: Context } {
  const definitions: ToolDefinition[] = []
  const ctx = {
    tools: {
      register(tool: ToolDefinition) {
        definitions.push(tool)
        return () => undefined
      },
    },
  } as unknown as Context
  return { definitions, ctx }
}

function fakeClient(): NovelClient & {
  createProject: ReturnType<typeof vi.fn>
  projectStatus: ReturnType<typeof vi.fn>
  runChapter: ReturnType<typeof vi.fn>
  runStatus: ReturnType<typeof vi.fn>
  resumeRun: ReturnType<typeof vi.fn>
  exportManuscript: ReturnType<typeof vi.fn>
} {
  const response: NovelEnvelope = {
    ok: true,
    request_id: 'req_1',
    protocol_version: '1.0',
    result: {},
    warnings: [],
    error: null,
  }
  return {
    createProject: vi.fn(async () => response),
    projectStatus: vi.fn(async () => response),
    runChapter: vi.fn(async () => response),
    runStatus: vi.fn(async () => response),
    resumeRun: vi.fn(async () => response),
    exportManuscript: vi.fn(async () => response),
  } as unknown as NovelClient & {
    createProject: ReturnType<typeof vi.fn>
    projectStatus: ReturnType<typeof vi.fn>
    runChapter: ReturnType<typeof vi.fn>
    runStatus: ReturnType<typeof vi.fn>
    resumeRun: ReturnType<typeof vi.fn>
    exportManuscript: ReturnType<typeof vi.fn>
  }
}
