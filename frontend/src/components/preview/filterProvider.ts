import type { PreviewSource } from '../../services/api';
import {
    FilterPageResponse,
    FilterSummary,
    getFilterPage,
    sourceApplyParams,
} from '../../services/filters';
import {
    CellMeta,
    FilterSeverity,
    PreviewDataProvider,
    PreviewFetchResult,
    PreviewQuery,
    RowMeta,
} from './types';

/**
 * Provider backed by GET /api/filters/sessions/{id}/apply — serves the source
 * through a saved filter, feeding highlights (cellMeta severities) and group
 * banding (rowMeta) into the same SpreadsheetGrid as plain previews. All
 * toggles are encoded in `key` so usePreviewData resets paging when they
 * change; sorting is disabled while the filter groups rows (grouping owns
 * the order server-side).
 */
export function makeFilterPreviewProvider(
    sessionId: string,
    source: PreviewSource,
    filterId: string,
    matchesOnly: boolean,
    enabledRules: string[] | null,
    groupingActive: boolean,
    onPageInfo?: (summary: FilterSummary) => void
): PreviewDataProvider {
    return {
        key: `filter:${sessionId}:${source.kind}:${source.id}:${filterId}:${matchesOnly}:${
            enabledRules ? enabledRules.join(',') : '*'
        }`,
        capabilities: { sort: !groupingActive, filter: true, search: true, stats: false },
        async fetchPage(
            query: PreviewQuery,
            opts?: { includeStats?: boolean; signal?: AbortSignal }
        ): Promise<PreviewFetchResult> {
            const pageSize = Math.max(1, query.limit);
            const response: FilterPageResponse = await getFilterPage(
                sessionId,
                {
                    filter_id: filterId,
                    ...sourceApplyParams(source),
                    page: Math.floor(query.offset / pageSize) + 1,
                    page_size: pageSize,
                    matches_only: matchesOnly,
                    rules: enabledRules ?? undefined,
                    sort_by: query.sortBy,
                    sort_dir: query.sortDir,
                    search: query.search || undefined,
                    filters: query.filters,
                },
                opts?.signal
            );

            onPageInfo?.(response.summary);

            return {
                columns: response.columns.map((c) => ({
                    name: c.name,
                    dtype: c.dtype,
                    kind: c.kind as 'number' | 'string' | 'date' | 'bool' | 'other',
                    note: c.origin === 'annotation' ? 'filter annotation' : undefined,
                })),
                rows: response.rows,
                cellMeta: response.cell_meta.map((row) =>
                    row.map(
                        (m): CellMeta | null =>
                            m && {
                                severity: m.k as FilterSeverity,
                                label: m.l,
                                tooltip: m.t,
                            }
                    )
                ),
                rowMeta: response.row_meta.map(
                    (m): RowMeta | null =>
                        m && {
                            group: m.g ?? null,
                            severity: (m.s as FilterSeverity) ?? null,
                            label: m.l ?? null,
                            tooltip: m.t ?? null,
                            matched: m.m,
                        }
                ),
                totalRows: response.total_rows,
                filteredRows: response.filtered_rows,
                approx: response.summary.approx,
            };
        },
    };
}
