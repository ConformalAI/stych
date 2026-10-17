import { useEffect, useLayoutEffect, useState, useRef } from 'react';
import { v4 as uuidv4 } from 'uuid';
import { useStore } from '../store/useStore';
import { useChat } from '../hooks/useChat';
import Sidebar from '../components/Sidebar';
import ChatMessages from '../components/ChatMessages';
import ChatInput from '../components/ChatInput';
import AddDataModal from '../components/data/AddDataModal';
import { Sparkles, MessageSquare, Table2, EyeOff, BarChart3, Database } from 'lucide-react';
import DataPreviewView from '../components/preview/DataPreviewView';
import VizView from '../components/visualize/VizView';
import OntologyView from '../components/ontology/OntologyView';
import { getSessionInfo, getHistory } from '../services/api';
import {
    getSessionId as getStoredSessionId,
    isSessionAnonymous,
    setSessionId as persistSessionId,
} from '../services/storage';
import type { TaskType } from '../generated/contracts/taskTypes';

const SUGGESTIONS = [
    'Summarize this dataset',
    'Find missing values',
    'Show distribution of columns',
    'Clean duplicate rows',
];

export default function Chat() {
    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const uploadedFiles = useStore((s) => s.uploadedFiles);
    const pendingQuery = useStore((s) => s.pendingQuery);
    const setPendingQuery = useStore((s) => s.setPendingQuery);
    const markActionPromptResolved = useStore((s) => s.markActionPromptResolved);
    const addMessage = useStore((s) => s.addMessage);
    const setError = useStore((s) => s.setError);
    const setUploadedFile = useStore((s) => s.setUploadedFile);
    const setUploadedFiles = useStore((s) => s.setUploadedFiles);
    const messages = useStore((s) => s.messages);
    const setMessages = useStore((s) => s.setMessages);
    const sessionModel = useStore((s) => s.sessionModel);
    const setSessionModel = useStore((s) => s.setSessionModel);
    const availableModels = useStore((s) => s.availableModels);
    const isAnonymousSession = useStore((s) => s.isAnonymousSession);
    const setIsAnonymousSession = useStore((s) => s.setIsAnonymousSession);
    const lastFilterEvent = useStore((s) => s.lastFilterEvent);
    const setOpenFilterId = useStore((s) => s.setOpenFilterId);
    const lastVizEvent = useStore((s) => s.lastVizEvent);
    const openVizId = useStore((s) => s.openVizId);
    const [showUploadModal, setShowUploadModal] = useState(false);
    // The session's primary dataset. Derived here rather than read off the store —
    // see the note on `usePrimaryFile` in store/useStore.ts.
    const hasData = uploadedFiles.length > 0;
    const activeView = useStore((s) => s.activeView);
    const setActiveView = useStore((s) => s.setActiveView);
    const lastOntologyEvent = useStore((s) => s.lastOntologyEvent);
    const [vizBadge, setVizBadge] = useState(false);
    const setOntologyBadge = useStore((s) => s.setOntologyBadge);
    const { sendMessage, isLoading, stopGeneration } = useChat(sessionId);
    const pendingQuerySent = useRef(false);
    const isHydratedRef = useRef(false);
    const knownFileIdsRef = useRef<Set<string>>(new Set());

    // Run synchronously before the first paint so sessionId is guaranteed to
    // be set by the time the user can interact with the chat input.
    useLayoutEffect(() => {
        const storedSessionId = getStoredSessionId();
        if (storedSessionId) {
            if (sessionId !== storedSessionId) setSessionId(storedSessionId);
            setIsAnonymousSession(isSessionAnonymous(storedSessionId));
        } else if (!sessionId) {
            const newSessionId = uuidv4();
            setSessionId(newSessionId);
            persistSessionId(newSessionId);
        }
    }, []);

    // Return to chat when switching sessions.
    useEffect(() => {
        setActiveView('chat');
    }, [sessionId, setActiveView]);

    // Agent saved/updated a filter (SSE 'filter' event): open it in the Data tab.
    useEffect(() => {
        if (!lastFilterEvent || lastFilterEvent.sessionId !== sessionId) return;
        setOpenFilterId(lastFilterEvent.filterId);
        if (!lastFilterEvent.fromReplay) setActiveView('data');
    }, [lastFilterEvent, sessionId, setOpenFilterId, setActiveView]);

    // Badge the Visualize tab when the agent creates/updates a chart while
    // the user is looking elsewhere; clear it when they open the tab.
    useEffect(() => {
        if (!lastVizEvent || lastVizEvent.sessionId !== sessionId) return;
        if (lastVizEvent.source === 'user') return;
        if (activeView !== 'visualize') setVizBadge(true);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [lastVizEvent]);
    useEffect(() => {
        if (activeView === 'visualize') setVizBadge(false);
    }, [activeView]);

    // Badge the Ontology tab when the agent proposes definitions while the
    // user is looking elsewhere; clear it when they open the tab.
    // Guard on sessionId so a proposal in another session doesn't light this tab.
    useEffect(() => {
        if (!lastOntologyEvent || lastOntologyEvent.sessionId !== sessionId) return;
        if (activeView !== 'ontology') setOntologyBadge(true);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [lastOntologyEvent]);
    useEffect(() => {
        if (activeView === 'ontology') setOntologyBadge(false);
    }, [activeView, setOntologyBadge]);

    // A chat VizCard "Edit" click requests the Visualize tab.
    useEffect(() => {
        if (openVizId) setActiveView('visualize');
    }, [openVizId, setActiveView]);

    useEffect(() => {
        if (pendingQuery && sessionId && !pendingQuerySent.current) {
            pendingQuerySent.current = true;
            const tid = setTimeout(() => {
                sendMessage(pendingQuery);
                setPendingQuery(null);
            }, 100);
            return () => clearTimeout(tid);
        }
    }, [pendingQuery, sessionId, sendMessage, setPendingQuery]);

    useEffect(() => {
        if (!sessionId) return;
        let mounted = true;

        const hydrateSessionDataset = async () => {
            try {
                const info = await getSessionInfo(sessionId);
                if (!mounted) return;

                // Restore every dataset in the session (primary + additional).
                // Fall back to the single dataset_info for older backends that
                // don't send the datasets array.
                const datasets =
                    info.datasets && info.datasets.length > 0
                        ? info.datasets
                        : info.dataset_info
                          ? [info.dataset_info]
                          : [];
                if (info.has_data && datasets.length > 0) {
                    const restored = datasets
                        .filter((d) => d?.file_id)
                        .map((d) => {
                            const shape = Array.isArray(d.shape) ? d.shape : [0, 0];
                            const fileId = String(d.file_id);
                            // Register existing file so the upload watcher doesn't re-prompt EDA
                            knownFileIdsRef.current.add(fileId);
                            return {
                                file_id: fileId,
                                filename: String(d.filename || 'dataset'),
                                rows: Number(shape[0] || 0),
                                columns: Number(shape[1] || 0),
                            };
                        });
                    setUploadedFiles(restored);
                } else {
                    setUploadedFile(null);
                }
            } catch (err: unknown) {
                if (!mounted) return;
                const status = (err as any)?.response?.status;
                if (status === 401) {
                    window.location.href = '/login';
                    return;
                }
                setError(
                    'Could not restore your session. Please refresh the page or start a new session.'
                );
            } finally {
                if (mounted) isHydratedRef.current = true;
            }
        };
        hydrateSessionDataset();

        return () => {
            mounted = false;
        };
    }, [sessionId, setUploadedFile, setUploadedFiles]);

    // On mount (or after a full page refresh), restore message history when
    // sessionId was recovered from localStorage but the in-memory messages
    // list is still empty.  This ensures the chat panel shows prior
    // conversation rather than the empty hero screen.
    useEffect(() => {
        if (!sessionId || messages.length > 0) return;
        let mounted = true;
        getHistory(sessionId, 100)
            .then((history) => {
                if (!mounted) return;
                const normalized = (history?.messages || []).map((msg: any) => ({
                    role: msg.role,
                    content: msg.content,
                    timestamp: new Date(msg.timestamp),
                    metadata: msg.metadata || {},
                    actionPrompt: msg.metadata?.action_prompt || null,
                    status: 'completed',
                }));
                if (normalized.length > 0) setMessages(normalized);
            })
            .catch(() => {
                // Non-fatal — user just sees an empty chat and can continue
            });
        return () => {
            mounted = false;
        };
    }, [sessionId]);

    // Inject EDA prompt card into chat whenever a brand-new file is uploaded
    useEffect(() => {
        if (!isHydratedRef.current) return;
        uploadedFiles.forEach((file) => {
            if (!knownFileIdsRef.current.has(file.file_id)) {
                knownFileIdsRef.current.add(file.file_id);
                addMessage({
                    role: 'assistant',
                    content: '',
                    metadata: {
                        type: 'eda_prompt',
                        file_id: file.file_id,
                        filename: file.filename,
                        rows: file.rows,
                        columns: file.columns,
                    },
                    actionPrompt: {
                        prompt_id: `eda_${file.file_id}`,
                        kind: 'run_eda',
                        question: '',
                        options: [
                            { label: 'Run Initial EDA', value: 'run' },
                            { label: 'Skip', value: 'skip' },
                        ],
                    },
                });
            }
        });
    }, [uploadedFiles, addMessage]);

    const handleSendMessage = async (message: string, taskType?: TaskType) => {
        await sendMessage(message, taskType);
    };

    const handleActionSelect = async (action: {
        promptId: string;
        kind: string;
        value: string;
        label: string;
        text?: string;
    }) => {
        if (action.kind === 'run_eda') {
            if (action.value === 'run') {
                const fileId = action.promptId.startsWith('eda_')
                    ? action.promptId.replace('eda_', '')
                    : undefined;
                const started = await handleRunInitialEDA(fileId);
                if (started) markActionPromptResolved(action.promptId);
            } else {
                markActionPromptResolved(action.promptId);
            }
            return;
        }

        markActionPromptResolved(action.promptId);
        const isCleaningAction =
            action.kind === 'cleaning_strategy' ||
            action.kind === 'apply_sql_changes' ||
            action.kind === 'cleaning_react_question' ||
            action.kind === 'cleaning_react_permission';
        const isLinkingAction = action.kind === 'linking_confirm';
        const taskType = isCleaningAction ? 'cleaning' : isLinkingAction ? 'linking' : 'analysis';
        await sendMessage('', taskType, {
            userAction: {
                prompt_id: action.promptId,
                kind: action.kind,
                value: action.value,
                label: action.label,
                text: action.text,
            },
            userDisplayMessage: action.label,
            suppressUserMessage: isCleaningAction || isLinkingAction,
        });
    };

    const handleRunInitialEDA = async (fileId?: string) => {
        const currentFile =
            (fileId ? uploadedFiles.find((f) => f.file_id === fileId) : uploadedFiles[0]) ?? null;
        if (!sessionId || !currentFile) return false;

        return sendMessage(`Run initial EDA on ${currentFile.filename}`, 'eda', {
            userDisplayMessage: `Run initial EDA on ${currentFile.filename}`,
            targetFileId: currentFile.file_id,
        });
    };

    return (
        // Page chrome (header, primary rail, toasts) belongs to AppShell.
        <div className="h-full flex overflow-hidden">
            <Sidebar />

            <main className="flex-1 flex flex-col relative overflow-hidden">
                {/* Anonymous-session ribbon — persistent across the empty hero and
                        the active conversation so it's clear before AND after the first
                        query that this chat is excluded from long-term memory. */}
                {isAnonymousSession && (
                    <div
                        className="flex items-center justify-center px-4 py-1.5 border-b backdrop-blur-sm"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                        }}
                    >
                        <span
                            className="inline-flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-0.5 rounded-full border"
                            style={{
                                color: 'var(--c-mist-fg)',
                                background: 'rgba(148,163,184,0.08)',
                                borderColor: 'rgba(148,163,184,0.25)',
                            }}
                        >
                            <EyeOff className="w-3 h-3" />
                            Anonymous session
                            <span aria-hidden="true" style={{ opacity: 0.4 }}>
                                ·
                            </span>
                            <span style={{ opacity: 0.85 }}>not used for long-term memory</span>
                        </span>
                    </div>
                )}

                {/* Session subheader — always present. With no data it is a single
                    call to action; with data it carries the view tabs. Attached files
                    are listed in the Data tab, not repeated here. */}
                <div
                    className="px-3 sm:px-5 py-2 flex items-center justify-between gap-3 border-b sticky top-0 z-10 backdrop-blur-sm"
                    style={{
                        background: 'var(--c-glass)',
                        borderColor: 'var(--c-glass-border)',
                    }}
                >
                    {hasData ? (
                        <div
                            className="flex items-center rounded-lg border p-0.5"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                background: 'var(--c-glass)',
                            }}
                            role="tablist"
                            aria-label="View"
                        >
                            {(
                                [
                                    ['chat', 'Chat', MessageSquare],
                                    ['data', 'Data', Table2],
                                    ['visualize', 'Visualize', BarChart3],
                                ] as const
                            ).map(([view, label, Icon]) => (
                                <button
                                    key={view}
                                    role="tab"
                                    aria-selected={activeView === view}
                                    onClick={() => setActiveView(view)}
                                    className="relative flex items-center gap-1 px-2 sm:px-2.5 py-1 rounded-md text-[11px] sm:text-xs font-medium transition-colors"
                                    style={
                                        activeView === view
                                            ? {
                                                  background: 'var(--c-signal-dim)',
                                                  color: 'var(--c-signal-text)',
                                              }
                                            : { color: 'var(--c-mist-fg)' }
                                    }
                                >
                                    <Icon className="w-3.5 h-3.5" />
                                    <span className="hidden sm:inline">{label}</span>
                                    {view === 'visualize' && vizBadge && (
                                        <span
                                            className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full"
                                            style={{ background: 'var(--c-signal)' }}
                                        />
                                    )}
                                </button>
                            ))}
                        </div>
                    ) : (
                        <p className="text-xs truncate" style={{ color: 'var(--c-mist-fg)' }}>
                            No data in this session yet.
                        </p>
                    )}

                    <button
                        onClick={() => setShowUploadModal(true)}
                        className="inline-flex items-center gap-1.5 rounded-lg font-semibold transition-all flex-shrink-0"
                        style={
                            hasData
                                ? {
                                      padding: '6px 12px',
                                      fontSize: 12,
                                      background: 'var(--c-signal-dim)',
                                      color: 'var(--c-signal-text)',
                                      border: '1px solid color-mix(in srgb, var(--c-signal) 28%, transparent)',
                                  }
                                : {
                                      padding: '8px 18px',
                                      fontSize: 13,
                                      background: 'var(--c-signal)',
                                      color: '#fff',
                                      boxShadow: '0 4px 16px var(--c-signal-glow)',
                                  }
                        }
                    >
                        <Database className={hasData ? 'w-3.5 h-3.5' : 'w-4 h-4'} />
                        {hasData ? 'Data sources' : 'Add data to get started'}
                    </button>
                </div>

                {activeView === 'data' && sessionId ? (
                    /* ── Data preview view ── */
                    <DataPreviewView
                        sessionId={sessionId}
                        onExplain={(prompt) => {
                            setActiveView('chat');
                            sendMessage(prompt);
                        }}
                    />
                ) : activeView === 'visualize' && sessionId ? (
                    /* ── Visualize workspace ── */
                    <VizView key={sessionId} sessionId={sessionId} />
                ) : activeView === 'ontology' ? (
                    /* ── Ontology (semantic layer) workspace ──
                       Not keyed on the session: the ontology is per user and
                       must survive switching sessions. */
                    <OntologyView />
                ) : messages.length > 0 ? (
                    /* ── Has messages: normal scrollable layout with sticky input ── */
                    <>
                        <ChatMessages isTyping={isLoading} onActionSelect={handleActionSelect} />
                        <ChatInput
                            key={sessionId}
                            sessionId={sessionId}
                            onSendMessage={handleSendMessage}
                            onUploadClick={() => setShowUploadModal(true)}
                            onStop={stopGeneration}
                            disabled={isLoading}
                            models={availableModels}
                            sessionModel={sessionModel}
                            onModelChange={setSessionModel}
                            sessionHasMessages={true}
                            centered={false}
                        />
                    </>
                ) : (
                    /* ── No messages: centered hero + input ── */
                    <div className="flex-1 flex flex-col items-center justify-center px-4 sm:px-6 pb-8 gap-8">
                        {/* Hero */}
                        <div className="flex flex-col items-center text-center gap-4 w-full max-w-lg">
                            {/* Icon */}
                            <div
                                className="w-14 h-14 rounded-2xl flex items-center justify-center"
                                style={{
                                    background: 'var(--c-signal)',
                                    boxShadow: '0 8px 32px var(--c-signal-glow)',
                                }}
                            >
                                <Sparkles className="w-7 h-7 text-white" />
                            </div>

                            {/* Heading */}
                            <div>
                                <h3
                                    className="text-2xl font-bold mb-1.5"
                                    style={{
                                        color: 'var(--c-ink)',
                                        fontFamily: 'Space Grotesk, sans-serif',
                                        letterSpacing: '-0.02em',
                                    }}
                                >
                                    How can I help with your data?
                                </h3>
                                <p
                                    className="text-sm leading-relaxed"
                                    style={{ color: 'var(--c-ink-2)' }}
                                >
                                    Upload a dataset and ask me to analyze, clean, or answer
                                    questions about it.
                                </p>
                            </div>

                            {/* Suggestion chips */}
                            <div className="flex flex-wrap gap-2 justify-center">
                                {SUGGESTIONS.map((s) => (
                                    <button
                                        key={s}
                                        type="button"
                                        onClick={() => sendMessage(s)}
                                        className="px-3.5 py-1.5 rounded-full text-xs font-medium cursor-pointer transition-all"
                                        style={{
                                            background: 'var(--c-glass)',
                                            border: '1px solid var(--c-glass-border)',
                                            color: 'var(--c-ink-2)',
                                        }}
                                        onMouseEnter={(e) => {
                                            (e.currentTarget as HTMLElement).style.background =
                                                'var(--c-signal-dim)';
                                            (e.currentTarget as HTMLElement).style.borderColor =
                                                'color-mix(in srgb, var(--c-signal) 30%, transparent)';
                                            (e.currentTarget as HTMLElement).style.color =
                                                'var(--c-signal-text)';
                                        }}
                                        onMouseLeave={(e) => {
                                            (e.currentTarget as HTMLElement).style.background =
                                                'var(--c-glass)';
                                            (e.currentTarget as HTMLElement).style.borderColor =
                                                'var(--c-glass-border)';
                                            (e.currentTarget as HTMLElement).style.color =
                                                'var(--c-ink-2)';
                                        }}
                                    >
                                        {s}
                                    </button>
                                ))}
                            </div>
                        </div>

                        {/* Centered input card */}
                        <div className="w-full max-w-2xl">
                            <ChatInput
                                key={sessionId}
                                sessionId={sessionId}
                                onSendMessage={handleSendMessage}
                                onUploadClick={() => setShowUploadModal(true)}
                                onStop={stopGeneration}
                                disabled={isLoading}
                                models={availableModels}
                                sessionModel={sessionModel}
                                onModelChange={setSessionModel}
                                sessionHasMessages={false}
                                centered={true}
                            />
                        </div>
                    </div>
                )}
            </main>

            {showUploadModal && <AddDataModal onClose={() => setShowUploadModal(false)} />}
        </div>
    );
}
