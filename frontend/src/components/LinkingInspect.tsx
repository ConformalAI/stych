import { useEffect, useMemo, useState } from 'react';
import { GitCompareArrows, Loader2, SlidersHorizontal } from 'lucide-react';
import {
    fetchLinkingViz,
    inspectLinkingRow,
    type LinkingInspectCandidate,
    type LinkingInspectContext,
    type LinkingPreviewRow,
    type LinkingPreviewTable,
    type LinkingVizKind,
    type LinkingVizResponse,
} from '../services/api';
import VegaChart from './VegaChart';

type StrategyFamily = 'lexical' | 'semantic';
type InspectView = 'tables' | LinkingVizKind;

const VIZ_TABS: { id: InspectView; label: string }[] = [
    { id: 'tables', label: 'Tables' },
    { id: 'score_matrix', label: 'Score matrix' },
    { id: 'coordinated', label: 'Coordinated' },
    { id: 'bipartite', label: 'Bipartite' },
];

interface LinkingInspectPanelProps {
    context: LinkingInspectContext;
    sessionId: string;
    isDark: boolean;
}

function cellText(value: unknown): string {
    if (value === null || value === undefined || value === '') return '—';
    return String(value);
}

function isSemanticStrategy(name: string): boolean {
    return name === 'semantic';
}

function scoreTint(score: number, isDark: boolean): string {
    const t = Math.max(0, Math.min(1, score));
    if (isDark) {
        return `rgba(59, 130, 246, ${0.12 + t * 0.42})`;
    }
    return `rgba(37, 99, 235, ${0.08 + t * 0.32})`;
}

