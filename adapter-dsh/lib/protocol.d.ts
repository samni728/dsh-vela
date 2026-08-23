/** Lossless JSON value accepted by the Harness tool protocol. */
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | {
    [key: string]: JsonValue;
};
export type JsonObject = {
    [key: string]: JsonValue;
};
export declare const ADAPTER_VERSION = "0.1.0";
export declare const SUPPORTED_PROTOCOL_MAJOR = 1;
export declare const REQUIRED_CAPABILITIES: readonly ["project.create", "project.status", "chapter.run", "run.status", "run.resume", "manuscript.export"];
export interface NovelErrorBody {
    code: string;
    message: string;
    retryable?: boolean;
    details?: JsonValue;
}
export type NovelEnvelope<T extends JsonValue = JsonValue> = JsonObject & {
    ok: boolean;
    request_id: string;
    protocol_version: string;
    result: T | null;
    warnings: JsonValue[];
    error: (JsonObject & {
        code: string;
        message: string;
        retryable: boolean;
        details: JsonValue | null;
    }) | null;
};
export interface CapabilityHandshake {
    protocolVersion: string;
    coreVersion: string;
    capabilities: string[];
}
export declare function isJsonObject(value: unknown): value is JsonObject;
export declare function isJsonValue(value: unknown): value is JsonValue;
export declare function isNovelEnvelope(value: unknown): value is NovelEnvelope;
//# sourceMappingURL=protocol.d.ts.map