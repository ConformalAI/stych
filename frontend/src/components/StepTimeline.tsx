/**
 * StepTimeline — Claude Code–style inline step display.
 *
 * Renders agent tool calls as a vertical list of compact rows.
 * Each row shows: agent dot • agent badge • tool summary • status indicator.
 * Clicking a row expands the full thought + observation.
 *
 * Used for both live (streaming) and persisted (post-completion) traces.
 */

import { useState } from 'react';
import {
    BarChart2,
    Brain,
    Check,
    ChevronDown,
    ChevronRight,
    Database,
    FileSpreadsheet,
    GitFork,
    Globe,
    Layers,
    Link2,
    Loader2,
    MessageSquare,
    Pause,
    Plug,
    TrendingUp,
    Wand2,
    X,
} from 'lucide-react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface StepRow {
    step: number;
    agent?: string;
    thought?: string;
    action?: string;
    input?: any;
    observation?: string;
    phase: 'start' | 'end' | 'final' | 'pause' | 'permission' | 'error';
}

interface StepTimelineProps {
    steps: StepRow[];
    isLive?: boolean;
    isDark?: boolean;
}

// ---------------------------------------------------------------------------
// Agent metadata
// ---------------------------------------------------------------------------

const AGENT_META: Record<
    string,
    { label: string; Icon: React.ComponentType<any>; dotColor: string; badgeColor: string }
> = {
    orchestrator: {
        label: 'orchestrator',
        Icon: GitFork,
        dotColor: 'bg-emerald-400',
        badgeColor: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/25',
    },
    analysis: {
        label: 'analysis',
        Icon: BarChart2,
        dotColor: 'bg-emerald-400',
        badgeColor: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/25',
    },
    cleaning: {
        label: 'cleaning',
        Icon: Wand2,
        dotColor: 'bg-emerald-400',
        badgeColor: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/25',
    },
    graph: {
        label: 'graph',
        Icon: Layers,
        dotColor: 'bg-amber-400',
        badgeColor: 'text-amber-300 bg-amber-500/15 border-amber-500/25',
    },
    qa: {
        label: 'qa',
        Icon: MessageSquare,
        dotColor: 'bg-sky-400',
        badgeColor: 'text-sky-300 bg-sky-500/15 border-sky-500/25',
    },
    linking: {
        label: 'linking',
        Icon: Link2,
        dotColor: 'bg-orange-400',
        badgeColor: 'text-orange-300 bg-orange-500/15 border-orange-500/25',
    },
    sql: {
        label: 'sql',
        Icon: Database,
        dotColor: 'bg-cyan-400',
        badgeColor: 'text-cyan-300 bg-cyan-500/15 border-cyan-500/25',
    },
    sql_agent: {
        label: 'sql',
        Icon: Database,
        dotColor: 'bg-cyan-400',
        badgeColor: 'text-cyan-300 bg-cyan-500/15 border-cyan-500/25',
    },
    csv: {
        label: 'csv',
        Icon: FileSpreadsheet,
        dotColor: 'bg-teal-400',
        badgeColor: 'text-teal-300 bg-teal-500/15 border-teal-500/25',
    },
    csv_agent: {
        label: 'csv',
        Icon: FileSpreadsheet,
        dotColor: 'bg-teal-400',
        badgeColor: 'text-teal-300 bg-teal-500/15 border-teal-500/25',
    },
    domain_qa: {
        label: 'domain_qa',
        Icon: Globe,
        dotColor: 'bg-emerald-400',
        badgeColor: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/25',
    },
    general_analysis: {
        label: 'analysis',
        Icon: TrendingUp,
        dotColor: 'bg-emerald-400',
        badgeColor: 'text-emerald-300 bg-emerald-500/15 border-emerald-500/25',
    },
    integration: {
        label: 'integration',
        Icon: Plug,
        dotColor: 'bg-rose-400',
        badgeColor: 'text-rose-300 bg-rose-500/15 border-rose-500/25',
    },
};

export function agentMeta(agent?: string) {
    const key = (agent || '').toLowerCase();
    return (
        AGENT_META[key] ?? {
            label: key || 'agent',
            Icon: Brain,
            dotColor: 'bg-slate-400',
            badgeColor: 'text-slate-300 bg-slate-500/15 border-slate-500/25',
        }
    );
}

// ---------------------------------------------------------------------------
// Step summary — human-readable one-liner per tool call
// ---------------------------------------------------------------------------

