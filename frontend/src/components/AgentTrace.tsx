/**
 * AgentTrace — hierarchical, collapsible ReAct trace driven by a single flat,
 * ordered list of parts with stable ids + `parentId` links.
 *
 * Top-level parts (orchestrator steps, `parentId == null`) render in arrival
 * order. A part's children (parts whose `parentId === part.id`) render as a
 * nested collapsible sub-agent block at that exact position — exact,
 * collision-free nesting that is identical live and after persistence (no
 * arrival-order heuristic, no step-number collisions across sub-agents).
 *
 * One component for BOTH the live stream and persisted messages. `isLive` only
 * drives the running pulse on the most-recent part, so the live tree and the
 * committed tree are structurally identical → the block never reflows or flashes.
 */

import { useState } from 'react';
import { ChevronRight, CornerDownRight } from 'lucide-react';

import type { TracePart } from '../store/useStore';
import { StepItem, agentMeta, type StepRow } from './StepTimeline';
import FoldLoader from './FoldLoader';

// ---------------------------------------------------------------------------
// Intermediary loading block — fills the dead air between a finished step and
// the next one arriving. Deliberately styled as a "not-yet-a-block": more
// transparent, dashed edge, breathing, so it reads as a placeholder in flux
// rather than a committed trace row. The variant tailors it to *which* gap it
// bridges so each hand-off in the trace reads a little differently:
//   • orchestrator   — the planner deciding its next step (top level)
//   • subagent-start — the orchestrator handing control down to a sub-agent
//   • subagent       — a sub-agent deciding its own next step (nested)
// ---------------------------------------------------------------------------

type LoaderVariant = 'orchestrator' | 'subagent-start' | 'subagent';

function TraceLoadingBlock({
    variant,
    agentLabel,
}: {
    variant: LoaderVariant;
    agentLabel?: string;
}) {
    const label =
        variant === 'orchestrator'
            ? 'Planning the next step'
            : variant === 'subagent-start'
              ? `Handing off to the ${agentLabel ?? 'sub'} agent`
              : `${agentLabel ?? 'Sub'} agent is thinking`;

    // Indent tracks nesting depth: top-level flush with orchestrator rows,
    // handoff aligned with the sub-agent block that's about to form, and the
    // in-flight sub-agent gap sitting inside its block.
    const margin =
        variant === 'orchestrator'
            ? 'mx-2 mt-0.5'
            : variant === 'subagent-start'
              ? 'ml-6 mt-0.5'
              : 'mx-2';

    return (
        <div
            className={`${margin} mb-1 flex items-center gap-2 px-2.5 py-2 rounded-lg trace-loading-block step-slide-in`}
        >
            {variant === 'subagent-start' && (
                <CornerDownRight
                    className="w-3 h-3 flex-shrink-0"
                    style={{ color: 'var(--c-signal)' }}
                />
            )}
            <FoldLoader
                size={variant === 'orchestrator' ? 20 : 18}
                color="var(--c-signal)"
                accent="var(--c-signal-text)"
                speed={variant === 'subagent' ? 1.3 : 1.15}
            />
            <span className="trace-loading-shimmer text-[11px] font-medium tracking-wide">
                {label}
            </span>
        </div>
    );
}

/** Best-effort sub-agent name from a `call_agent` step's input, which may be an
 *  object or a truncated preview string. */
function agentFromInput(input: unknown): string | undefined {
    if (input && typeof input === 'object' && 'agent' in (input as Record<string, unknown>)) {
        const a = (input as Record<string, unknown>).agent;
        return a ? String(a) : undefined;
    }
    if (typeof input === 'string') {
        const m = /['"]?agent['"]?\s*[:=]\s*['"]?([a-z_]+)/i.exec(input);
        return m ? m[1] : undefined;
    }
    return undefined;
}

// ---------------------------------------------------------------------------
// Gap detection — where (if anywhere) to show the loading block
// ---------------------------------------------------------------------------

export type TraceGap =
    | { level: 'orchestrator' }
    | { level: 'subagent-start' | 'subagent'; parentId: string; agentLabel: string }
    | null;

/**
 * Locate the single live "in-between" gap in a trace, if one exists.
 *
 * Parts upsert *in place*, so array position ≠ recency — but orchestrator steps
 * are created strictly in order and never reparented, so the last top-level part
 * is always the current frontier of the plan. We walk down its active branch to
 * classify the four hand-offs:
 *   • orchestrator→orchestrator / subagent→orchestrator → orch step 'end'      → 'orchestrator'
 *   • orchestrator→subagent      (call_agent 'start', no children yet)          → 'subagent-start'
 *   • subagent→subagent          (call_agent 'start', last child 'end')         → 'subagent'
 * A running step ('start') already spins its own row; 'pause'/'permission'/
 * 'error'/'final' aren't "more is coming" states and show no loader.
 */
