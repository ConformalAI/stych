import { useEffect, useMemo, useState } from 'react';
import {
    AlertTriangle,
    ArrowRight,
    ArrowRightLeft,
    GitCompareArrows,
    MessageSquarePlus,
} from 'lucide-react';
import { PreviewDiffColumn, PreviewDiffSummary, PreviewSource } from '../../services/api';
import { makeDiffPreviewProvider } from './diffProvider';
import SpreadsheetGrid, { DIFF_LABEL_COLOR, DIFF_TINT } from './SpreadsheetGrid';
import { DiffKind } from './types';
import SourcePicker from './SourcePicker';

interface DiffViewProps {
    sessionId: string;
    sources: PreviewSource[];
    onExplain?: (prompt: string) => void;
}

const LEGEND: Array<[DiffKind, string]> = [
    ['added', 'added'],
    ['removed', 'removed'],
    ['changed', 'changed'],
    ['imputed', 'imputed'],
    ['transformed', 'transformed'],
];

function SummaryChip({
    text,
    tone,
    active,
    onClick,
    title,
}: {
    text: string;
    tone?: string;
    active?: boolean;
    onClick?: () => void;
    title?: string;
}) {
    const Tag = onClick ? 'button' : 'span';
    return (
        <Tag
            type={onClick ? 'button' : undefined}
            onClick={onClick}
            title={title ?? (onClick ? 'Filter rows to this change type' : undefined)}
            className={`px-2 py-0.5 rounded-full border text-[11px] whitespace-nowrap transition-colors ${
                onClick ? 'cursor-pointer hover:bg-white/5' : ''
            }`}
            style={{
                borderColor: active ? (tone ?? 'var(--c-signal)') : 'var(--c-glass-border)',
                color: tone ?? 'var(--c-ink-2)',
                background: active
                    ? `color-mix(in srgb, ${tone ?? 'var(--c-signal)'} 14%, transparent)`
                    : 'var(--c-glass)',
            }}
        >
            {text}
        </Tag>
    );
}

function buildExplainPrompt(
    base: PreviewSource,
    target: PreviewSource,
    summary: PreviewDiffSummary,
    columns: PreviewDiffColumn[]
): string {
    const notes = columns
        .filter((c) => c.note)
        .map((c) => `${c.name}: ${c.note}`)
        .join('; ');
    const renamed = Object.entries(summary.columns_renamed)
        .map(([to, from]) => `${from} → ${to}`)
        .join(', ');
    const parts = [
        `I compared two versions of my data in the diff view: "${base.name}" (base) → "${target.name}" (target).`,
        `Summary: ${summary.rows_added} rows added, ${summary.rows_removed} rows removed, ${summary.rows_changed} rows changed.`,
    ];
    if (summary.columns_added.length > 0) {
        parts.push(`Columns added: ${summary.columns_added.join(', ')}.`);
    }
    if (summary.columns_removed.length > 0) {
        parts.push(`Columns removed: ${summary.columns_removed.join(', ')}.`);
    }
    if (renamed) parts.push(`Columns renamed: ${renamed}.`);
    if (notes) parts.push(`Detected transformations — ${notes}.`);
    parts.push(
        'Please explain these changes in plain language, assess whether the cleaning/transformations look correct, and flag anything suspicious.'
    );
    return parts.join(' ');
}

