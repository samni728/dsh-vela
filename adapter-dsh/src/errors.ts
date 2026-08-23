import { randomUUID } from 'node:crypto'
import type { JsonValue, NovelEnvelope, NovelErrorBody } from './protocol.js'

export class NovelAdapterError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly status?: number
  readonly requestId?: string
  readonly details?: JsonValue

  constructor(
    code: string,
    message: string,
    options: {
      retryable?: boolean | undefined
      status?: number | undefined
      requestId?: string | undefined
      details?: JsonValue | undefined
      cause?: unknown
    } = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'NovelAdapterError'
    this.code = code
    this.retryable = options.retryable ?? false
    if (options.status !== undefined) this.status = options.status
    if (options.requestId !== undefined) this.requestId = options.requestId
    if (options.details !== undefined) this.details = options.details
  }
}

export function errorEnvelope(error: unknown): NovelEnvelope {
  const normalized = normalizeError(error)
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
  }
}

export function normalizeError(error: unknown): NovelAdapterError {
  if (error instanceof NovelAdapterError) return error
  if (error instanceof Error) {
    return new NovelAdapterError('ADAPTER_INTERNAL_ERROR', error.message, { cause: error })
  }
  return new NovelAdapterError('ADAPTER_INTERNAL_ERROR', String(error))
}

export function errorFromBody(body: NovelErrorBody, status?: number, requestId?: string): NovelAdapterError {
  return new NovelAdapterError(body.code, body.message, {
    retryable: body.retryable,
    status,
    requestId,
    details: body.details,
  })
}
