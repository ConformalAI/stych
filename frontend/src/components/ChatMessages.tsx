import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { useStore, type TracePart } from '../store/useStore';
import { useShallow } from 'zustand/react/shallow';
import { useAuthStore } from '../store/useAuthStore';
import {
    Sparkles,
    User,
    Brain,
    BarChart2,
    Wand2,
    MessageSquare,
    GitFork,
    Layers,
    AlertCircle,
    FlaskConical,
    FileText,
    ArrowRight,
    Link2,
    Database,
    FileSpreadsheet,
    Globe,
    TrendingUp,
    Copy,
    CheckCheck,
    Clock,
    ChevronDown,
    Archive,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import ChartRenderer from './ChartRenderer';
import ChartsGallery from './ChartsGallery';
import AgentTrace, { partsFromLegacy } from './AgentTrace';
import LinkingInspectPanel from './LinkingInspect';
import VegaChart from './VegaChart';
import type { LinkingInspectContext } from '../services/api';

const VizEmbed = lazy(() => import('./visualize/VizCard'));
const OntologyEmbed = lazy(() => import('./ontology/OntologyCard'));

function linkingInspectFromMetadata(
    metadata?: Record<string, unknown>
): LinkingInspectContext | null {
    const raw = metadata?.linking_inspect;
    if (!raw || typeof raw !== 'object') return null;
    const ctx = raw as LinkingInspectContext;
    if (!ctx.left || !ctx.right || !ctx.plan) return null;
    return ctx;
}

interface ChatMessagesProps {
    isTyping: boolean;
    onActionSelect?: (action: {
        promptId: string;
        kind: string;
        value: string;
        label: string;
    }) => void;
}

const AGENT_META: Record<string, { label: string; Icon: React.ComponentType<any>; color: string }> =
    {
        orchestrator: { label: 'Orchestrator', Icon: GitFork, color: 'text-emerald-400' },
        analysis: { label: 'Analysis Agent', Icon: BarChart2, color: 'text-emerald-400' },
        cleaning: { label: 'Cleaning Agent', Icon: Wand2, color: 'text-emerald-400' },
        graph: { label: 'Graph Agent', Icon: Layers, color: 'text-amber-400' },
        qa: { label: 'Q&A Agent', Icon: MessageSquare, color: 'text-emerald-400' },
        linking: { label: 'Linking Agent', Icon: Link2, color: 'text-orange-400' },
        sql: { label: 'SQL Agent', Icon: Database, color: 'text-cyan-400' },
        sql_agent: { label: 'SQL Agent', Icon: Database, color: 'text-cyan-400' },
        csv: { label: 'CSV Agent', Icon: FileSpreadsheet, color: 'text-teal-400' },
        csv_agent: { label: 'CSV Agent', Icon: FileSpreadsheet, color: 'text-teal-400' },
        domain_qa: { label: 'Domain Q&A', Icon: Globe, color: 'text-emerald-400' },
        general_analysis: { label: 'Analysis Agent', Icon: TrendingUp, color: 'text-emerald-400' },
        formatter: { label: 'Formatter', Icon: Brain, color: 'text-emerald-400' },
    };

// ── Copy Button ───────────────────────────────────────────────────────────────
function CopyButton({
    text,
    className = '',
    style,
}: {
    text: string;
    className?: string;
    style?: React.CSSProperties;
}) {
    const [copied, setCopied] = useState(false);
    const tidRef = useRef<ReturnType<typeof setTimeout>>();

    useEffect(() => () => clearTimeout(tidRef.current), []);

    const handleCopy = () => {
        navigator.clipboard.writeText(text).catch(() => {});
        setCopied(true);
        clearTimeout(tidRef.current);
        tidRef.current = setTimeout(() => setCopied(false), 2000);
    };
    return (
        <button
            type="button"
            onClick={handleCopy}
            title={copied ? 'Copied!' : 'Copy'}
            className={`inline-flex items-center justify-center w-6 h-6 rounded-md transition-all duration-150 ${className}`}
            style={style}
        >
            {copied ? (
                <CheckCheck className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
                <Copy className="w-3.5 h-3.5" style={{ color: 'var(--c-mist-fg)' }} />
            )}
        </button>
    );
}

// ── Thinking content parser ───────────────────────────────────────────────────
/**
 * Splits a message that may contain <think>…</think> blocks into
 * { thinking, answer }.  Handles:
 *   - Complete blocks:   <think>…</think>answer text
 *   - Still-streaming:  <think>partial… (no closing tag yet)
 *   - No thinking:      answer text only
 */
const _THINK_OPEN = '<think>';
const _THINK_CLOSE = '</think>';

function parseThinkContent(content: string): { thinking: string | null; answer: string } {
    if (!content.startsWith(_THINK_OPEN)) return { thinking: null, answer: content };

    const closeIdx = content.indexOf(_THINK_CLOSE);
    if (closeIdx === -1) {
        // Stream hasn't closed the <think> block yet — everything is thinking
        return { thinking: content.slice(_THINK_OPEN.length), answer: '' };
    }

    const thinking = content.slice(_THINK_OPEN.length, closeIdx).trim();
    const answer = content.slice(closeIdx + _THINK_CLOSE.length).trim();
    return { thinking: thinking || null, answer };
}

// ── Collapsible thinking block ────────────────────────────────────────────────
interface ThinkingBlockProps {
    thinking: string;
    isStreaming?: boolean;
}

function ThinkingBlock({ thinking, isStreaming = false }: ThinkingBlockProps) {
    const [open, setOpen] = useState(isStreaming);

    // Sync open state with streaming: expand when streaming starts, collapse when done.
    useEffect(() => {
        setOpen(isStreaming);
    }, [isStreaming]);

    return (
        <div
            className="mb-3 rounded-xl border overflow-hidden"
            style={{
                borderColor: 'rgba(139,92,246,0.2)',
                background: 'rgba(139,92,246,0.05)',
            }}
        >
            {/* Header row */}
            <button
                type="button"
                onClick={() => setOpen((p) => !p)}
                className="w-full flex items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-white/5"
            >
                <Brain className="w-3.5 h-3.5 flex-shrink-0" style={{ color: 'var(--c-signal)' }} />
                <span
                    className="text-[11px] font-semibold uppercase tracking-wide flex-1"
                    style={{ color: 'var(--c-signal)' }}
                >
                    {isStreaming ? (
                        <span className="flex items-center gap-1.5">
                            Thinking
                            <span className="flex gap-0.5">
                                {[0, 160, 320].map((d) => (
                                    <span
                                        key={d}
                                        className="w-1 h-1 rounded-full typing-dot"
                                        style={{
                                            background: 'var(--c-signal)',
                                            animationDelay: `${d}ms`,
                                        }}
                                    />
                                ))}
                            </span>
                        </span>
                    ) : (
                        'Model reasoning'
                    )}
                </span>
                <ChevronDown
                    className={`w-3.5 h-3.5 flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
                    style={{ color: 'var(--c-signal-text)', opacity: 0.6 }}
                />
            </button>

            {/* Thinking content */}
            {open && (
                <div
                    className="px-3 pb-3 pt-1 text-xs leading-relaxed font-mono whitespace-pre-wrap"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    {thinking}
                    {isStreaming && <span className="streaming-cursor" aria-hidden="true" />}
                </div>
            )}
        </div>
    );
}

// ── Unified Working Panel (thinking + steps + timer) ─────────────────────────
interface WorkingPanelProps {
    thinkingAgent: { agent: string; message: string } | null;
    stepCount: number;
    elapsedSec: number;
    isDark: boolean;
}

function WorkingPanel({ thinkingAgent, stepCount, elapsedSec }: WorkingPanelProps) {
    const meta = thinkingAgent
        ? (AGENT_META[thinkingAgent.agent] ?? {
              label: thinkingAgent.agent,
              Icon: Brain,
              color: 'text-slate-400',
          })
        : { label: 'Processing', Icon: Sparkles, color: 'text-blue-400' };
    const { Icon } = meta;

    return (
        <div className="flex gap-3 justify-start msg-fade-in">
            {/* Avatar */}
            <div
                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center shadow-sm mt-1"
                style={{ background: 'linear-gradient(135deg, var(--c-signal), #0a9467)' }}
            >
                <Icon className="w-4 h-4 text-white" />
            </div>

            <div className="flex-1 min-w-0 max-w-[74%]">
                {/* Status card */}
                <div
                    className="rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm backdrop-blur-sm"
                    style={{
                        background: 'var(--c-glass)',
                        border: '1px solid var(--c-glass-border)',
                    }}
                >
                    {/* Header: agent name + elapsed timer */}
                    <div className="flex items-center justify-between gap-2 mb-1.5">
                        <div className="flex items-center gap-1.5">
                            <Icon className={`w-3.5 h-3.5 ${meta.color} flex-shrink-0`} />
                            <span
                                className={`text-[11px] font-semibold uppercase tracking-wide ${meta.color}`}
                            >
                                {meta.label}
                            </span>
                        </div>
                        {elapsedSec > 0 && (
                            <div
                                className="flex items-center gap-1 text-[10px]"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                <Clock className="w-3 h-3" />
                                <span>{elapsedSec}s</span>
                            </div>
                        )}
                    </div>

                    {/* Status message */}
                    {thinkingAgent?.message && (
                        <p
                            className="text-xs leading-relaxed mb-1.5"
                            style={{ color: 'var(--c-ink-2)' }}
                        >
                            {thinkingAgent.message}
                        </p>
                    )}

                    {/* Step count if steps have accumulated */}
                    {stepCount > 0 && (
                        <p className="text-[10px] mb-1.5" style={{ color: 'var(--c-mist-fg)' }}>
                            {stepCount} step{stepCount !== 1 ? 's' : ''} completed
                        </p>
                    )}

                    {/* Typing dots */}
                    <div className="flex items-center gap-1 mt-1">
                        {[0, 160, 320].map((d) => (
                            <span
                                key={d}
                                className="w-1.5 h-1.5 rounded-full typing-dot"
                                style={{ background: 'var(--c-signal)', animationDelay: `${d}ms` }}
                            />
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}

// ── Compaction indicator (transient — shown while older turns are summarized) ──
function CompactionBar() {
    return (
        <div className="flex gap-3 justify-start msg-fade-in">
            <div
                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center shadow-sm mt-1"
                style={{ background: 'linear-gradient(135deg, var(--c-signal), #0a9467)' }}
            >
                <Archive className="w-4 h-4 text-white" />
            </div>
            <div className="flex-1 min-w-0 max-w-[74%]">
                <div
                    className="rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm backdrop-blur-sm"
                    style={{
                        background: 'var(--c-glass)',
                        border: '1px solid var(--c-glass-border)',
                    }}
                >
                    <p className="text-xs mb-2" style={{ color: 'var(--c-ink-2)' }}>
                        Compacting conversation history…
                    </p>
                    <div className="progress-bar-track">
                        <div className="progress-bar-fill" />
                    </div>
                </div>
            </div>
        </div>
    );
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function ChatMessages({ isTyping, onActionSelect }: ChatMessagesProps) {
    const {
        messages,
        chatTheme,
        resolvedActionPromptIds,
        thinkingAgent,
        streamingAgent,
        liveTraceParts,
        streamingContent,
        streamingThinkingContent,
        isCompacting,
        sessionId,
    } = useStore(
        useShallow((s) => ({
            messages: s.messages,
            chatTheme: s.chatTheme,
            resolvedActionPromptIds: s.resolvedActionPromptIds,
            thinkingAgent: s.thinkingAgent,
            streamingAgent: s.streamingAgent,
            liveTraceParts: s.liveTraceParts,
            streamingContent: s.streamingContent,
            streamingThinkingContent: s.streamingThinkingContent,
            isCompacting: s.isCompacting,
            sessionId: s.sessionId,
        }))
    );
    const userProfilePicture = useAuthStore((s) => s.user?.profile_picture ?? null);
    const [customActionInput, setCustomActionInput] = useState<Record<string, string>>({});
    const isDark = chatTheme === 'dark';

    // Elapsed timer — starts when isTyping becomes true, keeps running through streaming
    const [elapsedSec, setElapsedSec] = useState(0);
    const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const startTimeRef = useRef<number | null>(null);

    useEffect(() => {
        if (isTyping) {
            if (startTimeRef.current === null) {
                startTimeRef.current = Date.now();
                timerRef.current = setInterval(() => {
                    setElapsedSec(Math.floor((Date.now() - startTimeRef.current!) / 1000));
                }, 1000);
            }
        } else {
            if (timerRef.current) {
                clearInterval(timerRef.current);
                timerRef.current = null;
            }
            startTimeRef.current = null;
            setElapsedSec(0);
        }
        return () => {
            if (timerRef.current) clearInterval(timerRef.current);
        };
    }, [isTyping]);

    // Code block renderer — shared between final messages and streaming bubble
    const buildCodeComponents = (allowCharts: boolean) => ({
        code: ({ node: _node, inline, className, children, ...props }: any) => {
            const match = /language-([\w-]+)/.exec(className || '');
            const language = match ? match[1] : '';
            const codeValue = String(children);
            const isInline = inline === true || (!className && !codeValue.includes('\n'));

            // Live persisted visualization: {"viz_id": "…"} or an array of them.
            if (allowCharts && language === 'viz') {
                try {
                    return (
                        <Suspense
                            fallback={
                                <div
                                    className="my-3 h-32 rounded-xl border flex items-center justify-center text-xs"
                                    style={{
                                        borderColor: 'var(--c-glass-border)',
                                        color: 'var(--c-mist-fg)',
                                    }}
                                >
                                    Loading visualization…
                                </div>
                            }
                        >
                            <VizEmbed payload={JSON.parse(String(children))} />
                        </Suspense>
                    );
                } catch {
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                }
            }

            // Agent-proposed semantic definitions awaiting review.
            if (language === 'ontology') {
                try {
                    return (
                        <Suspense
                            fallback={
                                <div
                                    className="my-3 h-24 rounded-xl border flex items-center justify-center text-xs"
                                    style={{
                                        borderColor: 'var(--c-glass-border)',
                                        color: 'var(--c-mist-fg)',
                                    }}
                                >
                                    Loading proposal…
                                </div>
                            }
                        >
                            <OntologyEmbed payload={JSON.parse(String(children))} />
                        </Suspense>
                    );
                } catch {
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                }
            }

            if (allowCharts && language === 'chart') {
                try {
                    return <ChartRenderer spec={JSON.parse(String(children))} />;
                } catch {
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                }
            }

            if (allowCharts && (language === 'vega' || language === 'vega-lite')) {
                try {
                    return <VegaChart spec={JSON.parse(String(children))} />;
                } catch {
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                }
            }

            if (allowCharts && language === 'charts') {
                try {
                    const specs = JSON.parse(String(children));
                    if (Array.isArray(specs)) return <ChartsGallery charts={specs} />;
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                } catch {
                    return (
                        <pre>
                            <code>{children}</code>
                        </pre>
                    );
                }
            }

            if (isInline) {
                return (
                    <code className={className} {...props}>
                        {children}
                    </code>
                );
            }

            // Fenced code block — persistent header bar with language label + copy button
            const codeText = String(children).replace(/\n$/, '');
            const langLabel = language || 'code';
            return (
                <div
                    className="rounded-lg overflow-hidden my-2"
                    style={{
                        background: 'var(--c-slate)',
                        border: '1px solid var(--c-glass-border)',
                    }}
                >
                    {/* Header bar */}
                    <div
                        className="flex items-center justify-between px-3 py-1.5 border-b"
                        style={{
                            background: 'rgba(0,0,0,0.2)',
                            borderColor: 'var(--c-glass-border)',
                        }}
                    >
                        <span className="text-[11px] font-mono font-medium text-slate-400 select-none">
                            {langLabel}
                        </span>
                        <CopyButton
                            text={codeText}
                            className="text-slate-400 hover:text-slate-200 hover:bg-white/10 rounded"
                        />
                    </div>
                    {/* Code */}
                    <pre className="!my-0 !rounded-none !border-0 overflow-x-auto">
                        <code className={className} {...props}>
                            {children}
                        </code>
                    </pre>
                </div>
            );
        },
        table: ({ children }: any) => (
            <div className="overflow-x-auto my-3 rounded-lg">
                <table>{children}</table>
            </div>
        ),
        // react-markdown already gives fenced code a block-level AST node. The
        // custom code renderer owns that wrapper, so retaining the default
        // <pre> would create nested block elements and invalid HTML.
        pre: ({ children }: any) => <>{children}</>,
    });
    // ReactMarkdown treats renderer functions as component types. Keep these
    // maps stable so chat streaming renders do not remount persisted VizCards.
    const [persistedMarkdownComponents] = useState(() => buildCodeComponents(true));
    const [streamingMarkdownComponents] = useState(() => buildCodeComponents(false));

    return (
        <div className="flex-1 overflow-y-auto px-4 py-6 space-y-5 scroll-smooth">
            {messages.map((message, index) => {
                const msgKey =
                    message.timestamp instanceof Date
                        ? `${message.timestamp.getTime()}-${index}`
                        : `${String(message.timestamp)}-${index}`;

                /* ── Error bubble ── */
                if (message.role === 'error') {
                    return (
                        <div key={msgKey} className="flex gap-3 justify-start msg-fade-in">
                            <div
                                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center mt-1"
                                style={{
                                    background: 'rgba(248,113,113,0.15)',
                                    border: '1px solid rgba(248,113,113,0.3)',
                                }}
                            >
                                <AlertCircle className="w-4 h-4 text-red-400" />
                            </div>
                            <div className="max-w-[74%] flex flex-col gap-1 items-start">
                                <div
                                    className="rounded-2xl rounded-tl-sm px-4 py-3"
                                    style={{
                                        background: 'rgba(248,113,113,0.1)',
                                        border: '1px solid rgba(248,113,113,0.2)',
                                    }}
                                >
                                    <p className="text-sm leading-relaxed text-red-300">
                                        {message.content}
                                    </p>
                                </div>
                                <span
                                    className="text-[11px] px-1"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    {new Date(message.timestamp).toLocaleTimeString([], {
                                        hour: '2-digit',
                                        minute: '2-digit',
                                    })}
                                </span>
                            </div>
                        </div>
                    );
                }

                /* ── Compaction notice ── small persistent divider left behind after
                     older turns are folded into the running summary. */
                if (message.metadata?.type === 'compaction_notice') {
                    return (
                        <div
                            key={msgKey}
                            className="flex items-center justify-center gap-2 py-1 msg-fade-in"
                        >
                            <div
                                className="h-px flex-1 max-w-[80px]"
                                style={{ background: 'var(--c-glass-border)' }}
                            />
                            <span
                                className="inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full"
                                style={{
                                    color: 'var(--c-mist-fg)',
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                }}
                            >
                                <Archive className="w-3 h-3" />
                                {message.content}
                            </span>
                            <div
                                className="h-px flex-1 max-w-[80px]"
                                style={{ background: 'var(--c-glass-border)' }}
                            />
                        </div>
                    );
                }

                /* ── EDA prompt card ── */
                if (message.metadata?.type === 'eda_prompt') {
                    const { file_id, filename, rows, columns } = message.metadata as any;
                    const promptId = `eda_${file_id}`;
                    if (resolvedActionPromptIds.includes(promptId)) return null;
                    return (
                        <div key={msgKey} className="flex gap-3 justify-start msg-fade-in">
                            <div
                                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center mt-1"
                                style={{
                                    background: 'rgba(251,191,36,0.15)',
                                    border: '1px solid rgba(251,191,36,0.3)',
                                }}
                            >
                                <FlaskConical className="w-4 h-4 text-amber-300" />
                            </div>
                            <div className="max-w-[480px] w-full">
                                <div
                                    className="rounded-2xl rounded-tl-sm px-5 py-4 shadow-sm"
                                    style={{
                                        background: 'var(--c-glass)',
                                        border: '1px solid var(--c-glass-border)',
                                    }}
                                >
                                    {/* File badge */}
                                    <div className="flex items-center gap-2 mb-3">
                                        <div
                                            className="flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium"
                                            style={{
                                                background: 'var(--c-signal-dim)',
                                                border: '1px solid color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                                color: 'var(--c-signal-text)',
                                            }}
                                        >
                                            <FileText className="w-3 h-3" />
                                            {filename}
                                        </div>
                                        <span
                                            className="text-xs"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {Number(rows).toLocaleString()} rows · {columns} cols
                                        </span>
                                    </div>
                                    <p
                                        className="text-sm font-semibold mb-1"
                                        style={{ color: 'var(--c-ink)' }}
                                    >
                                        Ready to explore your dataset?
                                    </p>
                                    <p
                                        className="text-xs leading-relaxed mb-4"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        Run an automated Exploratory Data Analysis to get a full
                                        profile, charts, missing value report, and AI-powered next
                                        steps.
                                    </p>
                                    <div className="flex items-center gap-2">
                                        <button
                                            type="button"
                                            onClick={() =>
                                                onActionSelect?.({
                                                    promptId,
                                                    kind: 'run_eda',
                                                    value: 'run',
                                                    label: 'Run Initial EDA',
                                                })
                                            }
                                            className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-semibold transition-colors"
                                            style={{
                                                background: 'rgba(251,191,36,0.15)',
                                                border: '1px solid rgba(251,191,36,0.3)',
                                                color: '#fcd34d',
                                            }}
                                        >
                                            <FlaskConical className="w-3.5 h-3.5" />
                                            Run Initial EDA
                                            <ArrowRight className="w-3 h-3" />
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                onActionSelect?.({
                                                    promptId,
                                                    kind: 'run_eda',
                                                    value: 'skip',
                                                    label: 'Skip',
                                                })
                                            }
                                            className="text-xs px-3 py-2 rounded-lg transition-colors hover:bg-white/5"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            Skip
                                        </button>
                                    </div>
                                </div>
                                <span
                                    className="text-[11px] px-1 mt-1 block"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    {new Date(message.timestamp).toLocaleTimeString([], {
                                        hour: '2-digit',
                                        minute: '2-digit',
                                    })}
                                </span>
                            </div>
                        </div>
                    );
                }

                /* ── User / Assistant bubble ── */
                return (
                    <div
                        key={msgKey}
                        className={`flex gap-3 msg-fade-in group/msg ${
                            message.role === 'user' ? 'justify-end' : 'justify-start'
                        }`}
                    >
                        {/* Assistant avatar — uses agent icon if available */}
                        {message.role === 'assistant' && (
                            <div
                                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center shadow-sm mt-1"
                                style={{
                                    background: 'linear-gradient(135deg, var(--c-signal), #0a9467)',
                                }}
                            >
                                {(() => {
                                    const agentName = message.metadata?.agent_name as
                                        | string
                                        | undefined;
                                    const AvatarIcon = agentName
                                        ? (AGENT_META[agentName]?.Icon ?? Sparkles)
                                        : Sparkles;
                                    return <AvatarIcon className="w-4 h-4 text-white" />;
                                })()}
                            </div>
                        )}

                        <div
                            className={`flex flex-col gap-1 ${
                                message.role === 'user' ? 'items-end' : 'items-start'
                            } ${
                                message.role === 'assistant' &&
                                linkingInspectFromMetadata(message.metadata)
                                    ? 'w-full max-w-[min(100%,72rem)]'
                                    : 'max-w-[74%]'
                            }`}
                        >
                            {/* Agent attribution badge — shown on final assistant messages */}
                            {message.role === 'assistant' &&
                                message.metadata?.agent_name &&
                                (() => {
                                    const agentName = message.metadata.agent_name as string;
                                    const agentMeta = AGENT_META[agentName] ?? {
                                        label: agentName,
                                        Icon: Sparkles,
                                        color: 'text-blue-400',
                                    };
                                    const { Icon } = agentMeta;
                                    return (
                                        <div
                                            className={`flex items-center gap-1 px-1 ${agentMeta.color}`}
                                        >
                                            <Icon className="w-3 h-3 flex-shrink-0" />
                                            <span className="text-[10px] font-semibold uppercase tracking-wide">
                                                {agentMeta.label}
                                            </span>
                                        </div>
                                    );
                                })()}

                            {/* Persisted hierarchical trace above the bubble. New messages
                                carry `metadata.parts`; older ones are adapted from the
                                legacy orchestrator/sub-agent trace fields. */}
                            {message.role === 'assistant' &&
                                (() => {
                                    const parts: TracePart[] = Array.isArray(
                                        message.metadata?.parts
                                    )
                                        ? (message.metadata.parts as TracePart[])
                                        : partsFromLegacy(message.metadata);
                                    if (parts.length === 0) return null;
                                    return (
                                        <AgentTrace parts={parts} isLive={false} isDark={isDark} />
                                    );
                                })()}

                            {/* Message bubble */}
                            <div
                                className={`rounded-2xl px-4 py-3 shadow-sm ${
                                    message.role === 'user' ? 'rounded-tr-sm' : 'rounded-tl-sm'
                                }`}
                                style={
                                    message.role === 'user'
                                        ? {
                                              background:
                                                  'linear-gradient(135deg, var(--c-signal), rgba(139,92,246,0.9))',
                                              color: '#fff',
                                              boxShadow: '0 4px 20px var(--c-signal-glow)',
                                          }
                                        : {
                                              background: 'var(--c-glass)',
                                              border: '1px solid var(--c-glass-border)',
                                              backdropFilter: 'blur(8px)',
                                          }
                                }
                            >
                                {message.role === 'assistant' ? (
                                    <>
                                        {/* ── Thinking section (reasoning models) ── */}
                                        {(() => {
                                            // Prefer metadata.thinking (from reasoning_content SSE path)
                                            const metaThinking = message.metadata?.thinking as
                                                | string
                                                | undefined;
                                            if (metaThinking) {
                                                return (
                                                    <ThinkingBlock
                                                        thinking={metaThinking}
                                                        isStreaming={false}
                                                    />
                                                );
                                            }
                                            // Fall back to parsing <think> tags from content
                                            const { thinking } = parseThinkContent(message.content);
                                            if (thinking) {
                                                return (
                                                    <ThinkingBlock
                                                        thinking={thinking}
                                                        isStreaming={false}
                                                    />
                                                );
                                            }
                                            return null;
                                        })()}

                                        <div className="chat-prose">
                                            <ReactMarkdown
                                                remarkPlugins={[remarkGfm]}
                                                components={persistedMarkdownComponents}
                                            >
                                                {parseThinkContent(message.content).answer ||
                                                    message.content}
                                            </ReactMarkdown>
                                        </div>

                                        {/* Temp table preview */}
                                        {message.metadata?.temp_preview &&
                                            Array.isArray(
                                                (message.metadata as any).temp_preview?.rows
                                            ) && (
                                                <div
                                                    className="mt-4 rounded-lg border"
                                                    style={{
                                                        borderColor: 'var(--c-glass-border)',
                                                        background: 'var(--c-glass)',
                                                    }}
                                                >
                                                    <div className="px-3 py-2 flex items-center justify-between">
                                                        <div
                                                            className="text-[11px] uppercase tracking-wide"
                                                            style={{ color: 'var(--c-mist-fg)' }}
                                                        >
                                                            Temp Table Preview
                                                        </div>
                                                        <div
                                                            className="text-[11px]"
                                                            style={{ color: 'var(--c-mist-fg)' }}
                                                        >
                                                            {
                                                                (message.metadata as any)
                                                                    .temp_preview?.temp_schema
                                                            }
                                                            .
                                                            {
                                                                (message.metadata as any)
                                                                    .temp_preview?.temp_table
                                                            }
                                                        </div>
                                                    </div>
                                                    <div className="max-h-56 overflow-auto">
                                                        <table className="min-w-full text-xs border-collapse">
                                                            <thead>
                                                                <tr
                                                                    style={{
                                                                        color: 'var(--c-mist-fg)',
                                                                    }}
                                                                >
                                                                    {(
                                                                        (message.metadata as any)
                                                                            .temp_preview
                                                                            ?.columns || []
                                                                    ).map((col: string) => (
                                                                        <th
                                                                            key={col}
                                                                            className="text-left font-medium px-3 py-1.5 border-b"
                                                                            style={{
                                                                                borderColor:
                                                                                    'var(--c-glass-border)',
                                                                                background:
                                                                                    'rgba(255,255,255,0.03)',
                                                                            }}
                                                                        >
                                                                            {col}
                                                                        </th>
                                                                    ))}
                                                                </tr>
                                                            </thead>
                                                            <tbody
                                                                style={{ color: 'var(--c-ink-2)' }}
                                                            >
                                                                {(
                                                                    (message.metadata as any)
                                                                        .temp_preview?.rows || []
                                                                ).map((row: any, idx: number) => (
                                                                    <tr
                                                                        key={idx}
                                                                        className="odd:bg-white/5"
                                                                    >
                                                                        {(
                                                                            (
                                                                                message.metadata as any
                                                                            ).temp_preview
                                                                                ?.columns || []
                                                                        ).map((col: string) => (
                                                                            <td
                                                                                key={`${idx}-${col}`}
                                                                                className="px-3 py-1.5 border-b"
                                                                                style={{
                                                                                    borderColor:
                                                                                        'var(--c-glass-border)',
                                                                                }}
                                                                            >
                                                                                {row[col] ===
                                                                                    null ||
                                                                                row[col] ===
                                                                                    undefined
                                                                                    ? '—'
                                                                                    : String(
                                                                                          row[col]
                                                                                      )}
                                                                            </td>
                                                                        ))}
                                                                    </tr>
                                                                ))}
                                                            </tbody>
                                                        </table>
                                                    </div>
                                                </div>
                                            )}

                                        {(() => {
                                            const inspect = linkingInspectFromMetadata(
                                                message.metadata
                                            );
                                            if (!inspect || !sessionId) return null;
                                            return (
                                                <LinkingInspectPanel
                                                    context={inspect}
                                                    sessionId={sessionId}
                                                    isDark={isDark}
                                                />
                                            );
                                        })()}

                                        {/* Action prompt */}
                                        {message.actionPrompt &&
                                            onActionSelect &&
                                            !resolvedActionPromptIds.includes(
                                                message.actionPrompt.prompt_id
                                            ) &&
                                            index === messages.length - 1 && (
                                                <div
                                                    className="mt-4 p-3 rounded-lg border"
                                                    style={{
                                                        borderColor: 'rgba(251,191,36,0.3)',
                                                        background: 'rgba(251,191,36,0.08)',
                                                    }}
                                                >
                                                    <p className="text-xs font-semibold uppercase tracking-wide mb-1.5 text-amber-300">
                                                        Action required
                                                    </p>

                                                    {/* Question text */}
                                                    {message.actionPrompt.question && (
                                                        <p
                                                            className="text-sm mb-2.5 leading-snug"
                                                            style={{ color: 'var(--c-ink)' }}
                                                        >
                                                            {message.actionPrompt.question}
                                                        </p>
                                                    )}

                                                    {message.actionPrompt.options &&
                                                        message.actionPrompt.options.length > 0 && (
                                                            <div className="flex flex-wrap gap-2">
                                                                {message.actionPrompt.options.map(
                                                                    (option) => (
                                                                        <button
                                                                            key={`${message.actionPrompt?.prompt_id}-${option.value}`}
                                                                            type="button"
                                                                            onClick={() =>
                                                                                onActionSelect({
                                                                                    promptId:
                                                                                        message
                                                                                            .actionPrompt!
                                                                                            .prompt_id,
                                                                                    kind: message
                                                                                        .actionPrompt!
                                                                                        .kind,
                                                                                    value: option.value,
                                                                                    label: option.label,
                                                                                })
                                                                            }
                                                                            className="text-xs px-3 py-1.5 rounded-md border transition-colors hover:bg-amber-300/20 text-amber-200"
                                                                            style={{
                                                                                borderColor:
                                                                                    'rgba(251,191,36,0.4)',
                                                                                background:
                                                                                    'rgba(255,255,255,0.03)',
                                                                            }}
                                                                        >
                                                                            {option.label}
                                                                        </button>
                                                                    )
                                                                )}
                                                            </div>
                                                        )}
                                                    {(message.actionPrompt.kind ===
                                                        'cleaning_react_question' ||
                                                        message.actionPrompt.allow_open_text) && (
                                                        <div className="mt-3 flex flex-wrap gap-2">
                                                            <input
                                                                type="text"
                                                                value={
                                                                    customActionInput[
                                                                        message.actionPrompt
                                                                            .prompt_id
                                                                    ] || ''
                                                                }
                                                                onChange={(e) =>
                                                                    setCustomActionInput(
                                                                        (prev) => ({
                                                                            ...prev,
                                                                            [message.actionPrompt!
                                                                                .prompt_id]:
                                                                                e.target.value,
                                                                        })
                                                                    )
                                                                }
                                                                placeholder={
                                                                    message.actionPrompt.context
                                                                        ?.input_placeholder ||
                                                                    'Add a custom response'
                                                                }
                                                                className="flex-1 min-w-[220px] rounded-md border px-3 py-2 text-xs"
                                                                style={{
                                                                    background: 'var(--c-glass)',
                                                                    borderColor:
                                                                        'rgba(251,191,36,0.3)',
                                                                    color: 'var(--c-ink)',
                                                                }}
                                                            />
                                                            <button
                                                                type="button"
                                                                onClick={() => {
                                                                    const value = (
                                                                        customActionInput[
                                                                            message.actionPrompt!
                                                                                .prompt_id
                                                                        ] || ''
                                                                    ).trim();
                                                                    if (!value) return;
                                                                    onActionSelect({
                                                                        promptId:
                                                                            message.actionPrompt!
                                                                                .prompt_id,
                                                                        kind: message.actionPrompt!
                                                                            .kind,
                                                                        value,
                                                                        label: value,
                                                                    });
                                                                }}
                                                                className="text-xs px-3 py-2 rounded-md border transition-colors hover:bg-amber-300/20 text-amber-200"
                                                                style={{
                                                                    borderColor:
                                                                        'rgba(251,191,36,0.4)',
                                                                    background:
                                                                        'rgba(255,255,255,0.03)',
                                                                }}
                                                            >
                                                                Submit
                                                            </button>
                                                        </div>
                                                    )}
                                                </div>
                                            )}
                                    </>
                                ) : (
                                    <p className="text-sm leading-relaxed">{message.content}</p>
                                )}
                            </div>

                            {/* Timestamp row + copy button (appears on hover) */}
                            <div className="flex items-center gap-1.5 px-1">
                                <span className="text-[11px]" style={{ color: 'var(--c-mist-fg)' }}>
                                    {new Date(message.timestamp).toLocaleTimeString([], {
                                        hour: '2-digit',
                                        minute: '2-digit',
                                    })}
                                </span>
                                {message.role === 'assistant' && message.content && (
                                    <CopyButton
                                        text={message.content}
                                        className="opacity-0 group-hover/msg:opacity-100 transition-opacity hover:bg-white/10"
                                        style={{ color: 'var(--c-mist-fg)' } as React.CSSProperties}
                                    />
                                )}
                                {message.metadata?.memory_retained && (
                                    <span
                                        title="This turn was queued for long-term memory"
                                        className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border"
                                        style={{
                                            borderColor:
                                                'color-mix(in srgb, var(--c-signal) 30%, transparent)',
                                            background: 'var(--c-signal-dim)',
                                            color: 'var(--c-signal-text)',
                                        }}
                                    >
                                        <Brain className="w-2.5 h-2.5" />
                                        Saved
                                    </span>
                                )}
                            </div>
                        </div>

                        {/* User avatar — shows profile picture if set, otherwise a generic icon */}
                        {message.role === 'user' && (
                            <div
                                className="flex-shrink-0 w-8 h-8 rounded-xl overflow-hidden shadow-sm mt-1"
                                style={{
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                }}
                            >
                                {userProfilePicture ? (
                                    <img
                                        src={userProfilePicture}
                                        alt="You"
                                        className="w-full h-full object-cover"
                                    />
                                ) : (
                                    <div className="w-full h-full flex items-center justify-center">
                                        <User
                                            className="w-4 h-4"
                                            style={{ color: 'var(--c-ink-2)' }}
                                        />
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                );
            })}

            {/* ── Compaction indicator — appears while older turns are being folded
                 into the running summary, then disappears once done (leaving the
                 persisted "This chat has been compacted" notice behind). ── */}
            {isTyping && isCompacting && <CompactionBar />}

            {/* ── Live agent trace ── one element mounted continuously through the
                 thinking → streaming → commit transition. On commit the persisted
                 trace (identical parts) takes over, so it never collapses to a strip
                 or disappears/reappears. */}
            {isTyping && liveTraceParts.length > 0 && (
                <div className="pl-11 pr-2 -mb-1">
                    <AgentTrace
                        parts={liveTraceParts}
                        isLive
                        isDark={isDark}
                        isStreaming={streamingContent !== null}
                    />
                </div>
            )}

            {/* ── Thinking status card — only before the answer starts streaming ── */}
            {isTyping && streamingContent === null && (
                <WorkingPanel
                    thinkingAgent={thinkingAgent}
                    stepCount={liveTraceParts.length}
                    elapsedSec={elapsedSec}
                    isDark={isDark}
                />
            )}

            {/* ── Live streaming bubble — token by token ── */}
            {streamingContent !== null && (
                <div className="flex gap-3 justify-start msg-fade-in">
                    {(() => {
                        const agentMeta = streamingAgent
                            ? (AGENT_META[streamingAgent] ?? {
                                  label: streamingAgent,
                                  Icon: Sparkles,
                                  color: 'text-blue-400',
                              })
                            : { label: 'Assistant', Icon: Sparkles, color: 'text-blue-400' };
                        const AgentIcon = agentMeta.Icon;
                        return (
                            <div
                                className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center shadow-sm mt-1"
                                style={{
                                    background: 'linear-gradient(135deg, var(--c-signal), #0a9467)',
                                }}
                            >
                                <AgentIcon className="w-4 h-4 text-white" />
                            </div>
                        );
                    })()}
                    <div className="max-w-[74%] flex flex-col gap-1 items-start">
                        {/* Agent name badge */}
                        {streamingAgent &&
                            (() => {
                                const meta = AGENT_META[streamingAgent] ?? {
                                    label: streamingAgent,
                                    Icon: Sparkles,
                                    color: 'text-blue-400',
                                };
                                const { Icon } = meta;
                                return (
                                    <div className={`flex items-center gap-1 px-1 ${meta.color}`}>
                                        <Icon className="w-3 h-3 flex-shrink-0" />
                                        <span className="text-[10px] font-semibold uppercase tracking-wide">
                                            {meta.label}
                                        </span>
                                    </div>
                                );
                            })()}
                        <div
                            className="rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm"
                            style={{
                                background: 'var(--c-glass)',
                                border: '1px solid var(--c-glass-border)',
                                backdropFilter: 'blur(8px)',
                            }}
                        >
                            {/* Live thinking section (reasoning_content SSE path) */}
                            {streamingThinkingContent !== null && (
                                <ThinkingBlock
                                    thinking={streamingThinkingContent}
                                    isStreaming={true}
                                />
                            )}

                            {/* Live thinking from <think> tags embedded in content */}
                            {streamingThinkingContent === null &&
                                streamingContent !== null &&
                                (() => {
                                    const { thinking } = parseThinkContent(streamingContent ?? '');
                                    return thinking ? (
                                        <ThinkingBlock thinking={thinking} isStreaming={true} />
                                    ) : null;
                                })()}

                            <div className="chat-prose">
                                <ReactMarkdown
                                    remarkPlugins={[remarkGfm]}
                                    components={streamingMarkdownComponents}
                                >
                                    {parseThinkContent(streamingContent ?? '').answer ||
                                        streamingContent!}
                                </ReactMarkdown>
                                {/* Show cursor only when there is answer content to follow */}
                                {(parseThinkContent(streamingContent ?? '').answer ||
                                    !streamingContent?.startsWith('<think>')) && (
                                    <span className="streaming-cursor" aria-hidden="true" />
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
