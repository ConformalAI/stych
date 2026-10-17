import { useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    Check,
    ChevronDown,
    ListFilter,
    Loader2,
    SlidersHorizontal,
    Trash2,
} from 'lucide-react';
import { PreviewSource } from '../../services/api';
import { deleteFilter, FilterSummary, listFilters, SavedFilter } from '../../services/filters';
import { SOURCE_REMOVED_MESSAGE } from '../../sourceRemoved';
import { useStore } from '../../store/useStore';
import { makeFilterPreviewProvider } from './filterProvider';
import SpreadsheetGrid, { SEVERITY_COLOR, SEVERITY_TINT } from './SpreadsheetGrid';
import { FilterSeverity } from './types';

interface FilterViewProps {
    sessionId: string;
    selected: PreviewSource | null;
}

interface SpecRule {
    id: string;
    severity: FilterSeverity;
    label?: string | null;
    target: 'row' | 'cells';
}

const specRules = (filter: SavedFilter | null): SpecRule[] =>
    ((filter?.spec?.present?.rules as SpecRule[] | undefined) ?? []).map((r) => ({
        id: r.id,
        severity: r.severity ?? 'warning',
        label: r.label,
        target: r.target ?? 'row',
    }));

function Chip({ text, tone, title }: { text: string; tone?: string; title?: string }) {
    return (
        <span
            className="px-2 py-0.5 rounded-full border text-[11px] whitespace-nowrap"
            title={title}
            style={{
                borderColor: 'var(--c-glass-border)',
                color: tone ?? 'var(--c-ink-2)',
                background: 'var(--c-glass)',
            }}
        >
            {text}
        </span>
    );
}