export function computeTraceGap(
    parts: TracePart[],
    opts: { isLive: boolean; isStreaming: boolean }
): TraceGap {
    if (!opts.isLive || opts.isStreaming || parts.length === 0) return null;
    const ids = new Set(parts.map((p) => p.id));
    const topLevel = parts.filter((p) => p.parentId == null || !ids.has(p.parentId));
    const currentOrch = topLevel[topLevel.length - 1] ?? null;
    if (!currentOrch) return null;

    const kids = parts.filter((p) => p.parentId === currentOrch.id);
    const isAgentCall = currentOrch.action === 'call_agent' || kids.length > 0;
    if (currentOrch.phase === 'start' && isAgentCall) {
        const agentLabel = agentMeta(kids[0]?.agent ?? agentFromInput(currentOrch.input)).label;
        if (kids.length === 0) {
            return { level: 'subagent-start', parentId: currentOrch.id, agentLabel };
        }
        if (kids[kids.length - 1].phase === 'end') {
            return { level: 'subagent', parentId: currentOrch.id, agentLabel };
        }
        return null; // last child 'start' → running; 'final'/'pause' → returning/paused
    }
    if (currentOrch.phase === 'end') return { level: 'orchestrator' };
    return null;
}

// ---------------------------------------------------------------------------
// Legacy adapter — render messages persisted before the parts model existed
// ---------------------------------------------------------------------------

/**
 * Convert a pre-parts message's metadata (orchestrator_reasoning_trace +
 * reasoning_trace + step-keyed sub_agent_traces) into a parts tree, so the single
 * AgentTrace renderer also covers history written before this change.
 */
export function partsFromLegacy(metadata: Record<string, any> | undefined): TracePart[] {
    if (!metadata) return [];
    const orch = Array.isArray(metadata.orchestrator_reasoning_trace)
        ? metadata.orchestrator_reasoning_trace
        : [];
    const flatSub = Array.isArray(metadata.reasoning_trace) ? metadata.reasoning_trace : [];
    const subTraces = (metadata.sub_agent_traces ?? {}) as Record<string | number, any[]>;
    const parts: TracePart[] = [];

    // No orchestrator wrapper — a flat sub-agent trace.
    if (orch.length === 0) {
        flatSub.forEach((s: any, i: number) =>
            parts.push({ ...s, id: `legacy-sub-${i}`, parentId: null })
        );
        return parts;
    }

    orch.forEach((s: any) => {
        const oid = `legacy-orch-${s.step}`;
        parts.push({ ...s, id: oid, parentId: null, agent: s.agent ?? 'orchestrator' });
        const kids = subTraces[s.step] ?? subTraces[String(s.step)] ?? [];
        kids.forEach((k: any, i: number) =>
            parts.push({
                ...k,
                id: `${oid}-${i}`,
                parentId: oid,
                agent: k.agent ?? s.input?.agent ?? 'agent',
            })
        );
    });
    return parts;
}

// ---------------------------------------------------------------------------
// Step-count label — the number re-mounts (keyed) on change so it ticks/rolls
// ---------------------------------------------------------------------------

function StepCount({ count, className }: { count: number; className: string }) {
    return (
        <span className={className}>
            <span key={count} className="count-tick inline-block">
                {count}
            </span>{' '}
            step{count !== 1 ? 's' : ''}
        </span>
    );
}

// ---------------------------------------------------------------------------
// Nested sub-agent block (collapsible)
// ---------------------------------------------------------------------------

