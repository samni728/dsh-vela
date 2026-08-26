import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "dsh-novel-plugin";
export declare const inject: string[];
/**
 * When the adapter performs the Sidecar capability handshake:
 *
 * - `lazy` (default): mounting never touches the network, so a missing Sidecar
 *   can never fail the Harness profile bootstrap. The first tool call performs
 *   the handshake once (bounded by `handshakeTimeoutMs`) and remembers success;
 *   a failure returns an `ADAPTER_SIDECAR_UNAVAILABLE` envelope for that call
 *   only and is retried on the next call.
 * - `boot`: strict legacy behaviour — shake hands while mounting and throw on
 *   failure.
 * - `off`: never handshake.
 */
export type HandshakeMode = 'lazy' | 'boot' | 'off';
export interface Config {
    endpoint?: string;
    token?: string;
    tokenEnv?: string;
    requestTimeoutMs?: number;
    handshakeTimeoutMs?: number;
    maxRenderChars?: number;
    /** @deprecated Use `handshakeMode` instead. Only an explicit `false` is honored (mapped to `'off'`) and only while `handshakeMode` is unset. */
    requireHandshake?: boolean;
    handshakeMode?: HandshakeMode;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config?: Config): Promise<void>;
export { NovelClient, compactPolicy, routes } from './client.js';
export type { NovelPolicyInput, NovelSidecar } from './client.js';
export { NovelAdapterError } from './errors.js';
export { performHandshake } from './handshake.js';
export { createLazySidecar } from './lazy.js';
export { REQUIRED_CAPABILITIES } from './protocol.js';
//# sourceMappingURL=index.d.ts.map