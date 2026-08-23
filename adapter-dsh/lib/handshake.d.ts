import type { NovelClient } from './client.js';
import type { CapabilityHandshake } from './protocol.js';
export declare function performHandshake(client: NovelClient, options: {
    timeoutMs: number;
    requiredCapabilities?: readonly string[];
    signal?: AbortSignal;
}): Promise<CapabilityHandshake>;
//# sourceMappingURL=handshake.d.ts.map