function SubAgentBlock({
    steps,
    agentName,
    isLive,
    lastPartId,
    isDark,
    showLoader = false,
}: {
    steps: TracePart[];
    agentName: string;
    isLive: boolean;
    lastPartId: string | null;
    isDark: boolean;
    showLoader?: boolean;
}) {
    const [open, setOpen] = useState(true);
    const meta = agentMeta(agentName);

    return (
        <div
            className={`ml-6 mt-0.5 mb-1 rounded-lg border overflow-hidden step-slide-in ${
                isDark ? 'border-white/[0.06] bg-white/[0.02]' : 'border-slate-100 bg-slate-50/50'
            }`}
        >
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className={`group w-full flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] transition-colors ${
                    isDark ? 'hover:bg-white/[0.04]' : 'hover:bg-slate-100/60'
                }`}
            >
                <ChevronRight
                    className={`w-3 h-3 flex-shrink-0 transition-transform ${open ? 'rotate-90' : 'group-hover:translate-x-0.5'} ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                />
                <span
                    className={`font-semibold uppercase tracking-wide ${meta.badgeColor.split(' ')[0]}`}
                >
                    {meta.label}
                </span>
                <span className={isDark ? 'text-white/30' : 'text-slate-400'}>trace</span>
                {isLive && (
                    <span
                        className={`ml-auto step-pulse text-[10px] ${meta.badgeColor.split(' ')[0]}`}
                    >
                        ●
                    </span>
                )}
                <StepCount
                    count={steps.length}
                    className={`${isLive ? '' : 'ml-auto'} text-[10px] ${isDark ? 'text-white/20' : 'text-slate-400'}`}
                />
            </button>

            <div className={`collapse-grid ${open ? 'open' : ''}`}>
                <div>
                    <div className="py-0.5">
                        {steps.map((step, i) => (
                            <StepItem
                                key={step.id}
                                step={step as StepRow}
                                isLive={isLive}
                                isLastActive={isLive && step.id === lastPartId}
                                isDark={isDark}
                                staggerDelay={isLive ? 0 : Math.min(i, 12) * 30}
                            />
                        ))}
                        {showLoader && (
                            <TraceLoadingBlock variant="subagent" agentLabel={meta.label} />
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface AgentTraceProps {
    parts: TracePart[];
    isLive?: boolean;
    isDark?: boolean;
    /** True once the final answer is streaming — suppresses the between-steps
        loader so it doesn't linger alongside the streaming bubble. */
    isStreaming?: boolean;
}

export default function AgentTrace({
    parts,
    isLive = false,
    isDark = true,
    isStreaming = false,
}: AgentTraceProps) {
    const [open, setOpen] = useState(true);

    if (!parts || parts.length === 0) return null;

    // Top-level = no parent, or a parent that isn't present (defensive: never drop
    // an orphan sub-agent step). Children are grouped under their parent's id.
    const ids = new Set(parts.map((p) => p.id));
    const topLevel = parts.filter((p) => p.parentId == null || !ids.has(p.parentId));
    const childrenOf = (id: string) => parts.filter((p) => p.parentId === id);
    // The most-recent part overall is the one that pulses while still running.
    const lastPartId = parts[parts.length - 1]?.id ?? null;

    // The one live "in-between" gap to bridge with a loading block, if any.
    const gap = computeTraceGap(parts, { isLive, isStreaming });

    return (
        <div
            className={`w-full mb-1 rounded-xl border overflow-hidden step-slide-in ${
                isDark
                    ? 'border-white/[0.07] bg-white/[0.025]'
                    : 'border-slate-100 bg-white shadow-sm'
            }`}
        >
            {/* Outer header */}
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className={`group w-full flex items-center gap-2 px-3 py-2 text-[11px] transition-colors ${
                    isDark ? 'hover:bg-white/[0.04]' : 'hover:bg-slate-50'
                }`}
            >
                <ChevronRight
                    className={`w-3.5 h-3.5 flex-shrink-0 transition-transform ${open ? 'rotate-90' : 'group-hover:translate-x-0.5'} ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                />
                <span
                    className={`font-semibold uppercase tracking-wide ${isDark ? 'text-white/40' : 'text-slate-500'}`}
                >
                    Agent trace
                </span>
                <StepCount
                    count={parts.length}
                    className={`text-[10px] ${isDark ? 'text-white/20' : 'text-slate-400'}`}
                />
                {isLive && (
                    <span
                        className="ml-auto step-pulse text-[10px]"
                        style={{ color: 'var(--c-signal)' }}
                    >
                        ●
                    </span>
                )}
            </button>

            <div className={`collapse-grid ${open ? 'open' : ''}`}>
                <div>
                    <div className="pb-1">
                        {topLevel.map((part, i) => {
                            const kids = childrenOf(part.id);
                            const subAgentName =
                                kids[0]?.agent ?? agentFromInput(part.input) ?? 'agent';
                            return (
                                <div key={part.id}>
                                    <StepItem
                                        step={part as StepRow}
                                        isLive={isLive}
                                        isLastActive={isLive && part.id === lastPartId}
                                        isDark={isDark}
                                        staggerDelay={isLive ? 0 : Math.min(i, 12) * 30}
                                    />
                                    {kids.length > 0 && (
                                        <SubAgentBlock
                                            steps={kids}
                                            agentName={subAgentName}
                                            isLive={isLive}
                                            lastPartId={lastPartId}
                                            isDark={isDark}
                                            showLoader={
                                                gap?.level === 'subagent' &&
                                                gap.parentId === part.id
                                            }
                                        />
                                    )}
                                    {gap?.level === 'subagent-start' &&
                                        gap.parentId === part.id && (
                                            <TraceLoadingBlock
                                                variant="subagent-start"
                                                agentLabel={gap.agentLabel}
                                            />
                                        )}
                                </div>
                            );
                        })}
                        {gap?.level === 'orchestrator' && (
                            <TraceLoadingBlock variant="orchestrator" />
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
