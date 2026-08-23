import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
export declare const name = "dsh-novel-plugin";
export declare const inject: string[];
export interface Config {
    endpoint?: string;
    token?: string;
    tokenEnv?: string;
    requestTimeoutMs?: number;
    handshakeTimeoutMs?: number;
    maxRenderChars?: number;
    requireHandshake?: boolean;
}
export declare const Config: Schema<Config>;
export declare function apply(ctx: Context, config?: Config): Promise<void>;
export { NovelClient, routes } from './client.js';
export { NovelAdapterError } from './errors.js';
export { performHandshake } from './handshake.js';
export { REQUIRED_CAPABILITIES } from './protocol.js';
//# sourceMappingURL=index.d.ts.map