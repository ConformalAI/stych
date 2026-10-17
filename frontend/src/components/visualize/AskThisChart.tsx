/**
 * "Ask this chart" overlay — lives ON the canvas, not in the chat.
 *
 * SelectionToolbar: appears when marks are selected; shows instant
 * deterministic stats (client-side, zero latency) plus one-click intents
 * (Explain / Compare / Drill) and a free-text ask.
 *
 * InsightCard: the AI answer anchored to the chart — headline, evidence-cited
 * insight, the strongest drivers, and a jump into the agent-created drill-down
 * visualization when one was produced.
 */

import { useState } from 'react';
import {
    ArrowRight,
    GitFork,
    Loader2,
    MessageCircleQuestion,
    Scale,
    Sparkles,
    X,
} from 'lucide-react';
import type { AskMode, AskResponse } from './types';
import type { SelectionSummary } from './selection';

const glass = {
    background: 'color-mix(in srgb, var(--c-slate) 92%, transparent)',
    borderColor: 'var(--c-glass-border)',
    backdropFilter: 'blur(8px)',
} as const;

interface ToolbarProps {
    summary: SelectionSummary;
    asking: boolean;
    onAsk: (mode: AskMode, question?: string) => void;
    onClear: () => void;
}

export function SelectionToolbar({ summary, asking, onAsk, onClear }: ToolbarProps) {
    const [question, setQuestion] = useState('');

    const intent = (mode: AskMode, icon: React.ReactNode, label: string) => (
        <button
            type="button"
            disabled={asking}
            onClick={() => onAsk(mode)}
            className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors disabled:opacity-50"
            style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
        >
            {icon}
            {label}
        </button>
    );

    return (
        <div
            className="absolute bottom-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-2 rounded-xl border px-3 py-2 shadow-lg max-w-[92%]"
            style={glass}
        >
            <div className="flex flex-col min-w-0">
                <span className="text-[11px] font-semibold" style={{ color: 'var(--c-ink)' }}>
                    {summary.count} of {summary.total} selected
                </span>
                {summary.chips.length > 0 && (
                    <span className="text-[10px] truncate" style={{ color: 'var(--c-mist-fg)' }}>
                        {summary.chips.join(' · ')}
                    </span>
                )}
            </div>
            <div className="w-px h-6" style={{ background: 'var(--c-glass-border)' }} />
            {intent('explain', <Sparkles className="w-3 h-3" />, 'Explain')}
            {intent('compare', <Scale className="w-3 h-3" />, 'Compare')}
            {intent('drill', <GitFork className="w-3 h-3" />, 'Drill down')}
            <form
                className="flex items-center gap-1"
                onSubmit={(e) => {
                    e.preventDefault();
                    if (!question.trim() || asking) return;
                    onAsk('custom', question.trim());
                    setQuestion('');
                }}
            >
                <input
                    type="text"
                    value={question}
                    onChange={(e) => setQuestion(e.target.value)}
                    placeholder="Ask about this…"
                    className="w-36 rounded-md px-2 py-1 text-[11px]"
                    style={{
                        background: 'var(--c-glass)',
                        border: '1px solid var(--c-glass-border)',
                        color: 'var(--c-ink)',
                    }}
                />
                <button
                    type="submit"
                    disabled={asking || !question.trim()}
                    title="Ask"
                    className="disabled:opacity-40"
                    style={{ color: 'var(--c-signal-text)' }}
                >
                    {asking ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                        <ArrowRight className="w-3.5 h-3.5" />
                    )}
                </button>
            </form>
            <button
                type="button"
                onClick={onClear}
                title="Clear selection (Esc)"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                <X className="w-3.5 h-3.5" />
            </button>
        </div>
    );
}

interface InsightProps {
    asking: boolean;
    result: AskResponse | null;
    error: string | null;
    onClose: () => void;
    onOpenDrill: (vizId: string) => void;
}

export function InsightCard({ asking, result, error, onClose, onOpenDrill }: InsightProps) {
    if (!asking && !result && !error) return null;
    const topDrivers = (result?.drivers ?? []).slice(0, 3);

    return (
        <div
            className="absolute top-3 right-3 z-10 w-72 max-h-[85%] overflow-y-auto rounded-xl border p-3 shadow-xl flex flex-col gap-2"
            style={glass}
        >
            <div className="flex items-start gap-2">
                <MessageCircleQuestion
                    className="w-4 h-4 mt-0.5 flex-shrink-0"
                    style={{ color: 'var(--c-signal-text)' }}
                />
                <p className="flex-1 text-xs font-semibold" style={{ color: 'var(--c-ink)' }}>
                    {asking ? 'Analyzing selection…' : (result?.headline ?? 'Selection')}
                </p>
                <button type="button" onClick={onClose} style={{ color: 'var(--c-mist-fg)' }}>
                    <X className="w-3.5 h-3.5" />
                </button>
            </div>

            {asking && (
                <div
                    className="flex items-center gap-2 text-[11px]"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Running selection-vs-rest and driver analysis…
                </div>
            )}

            {error && !asking && (
                <p className="text-[11px]" style={{ color: 'var(--c-danger)' }}>
                    {error}
                </p>
            )}

            {result && !asking && (
                <>
                    <p
                        className="text-[10px] uppercase tracking-wider"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {result.selection}
                    </p>
                    {result.insight.split(/\n{2,}/).map((para, i) => (
                        <p
                            key={i}
                            className="text-[11px] leading-relaxed"
                            style={{ color: 'var(--c-ink-2)' }}
                        >
                            {para}
                        </p>
                    ))}
                    {topDrivers.length > 0 && (
                        <div className="flex flex-col gap-1">
                            <p
                                className="text-[10px] font-semibold uppercase tracking-wider"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                Strongest drivers
                            </p>
                            {topDrivers.map((d, i) => (
                                <div
                                    key={i}
                                    className="flex items-center justify-between rounded-md px-2 py-1 text-[10px]"
                                    style={{
                                        background: 'var(--c-glass)',
                                        color: 'var(--c-ink-2)',
                                    }}
                                >
                                    <span className="truncate">
                                        {d.field}: {d.segment}
                                    </span>
                                    <span
                                        className="font-semibold flex-shrink-0 ml-2"
                                        style={{ color: 'var(--c-signal-text)' }}
                                    >
                                        {Math.round(d.selected_share * 100)}% vs{' '}
                                        {Math.round(d.overall_share * 100)}%
                                    </span>
                                </div>
                            ))}
                        </div>
                    )}
                    {result.drill && (
                        <button
                            type="button"
                            onClick={() => onOpenDrill(result.drill!.viz_id)}
                            className="flex items-center gap-1.5 rounded-md border px-2 py-1.5 text-[11px] font-semibold"
                            style={{
                                background: 'var(--c-signal-dim)',
                                borderColor: 'color-mix(in srgb, var(--c-signal) 35%, transparent)',
                                color: 'var(--c-signal-text)',
                            }}
                        >
                            <GitFork className="w-3 h-3" />
                            Open drill-down: {result.drill.title}
                        </button>
                    )}
                </>
            )}
        </div>
    );
}
