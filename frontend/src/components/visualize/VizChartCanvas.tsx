import { useEffect, useState, type RefObject } from 'react';
import type { AskMode, AskResponse, VizSpec } from './types';
import type { UseVizSpec } from './useVizSpec';
import EChartsRenderer, { type EChartsRendererHandle } from './EChartsRenderer';
import ChartDataTable from './ChartDataTable';
import { InsightCard, SelectionToolbar } from './AskThisChart';
import { selectionSummary } from './selection';
import { rawScatterOverlapSummary } from './scatterOverlap';

interface Props {
    viz: UseVizSpec;
    renderedSpec: VizSpec;
    chartRef: RefObject<EChartsRendererHandle>;
    selectedRows: Set<number>;
    asking: boolean;
    askResult: AskResponse | null;
    askError: string | null;
    onSelectRows: (rows: number[], additive: boolean) => void;
    onAsk: (mode: AskMode, question?: string) => Promise<void>;
    onClearSelection: () => void;
    onCloseInsight: () => void;
}

export default function VizChartCanvas({
    viz,
    renderedSpec,
    chartRef,
    selectedRows,
    asking,
    askResult,
    askError,
    onSelectRows,
    onAsk,
    onClearSelection,
    onCloseInsight,
}: Props) {
    const [showDataTable, setShowDataTable] = useState(false);
    const overlapSummary = viz.data ? rawScatterOverlapSummary(renderedSpec, viz.data) : null;
    const isAggregated = Object.values(renderedSpec.encoding).some(
        (channel) => !Array.isArray(channel) && channel?.aggregate
    );

    useEffect(() => setShowDataTable(false), [viz.data]);

    if (!viz.data || viz.data.rows.length === 0) {
        return (
            <div
                className="flex flex-1 items-center justify-center text-sm"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {viz.loading
                    ? 'Building chart…'
                    : viz.data
                      ? 'No data matches — loosen your filters.'
                      : 'Drag fields onto the shelves to build a chart.'}
            </div>
        );
    }

    return (
        <>
            <div className="relative min-h-0 flex-1 p-2">
                <EChartsRenderer
                    ref={chartRef}
                    spec={renderedSpec}
                    data={viz.data}
                    className="h-full w-full"
                    selectedRows={selectedRows}
                    onSelectRows={onSelectRows}
                />
                {selectedRows.size > 0 && (
                    <SelectionToolbar
                        summary={selectionSummary(renderedSpec, viz.data, selectedRows)}
                        asking={asking}
                        onAsk={onAsk}
                        onClear={onClearSelection}
                    />
                )}
                <InsightCard
                    asking={asking}
                    result={askResult}
                    error={askError}
                    onClose={onCloseInsight}
                    onOpenDrill={(vizId) => {
                        onCloseInsight();
                        viz.selectViz(vizId).catch(() => undefined);
                    }}
                />
                {showDataTable && (
                    <ChartDataTable data={viz.data} onClose={() => setShowDataTable(false)} />
                )}
            </div>
            <div
                className="flex items-center gap-2 border-t px-3 py-1.5 text-[10px]"
                style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
            >
                <span>
                    {viz.data.meta.row_count.toLocaleString()} points ·{' '}
                    {viz.data.meta.engine === 'sql'
                        ? isAggregated
                            ? 'full-table aggregation'
                            : 'PostgreSQL rows'
                        : viz.data.meta.engine === 'mongodb'
                          ? isAggregated
                              ? 'MongoDB aggregation'
                              : 'MongoDB documents'
                          : viz.data.meta.engine === 'duckdb'
                            ? viz.data.meta.source_truncated
                                ? `DuckDB snapshot (up to ${viz.data.meta.source_row_limit?.toLocaleString() ?? 'the loaded limit'} source rows)`
                                : 'DuckDB snapshot'
                            : 'in-memory'}
                </span>
                {overlapSummary && (
                    <span style={{ color: 'var(--c-warning)' }}>{overlapSummary}</span>
                )}
                <button
                    type="button"
                    onClick={() => setShowDataTable(true)}
                    className="ml-auto underline underline-offset-2"
                    style={{ color: 'var(--c-signal-text)' }}
                >
                    View chart data
                </button>
                {viz.data.meta.truncated && (
                    <span style={{ color: 'var(--c-warning)' }}>
                        capped — add filters or aggregate further
                    </span>
                )}
                {viz.loading && <span>updating…</span>}
            </div>
        </>
    );
}
