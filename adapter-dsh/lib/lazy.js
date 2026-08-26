import { NovelAdapterError } from './errors.js';
import { performHandshake } from './handshake.js';
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
export function createLazySidecar(client, options) {
    let handshaken = false;
    let pending;
    const ensureHandshake = () => {
        if (handshaken)
            return Promise.resolve();
        // Concurrent first calls share one in-flight handshake.
        pending ??= performHandshake(client, { timeoutMs: options.timeoutMs })
            .then(() => {
            handshaken = true;
        })
            .finally(() => {
            pending = undefined;
        });
        return pending;
    };
    const gated = async (operation) => {
        try {
            await ensureHandshake();
        }
        catch (error) {
            if (!isReachabilityFailure(error))
                throw error;
            throw unavailableError(error, options.endpoint);
        }
        return operation();
    };
    return {
        createProject: (input, signal) => gated(() => client.createProject(input, signal)),
        projectStatus: (projectId, signal) => gated(() => client.projectStatus(projectId, signal)),
        generateOutline: (projectId, signal) => gated(() => client.generateOutline(projectId, signal)),
        startAutorun: (projectId, fromChapter, toChapter, policy, signal) => gated(() => client.startAutorun(projectId, fromChapter, toChapter, policy, signal)),
        autorunStatus: (projectId, signal) => gated(() => client.autorunStatus(projectId, signal)),
        pipelineStatus: (projectId, signal) => gated(() => client.pipelineStatus(projectId, signal)),
        report: (projectId, signal) => gated(() => client.report(projectId, signal)),
        autoCreate: (input, signal) => gated(() => client.autoCreate(input, signal)),
        runChapter: (projectId, chapterNumber, input, signal) => gated(() => client.runChapter(projectId, chapterNumber, input, signal)),
        runStatus: (runId, signal) => gated(() => client.runStatus(runId, signal)),
        resumeRun: (runId, forceRedraft, signal) => gated(() => client.resumeRun(runId, forceRedraft, signal)),
        exportManuscript: (projectId, format, signal) => gated(() => client.exportManuscript(projectId, format, signal)),
    };
}
function isReachabilityFailure(error) {
    return error instanceof NovelAdapterError
        && (error.code === 'SIDECAR_UNAVAILABLE' || error.code === 'SIDECAR_TIMEOUT');
}
function unavailableError(cause, endpoint) {
    return new NovelAdapterError('ADAPTER_SIDECAR_UNAVAILABLE', `DSH Novel Sidecar is not reachable at ${endpoint}. `
        + 'Start it with "uv run dsh-novel serve" (see README section 1), then retry this tool call.', { retryable: true, cause });
}
//# sourceMappingURL=lazy.js.map