export default function DiffView({ sessionId, sources, onExplain }: DiffViewProps) {
    // Overlay connectors can diff the loaded sample (::base) against the
    // working overlay (::working). Postgres diffs live base vs stych_temp.
    const tableSources = sources.filter((s) => s.renderer === 'table');
    // When the agent has working copies of a DB table, default to comparing the
    // ACTIVE copy against its base table (ids are "<prefix>::base" /
    // "<prefix>::temp" / "<prefix>::copy:<name>").
    const workingSql =
        tableSources.find((s) => s.kind === 'sql' && s.role === 'working' && s.is_active) ??
        tableSources.find((s) => s.kind === 'sql' && s.role === 'working') ??
        null;
    const workingSqlBase = workingSql
        ? (tableSources.find(
              (s) => s.kind === 'sql' && s.id === `${workingSql.id.split('::')[0]}::base`
          ) ?? null)
        : null;
    const [base, setBase] = useState<PreviewSource | null>(() => workingSqlBase);
    const [target, setTarget] = useState<PreviewSource | null>(
        () => workingSql ?? tableSources.find((s) => s.is_active) ?? null
    );
    const [changesOnly, setChangesOnly] = useState(false);
    const [kindFilter, setKindFilter] = useState<DiffKind | null>(null);
    const [summary, setSummary] = useState<PreviewDiffSummary | null>(null);
    const [columns, setColumns] = useState<PreviewDiffColumn[]>([]);

    const sameSource = !!base && !!target && base.kind === target.kind && base.id === target.id;
    // A pair is comparable iff both sides are files/datasets, or both sides are
    // sql tables from the same connection — the backend rejects mixed pairs.
    const mixedSqlPair = !!base && !!target && (base.kind === 'sql') !== (target.kind === 'sql');

    const provider = useMemo(() => {
        if (!base || !target || sameSource || mixedSqlPair) return null;
        return makeDiffPreviewProvider(
            sessionId,
            base,
            target,
            changesOnly,
            kindFilter,
            (pageSummary, pageColumns) => {
                setSummary(pageSummary);
                setColumns(pageColumns);
            }
        );
    }, [sessionId, base, target, changesOnly, kindFilter, sameSource, mixedSqlPair]);

    // Stale summaries shouldn't linger while a new comparison loads; keep them
    // when only the kind filter / changes-only toggle changed (same pair).
    const pairKey = base && target ? `${base.kind}:${base.id}->${target.kind}:${target.id}` : '';
    useEffect(() => {
        setSummary(null);
        setColumns([]);
        setKindFilter(null);
    }, [pairKey]);

    const toggleKind = (kind: DiffKind) => setKindFilter((prev) => (prev === kind ? null : kind));

    return (
        <div className="flex-1 flex flex-col min-h-0 gap-3">
            {/* Source selection row */}
            <div className="flex items-center gap-2 flex-wrap">
                <SourcePicker sources={tableSources} selected={base} onSelect={setBase} />
                <ArrowRight
                    className="w-4 h-4 flex-shrink-0"
                    style={{ color: 'var(--c-mist-fg)' }}
                />
                <SourcePicker sources={tableSources} selected={target} onSelect={setTarget} />
                <button
                    type="button"
                    onClick={() => {
                        setBase(target);
                        setTarget(base);
                    }}
                    disabled={!base && !target}
                    className="p-1.5 rounded-md border transition-colors hover:bg-white/5 disabled:opacity-30"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
                    title="Swap base and target"
                    aria-label="Swap base and target"
                >
                    <ArrowRightLeft className="w-3.5 h-3.5" />
                </button>
                <div className="flex items-center gap-3 ml-auto">
                    {onExplain && summary && base && target && (
                        <button
                            type="button"
                            onClick={() =>
                                onExplain(buildExplainPrompt(base, target, summary, columns))
                            }
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                color: 'var(--c-signal-text)',
                            }}
                            title="Send this comparison to the chat for an explanation"
                        >
                            <MessageSquarePlus className="w-3.5 h-3.5" />
                            Explain these changes
                        </button>
                    )}
                    <label
                        className="flex items-center gap-1.5 text-xs cursor-pointer select-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    >
                        <input
                            type="checkbox"
                            checked={changesOnly}
                            onChange={(e) => setChangesOnly(e.target.checked)}
                            className="accent-current"
                        />
                        Changes only
                    </label>
                </div>
            </div>

            {/* Similarity warning */}
            {summary?.warning && (
                <div
                    className="flex items-center gap-2 px-3 py-2 rounded-lg border text-xs"
                    style={{
                        color: 'var(--c-warning)',
                        borderColor: 'rgba(232,161,58,0.3)',
                        background: 'rgba(232,161,58,0.08)',
                    }}
                >
                    <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                    {summary.warning}
                </div>
            )}

            {/* Summary + legend */}
            {summary && (
                <div className="flex items-center gap-1.5 flex-wrap">
                    <SummaryChip
                        text={`+${summary.rows_added.toLocaleString()} rows`}
                        tone="var(--c-success)"
                        active={kindFilter === 'added'}
                        onClick={() => toggleKind('added')}
                    />
                    <SummaryChip
                        text={`−${summary.rows_removed.toLocaleString()} rows`}
                        tone="var(--c-danger)"
                        active={kindFilter === 'removed'}
                        onClick={() => toggleKind('removed')}
                    />
                    <SummaryChip
                        text={`~${summary.rows_changed.toLocaleString()} rows changed`}
                        tone="var(--c-warning)"
                        active={changesOnly}
                        onClick={() => setChangesOnly((v) => !v)}
                        title="Show only rows with changes"
                    />
                    {summary.columns_added.length > 0 && (
                        <SummaryChip
                            text={`+${summary.columns_added.length} cols`}
                            tone="var(--c-success)"
                        />
                    )}
                    {summary.columns_removed.length > 0 && (
                        <SummaryChip
                            text={`−${summary.columns_removed.length} cols (${summary.columns_removed.join(', ')})`}
                            tone="var(--c-danger)"
                        />
                    )}
                    {Object.keys(summary.columns_renamed).length > 0 && (
                        <SummaryChip
                            text={`${Object.keys(summary.columns_renamed).length} renamed`}
                            tone="#8b5cf6"
                        />
                    )}
                    <SummaryChip
                        text={
                            summary.alignment.startsWith('key:')
                                ? `aligned by ${summary.alignment.slice(4)}`
                                : 'aligned by row position'
                        }
                    />
                    <span className="flex items-center gap-2 ml-auto">
                        {LEGEND.map(([kind, label]) => (
                            <button
                                key={kind}
                                type="button"
                                onClick={() => toggleKind(kind)}
                                className={`flex items-center gap-1 text-[10px] transition-opacity ${
                                    kindFilter && kindFilter !== kind ? 'opacity-40' : ''
                                }`}
                                style={{ color: 'var(--c-mist-fg)' }}
                                title={`Show only rows with ${label} changes`}
                            >
                                <span
                                    className="w-2.5 h-2.5 rounded-sm inline-block"
                                    style={{
                                        background: DIFF_TINT[kind],
                                        border: `1px solid ${DIFF_LABEL_COLOR[kind]}`,
                                    }}
                                />
                                {label}
                            </button>
                        ))}
                    </span>
                </div>
            )}

            {/* Body */}
            {provider ? (
                <SpreadsheetGrid provider={provider} />
            ) : (
                <div
                    className="flex-1 flex flex-col items-center justify-center gap-2 text-sm"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    <GitCompareArrows className="w-8 h-8 opacity-50" />
                    <span>
                        {sameSource
                            ? 'Pick two different sources to compare.'
                            : mixedSqlPair
                              ? 'Database tables can only be compared with other tables from the same PostgreSQL connection.'
                              : 'Pick a base and a target spreadsheet to see what changed.'}
                    </span>
                </div>
            )}
        </div>
    );
}
