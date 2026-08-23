import { randomUUID } from 'node:crypto';
export class NovelAdapterError extends Error {
    code;
    retryable;
    status;
    requestId;
    details;
    constructor(code, message, options = {}) {
        super(message, options.cause === undefined ? undefined : { cause: options.cause });
        this.name = 'NovelAdapterError';
        this.code = code;
        this.retryable = options.retryable ?? false;
        if (options.status !== undefined)
            this.status = options.status;
        if (options.requestId !== undefined)
            this.requestId = options.requestId;
        if (options.details !== undefined)
            this.details = options.details;
    }
}
export function errorEnvelope(error) {
    const normalized = normalizeError(error);
    return {
        ok: false,
        request_id: normalized.requestId ?? `adapter_${randomUUID()}`,
        protocol_version: '1.0',
        result: null,
        warnings: [],
        error: {
            code: normalized.code,
            message: normalized.message,
            retryable: normalized.retryable,
            details: normalized.details ?? null,
        },
    };
}
export function normalizeError(error) {
    if (error instanceof NovelAdapterError)
        return error;
    if (error instanceof Error) {
        return new NovelAdapterError('ADAPTER_INTERNAL_ERROR', error.message, { cause: error });
    }
    return new NovelAdapterError('ADAPTER_INTERNAL_ERROR', String(error));
}
export function errorFromBody(body, status, requestId) {
    return new NovelAdapterError(body.code, body.message, {
        retryable: body.retryable,
        status,
        requestId,
        details: body.details,
    });
}
//# sourceMappingURL=errors.js.map