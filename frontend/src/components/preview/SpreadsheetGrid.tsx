import { Loader2, Search } from 'lucide-react';
import { CellMeta, FilterSeverity, PreviewDataProvider } from './types';
import { usePreviewData } from './usePreviewData';
import { formatCell } from './cellFormat';
import ColumnHeader from './ColumnHeader';
import PaginationFooter from './PaginationFooter';

const PAGE_SIZE = 100;
// Height of the column-header row (h-9); the filter row sticks just below it.
const HEADER_HEIGHT_PX = 36;

export const DIFF_TINT: Record<NonNullable<CellMeta['diff']>, string> = {
    added: 'color-mix(in srgb, var(--c-success) 16%, transparent)',
    removed: 'color-mix(in srgb, var(--c-danger) 16%, transparent)',
    changed: 'color-mix(in srgb, var(--c-warning) 18%, transparent)',
    imputed: 'color-mix(in srgb, #38bdf8 18%, transparent)',
    transformed: 'color-mix(in srgb, #8b5cf6 18%, transparent)',
};

export const DIFF_LABEL_COLOR: Record<NonNullable<CellMeta['diff']>, string> = {
    added: 'var(--c-success)',
    removed: 'var(--c-danger)',
    changed: 'var(--c-warning)',
    imputed: '#38bdf8',
    transformed: '#8b5cf6',
};

// Filter-highlight severities: same tint recipe as diffs so both views read
// as one system and stay theme-aware.
export const SEVERITY_TINT: Record<FilterSeverity, string> = {
    danger: 'color-mix(in srgb, var(--c-danger) 16%, transparent)',
    warning: 'color-mix(in srgb, var(--c-warning) 18%, transparent)',
    accent: 'color-mix(in srgb, #8b5cf6 18%, transparent)',
    info: 'color-mix(in srgb, #38bdf8 18%, transparent)',
    success: 'color-mix(in srgb, var(--c-success) 16%, transparent)',
};

export const SEVERITY_COLOR: Record<FilterSeverity, string> = {
    danger: 'var(--c-danger)',
    warning: 'var(--c-warning)',
    accent: '#8b5cf6',
    info: '#38bdf8',
    success: 'var(--c-success)',
};

// Group banding: alternate subtle signal tints so paired rows read as blocks.
const GROUP_BAND = [
    'color-mix(in srgb, var(--c-signal) 9%, transparent)',
    'color-mix(in srgb, var(--c-signal) 3%, transparent)',
];

interface SpreadsheetGridProps {
    provider: PreviewDataProvider;
}

