import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { describe, expect, it, vi } from 'vitest'
import type { NovelClient } from '../src/client.js'
import type { NovelEnvelope } from '../src/protocol.js'
import { registerNovelTools } from '../src/tools.js'

describe('registerNovelTools', () => {
  it('registers only the twelve coarse-grained public tools', () => {
    const { definitions, ctx } = collectingContext()
    registerNovelTools(ctx, fakeClient(), 30_000, 20_000)
    expect(definitions.map(tool => tool.name)).toEqual([
      'novel_project_create',
      'novel_project_status',
      'novel_outline_generate',
      'novel_chapter_run',
      'novel_run_status',
      'novel_run_resume',
      'novel_manuscript_export',
      'novel_auto_create',
      'novel_autorun_start',
      'novel_autorun_status',
      'novel_pipeline_status',
      'novel_report',
    ])
    expect(definitions.every(tool => tool.timeoutMs === 30_000)).toBe(true)
  })

  it('exposes every new autorun tool with the expected concurrency and call kind', () => {
    const { definitions, ctx } = collectingContext()
    registerNovelTools(ctx, fakeClient(), 30_000, 20_000)
    const byName = new Map(definitions.map(tool => [tool.name, tool]))
    expect(byName.get('novel_outline_generate')?.isConcurrencySafe?.({ project_id: 'prj_1' } as never)).toBe(true)
    expect(byName.get('novel_autorun_status')?.isConcurrencySafe?.({ project_id: 'prj_1' } as never)).toBe(true)
    expect(byName.get('novel_pipeline_status')?.isConcurrencySafe?.({ project_id: 'prj_1' } as never)).toBe(true)
    expect(byName.get('novel_report')?.isConcurrencySafe?.({ project_id: 'prj_1' } as never)).toBe(true)
    expect(byName.get('novel_auto_create')?.presentCall?.({
      title: '雾港档案',
      premise: '一名档案员发现城市每晚都会忘记一个人',
      target_chapters: 10,
    })).toMatchObject({ kind: 'execute' })
    expect(byName.get('novel_autorun_start')?.presentCall?.({ project_id: 'prj_1' })).toMatchObject({ kind: 'execute' })
    expect(byName.get('novel_autorun_status')?.presentCall?.({ project_id: 'prj_1' })).toMatchObject({ kind: 'read' })
    expect(byName.get('novel_pipeline_status')?.presentCall?.({ project_id: 'prj_1' })).toMatchObject({ kind: 'read' })
    expect(byName.get('novel_report')?.presentCall?.({ project_id: 'prj_1' })).toMatchObject({ kind: 'read' })
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

  it('routes the 0.4.0 autorun tools to the matching client methods', async () => {
    const { definitions, ctx } = collectingContext()
    const client = fakeClient()
    registerNovelTools(ctx, client, 30_000, 20_000)
    const byName = new Map(definitions.map(tool => [tool.name, tool]))
    const signal = new AbortController().signal
    const exec = { signal } as never

    await byName.get('novel_outline_generate')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.generateOutline).toHaveBeenCalledWith('prj_1', signal)

    await byName.get('novel_auto_create')?.execute({
      title: '雾港档案',
      premise: '一名档案员发现城市每晚都会忘记一个人',
      target_chapters: 10,
      target_words: 4000,
      hard_rules: ['每章必须推进一次调查'],
    }, exec)
    expect(client.autoCreate).toHaveBeenCalledWith({
      title: '雾港档案',
      premise: '一名档案员发现城市每晚都会忘记一个人',
      target_chapters: 10,
      target_words: 4000,
      hard_rules: ['每章必须推进一次调查'],
    }, signal)

    await byName.get('novel_autorun_start')?.execute({ project_id: 'prj_1', from_chapter: 2, to_chapter: 5 }, exec)
    expect(client.startAutorun).toHaveBeenCalledWith('prj_1', 2, 5, undefined, signal)

    await byName.get('novel_autorun_start')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.startAutorun).toHaveBeenLastCalledWith('prj_1', undefined, undefined, undefined, signal)

    await byName.get('novel_autorun_status')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.autorunStatus).toHaveBeenCalledWith('prj_1', signal)

    await byName.get('novel_pipeline_status')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.pipelineStatus).toHaveBeenCalledWith('prj_1', signal)

    await byName.get('novel_report')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.report).toHaveBeenCalledWith('prj_1', signal)

    for (const name of ['novel_outline_generate', 'novel_auto_create', 'novel_autorun_start', 'novel_autorun_status', 'novel_pipeline_status', 'novel_report']) {
      const result = await byName.get(name)?.execute(
        name === 'novel_auto_create'
          ? { title: 't', premise: 'p', target_chapters: 1 }
          : { project_id: 'prj_1' },
        exec,
      )
      expect(result).toMatchObject({ ok: true })
    }
  })

  it('forwards a partial policy on novel_auto_create and drops omitted policy fields', async () => {
    const { definitions, ctx } = collectingContext()
    const client = fakeClient()
    registerNovelTools(ctx, client, 30_000, 20_000)
    const tool = definitions.find(item => item.name === 'novel_auto_create')
    const signal = new AbortController().signal
    const exec = { signal } as never

    await tool?.execute({
      title: '雾港档案',
      premise: '一名档案员发现城市每晚都会忘记一个人',
      target_chapters: 10,
      policy: { score_threshold: 8.5, max_revisions: 4 },
    }, exec)

    // Only the provided policy keys survive; omitted fields are not sent.
    expect(client.autoCreate).toHaveBeenCalledWith({
      title: '雾港档案',
      premise: '一名档案员发现城市每晚都会忘记一个人',
      target_chapters: 10,
      policy: { score_threshold: 8.5, max_revisions: 4 },
    }, signal)
    const body = vi.mocked(client.autoCreate).mock.calls[0]![0]
    expect(Object.keys(body.policy as object).sort()).toEqual(['max_revisions', 'score_threshold'])

    // No policy at all -> the key never reaches the client.
    await tool?.execute({ title: 't', premise: 'p', target_chapters: 3 }, exec)
    expect(vi.mocked(client.autoCreate).mock.calls[1]![0]).not.toHaveProperty('policy')
  })

  it('forwards the autorun policy verbatim and omits it when unset', async () => {
    const { definitions, ctx } = collectingContext()
    const client = fakeClient()
    registerNovelTools(ctx, client, 30_000, 20_000)
    const byName = new Map(definitions.map(tool => [tool.name, tool]))
    const signal = new AbortController().signal
    const exec = { signal } as never

    await byName.get('novel_autorun_start')?.execute({
      project_id: 'prj_1',
      policy: { target_words: 3500, on_chapter_failure: 'pause' },
    }, exec)
    expect(client.startAutorun).toHaveBeenLastCalledWith(
      'prj_1',
      undefined,
      undefined,
      { target_words: 3500, on_chapter_failure: 'pause' },
      signal,
    )

    await byName.get('novel_autorun_start')?.execute({ project_id: 'prj_1' }, exec)
    expect(client.startAutorun).toHaveBeenLastCalledWith('prj_1', undefined, undefined, undefined, signal)
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
  generateOutline: ReturnType<typeof vi.fn>
  startAutorun: ReturnType<typeof vi.fn>
  autorunStatus: ReturnType<typeof vi.fn>
  pipelineStatus: ReturnType<typeof vi.fn>
  report: ReturnType<typeof vi.fn>
  autoCreate: ReturnType<typeof vi.fn>
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
    generateOutline: vi.fn(async () => response),
    startAutorun: vi.fn(async () => response),
    autorunStatus: vi.fn(async () => response),
    pipelineStatus: vi.fn(async () => response),
    report: vi.fn(async () => response),
    autoCreate: vi.fn(async () => response),
    runChapter: vi.fn(async () => response),
    runStatus: vi.fn(async () => response),
    resumeRun: vi.fn(async () => response),
    exportManuscript: vi.fn(async () => response),
  } as unknown as NovelClient & {
    createProject: ReturnType<typeof vi.fn>
    projectStatus: ReturnType<typeof vi.fn>
    generateOutline: ReturnType<typeof vi.fn>
    startAutorun: ReturnType<typeof vi.fn>
    autorunStatus: ReturnType<typeof vi.fn>
    pipelineStatus: ReturnType<typeof vi.fn>
    report: ReturnType<typeof vi.fn>
    autoCreate: ReturnType<typeof vi.fn>
    runChapter: ReturnType<typeof vi.fn>
    runStatus: ReturnType<typeof vi.fn>
    resumeRun: ReturnType<typeof vi.fn>
    exportManuscript: ReturnType<typeof vi.fn>
  }
}
