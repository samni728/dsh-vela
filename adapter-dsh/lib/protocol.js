export const ADAPTER_VERSION = '0.1.0';
export const SUPPORTED_PROTOCOL_MAJOR = 1;
export const REQUIRED_CAPABILITIES = [
    'project.create',
    'project.status',
    'chapter.run',
    'run.status',
    'run.resume',
    'manuscript.export',
];
export function isJsonObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function isJsonValue(value) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean')
        return true;
    if (typeof value === 'number')
        return Number.isFinite(value);
    if (Array.isArray(value))
        return value.every(isJsonValue);
    return isJsonObject(value) && Object.values(value).every(isJsonValue);
}
export function isNovelEnvelope(value) {
    return isJsonObject(value) && typeof value.ok === 'boolean';
}
//# sourceMappingURL=protocol.js.map