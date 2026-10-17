import {
    getPreviewDiff,
    PreviewDiffCellMeta,
    PreviewDiffColumn,
    PreviewDiffSummary,
    PreviewSource,
} from '../../services/api';
import { CellMeta, DiffKind, PreviewDataProvider, PreviewFetchResult, PreviewQuery } from './types';

function toCellMeta(meta: PreviewDiffCellMeta | null): CellMeta | null {
    if (!meta) return null;
    let tooltip: string | undefined;
    if (meta.k === 'removed' && meta.o === undefined) {
        tooltip = 'row not present in target';
    } else if (meta.k !== 'added') {
        tooltip = meta.o === null || meta.o === undefined ? 'was: empty' : `was: ${meta.o}`;
    }
    const label = meta.l ?? (meta.k === 'changed' ? 'changed' : undefined);
    return { diff: meta.k, label, tooltip };
}

/**
 * Provider backed by GET /api/preview/diff — feeds the target spreadsheet
 * (plus red ghost rows for removed records) with per-cell change annotations
 * into the same SpreadsheetGrid as plain previews. Filters/search/sort run
 * server-side over the diffed frame; `changeKind` narrows rows to one change
 * type (driven by the summary chips / legend).
 */
export function makeDiffPreviewProvider(
    sessionId: string,
    base: PreviewSource,
    target: PreviewSource,
    changesOnly: boolean,
    changeKind: DiffKind | null,
    onPageInfo?: (summary: PreviewDiffSummary, columns: PreviewDiffColumn[]) => void
): PreviewDataProvider {
    return {
        key: `diff:${sessionId}:${base.kind}:${base.id}->${target.kind}:${target.id}:${changesOnly}:${changeKind ?? ''}`,
        capabilities: { sort: true, filter: true, search: true, stats: false },
        async fetchPage(
            query: PreviewQuery,
            opts?: { includeStats?: boolean; signal?: AbortSignal }
        ): Promise<PreviewFetchResult> {
            const pageSize = Math.max(1, query.limit);
            const response = await getPreviewDiff(
                sessionId,
                {
                    base_source: base.kind,
                    base_path: base.kind === 'file' ? base.id : undefined,
                    base_file_id: base.kind === 'dataset' ? base.id : undefined,
                    base_source_id: base.kind === 'sql' ? base.id : undefined,
                    target_source: target.kind,
                    target_path: target.kind === 'file' ? target.id : undefined,
                    target_file_id: target.kind === 'dataset' ? target.id : undefined,
                    target_source_id: target.kind === 'sql' ? target.id : undefined,
                    page: Math.floor(query.offset / pageSize) + 1,
                    page_size: pageSize,
                    changes_only: changesOnly,
                    change_kind: changeKind ?? undefined,
                    sort_by: query.sortBy,
                    sort_dir: query.sortDir,
                    search: query.search || undefined,
                    filters: query.filters,
                },
                opts?.signal
            );

            onPageInfo?.(response.summary, response.columns);

            return {
                columns: response.columns.map((c) => ({
                    name: c.name,
                    dtype: c.dtype,
                    kind: c.kind,
                    status: c.status,
                    note: c.note,
                })),
                rows: response.rows,
                cellMeta: response.cell_meta.map((row) => row.map(toCellMeta)),
                totalRows: response.total_rows,
                filteredRows: response.filtered_rows,
            };
        },
    };
}