export default function SpreadsheetGrid({ provider }: SpreadsheetGridProps) {
    const grid = usePreviewData(provider, PAGE_SIZE);
    const { data } = grid;
    const caps = { sort: true, filter: true, search: true, stats: true, ...provider.capabilities };

    if (grid.error && !data) {
        return (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-sm">
                <span style={{ color: 'var(--c-danger)' }}>{grid.error}</span>
                <button
                    type="button"
                    onClick={grid.retry}
                    className="px-3 py-1.5 rounded-md border text-xs transition-colors hover:bg-white/5"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                >
                    Retry
                </button>
            </div>
        );
    }

    return (
        <div
            className="flex-1 flex flex-col min-h-0 rounded-lg border overflow-hidden"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            {/* Toolbar */}
            {(caps.search || grid.error) && (
                <div
                    className="flex items-center gap-2 px-3 py-2 border-b"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    {caps.search && (
                        <div className="relative flex-1 max-w-xs">
                            <Search
                                className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2"
                                style={{ color: 'var(--c-mist-fg)' }}
                            />
                            <input
                                type="text"
                                value={grid.search}
                                onChange={(e) => grid.setSearch(e.target.value)}
                                placeholder="Search all columns…"
                                className="w-full pl-8 pr-2.5 py-1.5 rounded-md border text-xs focus:outline-none"
                                style={{
                                    borderColor: 'var(--c-glass-border)',
                                    background: 'var(--c-glass)',
                                    color: 'var(--c-ink)',
                                }}
                            />
                        </div>
                    )}
                    {grid.error && (
                        <span className="text-[11px]" style={{ color: 'var(--c-danger)' }}>
                            {grid.error}
                        </span>
                    )}
                </div>
            )}

            {/* Grid */}
            <div className="relative flex-1 min-h-0">
                {!data ? (
                    <SkeletonRows />
                ) : (
                    <div
                        className={`h-full overflow-auto transition-opacity ${
                            grid.loading ? 'opacity-50' : ''
                        }`}
                    >
                        <table className="min-w-full text-xs border-collapse">
                            <thead>
                                <tr>
                                    {data.columns.map((col) => (
                                        <ColumnHeader
                                            key={col.name}
                                            column={col}
                                            sortDir={grid.sortBy === col.name ? grid.sortDir : null}
                                            stats={grid.stats?.[col.name]}
                                            onToggleSort={() => grid.toggleSort(col.name)}
                                            sortable={caps.sort}
                                            statsEnabled={caps.stats}
                                        />
                                    ))}
                                </tr>
                                {caps.filter && (
                                    <tr>
                                        {data.columns.map((col) => (
                                            <th
                                                key={col.name}
                                                className="px-2 py-1 border-b sticky z-10 font-normal"
                                                style={{
                                                    top: HEADER_HEIGHT_PX,
                                                    borderColor: 'var(--c-glass-border)',
                                                    background: 'var(--c-slate)',
                                                }}
                                            >
                                                <input
                                                    type="text"
                                                    value={grid.filters[col.name] ?? ''}
                                                    onChange={(e) =>
                                                        grid.setFilter(col.name, e.target.value)
                                                    }
                                                    placeholder="Filter…"
                                                    className="w-full min-w-[90px] px-2 py-1 rounded border text-[11px] focus:outline-none"
                                                    style={{
                                                        borderColor: 'var(--c-glass-border)',
                                                        background: 'var(--c-glass)',
                                                        color: 'var(--c-ink)',
                                                    }}
                                                    aria-label={`Filter ${col.name}`}
                                                />
                                            </th>
                                        ))}
                                    </tr>
                                )}
                            </thead>
                            <tbody style={{ color: 'var(--c-ink-2)' }}>
                                {data.rows.length === 0 ? (
                                    <tr>
                                        <td
                                            colSpan={data.columns.length}
                                            className="px-3 py-8 text-center"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            No rows match the current filters.
                                        </td>
                                    </tr>
                                ) : (
                                    data.rows.map((row, rowIdx) => {
                                        const rowMeta = data.rowMeta?.[rowIdx];
                                        const grouped =
                                            rowMeta?.group !== null && rowMeta?.group !== undefined;
                                        const rowBackground = grouped
                                            ? GROUP_BAND[(rowMeta!.group as number) % 2]
                                            : rowMeta?.severity
                                              ? SEVERITY_TINT[rowMeta.severity]
                                              : undefined;
                                        return (
                                            <tr
                                                key={rowIdx}
                                                className={
                                                    data.rowMeta ? undefined : 'odd:bg-white/5'
                                                }
                                                style={
                                                    rowBackground
                                                        ? { background: rowBackground }
                                                        : undefined
                                                }
                                                title={rowMeta?.tooltip ?? undefined}
                                            >
                                                {row.map((value, colIdx) => {
                                                    const cell = formatCell(
                                                        value,
                                                        data.columns[colIdx].kind
                                                    );
                                                    const meta = data.cellMeta?.[rowIdx]?.[colIdx];
                                                    const isNumeric =
                                                        data.columns[colIdx].kind === 'number';
                                                    const metaColor = meta?.severity
                                                        ? SEVERITY_COLOR[meta.severity]
                                                        : DIFF_LABEL_COLOR[meta?.diff ?? 'changed'];
                                                    const metaTint = meta?.severity
                                                        ? SEVERITY_TINT[meta.severity]
                                                        : meta?.diff
                                                          ? DIFF_TINT[meta.diff]
                                                          : undefined;
                                                    const label = meta?.label && (
                                                        <span
                                                            className={`px-1 py-px rounded text-[9px] font-mono align-middle whitespace-nowrap ${
                                                                isNumeric ? 'mr-1.5' : 'ml-1.5'
                                                            }`}
                                                            style={{
                                                                color: metaColor,
                                                                border: `1px solid ${metaTint ?? DIFF_TINT.changed}`,
                                                                background:
                                                                    metaTint ?? DIFF_TINT.changed,
                                                            }}
                                                        >
                                                            {meta.label}
                                                        </span>
                                                    );
                                                    return (
                                                        <td
                                                            key={colIdx}
                                                            className={`px-3 py-1.5 border-b whitespace-nowrap ${cell.className}`}
                                                            style={{
                                                                borderColor:
                                                                    'var(--c-glass-border)',
                                                                ...(cell.muted
                                                                    ? { color: 'var(--c-mist-fg)' }
                                                                    : {}),
                                                                ...(metaTint
                                                                    ? { background: metaTint }
                                                                    : {}),
                                                                // Severity indicator bar on the row's
                                                                // first cell (survives group banding).
                                                                ...(colIdx === 0 &&
                                                                rowMeta?.severity
                                                                    ? {
                                                                          boxShadow: `inset 2px 0 0 ${SEVERITY_COLOR[rowMeta.severity]}`,
                                                                      }
                                                                    : {}),
                                                            }}
                                                            title={meta?.tooltip ?? cell.title}
                                                        >
                                                            {isNumeric ? (
                                                                <>
                                                                    {label}
                                                                    {cell.text}
                                                                </>
                                                            ) : (
                                                                <>
                                                                    {cell.text}
                                                                    {label}
                                                                </>
                                                            )}
                                                        </td>
                                                    );
                                                })}
                                            </tr>
                                        );
                                    })
                                )}
                            </tbody>
                        </table>
                    </div>
                )}

                {grid.loading && data && (
                    <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                        <Loader2
                            className="w-5 h-5 animate-spin"
                            style={{ color: 'var(--c-signal-text)' }}
                        />
                    </div>
                )}
            </div>

            {data && (
                <PaginationFooter
                    page={grid.page}
                    totalPages={grid.totalPages}
                    pageSize={PAGE_SIZE}
                    filteredRows={data.filteredRows}
                    totalRows={data.totalRows}
                    truncated={data.truncated}
                    approx={data.approx}
                    onPageChange={grid.setPage}
                />
            )}
        </div>
    );
}

function SkeletonRows() {
    return (
        <div className="p-3 space-y-2 animate-pulse">
            {Array.from({ length: 12 }).map((_, i) => (
                <div
                    key={i}
                    className="h-6 rounded"
                    style={{ background: 'var(--c-mist)', opacity: 0.4 }}
                />
            ))}
        </div>
    );
}