function stepSummary(action: string, input: any): string {
    const str = (v: any) => (typeof v === 'string' ? v : (JSON.stringify(v) ?? ''));
    switch (action) {
        case 'call_agent': {
            const ag = input?.agent ?? '';
            const prompt = String(input?.prompt ?? '').slice(0, 64);
            return `→ ${ag} agent${prompt ? `: ${prompt}` : ''}`;
        }
        case 'run_python':
            return 'Running Python';
        case 'run_sql':
            return 'Running SQL query';
        case 'get_schema':
            return 'Inspecting schema';
        case 'get_sample_rows':
            return `Sampling rows`;
        case 'compute_stats':
            return `Stats: ${input?.column ?? str(input)}`;
        case 'list_files':
        case 'list_workspace_files':
            return 'Listing workspace files';
        case 'read_file':
        case 'read_workspace_file':
            return `Reading ${input?.path ?? str(input)}`;
        case 'fetch_opendata_to_workspace':
            return `Downloading dataset → ${input?.filename ?? str(input)}`;
        case 'write_csv_to_spreadsheet':
            return `Exporting ${input?.filename ?? 'CSV'} → Google Sheets`;
        case 'write_file':
            return `Writing ${input?.path ?? ''}`;
        case 'run_script':
            return `Running script`;
        case 'sql_read':
            return 'SQL read';
        case 'sql_preview':
            return 'SQL preview';
        case 'sql_materialize_copy':
            return 'Materializing copy table';
        case 'sql_write_temp':
        case 'sql_write_copy':
            return 'Writing SQL result';
        case 'create_copy_table':
            return 'Creating copy table';
        case 'create_view':
            return 'Creating view';
        case 'final_answer':
            return 'Writing response';
        case 'recall_memory':
            return 'Searching memory';
        case 'needs_cleaning':
            return 'Requesting data cleaning';
        case 'question':
            return `Asking: ${String(input?.question ?? str(input)).slice(0, 60)}`;
        default:
            return action;
    }
}

// ---------------------------------------------------------------------------
// Single step row
// ---------------------------------------------------------------------------

