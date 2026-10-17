/**
 * Metric preview — run a defined metric and see the number it produces.
 *
 * This is the ontology's equivalent of the chart canvas: proof the definition
 * means what the author thinks it means, before anyone certifies it. It runs
 * through the same `/query` endpoint the agent's `query_metric` tool uses, so
 * what the author sees here is exactly what the agent will answer with.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Play } from 'lucide-react';
import { queryOntology } from '../../services/ontology';
import type { OntologyQueryResult, OntologySpec } from './types';

interface Props {
    sessionId: string;
    spec: OntologySpec;
    metricName: string;
}

export default function MetricPreview({ sessionId, spec, metricName }: Props) {
    const [groupBy, setGroupBy] = useState<string>('');
    const [result, setResult] = useState<OntologyQueryResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);

    // A different metric means the previous result is about something else.
    useEffect(() => {
        setResult(null);
        setError(null);
        setGroupBy('');
    }, [metricName]);

    // Certifying changes what the result should SAY about itself (the draft
    // warning), so a stale result would keep claiming definitions are drafts
    // after the user certified them.
    const certifiedKey = spec.metrics.map((m) => `${m.name}:${m.certified}`).join(',');
    useEffect(() => {
        setResult(null);
    }, [certifiedKey]);

    const run = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await queryOntology(
                sessionId,
                [metricName],
                groupBy ? [{ name: groupBy, top_k: 15 }] : []
            );
            setResult(res);
        } catch (err: unknown) {
            const detail = (
                err as { response?: { data?: { detail?: { errors?: { message: string }[] } } } }
            )?.response?.data?.detail;
            setError(
                detail?.errors?.map((e) => e.message).join(' ') ?? 'Could not run this metric.'
            );
            setResult(null);
        } finally {
            setLoading(false);
        }
    }, [sessionId, metricName, groupBy]);

    const columns = result?.data.columns.map((c) => c.name) ?? [];

    return (
        <div
            className="border-t p-3"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            <div className="flex items-center gap-2 mb-2">
                <span
                    className="text-[10px] uppercase tracking-widest font-semibold"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Preview
                </span>
                <select
                    value={groupBy}
                    onChange={(e) => setGroupBy(e.target.value)}
                    className="text-[11px] rounded border px-1.5 py-0.5 outline-none"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-ink)',
                    }}
                >
                    <option value="">total</option>
                    {spec.dimensions.map((d) => (
                        <option key={d.name} value={d.name}>
                            by {d.name}
                        </option>
                    ))}
                </select>
                <button
                    type="button"
                    onClick={run}
                    disabled={loading}
                    className="ml-auto flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md transition-colors disabled:opacity-50"
                    style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
                >
                    <Play className="w-3 h-3" />
                    {loading ? 'Running…' : 'Run'}
                </button>
            </div>

            {error && (
                <p className="text-[11px] leading-relaxed" style={{ color: 'var(--c-danger)' }}>
                    {error}
                </p>
            )}

            {result && (
                <>
                    {result.uncertified.length > 0 && (
                        <p
                            className="flex items-start gap-1 text-[10px] mb-1.5"
                            style={{ color: 'var(--c-warning)' }}
                        >
                            <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-px" />
                            <span>Draft definitions: {result.uncertified.join(', ')}</span>
                        </p>
                    )}
                    {result.fan_out?.length > 0 && (
                        <p
                            className="flex items-start gap-1 text-[10px] mb-1.5"
                            style={{ color: 'var(--c-danger)' }}
                        >
                            <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-px" />
                            <span>
                                Joining through {result.fan_out.join(', ')} matches many rows per{' '}
                                {result.base_entity} row — sums and counts here are inflated by the
                                duplication.
                            </span>
                        </p>
                    )}
                    {result.spans_entities && (
                        <p className="text-[10px] mb-1.5" style={{ color: 'var(--c-mist-fg)' }}>
                            Joined across entities via the ontology's relationships.
                        </p>
                    )}
                    <div
                        className="max-h-40 overflow-auto rounded-md"
                        style={{ background: 'var(--c-slate)' }}
                    >
                        <table className="w-full text-[11px]">
                            <thead>
                                <tr>
                                    {columns.map((c) => (
                                        <th
                                            key={c}
                                            className="text-left px-2 py-1 font-medium sticky top-0 font-mono"
                                            style={{
                                                color: 'var(--c-mist-fg)',
                                                background: 'var(--c-slate)',
                                            }}
                                        >
                                            {c}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {result.data.rows.map((row, i) => (
                                    <tr key={i}>
                                        {row.map((cell, j) => (
                                            <td
                                                key={j}
                                                className="px-2 py-1 truncate"
                                                style={{ color: 'var(--c-ink)' }}
                                            >
                                                {formatCell(cell)}
                                            </td>
                                        ))}
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </>
            )}
        </div>
    );
}

export function formatCell(value: unknown): string {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number') {
        return Number.isInteger(value)
            ? value.toLocaleString()
            : value.toLocaleString(undefined, {
                  maximumFractionDigits: 2,
              });
    }
    return String(value);
}
