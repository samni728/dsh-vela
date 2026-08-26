import type { NovelClient, NovelSidecar } from './client.js';
export interface LazySidecarOptions {
    endpoint: string;
    timeoutMs: number;
}
/**
 * Wrap a client so the capability handshake runs once before the first tool
 * call instead of at plugin mount. Mounting therefore never touches the
 * network and cannot fail because the Sidecar is down.
 *
 * A successful handshake is remembered for the lifetime of the plugin; a
 * failed one is never cached, so the next tool call retries it. Reachability
 * failures surface as `ADAPTER_SIDECAR_UNAVAILABLE` envelopes with actionable
 * guidance; other handshake failures (for example `PROTOCOL_INCOMPATIBLE`)
 * keep their own code so misconfiguration stays diagnosable.
 */
export declare function createLazySidecar(client: NovelClient, options: LazySidecarOptions): NovelSidecar;
//# sourceMappingURL=lazy.d.ts.map