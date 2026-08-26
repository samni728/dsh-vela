import { defineTool } from '@deepseek-ai/dsh-tools';
import { compactPolicy } from './client.js';
import { errorEnvelope } from './errors.js';
/**
 * Shared optional writing-policy override for `novel_auto_create` and
 * `novel_autorun_start`. Kept as one constant so both tool schemas stay in
 * sync; unset fields are dropped before the request leaves the adapter.
 */
const policyParameter = {
    type: 'object',
    additionalProperties: false,
    description: 'Optional partial writing-policy override. Omitted fields fall back to the stored project policy, then Sidecar defaults.',
    properties: {
        score_threshold: {
            type: 'number',
            description: 'Review overall-score threshold (0-10). Chapters scoring below it are blocked for revision.',
        },
        max_revisions: {
            type: 'integer',
            description: 'Total draft attempts allowed per chapter (minimum 1) before the failure policy applies.',
        },
        target_words: {
            type: 'integer',
            description: 'Per-chapter target word budget (100-20000).',
        },
        on_chapter_failure: {
            type: 'string',
            enum: ['skip_continue', 'pause'],
            description: 'skip_continue queues the chapter for rework and moves on; pause stops the run at the failing chapter.',
        },
    },
};
export function registerNovelTools(ctx, sidecar, timeoutMs, maxRenderChars) {
    const envelopeOutput = createEnvelopeOutput(maxRenderChars);
    ctx.tools.register(defineTool({
        name: 'novel_project_create',
        description: 'Create a durable local novel project. Use once per book before planning or writing chapters.',
        parameters: {
            title: { type: 'string', required: true, description: 'Novel title.' },
            premise: { type: 'string', description: 'Short story premise or creative brief.' },
            target_chapters: { type: 'integer', description: 'Planned chapter count from 1 to 3000.' },
            hard_rules: {
                type: 'array',
                items: { type: 'string' },
                description: 'Global story constraints that every chapter must preserve.',
            },
            story_spine: {
                type: 'object',
                additionalProperties: true,
                description: 'Optional structured global story spine or blueprint summary.',
            },
            project_id: { type: 'string', description: 'Optional caller-selected local project id.' },
        },
        output: envelopeOutput,
        timeoutMs,
        execute: (args, exec) => safeCall(() => sidecar.createProject(compactObject(args), exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Create novel project: ${args.title}`, kind: 'execute' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_project_status',
        description: 'Read durable progress, chapter state, warnings, and active run information for one novel project.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Project id returned by novel_project_create.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.projectStatus(args.project_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Read novel project ${args.project_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_outline_generate',
        description: 'Generate the whole-book structured outline (story spine plus per-chapter contracts) for one project with the Sidecar outline agent.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.generateOutline(args.project_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Generate outline for ${args.project_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_chapter_run',
        description: 'Start or continue the persisted generate-review-repair-finalize workflow for one chapter. The Sidecar owns the internal loop.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
            chapter_number: { type: 'integer', required: true, description: 'One-based chapter number.' },
            contract: {
                type: 'object',
                additionalProperties: false,
                description: 'Optional explicit chapter contract. Omit to let the Sidecar derive the contract.',
                properties: {
                    title: { type: 'string', required: true, description: 'Chapter title.' },
                    purpose: { type: 'string', required: true, description: 'Narrative purpose of this chapter.' },
                    required_events: { type: 'array', items: { type: 'string' } },
                    required_state_changes: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    forbidden_changes: { type: 'array', items: { type: 'object', additionalProperties: true } },
                    hooks_to_plant: { type: 'array', items: { type: 'string' } },
                    hooks_to_advance: { type: 'array', items: { type: 'string' } },
                    handoff: { type: 'string' },
                    target_words: { type: 'integer' },
                },
            },
            idempotency_key: {
                type: 'string',
                description: 'Optional stable retry key (8-128 characters) for idempotent chapter execution.',
            },
        },
        output: envelopeOutput,
        timeoutMs,
        execute: (args, exec) => safeCall(() => sidecar.runChapter(args.project_id, args.chapter_number, compactObject({
            contract: args.contract === undefined
                ? undefined
                : { ...args.contract, chapter_number: args.chapter_number },
            idempotency_key: args.idempotency_key,
        }), exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Run chapter ${args.chapter_number}`, kind: 'execute' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_run_status',
        description: 'Read the persisted state and latest checkpoint of a chapter workflow run.',
        parameters: {
            run_id: { type: 'string', required: true, description: 'Run id returned by novel_chapter_run or project status.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.runStatus(args.run_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Read novel run ${args.run_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_run_resume',
        description: 'Resume a paused, interrupted, or retryable persisted novel workflow from its last safe checkpoint.',
        parameters: {
            run_id: { type: 'string', required: true, description: 'Persisted run id to resume.' },
            force_redraft: {
                type: 'boolean',
                description: 'Regenerate the draft while resuming. Defaults to true.',
            },
        },
        output: envelopeOutput,
        timeoutMs,
        execute: (args, exec) => safeCall(() => sidecar.resumeRun(args.run_id, args.force_redraft ?? true, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Resume novel run ${args.run_id}`, kind: 'execute' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_manuscript_export',
        description: 'Export the currently committed manuscript. Only finalized chapter versions are included.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Novel project id.' },
            format: {
                type: 'string',
                enum: ['markdown', 'text'],
                description: 'Export format. Defaults to markdown in the adapter.',
            },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.exportManuscript(args.project_id, args.format ?? 'markdown', exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Export novel project ${args.project_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_auto_create',
        description: 'One-shot fully automatic mode: create the project, generate the whole-book outline, and start the chapter-by-chapter autorun long haul in a single call. Use this instead of chaining novel_project_create + novel_outline_generate + novel_autorun_start by hand.',
        parameters: {
            title: { type: 'string', required: true, description: 'Novel title.' },
            premise: { type: 'string', required: true, description: 'Short story premise or creative brief.' },
            target_chapters: { type: 'integer', required: true, description: 'Planned chapter count from 1 to 3000.' },
            target_words: { type: 'integer', description: 'Optional per-chapter target word budget (100-20000).' },
            hard_rules: {
                type: 'array',
                items: { type: 'string' },
                description: 'Global story constraints that every chapter must preserve.',
            },
            policy: policyParameter,
        },
        output: envelopeOutput,
        timeoutMs,
        execute: (args, exec) => safeCall(() => sidecar.autoCreate(compactObject({
            title: args.title,
            premise: args.premise,
            target_chapters: args.target_chapters,
            target_words: args.target_words,
            hard_rules: args.hard_rules,
            policy: compactPolicy(args.policy),
        }), exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Auto-create novel project: ${args.title}`, kind: 'execute' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_autorun_start',
        description: 'Start (or resume) the server-side automatic chapter-by-chapter long run for one project. Already committed chapters are never rewritten; rework-queue chapters are retried first.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
            from_chapter: { type: 'integer', description: 'Optional one-based first chapter to run. Defaults to committed+1.' },
            to_chapter: { type: 'integer', description: 'Optional last chapter to run. Defaults to the project target chapter count.' },
            policy: policyParameter,
        },
        output: envelopeOutput,
        timeoutMs,
        execute: (args, exec) => safeCall(() => sidecar.startAutorun(args.project_id, args.from_chapter, args.to_chapter, args.policy, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Start autorun for ${args.project_id}`, kind: 'execute' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_autorun_status',
        description: 'Read autorun progress (state, current chapter, committed count, scores, last error) for one project. Poll repeatedly until state is completed or failed.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.autorunStatus(args.project_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Read autorun status of ${args.project_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_pipeline_status',
        description: 'Read the zero-prose management snapshot for one project: run state, effective policy, per-chapter scores/statuses/word counts, rework queue, and totals. 仅返回分数与状态，不含正文 — manuscript content is never included.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.pipelineStatus(args.project_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Read pipeline status of ${args.project_id}`, kind: 'read' }),
    }));
    ctx.tools.register(defineTool({
        name: 'novel_report',
        description: 'Read the auto-generated markdown report (project metadata, per-chapter scores, quality events) written when an autorun finishes.',
        parameters: {
            project_id: { type: 'string', required: true, description: 'Target novel project id.' },
        },
        output: envelopeOutput,
        timeoutMs,
        isConcurrencySafe: () => true,
        execute: (args, exec) => safeCall(() => sidecar.report(args.project_id, exec.signal)),
        presentCall: args => ({ card: 'generic', title: `Read novel report of ${args.project_id}`, kind: 'read' }),
    }));
}
async function safeCall(operation) {
    try {
        return await operation();
    }
    catch (error) {
        return errorEnvelope(error);
    }
}
function compactObject(value) {
    return Object.fromEntries(Object.entries(value).filter((entry) => entry[1] !== undefined));
}
function createEnvelopeOutput(maxRenderChars) {
    return {
        schema: { type: 'json' },
        render: (_args, value) => {
            const serialized = JSON.stringify(value, null, 2);
            const text = serialized.length <= maxRenderChars
                ? serialized
                : `${serialized.slice(0, maxRenderChars)}\n\n[Adapter truncated model-facing output at ${maxRenderChars} characters.]`;
            return [{ type: 'text', text }];
        },
    };
}
//# sourceMappingURL=tools.js.map