import type { NovelClient } from './client.js'
import { NovelAdapterError } from './errors.js'
import {
  REQUIRED_CAPABILITIES,
  SUPPORTED_PROTOCOL_MAJOR,
  isJsonObject,
} from './protocol.js'
import type { CapabilityHandshake, JsonValue, NovelEnvelope } from './protocol.js'

export async function performHandshake(
  client: NovelClient,
  options: {
    timeoutMs: number
    requiredCapabilities?: readonly string[]
    signal?: AbortSignal
  },
): Promise<CapabilityHandshake> {
  const health = await client.health(options.signal, options.timeoutMs)
  requireSuccessfulEnvelope(health, 'health')
  const capabilitiesEnvelope = await client.capabilities(options.signal, options.timeoutMs)
  const capabilityPayload = requireSuccessfulEnvelope(capabilitiesEnvelope, 'capabilities')
  if (!isJsonObject(capabilityPayload)) {
    throw new NovelAdapterError('PROTOCOL_INCOMPATIBLE', 'Capability response result must be an object.')
  }

  const protocolVersion = stringField(capabilityPayload, 'protocol_version')
    ?? capabilitiesEnvelope.protocol_version
  const coreVersion = stringField(capabilityPayload, 'core_version') ?? 'unknown'
  const capabilities = stringArrayField(capabilityPayload, 'capabilities')
  const protocolMajor = Number.parseInt(protocolVersion.split('.')[0] ?? '', 10)
  if (!Number.isInteger(protocolMajor) || protocolMajor !== SUPPORTED_PROTOCOL_MAJOR) {
    throw new NovelAdapterError(
      'PROTOCOL_INCOMPATIBLE',
      `Sidecar protocol ${protocolVersion} is incompatible; adapter requires major ${SUPPORTED_PROTOCOL_MAJOR}.`,
    )
  }

  const required = options.requiredCapabilities ?? REQUIRED_CAPABILITIES
  const available = new Set(capabilities)
  const missing = required.filter(capability => !available.has(capability))
  if (missing.length > 0) {
    throw new NovelAdapterError('PROTOCOL_INCOMPATIBLE', `Sidecar lacks required capabilities: ${missing.join(', ')}.`, {
      details: { missing, available: capabilities },
    })
  }
  return { protocolVersion, coreVersion, capabilities }
}

function requireSuccessfulEnvelope(envelope: NovelEnvelope, operation: string): JsonValue {
  if (!envelope.ok) {
    const code = envelope.error?.code ?? 'SIDECAR_HANDSHAKE_FAILED'
    const message = envelope.error?.message ?? `Sidecar ${operation} check failed.`
    throw new NovelAdapterError(code, message, {
      requestId: envelope.request_id,
      retryable: envelope.error?.retryable ?? false,
      details: envelope.error?.details ?? null,
    })
  }
  return envelope.result
}

function stringField(value: { [key: string]: JsonValue }, name: string): string | undefined {
  const field = value[name]
  return typeof field === 'string' ? field : undefined
}

function stringArrayField(value: { [key: string]: JsonValue }, name: string): string[] {
  const field = value[name]
  if (!Array.isArray(field) || !field.every(item => typeof item === 'string')) {
    throw new NovelAdapterError('PROTOCOL_INCOMPATIBLE', `Capability response ${name} must be a string array.`)
  }
  return field
}