export default function FilterView({ sessionId, selected }: FilterViewProps) {
    const [filters, setFilters] = useState<SavedFilter[]>([]);
    const [filter, setFilter] = useState<SavedFilter | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [pickerOpen, setPickerOpen] = useState(false);
    const [matchesOnly, setMatchesOnly] = useState(false);
    const [disabledRules, setDisabledRules] = useState<string[]>([]);
    const [summary, setSummary] = useState<FilterSummary | null>(null);
    const [deleteArmed, setDeleteArmed] = useState(false);
    const [reloadTick, setReloadTick] = useState(0);
    const pickerRef = useRef<HTMLDivElement>(null);

    const openFilterId = useStore((s) => s.openFilterId);
    const setOpenFilterId = useStore((s) => s.setOpenFilterId);
    const addToast = useStore((s) => s.addToast);

    // Load filters saved for the currently selected sheet only — a filter's
    // annotate/select columns are authored against one dataset's schema, so
    // applying it against a different sheet would error (or worse, silently
    // match the wrong columns).
    useEffect(() => {
        if (!selected) {
            setLoading(false);
            setFilters([]);
            setFilter(null);
            return;
        }
        let cancelled = false;
        setLoading(true);
        setError(null);
        listFilters(sessionId, selected?.id)
            .then((result) => {
                if (cancelled) return;
                const sorted = [...result].sort((a, b) =>
                    (a.created_at ?? '').localeCompare(b.created_at ?? '')
                );
                setFilters(sorted);
                setFilter((prev) => {
                    const wanted = openFilterId
                        ? sorted.find((f) => f.id === openFilterId)
                        : undefined;
                    const kept = prev && sorted.find((f) => f.id === prev.id);
                    return wanted ?? kept ?? sorted[0] ?? null;
                });
                if (openFilterId) setOpenFilterId(null);
            })
            .catch((err) => {
                if (cancelled) return;
                setError(err?.response?.data?.detail || err?.message || 'Failed to load filters.');
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
        // openFilterId intentionally read once per load; the tick covers refreshes.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sessionId, selected?.id, reloadTick]);

    // Agent saved a filter while this view is open — refresh and select it.
    useEffect(() => {
        if (openFilterId) setReloadTick((t) => t + 1);
    }, [openFilterId]);

    // Per-filter view state resets when switching filters.
    useEffect(() => {
        setMatchesOnly(Boolean(filter?.spec?.present?.matches_only));
        setDisabledRules([]);
        setSummary(null);
        setDeleteArmed(false);
    }, [filter?.id, filter?.version, filter?.spec?.present?.matches_only]);

    // Close picker on outside click.
    useEffect(() => {
        if (!pickerOpen) return;
        const handler = (e: MouseEvent) => {
            if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
                setPickerOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [pickerOpen]);

    const rules = specRules(filter);
    const groupingActive = Boolean(filter?.spec?.present?.group_by);
    const enabledRules = useMemo(
        () =>
            disabledRules.length === 0
                ? null
                : rules.map((r) => r.id).filter((id) => !disabledRules.includes(id)),
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [disabledRules, filter?.id, filter?.version]
    );

    const provider = useMemo(() => {
        if (!filter || !selected) return null;
        return makeFilterPreviewProvider(
            sessionId,
            selected,
            filter.id,
            matchesOnly,
            enabledRules,
            groupingActive,
            setSummary
        );
    }, [sessionId, selected, filter, matchesOnly, enabledRules, groupingActive]);

    const toggleRule = (id: string) =>
        setDisabledRules((prev) =>
            prev.includes(id) ? prev.filter((r) => r !== id) : [...prev, id]
        );

    const handleDelete = async () => {
        if (!filter) return;
        if (!deleteArmed) {
            setDeleteArmed(true);
            return;
        }
        try {
            await deleteFilter(filter.id);
            setFilter(null);
            setReloadTick((t) => t + 1);
        } catch {
            addToast({ message: 'Failed to delete filter.', type: 'error' });
        }
    };

    if (!selected) {
        return (
            <div
                className="flex-1 flex items-center justify-center text-sm"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {SOURCE_REMOVED_MESSAGE}
            </div>
        );
    }

    if (loading && filters.length === 0) {
        return (
            <div className="flex-1 flex items-center justify-center">
                <Loader2
                    className="w-5 h-5 animate-spin"
                    style={{ color: 'var(--c-signal-text)' }}
                />
            </div>
        );
    }

    if (filters.length === 0) {
        return (
            <div
                className="flex-1 flex flex-col items-center justify-center gap-2 text-sm"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                <ListFilter className="w-8 h-8 opacity-50" />
                <span>No saved filters yet.</span>
                <span className="text-xs">
                    Type <code>/filter</code> in the chat — e.g. “/filter highlight rows that are
                    90% duplicates of each other”.
                </span>
                {error && <span style={{ color: 'var(--c-danger)' }}>{error}</span>}
            </div>
        );
    }

    return (
        <div className="flex-1 flex flex-col min-h-0 gap-3">
            {/* Filter picker row */}
            <div className="flex items-center gap-2 flex-wrap">
                <div ref={pickerRef} className="relative">
                    <button
                        type="button"
                        onClick={() => setPickerOpen((p) => !p)}
                        className="flex items-center gap-2 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5 min-w-[220px]"
                        style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                    >
                        <SlidersHorizontal
                            className="w-3.5 h-3.5"
                            style={{ color: 'var(--c-signal-text)' }}
                        />
                        <span className="truncate flex-1 text-left">
                            {filter ? filter.name : 'Pick a filter'}
                        </span>
                        {filter && (
                            <span className="text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                                v{filter.version}
                            </span>
                        )}
                        <ChevronDown className="w-3.5 h-3.5 flex-shrink-0" />
                    </button>
                    {pickerOpen && (
                        <div
                            className="absolute left-0 top-full mt-1 z-30 w-[320px] max-h-72 overflow-y-auto rounded-lg border py-1"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                background: 'var(--c-slate)',
                                boxShadow: '0 16px 48px rgba(0,0,0,0.4)',
                            }}
                        >
                            {filters.map((f) => (
                                <button
                                    key={f.id}
                                    type="button"
                                    onClick={() => {
                                        setFilter(f);
                                        setPickerOpen(false);
                                    }}
                                    className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-white/5"
                                    style={{ color: 'var(--c-ink)' }}
                                >
                                    <span className="truncate flex-1">{f.name}</span>
                                    <span
                                        className="text-[10px]"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        v{f.version}
                                    </span>
                                    {filter?.id === f.id && (
                                        <Check
                                            className="w-3.5 h-3.5"
                                            style={{ color: 'var(--c-signal-text)' }}
                                        />
                                    )}
                                </button>
                            ))}
                        </div>
                    )}
                </div>

                <div className="flex items-center gap-3 ml-auto">
                    <label
                        className="flex items-center gap-1.5 text-xs cursor-pointer select-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    >
                        <input
                            type="checkbox"
                            checked={matchesOnly}
                            onChange={(e) => setMatchesOnly(e.target.checked)}
                            className="accent-current"
                        />
                        Matches only
                    </label>
                    {filter && (
                        <button
                            type="button"
                            onClick={handleDelete}
                            onBlur={() => setDeleteArmed(false)}
                            className="flex items-center gap-1 px-2 py-1.5 rounded-md border text-xs transition-colors hover:bg-white/5"
                            style={{
                                borderColor: deleteArmed
                                    ? 'var(--c-danger)'
                                    : 'var(--c-glass-border)',
                                color: deleteArmed ? 'var(--c-danger)' : 'var(--c-mist-fg)',
                            }}
                            title="Delete this filter"
                        >
                            <Trash2 className="w-3.5 h-3.5" />
                            {deleteArmed ? 'Confirm delete' : ''}
                        </button>
                    )}
                </div>
            </div>

            {/* Degradation / identity warnings */}
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

            {/* Summary chips + rule legend */}
            {summary && (
                <div className="flex items-center gap-1.5 flex-wrap">
                    <Chip
                        text={`${summary.matched_rows.toLocaleString()} of ${summary.total_rows.toLocaleString()} rows match${summary.approx ? ' (approx)' : ''}`}
                        tone="var(--c-signal-text)"
                    />
                    {summary.engine && (
                        <Chip
                            text={
                                summary.source_truncated
                                    ? `${summary.engine} ${summary.scope} snapshot · up to ${summary.source_row_limit?.toLocaleString() ?? 'loaded'} rows`
                                    : `${summary.engine} ${summary.scope ?? ''} snapshot`.trim()
                            }
                        />
                    )}
                    {summary.group_count !== null && summary.group_count !== undefined && (
                        <Chip
                            text={`${summary.group_count.toLocaleString()} groups`}
                            tone="var(--c-signal-text)"
                            title="Matching rows are banded together by group"
                        />
                    )}
                    <Chip
                        text={
                            summary.identity_resolved.startsWith('key:')
                                ? `keyed by ${summary.identity_resolved.slice(4)}`
                                : 'keyed by row position'
                        }
                    />
                    <span className="flex items-center gap-2 ml-auto">
                        {rules.map((rule) => {
                            const off = disabledRules.includes(rule.id);
                            const count = summary.rule_counts[rule.id];
                            return (
                                <button
                                    key={rule.id}
                                    type="button"
                                    onClick={() => toggleRule(rule.id)}
                                    className={`flex items-center gap-1 text-[10px] transition-opacity ${off ? 'opacity-40' : ''}`}
                                    style={{ color: 'var(--c-mist-fg)' }}
                                    title={`Toggle rule "${rule.label ?? rule.id}"`}
                                >
                                    <span
                                        className="w-2.5 h-2.5 rounded-sm inline-block"
                                        style={{
                                            background: SEVERITY_TINT[rule.severity],
                                            border: `1px solid ${SEVERITY_COLOR[rule.severity]}`,
                                        }}
                                    />
                                    {rule.label ?? rule.id}
                                    {count !== undefined && ` (${count.toLocaleString()})`}
                                </button>
                            );
                        })}
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
                    <ListFilter className="w-8 h-8 opacity-50" />
                    <span>
                        {selected ? 'Pick a saved filter to apply.' : 'Pick a data source first.'}
                    </span>
                </div>
            )}
        </div>
    );
}