export function StepItem({
    step,
    isLive,
    isLastActive,
    isDark,
    staggerDelay = 0,
}: {
    step: StepRow;
    isLive: boolean;
    isLastActive: boolean;
    isDark: boolean;
    staggerDelay?: number;
}) {
    const [expanded, setExpanded] = useState(false);

    const meta = agentMeta(step.agent);
    const action = step.action ?? '…';
    const summary = stepSummary(action, step.input);
    const isDone =
        step.phase === 'end' ||
        step.phase === 'final' ||
        step.phase === 'pause' ||
        step.phase === 'permission';
    const isError = step.phase === 'error';
    const isRunning = isLive && isLastActive && step.phase === 'start';

    // Observation preview
    const obsText = step.observation ? String(step.observation) : '';
    const obsPreview = obsText.length > 2000 ? obsText.slice(0, 2000) + '…' : obsText;

    // Thought preview
    const thoughtText = step.thought ? String(step.thought) : '';
    const thoughtPreview =
        thoughtText.length > 1000 ? thoughtText.slice(0, 1000) + '…' : thoughtText;

    // Input — stringify objects for display; skip trivial string inputs that duplicate the summary
    const rawInput = step.input;
    const inputDisplay =
        rawInput !== undefined && rawInput !== null && rawInput !== ''
            ? typeof rawInput === 'object'
                ? JSON.stringify(rawInput, null, 2).slice(0, 3000)
                : String(rawInput).slice(0, 3000)
            : null;

    const hasDetails = !!(thoughtText || obsText || inputDisplay);

    return (
        <div
            className="step-slide-in"
            style={staggerDelay ? { animationDelay: `${staggerDelay}ms` } : undefined}
        >
            <button
                type="button"
                onClick={() => hasDetails && setExpanded((e) => !e)}
                className={`relative w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg text-left transition-colors ${
                    hasDetails
                        ? isDark
                            ? 'hover:bg-white/[0.04] cursor-pointer'
                            : 'hover:bg-slate-50 cursor-pointer'
                        : 'cursor-default'
                }`}
            >
                {/* Running-step accent rail */}
                {isRunning && (
                    <span
                        className={`accent-rail absolute left-0 top-1.5 bottom-1.5 w-[2px] rounded-full ${meta.dotColor}`}
                    />
                )}

                {/* Agent dot */}
                <span className="flex-shrink-0 relative flex items-center justify-center w-5 h-5">
                    <span
                        className={`w-2 h-2 rounded-full ${meta.dotColor} ${isRunning ? 'step-pulse' : ''} ${isDone && !isError ? 'opacity-60' : ''} ${isError ? 'bg-red-400' : ''}`}
                    />
                </span>

                {/* Agent badge */}
                <span
                    className={`flex-shrink-0 text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border ${meta.badgeColor}`}
                >
                    {meta.label}
                </span>

                {/* Summary */}
                <span
                    className={`flex-1 text-[12px] truncate ${
                        isDark
                            ? isDone
                                ? 'text-white/50'
                                : 'text-white/80'
                            : isDone
                              ? 'text-slate-400'
                              : 'text-slate-700'
                    }`}
                >
                    {summary}
                </span>

                {/* Status icon */}
                <span className="flex-shrink-0 w-4 h-4 flex items-center justify-center">
                    {isRunning ? (
                        <Loader2
                            className={`w-3 h-3 animate-spin ${isDark ? 'text-white/40' : 'text-slate-400'}`}
                        />
                    ) : isError ? (
                        <X className="icon-pop w-3 h-3 text-red-400" />
                    ) : step.phase === 'pause' || step.phase === 'permission' ? (
                        <Pause
                            className={`icon-pop w-3 h-3 ${isDark ? 'text-amber-400' : 'text-amber-500'}`}
                        />
                    ) : isDone ? (
                        <Check
                            className={`icon-pop w-3 h-3 ${isDark ? 'text-white/30' : 'text-slate-300'}`}
                        />
                    ) : hasDetails ? (
                        expanded ? (
                            <ChevronDown
                                className={`w-3 h-3 ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                            />
                        ) : (
                            <ChevronRight
                                className={`w-3 h-3 ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                            />
                        )
                    ) : null}
                </span>
            </button>

            {/* Expanded details */}
            {hasDetails && (
                <div className={`collapse-grid ${expanded ? 'open' : ''}`}>
                    <div>
                        <div
                            className={`mx-2 mb-1.5 px-3 py-2 rounded-lg text-[11px] space-y-2 ${
                                isDark
                                    ? 'bg-white/[0.03] border border-white/[0.06]'
                                    : 'bg-slate-50 border border-slate-100'
                            }`}
                        >
                            {thoughtPreview && (
                                <div>
                                    <span
                                        className={`font-semibold uppercase tracking-wide text-[10px] ${isDark ? 'text-white/35' : 'text-slate-400'}`}
                                    >
                                        Thought
                                    </span>
                                    <p
                                        className={`mt-0.5 leading-relaxed whitespace-pre-wrap ${isDark ? 'text-white/60' : 'text-slate-600'}`}
                                    >
                                        {thoughtPreview}
                                    </p>
                                </div>
                            )}
                            {inputDisplay && (
                                <div>
                                    <span
                                        className={`font-semibold uppercase tracking-wide text-[10px] ${isDark ? 'text-white/35' : 'text-slate-400'}`}
                                    >
                                        Input
                                    </span>
                                    <pre
                                        className={`mt-0.5 text-[10px] leading-relaxed whitespace-pre-wrap font-mono break-all overflow-auto max-h-48 ${isDark ? 'text-white/55' : 'text-slate-600'}`}
                                    >
                                        {inputDisplay}
                                    </pre>
                                </div>
                            )}
                            {obsPreview && (
                                <div>
                                    <span
                                        className={`font-semibold uppercase tracking-wide text-[10px] ${isDark ? 'text-white/35' : 'text-slate-400'}`}
                                    >
                                        Output
                                    </span>
                                    <pre
                                        className={`mt-0.5 text-[10px] leading-relaxed whitespace-pre-wrap font-mono break-all overflow-auto max-h-64 ${isDark ? 'text-white/55' : 'text-slate-600'}`}
                                    >
                                        {obsPreview}
                                    </pre>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function StepTimeline({ steps, isLive = false, isDark = true }: StepTimelineProps) {
    if (!steps.length) return null;

    const lastIdx = steps.length - 1;

    return (
        <div
            className={`w-full rounded-xl border py-1 ${
                isDark
                    ? 'border-white/[0.07] bg-white/[0.025]'
                    : 'border-slate-100 bg-white shadow-sm'
            }`}
        >
            {steps.map((step, idx) => (
                <StepItem
                    key={`${step.step}-${step.agent}-${idx}`}
                    step={step}
                    isLive={isLive}
                    isLastActive={isLive && idx === lastIdx}
                    isDark={isDark}
                />
            ))}
        </div>
    );
}
