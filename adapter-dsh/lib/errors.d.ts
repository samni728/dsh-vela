import type { JsonValue, NovelEnvelope, NovelErrorBody } from './protocol.js';
export declare class NovelAdapterError extends Error {
    readonly code: string;
    readonly retryable: boolean;
    readonly status?: number;
    readonly requestId?: string;
    readonly details?: JsonValue;
    constructor(code: string, message: string, options?: {
        retryable?: boolean | undefined;
        status?: number | undefined;
        requestId?: string | undefined;
        details?: JsonValue | undefined;
        cause?: unknown;
    });
}
export declare function errorEnvelope(error: unknown): NovelEnvelope;
export declare function normalizeError(error: unknown): NovelAdapterError;
export declare function errorFromBody(body: NovelErrorBody, status?: number, requestId?: string): NovelAdapterError;
//# sourceMappingURL=errors.d.ts.map