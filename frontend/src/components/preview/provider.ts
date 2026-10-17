import { getPreviewPage, PreviewSource } from '../../services/api';
import { PreviewDataProvider, PreviewFetchResult, PreviewQuery } from './types';

/**
 * Provider backed by GET /api/preview/page — maps the snake_case API shapes
 * onto the camelCase provider contract the grid consumes.
 */
export function makeApiPreviewProvider(
    sessionId: string,
    source: PreviewSource
): PreviewDataProvider {
    return {
        key: `${sessionId}:${source.kind}:${source.id}`,
        capabilities: source.supports_stats === false ? { stats: false } : undefined,
        async fetchPage(
            query: PreviewQuery,
            opts?: { includeStats?: boolean; signal?: AbortSignal }
        ): Promise<PreviewFetchResult> {
            const pageSize = Math.max(1, query.limit);
            const response = await getPreviewPage(
                sessionId,
                {
                    source: source.kind,
                    path: source.kind === 'file' ? source.id : undefined,
                    file_id: source.kind === 'dataset' ? source.id : undefined,
                    source_id: source.kind === 'sql' ? source.id : undefined,
                    page: Math.floor(query.offset / pageSize) + 1,
                    page_size: pageSize,
                    sort_by: query.sortBy,
                    sort_dir: query.sortDir,
                    search: query.search || undefined,
                    filters: query.filters,
                    include_stats: opts?.includeStats || undefined,
                },
                opts?.signal
            );

            if (response.kind !== 'table') {
                throw new Error('This source is not tabular.');
            }

            return {
                columns: response.columns,
                rows: response.rows,
                totalRows: response.total_rows,
                filteredRows: response.filtered_rows,
                truncated: response.truncated,
                approx: response.approx,
                stats: response.stats ?? undefined,
            };
        },
    };
}
