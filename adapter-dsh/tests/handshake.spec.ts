import { describe, expect, it } from 'vitest'
import { NovelClient } from '../src/client.js'
import { performHandshake } from '../src/handshake.js'
import { REQUIRED_CAPABILITIES } from '../src/protocol.js'

describe('performHandshake', () => {
  it('accepts a compatible Sidecar with every required capability', async () => {
    const client = sequenceClient([
      { status: 'ok' },
      {
        protocol_version: '1.4.2',
        core_version: '0.1.0',
        capabilities: [...REQUIRED_CAPABILITIES, 'future.optional'],
      },
    ])
    await expect(performHandshake(client, { timeoutMs: 1_000 })).resolves.toEqual({
      protocolVersion: '1.4.2',
      coreVersion: '0.1.0',
      capabilities: [...REQUIRED_CAPABILITIES, 'future.optional'],
    })
  })

  it('fails loudly when a required capability is missing', async () => {
    const client = sequenceClient([
      { status: 'ok' },
      { protocol_version: '1.0', core_version: '0.1.0', capabilities: ['project.status'] },
    ])
    await expect(performHandshake(client, { timeoutMs: 1_000 })).rejects.toMatchObject({
      code: 'PROTOCOL_INCOMPATIBLE',
    })
  })

  it('rejects a different protocol major', async () => {
    const client = sequenceClient([
      { status: 'ok' },
      { protocol_version: '2.0', core_version: '2.0.0', capabilities: [...REQUIRED_CAPABILITIES] },
    ])
    await expect(performHandshake(client, { timeoutMs: 1_000 })).rejects.toThrow('incompatible')
  })
})

function sequenceClient(values: unknown[]): NovelClient {
  let index = 0
  const fetchImpl = async (): Promise<Response> => {
    const value = values[index]
    index += 1
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { 'content-type': 'application/json', 'x-protocol-version': '1.0' },
    })
  }
  return new NovelClient({
    endpoint: 'http://localhost:17861',
    timeoutMs: 1_000,
    fetch: fetchImpl as typeof fetch,
  })
}
