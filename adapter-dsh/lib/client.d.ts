import type { JsonObject, JsonValue, NovelEnvelope } from './protocol.js';
export interface NovelClientOptions {
    endpoint: string;
    token?: string;
    timeoutMs: number;
    fetch?: typeof globalThis.fetch;
}
interface RequestOptions {
    method?: 'GET' | 'POST';
    body?: JsonValue;
    signal?: AbortSignal | undefined;
    timeoutMs?: number | undefined;
}
/**
 * Partial per-project writing policy accepted by the autorun and auto-create
 * endpoints. Omitted fields are never serialized into the request body; the
 * Sidecar then falls back to stored project policy and its own defaults.
 */
export interface NovelPolicyInput {
    score_threshold?: number | undefined;
    max_revisions?: number | undefined;
    target_words?: number | undefined;
    on_chapter_failure?: 'skip_continue' | 'pause' | undefined;
}
/**
 * Drop unset (undefined/null) policy fields so omitted keys never reach the
 * Sidecar, and collapse an effectively empty policy to `undefined` so the
 * whole `policy` key disappears from the body.
 */
export declare function compactPolicy(policy: NovelPolicyInput | undefined): JsonObject | undefined;
/** All Sidecar paths live here so a protocol revision does not leak into tools. */
export declare const routes: {
    readonly health: "/health";
    readonly capabilities: "/api/v1/capabilities";
    readonly projects: "/api/v1/projects";
    readonly project: (projectId: string) => string;
    readonly outline: (projectId: string) => string;
    readonly chapterRun: (projectId: string, chapterNumber: number) => string;
    readonly run: (runId: string) => string;
    readonly runResume: (runId: string) => string;
    readonly autorun: (projectId: string) => string;
    readonly pipeline: (projectId: string) => string;
    readonly projectReport: (projectId: string) => string;
    readonly manuscriptExport: (projectId: string) => string;
    readonly auto: "/api/v1/auto";
};
export declare class NovelClient {
    #private;
    readonly endpoint: string;
    readonly timeoutMs: number;
    constructor(options: NovelClientOptions);
    close(): void;
    health(signal?: AbortSignal, timeoutMs?: number): Promise<NovelEnvelope>;
    capabilities(signal?: AbortSignal, timeoutMs?: number): Promise<NovelEnvelope>;
    createProject(input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    projectStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    generateOutline(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    startAutorun(projectId: string, fromChapter?: number, toChapter?: number, policy?: NovelPolicyInput | undefined, signal?: AbortSignal): Promise<NovelEnvelope>;
    autorunStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    /**
     * Zero-prose management snapshot (`GET /pipeline`): scores, statuses and
     * counters only. The Sidecar contract guarantees no manuscript content.
     */
    pipelineStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    report(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    autoCreate(input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    runChapter(projectId: string, chapterNumber: number, input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    runStatus(runId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    resumeRun(runId: string, forceRedraft: boolean, signal?: AbortSignal): Promise<NovelEnvelope>;
    exportManuscript(projectId: string, format: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    request(path: string, options?: RequestOptions): Promise<NovelEnvelope>;
}
/**
 * Tool-facing subset of the Sidecar API. `registerNovelTools` depends on this
 * shape instead of the concrete client so decorators (the lazy handshake
 * wrapper) can stand in for a NovelClient.
 */
export interface NovelSidecar {
    createProject(input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    projectStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    generateOutline(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    startAutorun(projectId: string, fromChapter: number | undefined, toChapter: number | undefined, policy: NovelPolicyInput | undefined, signal?: AbortSignal): Promise<NovelEnvelope>;
    autorunStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    pipelineStatus(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    report(projectId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    autoCreate(input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    runChapter(projectId: string, chapterNumber: number, input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    runStatus(runId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    resumeRun(runId: string, forceRedraft: boolean, signal?: AbortSignal): Promise<NovelEnvelope>;
    exportManuscript(projectId: string, format: string, signal?: AbortSignal): Promise<NovelEnvelope>;
}
export {};
//# sourceMappingURL=client.d.ts.map