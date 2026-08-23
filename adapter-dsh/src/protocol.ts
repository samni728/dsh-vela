/** Lossless JSON value accepted by the Harness tool protocol. */
export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = { [key: string]: JsonValue }

export const ADAPTER_VERSION = '0.1.0'
export const SUPPORTED_PROTOCOL_MAJOR = 1

export const REQUIRED_CAPABILITIES = [
  'project.create',
  'project.status',
  'chapter.run',
  'run.status',
  'run.resume',
  'manuscript.export',
] as const

export interface NovelErrorBody {
  code: string
  message: string
  retryable?: boolean
  details?: JsonValue
}

export type NovelEnvelope<T extends JsonValue = JsonValue> = JsonObject & {
  ok: boolean
  request_id: string
  protocol_version: string
  result: T | null
  warnings: JsonValue[]
  error: (JsonObject & {
    code: string
    message: string
    retryable: boolean
    details: JsonValue | null
  }) | null
}

export interface CapabilityHandshake {
  protocolVersion: string
  coreVersion: string
  capabilities: string[]
}

export function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (Array.isArray(value)) return value.every(isJsonValue)
  return isJsonObject(value) && Object.values(value).every(isJsonValue)
}

export function isNovelEnvelope(value: unknown): value is NovelEnvelope {
  return isJsonObject(value) && typeof value.ok === 'boolean'
}
