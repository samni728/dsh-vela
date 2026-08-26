import Schema from '@deepseek-ai/schemastery';
import { NovelClient } from './client.js';
import { NovelAdapterError } from './errors.js';
import { performHandshake } from './handshake.js';
import { createLazySidecar } from './lazy.js';
import { registerNovelTools } from './tools.js';
export const name = 'dsh-novel-plugin';
export const inject = ['tools'];
export const Config = Schema.object({
    endpoint: Schema.string().default('http://127.0.0.1:17861'),
    token: Schema.string(),
    tokenEnv: Schema.string().default('DSH_NOVEL_TOKEN'),
    requestTimeoutMs: Schema.number().min(1).default(30_000),
    handshakeTimeoutMs: Schema.number().min(1).default(5_000),
    maxRenderChars: Schema.number().min(1_000).default(20_000),
    /** @deprecated Only `false` still means something (`handshakeMode: 'off'`). */
    requireHandshake: Schema.boolean(),
    // No schema default on purpose: resolveConfig owns the effective default so
    // the legacy `requireHandshake: false` mapping keeps working when this field
    // is absent.
    handshakeMode: Schema.union(['lazy', 'boot', 'off']),
});
export async function apply(ctx, config = {}) {
    const resolved = resolveConfig(config);
    const client = new NovelClient({
        endpoint: resolved.endpoint,
        timeoutMs: resolved.requestTimeoutMs,
        ...(resolved.token === undefined ? {} : { token: resolved.token }),
    });
    ctx.effect(() => () => client.close());
    try {
        if (resolved.handshakeMode === 'boot') {
            await performHandshake(client, { timeoutMs: resolved.handshakeTimeoutMs });
        }
        const sidecar = resolved.handshakeMode === 'lazy'
            ? createLazySidecar(client, { endpoint: resolved.endpoint, timeoutMs: resolved.handshakeTimeoutMs })
            : client;
        registerNovelTools(ctx, sidecar, resolved.requestTimeoutMs, resolved.maxRenderChars);
    }
    catch (error) {
        client.close();
        throw error;
    }
}
function resolveConfig(config) {
    const endpoint = config.endpoint ?? 'http://127.0.0.1:17861';
    const requestTimeoutMs = positiveInteger('requestTimeoutMs', config.requestTimeoutMs ?? 30_000);
    const handshakeTimeoutMs = positiveInteger('handshakeTimeoutMs', config.handshakeTimeoutMs ?? 5_000);
    const maxRenderChars = positiveInteger('maxRenderChars', config.maxRenderChars ?? 20_000);
    if (maxRenderChars < 1_000)
        throw new NovelAdapterError('CONFIG_INVALID', 'maxRenderChars must be at least 1000.');
    if (config.token !== undefined && config.tokenEnv !== undefined && process.env[config.tokenEnv] !== undefined) {
        throw new NovelAdapterError('CONFIG_INVALID', 'Configure either token or tokenEnv, not both.');
    }
    const token = config.token ?? (config.tokenEnv === undefined ? undefined : process.env[config.tokenEnv]);
    return {
        endpoint,
        requestTimeoutMs,
        handshakeTimeoutMs,
        maxRenderChars,
        handshakeMode: resolveHandshakeMode(config),
        ...(token === undefined || token.length === 0 ? {} : { token }),
    };
}
/** `handshakeMode` wins; the deprecated `requireHandshake: false` maps to `'off'`. */
function resolveHandshakeMode(config) {
    if (config.handshakeMode !== undefined)
        return config.handshakeMode;
    return config.requireHandshake === false ? 'off' : 'lazy';
}
function positiveInteger(name, value) {
    if (!Number.isSafeInteger(value) || value < 1) {
        throw new NovelAdapterError('CONFIG_INVALID', `${name} must be a positive safe integer.`);
    }
    return value;
}
export { NovelClient, compactPolicy, routes } from './client.js';
export { NovelAdapterError } from './errors.js';
export { performHandshake } from './handshake.js';
export { createLazySidecar } from './lazy.js';
export { REQUIRED_CAPABILITIES } from './protocol.js';
//# sourceMappingURL=index.js.map