import type { PreviewSource } from '../../services/api';

export function resolveFilterEventSource(
    sources: PreviewSource[],
    datasetRef: string
): PreviewSource | undefined {
    const exact = sources.find((source) => source.id === datasetRef);
    if (exact || !datasetRef.endsWith('::temp')) return exact;

    const fileId = datasetRef.slice(0, -'::temp'.length);
    return sources.find(
        (source) =>
            source.kind === 'sql' &&
            source.file_id === fileId &&
            source.role === 'working' &&
            source.is_active
    );
}
