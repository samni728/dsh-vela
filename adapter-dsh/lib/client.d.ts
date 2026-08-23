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
/** All Sidecar paths live here so a protocol revision does not leak into tools. */
export declare const routes: {
    readonly health: "/health";
    readonly capabilities: "/api/v1/capabilities";
    readonly projects: "/api/v1/projects";
    readonly project: (projectId: string) => string;
    readonly chapterRun: (projectId: string, chapterNumber: number) => string;
    readonly run: (runId: string) => string;
    readonly runResume: (runId: string) => string;
    readonly manuscriptExport: (projectId: string) => string;
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
    runChapter(projectId: string, chapterNumber: number, input: JsonObject, signal?: AbortSignal): Promise<NovelEnvelope>;
    runStatus(runId: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    resumeRun(runId: string, forceRedraft: boolean, signal?: AbortSignal): Promise<NovelEnvelope>;
    exportManuscript(projectId: string, format: string, signal?: AbortSignal): Promise<NovelEnvelope>;
    request(path: string, options?: RequestOptions): Promise<NovelEnvelope>;
}
export {};
//# sourceMappingURL=client.d.ts.map