function InspectTable({
    table,
    isDark,
    selectedRowId,
    highlighted,
    extraRows,
    onRowClick,
    emptyHint,
}: {
    table: LinkingPreviewTable;
    isDark: boolean;
    selectedRowId?: number | null;
    highlighted?: Map<number, number>;
    extraRows?: LinkingPreviewRow[];
    onRowClick?: (rowId: number) => void;
    emptyHint?: string;
}) {
    const clickable = Boolean(onRowClick);
    const visible = new Set(table.rows.map((r) => Number(r._row_id)));
    const appended = (extraRows || []).filter((r) => !visible.has(Number(r._row_id)));
    const rows = appended.length ? [...table.rows, ...appended] : table.rows;

    return (
        <div
            className={`rounded-lg border overflow-hidden ${
                isDark ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-white'
            }`}
        >
            <div className="px-3 py-2 flex items-center justify-between gap-2">
                <div
                    className={`text-[11px] uppercase tracking-wide truncate ${
                        isDark ? 'text-white/50' : 'text-slate-500'
                    }`}
                >
                    {table.name}
                </div>
                <div
                    className={`text-[11px] flex-shrink-0 ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                >
                    {table.row_count.toLocaleString()} rows
                    {table.truncated ? ' · preview' : ''}
                </div>
            </div>
            <div className="max-h-72 overflow-auto">
                <table className="min-w-full text-xs border-collapse">
                    <thead>
                        <tr className={isDark ? 'text-white/60' : 'text-slate-600'}>
                            {table.columns.map((col) => (
                                <th
                                    key={col}
                                    className={`text-left font-medium px-3 py-1.5 border-b sticky top-0 ${
                                        isDark
                                            ? 'border-white/10 bg-[#0f1e35]'
                                            : 'border-gray-200 bg-gray-50'
                                    } ${col === table.join_key ? (isDark ? 'text-blue-300' : 'text-blue-700') : ''}`}
                                >
                                    {col}
                                    {col === table.join_key ? ' · key' : ''}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody className={isDark ? 'text-white/80' : 'text-slate-700'}>
                        {rows.length === 0 ? (
                            <tr>
                                <td
                                    colSpan={Math.max(table.columns.length, 1)}
                                    className={`px-3 py-6 text-center ${
                                        isDark ? 'text-white/30' : 'text-slate-400'
                                    }`}
                                >
                                    {emptyHint || 'No rows'}
                                </td>
                            </tr>
                        ) : (
                            rows.map((row) => {
                                const rowId = Number(row._row_id);
                                const selected = selectedRowId === rowId;
                                const score = highlighted?.get(rowId);
                                const tint =
                                    score !== undefined ? scoreTint(score, isDark) : undefined;
                                return (
                                    <tr
                                        key={rowId}
                                        onClick={clickable ? () => onRowClick?.(rowId) : undefined}
                                        className={`${clickable ? 'cursor-pointer' : ''} ${
                                            selected
                                                ? isDark
                                                    ? 'ring-1 ring-inset ring-blue-400/70'
                                                    : 'ring-1 ring-inset ring-blue-500/60'
                                                : ''
                                        }`}
                                        style={{
                                            background: tint,
                                        }}
                                    >
                                        {table.columns.map((col) => (
                                            <td
                                                key={`${rowId}-${col}`}
                                                className={`px-3 py-1.5 border-b whitespace-nowrap ${
                                                    isDark ? 'border-white/5' : 'border-gray-100'
                                                } ${col === table.join_key ? 'font-medium' : ''}`}
                                            >
                                                {cellText(row[col])}
                                            </td>
                                        ))}
                                    </tr>
                                );
                            })
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

function ScoreBars({
    candidates,
    threshold,
    isDark,
}: {
    candidates: LinkingInspectCandidate[];
    threshold: number;
    isDark: boolean;
}) {
    if (candidates.length === 0) {
        return (
            <p
                className={`text-xs px-1 py-6 text-center ${isDark ? 'text-white/35' : 'text-slate-400'}`}
            >
                No candidates at this threshold.
            </p>
        );
    }
    const maxScore = Math.max(...candidates.map((c) => c.score), 0.01);
    return (
        <ol className="space-y-2">
            {candidates.map((c, idx) => {
                const pct = Math.round(c.score * 100);
                const width = `${Math.max(6, (c.score / maxScore) * 100)}%`;
                const passes = c.score >= threshold;
                return (
                    <li key={`${c.candidate_row_id}-${c.strategy}-${idx}`}>
                        <div className="flex items-baseline justify-between gap-2 mb-0.5">
                            <span
                                className={`text-xs truncate ${
                                    isDark ? 'text-white/80' : 'text-slate-700'
                                }`}
                                title={cellText(c.value)}
                            >
                                <span className={isDark ? 'text-white/35' : 'text-slate-400'}>
                                    #{idx + 1}
                                </span>{' '}
                                {cellText(c.value)}
                            </span>
                            <span
                                className={`text-[11px] tabular-nums flex-shrink-0 ${
                                    passes
                                        ? isDark
                                            ? 'text-emerald-300'
                                            : 'text-emerald-700'
                                        : isDark
                                          ? 'text-white/40'
                                          : 'text-slate-400'
                                }`}
                            >
                                {pct}%
                            </span>
                        </div>
                        <div
                            className={`h-1.5 rounded-full overflow-hidden ${
                                isDark ? 'bg-white/10' : 'bg-slate-100'
                            }`}
                        >
                            <div
                                className={`h-full rounded-full ${
                                    passes ? 'bg-blue-500' : isDark ? 'bg-white/25' : 'bg-slate-300'
                                }`}
                                style={{ width }}
                            />
                        </div>
                    </li>
                );
            })}
        </ol>
    );
}

export default function LinkingInspectPanel({
    context,
    sessionId,
    isDark,
}: LinkingInspectPanelProps) {
    const plan = context.plan;
    const hasSemantic = Boolean(context.has_semantic);
    const hasLexical = Boolean(context.has_lexical ?? true);
    const defaultFamily: StrategyFamily =
        plan.strategy === 'semantic' && hasSemantic ? 'semantic' : 'lexical';

    const [family, setFamily] = useState<StrategyFamily>(defaultFamily);
    const [threshold, setThreshold] = useState<number>(
        typeof plan.threshold === 'number' ? plan.threshold : 0.8
    );
    const [selectedRowId, setSelectedRowId] = useState<number | null>(null);
    const [allCandidates, setAllCandidates] = useState<LinkingInspectCandidate[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [view, setView] = useState<InspectView>('tables');
    const [debouncedThreshold, setDebouncedThreshold] = useState(threshold);
    const [viz, setViz] = useState<LinkingVizResponse | null>(null);
    const [vizLoading, setVizLoading] = useState(false);
    const [vizError, setVizError] = useState<string | null>(null);

    useEffect(() => {
        const timer = window.setTimeout(() => setDebouncedThreshold(threshold), 150);
        return () => window.clearTimeout(timer);
    }, [threshold]);

    useEffect(() => {
        if (selectedRowId === null) {
            setAllCandidates([]);
            return;
        }
        let cancelled = false;
        setLoading(true);
        setError(null);
        inspectLinkingRow(sessionId, selectedRowId, { top_n: 10 })
            .then((res) => {
                if (cancelled) return;
                setAllCandidates(res.candidates || []);
            })
            .catch((err) => {
                if (cancelled) return;
                setError(err?.response?.data?.detail || err?.message || 'Inspect failed');
                setAllCandidates([]);
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sessionId, selectedRowId]);

    useEffect(() => {
        if (view === 'tables') {
            return;
        }
        let cancelled = false;
        setVizLoading(true);
        setVizError(null);
        fetchLinkingViz(sessionId, view, {
            strategy: family,
            threshold: debouncedThreshold,
            dark: isDark,
        })
            .then((res) => {
                if (cancelled) return;
                setViz(res);
            })
            .catch((err) => {
                if (cancelled) return;
                setViz(null);
                setVizError(err?.response?.data?.detail || err?.message || 'Chart failed');
            })
            .finally(() => {
                if (!cancelled) setVizLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sessionId, view, family, debouncedThreshold, isDark]);

    const filtered = useMemo(() => {
        return allCandidates
            .filter((c) =>
                family === 'semantic'
                    ? isSemanticStrategy(c.strategy)
                    : !isSemanticStrategy(c.strategy)
            )
            .sort((a, b) => b.score - a.score);
    }, [allCandidates, family]);

    const passing = useMemo(
        () => filtered.filter((c) => c.score >= threshold),
        [filtered, threshold]
    );

    const highlights = useMemo(() => {
        const map = new Map<number, number>();
        for (const c of passing) {
            const prev = map.get(c.candidate_row_id);
            if (prev === undefined || c.score > prev) {
                map.set(c.candidate_row_id, c.score);
            }
        }
        return map;
    }, [passing]);

    const canToggle = hasSemantic && hasLexical;

    return (
        <div
            className={`mt-4 w-full rounded-xl border p-3 sm:p-4 ${
                isDark ? 'border-white/10 bg-white/[0.03]' : 'border-gray-200 bg-slate-50'
            }`}
        >
            <div className="flex flex-wrap items-center gap-2 mb-3">
                <GitCompareArrows
                    className={`w-4 h-4 ${isDark ? 'text-blue-300' : 'text-blue-600'}`}
                />
                <span
                    className={`text-xs font-semibold ${isDark ? 'text-white/80' : 'text-slate-700'}`}
                >
                    Match inspect
                </span>
                <span className={`text-[11px] ${isDark ? 'text-white/35' : 'text-slate-400'}`}>
                    {plan.left_on} ↔ {plan.right_on} · {plan.strategy} · {plan.join_type}
                    {context.committed ? ' · joined' : ' · preview'}
                </span>
            </div>

            <div
                className={`inline-flex rounded-lg border p-0.5 text-[11px] mb-3 ${
                    isDark ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-white'
                }`}
            >
                {VIZ_TABS.map((tab) => (
                    <button
                        key={tab.id}
                        type="button"
                        onClick={() => setView(tab.id)}
                        className={`px-2.5 py-1 rounded-md transition-colors ${
                            view === tab.id
                                ? isDark
                                    ? 'bg-blue-500/25 text-blue-200'
                                    : 'bg-blue-100 text-blue-800'
                                : isDark
                                  ? 'text-white/50 hover:text-white/80'
                                  : 'text-slate-500 hover:text-slate-800'
                        }`}
                    >
                        {tab.label}
                    </button>
                ))}
            </div>

            <div className="flex flex-wrap items-center gap-3 mb-3">
                <div
                    className={`inline-flex rounded-lg border p-0.5 text-[11px] ${
                        isDark ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-white'
                    }`}
                >
                    <button
                        type="button"
                        disabled={!hasLexical}
                        onClick={() => setFamily('lexical')}
                        className={`px-2.5 py-1 rounded-md transition-colors ${
                            family === 'lexical'
                                ? isDark
                                    ? 'bg-blue-500/25 text-blue-200'
                                    : 'bg-blue-100 text-blue-800'
                                : isDark
                                  ? 'text-white/50 hover:text-white/80'
                                  : 'text-slate-500 hover:text-slate-800'
                        } disabled:opacity-40`}
                    >
                        Lexical
                    </button>
                    <button
                        type="button"
                        disabled={!hasSemantic}
                        onClick={() => setFamily('semantic')}
                        className={`px-2.5 py-1 rounded-md transition-colors ${
                            family === 'semantic'
                                ? isDark
                                    ? 'bg-blue-500/25 text-blue-200'
                                    : 'bg-blue-100 text-blue-800'
                                : isDark
                                  ? 'text-white/50 hover:text-white/80'
                                  : 'text-slate-500 hover:text-slate-800'
                        } disabled:opacity-40`}
                    >
                        Semantic
                    </button>
                </div>
                {!canToggle && (
                    <span className={`text-[11px] ${isDark ? 'text-white/30' : 'text-slate-400'}`}>
                        {hasSemantic
                            ? 'Semantic scores only'
                            : 'Lexical scores only (semantic unavailable)'}
                    </span>
                )}
                <label className="flex items-center gap-2 flex-1 min-w-[180px]">
                    <SlidersHorizontal
                        className={`w-3.5 h-3.5 ${isDark ? 'text-white/40' : 'text-slate-400'}`}
                    />
                    <span
                        className={`text-[11px] whitespace-nowrap ${isDark ? 'text-white/50' : 'text-slate-500'}`}
                    >
                        Threshold {Math.round(threshold * 100)}%
                    </span>
                    <input
                        type="range"
                        min={0}
                        max={100}
                        value={Math.round(threshold * 100)}
                        onChange={(e) => setThreshold(Number(e.target.value) / 100)}
                        className="flex-1 accent-blue-500"
                    />
                </label>
            </div>

            {view === 'tables' ? (
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                    <InspectTable
                        table={context.left}
                        isDark={isDark}
                        selectedRowId={selectedRowId}
                        onRowClick={setSelectedRowId}
                        emptyHint="No source rows"
                    />
                    <InspectTable
                        table={context.right}
                        isDark={isDark}
                        highlighted={highlights}
                        extraRows={passing.map((c) => c.row).filter(Boolean) as LinkingPreviewRow[]}
                        emptyHint="Click a row on the left to highlight matches"
                    />
                    <div
                        className={`rounded-lg border p-3 min-h-[12rem] ${
                            isDark ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-white'
                        }`}
                    >
                        <div className="flex items-center justify-between mb-2">
                            <div
                                className={`text-[11px] uppercase tracking-wide ${
                                    isDark ? 'text-white/50' : 'text-slate-500'
                                }`}
                            >
                                Ranked scores
                            </div>
                            {loading && (
                                <Loader2
                                    className={`w-3.5 h-3.5 animate-spin ${
                                        isDark ? 'text-white/40' : 'text-slate-400'
                                    }`}
                                />
                            )}
                        </div>
                        {selectedRowId === null ? (
                            <p
                                className={`text-xs leading-relaxed ${
                                    isDark ? 'text-white/35' : 'text-slate-400'
                                }`}
                            >
                                Click a row in dataset 1 to see the candidates the linker
                                considered, ranked by similarity.
                            </p>
                        ) : error ? (
                            <p className={`text-xs ${isDark ? 'text-red-300' : 'text-red-600'}`}>
                                {error}
                            </p>
                        ) : (
                            <ScoreBars
                                candidates={filtered}
                                threshold={threshold}
                                isDark={isDark}
                            />
                        )}
                        {selectedRowId !== null && !loading && !error && (
                            <p
                                className={`mt-3 text-[11px] ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                            >
                                {passing.length} of {filtered.length} candidates pass the cutoff.
                                Dimmed bars are below threshold.
                            </p>
                        )}
                    </div>
                </div>
            ) : (
                <div
                    className={`rounded-lg border p-3 min-h-[16rem] ${
                        isDark ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-white'
                    }`}
                >
                    <div className="flex items-center justify-between mb-2">
                        <div
                            className={`text-[11px] ${isDark ? 'text-white/40' : 'text-slate-500'}`}
                        >
                            {view === 'score_matrix' &&
                                'Source rows × candidate rows. Color is similarity.'}
                            {view === 'coordinated' &&
                                'Click a source (or a score bar) to highlight the matching rows.'}
                            {view === 'bipartite' &&
                                'Left = dataset 1, right = candidates. Edge thickness follows score. Click a left node to isolate its edges.'}
                        </div>
                        {vizLoading && (
                            <Loader2
                                className={`w-3.5 h-3.5 animate-spin ${
                                    isDark ? 'text-white/40' : 'text-slate-400'
                                }`}
                            />
                        )}
                    </div>
                    {vizError ? (
                        <p className={`text-xs ${isDark ? 'text-red-300' : 'text-red-600'}`}>
                            {vizError}
                        </p>
                    ) : viz?.spec ? (
                        <>
                            <VegaChart
                                spec={viz.spec}
                                ariaLabel={
                                    view === 'score_matrix'
                                        ? 'Similarity score matrix heatmap'
                                        : view === 'coordinated'
                                          ? 'Coordinated source and candidate score charts'
                                          : 'Bipartite match graph'
                                }
                            />
                            <p
                                className={`mt-2 text-[11px] ${
                                    isDark ? 'text-white/30' : 'text-slate-400'
                                }`}
                            >
                                {viz.n_edges} edges · {viz.n_sources} sources · {viz.n_candidates}{' '}
                                candidates
                            </p>
                        </>
                    ) : (
                        !vizLoading && (
                            <p className={`text-xs ${isDark ? 'text-white/35' : 'text-slate-400'}`}>
                                No scored candidates to chart.
                            </p>
                        )
                    )}
                </div>
            )}
        </div>
    );
}
