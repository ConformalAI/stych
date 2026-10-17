import { useEffect, useRef, useState, useCallback } from 'react';
import {
    Plus,
    Clock3,
    MoreHorizontal,
    Pin,
    PinOff,
    Pencil,
    Trash2,
    ChevronDown,
    PanelLeftClose,
    PanelLeftOpen,
    EyeOff,
    Network,
} from 'lucide-react';
import { v4 as uuidv4 } from 'uuid';
import { useStore } from '../store/useStore';
import { archiveWorkspace, deleteSession, getHistory } from '../services/api';
import { getSessionSummaries, type SessionSummary } from '../services/auth';
import {
    isSessionAnonymous,
    loadSessionMeta,
    markSessionAnonymous,
    saveSessionMeta,
    setSessionId as persistSessionId,
    type SessionMetaMap,
} from '../services/storage';

// ---------------------------------------------------------------------------
// Static data
// ---------------------------------------------------------------------------

const SESSION_PAGE_SIZE = 20;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function Sidebar() {
    const isSidebarOpen = useStore((s) => s.isSidebarOpen);
    const toggleSidebar = useStore((s) => s.toggleSidebar);
    const sessionId = useStore((s) => s.sessionId);
    const sessionActivityEvent = useStore((s) => s.sessionActivityEvent);
    const clearMessages = useStore((s) => s.clearMessages);
    const setMessages = useStore((s) => s.setMessages);
    const setUploadedFile = useStore((s) => s.setUploadedFile);
    const setSessionId = useStore((s) => s.setSessionId);
    const setIsAnonymousSession = useStore((s) => s.setIsAnonymousSession);
    const activeView = useStore((s) => s.activeView);
    const setActiveView = useStore((s) => s.setActiveView);
    const ontologyBadge = useStore((s) => s.ontologyBadge);

    // Semantic class helpers using CSS variables
    const muted = 'text-[var(--c-mist-fg)]';
    const label = 'text-[var(--c-mist-fg)]';
    const text = 'text-[var(--c-ink)]';

    const [showNewSessionMenu, setShowNewSessionMenu] = useState(false);
    const newSessionMenuRef = useRef<HTMLDivElement>(null);
    const [sessionSummaries, setSessionSummaries] = useState<SessionSummary[]>([]);
    const [initialLoading, setInitialLoading] = useState(true);
    const [hasMoreSessions, setHasMoreSessions] = useState(false);
    const [nextBefore, setNextBefore] = useState<string | null>(null);
    const [nextBeforeId, setNextBeforeId] = useState<string | null>(null);
    const [loadingMore, setLoadingMore] = useState(false);
    const listScrollRef = useRef<HTMLDivElement>(null);
    const loadMoreSentinelRef = useRef<HTMLDivElement>(null);
    const loadingMoreRef = useRef(false);
    const skipAppearAnimRef = useRef(false);
    const [openingSessionId, setOpeningSessionId] = useState<string | null>(null);
    const [deletingId, setDeletingId] = useState<string | null>(null);

    // Session context-menu state
    const [sessionMeta, setSessionMeta] = useState<SessionMetaMap>(loadSessionMeta);
    const [menuOpenFor, setMenuOpenFor] = useState<string | null>(null);
    const [renamingFor, setRenamingFor] = useState<string | null>(null);
    const [renameValue, setRenameValue] = useState('');
    const renameInputRef = useRef<HTMLInputElement>(null);
    // Set to true when Escape is pressed so the blur handler skips commitRename.
    const renameCancelledRef = useRef(false);
    // Delete confirmation — tracks which session is pending confirmation
    const [deletingFor, setDeletingFor] = useState<string | null>(null);

    // Animation: track which session IDs just appeared so we can play the entrance animation
    const prevSessionIdsRef = useRef<Set<string>>(new Set());
    const [newSessionIds, setNewSessionIds] = useState<Set<string>>(new Set());

    // Close context menu when clicking outside
    useEffect(() => {
        if (!menuOpenFor) return;
        const handler = (e: MouseEvent) => {
            if (!(e.target as Element).closest('[data-session-menu]')) {
                setMenuOpenFor(null);
                setDeletingFor(null);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [menuOpenFor]);

    // Close new-session dropdown when clicking outside
    useEffect(() => {
        if (!showNewSessionMenu) return;
        const handler = (e: MouseEvent) => {
            if (
                newSessionMenuRef.current &&
                !newSessionMenuRef.current.contains(e.target as Node)
            ) {
                setShowNewSessionMenu(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [showNewSessionMenu]);

    const applySessionPage = useCallback(
        (
            res: {
                sessions?: SessionSummary[];
                has_more?: boolean;
                next_before?: string | null;
                next_before_id?: string | null;
            },
            append: boolean
        ) => {
            const incoming = res.sessions || [];
            setHasMoreSessions(!!res.has_more);
            setNextBefore(res.next_before ?? null);
            setNextBeforeId(res.next_before_id ?? null);
            setSessionSummaries((prev) => {
                if (!append) return incoming;
                skipAppearAnimRef.current = true;
                const seen = new Set(prev.map((s) => s.session_id));
                return [...prev, ...incoming.filter((s) => !seen.has(s.session_id))];
            });
        },
        []
    );

    // Shared fetch — first page, used by auto-refresh after durable activity.
    const fetchSummaries = useCallback(async () => {
        try {
            const res = await getSessionSummaries({ limit: SESSION_PAGE_SIZE });
            applySessionPage(res, false);
        } catch {
            // Keep whatever was shown before on error
        }
    }, [applySessionPage]);

    const loadMoreSessions = useCallback(async () => {
        if (!hasMoreSessions || loadingMoreRef.current || !nextBefore) return;
        loadingMoreRef.current = true;
        setLoadingMore(true);
        try {
            const res = await getSessionSummaries({
                limit: SESSION_PAGE_SIZE,
                before: nextBefore,
                before_id: nextBeforeId ?? undefined,
            });
            applySessionPage(res, true);
        } catch {
            // Keep the pages already shown
        } finally {
            loadingMoreRef.current = false;
            setLoadingMore(false);
        }
    }, [applySessionPage, hasMoreSessions, nextBefore, nextBeforeId]);

    // Initial load (and refresh when sidebar is re-opened)
    useEffect(() => {
        let mounted = true;
        const load = async () => {
            try {
                const res = await getSessionSummaries({ limit: SESSION_PAGE_SIZE });
                if (mounted) applySessionPage(res, false);
            } catch {
                // Keep whatever was shown before on error
            } finally {
                if (mounted) setInitialLoading(false);
            }
        };
        load();
        return () => {
            mounted = false;
        };
    }, [isSidebarOpen, applySessionPage]);

    // Refresh immediately after an operation confirms durable session state. A
    // second delayed refresh picks up the asynchronously generated title.
    useEffect(() => {
        if (!sessionActivityEvent || sessionActivityEvent.sessionId !== sessionId) return;

        const immediateTimer = setTimeout(fetchSummaries, 100);
        const titleTimer = setTimeout(fetchSummaries, 2500);
        return () => {
            clearTimeout(immediateTimer);
            clearTimeout(titleTimer);
        };
    }, [fetchSummaries, sessionActivityEvent, sessionId]);

    useEffect(() => {
        const sentinel = loadMoreSentinelRef.current;
        const root = listScrollRef.current;
        if (!sentinel || !root || !hasMoreSessions) return;
        const observer = new IntersectionObserver(
            (entries) => {
                if (entries[0]?.isIntersecting) {
                    loadMoreSessions();
                }
            },
            { root, rootMargin: '80px' }
        );
        observer.observe(sentinel);
        return () => observer.disconnect();
    }, [hasMoreSessions, loadMoreSessions, sessionSummaries.length]);

    // Detect sessions that just appeared in the list and tag them for animation.
    // Skip on the very first load (prevIds is empty) so we don't animate stale history.
    useEffect(() => {
        const currentIds = new Set(sessionSummaries.map((s) => s.session_id));
        const prevIds = prevSessionIdsRef.current;
        let tid: ReturnType<typeof setTimeout> | undefined;

        if (skipAppearAnimRef.current) {
            skipAppearAnimRef.current = false;
            prevSessionIdsRef.current = currentIds;
            return;
        }

        if (prevIds.size > 0) {
            const appeared = [...currentIds].filter((id) => !prevIds.has(id));
            if (appeared.length > 0) {
                setNewSessionIds((prev) => new Set([...prev, ...appeared]));
                // Remove after animation completes (520ms + 300ms delay + buffer)
                tid = setTimeout(() => {
                    setNewSessionIds((prev) => {
                        const next = new Set(prev);
                        appeared.forEach((id) => next.delete(id));
                        return next;
                    });
                }, 1100);
            }
        }

        prevSessionIdsRef.current = currentIds;
        return () => {
            if (tid !== undefined) clearTimeout(tid);
        };
    }, [sessionSummaries]);

    // -----------------------------------------------------------------------
    // Handlers
    // -----------------------------------------------------------------------

    const handleNewSession = (anonymous = false, archiveCurrent = true) => {
        if (archiveCurrent && sessionId) {
            archiveWorkspace(sessionId).catch((err) =>
                console.warn('Workspace archive failed (non-blocking):', err)
            );
        }
        const newSessionId = uuidv4();
        setSessionId(newSessionId);
        persistSessionId(newSessionId);
        if (anonymous) {
            markSessionAnonymous(newSessionId);
        }
        setIsAnonymousSession(anonymous);
        clearMessages();
        // Clear live streaming/reasoning state so it doesn't bleed into the
        // new session while an old in-flight request is still running.
        useStore.getState().setStreamingContent(null);
        useStore.getState().setStreamingAgent(null);
        useStore.getState().setThinkingAgent(null);
        useStore.getState().setLiveTraceParts([]);
        setUploadedFile(null);
        setShowNewSessionMenu(false);
    };

    const handleOpenSession = async (targetSessionId: string) => {
        if (!targetSessionId || openingSessionId) return;
        setOpeningSessionId(targetSessionId);
        // Clear live state before switching so stale indicators from the
        // previous session don't flash on the incoming session's view.
        useStore.getState().setStreamingContent(null);
        useStore.getState().setStreamingAgent(null);
        useStore.getState().setThinkingAgent(null);
        useStore.getState().setLiveTraceParts([]);
        const previousSessionId = sessionId;
        const previousMessages = useStore.getState().messages;
        const previousAnon = useStore.getState().isAnonymousSession;
        clearMessages();
        // Switch the session id up front (before fetching history) so Chat's
        // dataset-hydration effect — keyed on sessionId — starts its own fetch
        // in parallel with the history call below, instead of waiting for it.
        setSessionId(targetSessionId);
        persistSessionId(targetSessionId);
        const anon = isSessionAnonymous(targetSessionId);
        setIsAnonymousSession(anon);
        try {
            const history = await getHistory(targetSessionId, 100);
            const normalized = (history?.messages || []).map((msg: any) => ({
                role: msg.role,
                content: msg.content,
                timestamp: new Date(msg.timestamp),
                metadata: msg.metadata || {},
                actionPrompt: msg.metadata?.action_prompt || null,
                status: 'completed',
            }));
            setMessages(normalized);
        } catch (error) {
            console.error('Failed to open session history:', error);
            setSessionId(previousSessionId);
            if (previousSessionId) persistSessionId(previousSessionId);
            setIsAnonymousSession(previousAnon);
            setMessages(previousMessages);
            useStore.getState().addToast({
                message: 'Failed to load session history. Please try again.',
                type: 'error',
            });
        } finally {
            setOpeningSessionId(null);
        }
    };

    // --- Session context-menu actions ---

    const openMenu = (e: React.MouseEvent, sid: string) => {
        e.stopPropagation();
        const next = menuOpenFor === sid ? null : sid;
        setMenuOpenFor(next);
        if (!next) setDeletingFor(null);
    };

    const handleStartRename = (sid: string) => {
        setMenuOpenFor(null);
        const current = sessionMeta[sid]?.name || '';
        setRenameValue(current);
        setRenamingFor(sid);
        // Input autofocuses via autoFocus prop, but also call focus() in case
        setTimeout(() => renameInputRef.current?.focus(), 0);
    };

    const commitRename = (sid: string) => {
        const trimmed = renameValue.trim();
        const next = { ...sessionMeta };
        if (trimmed) {
            next[sid] = { ...next[sid], name: trimmed };
        } else {
            // Empty → revert to default (remove custom name)
            const { name: _removed, ...rest } = next[sid] || {};
            next[sid] = rest;
        }
        setSessionMeta(next);
        saveSessionMeta(next);
        setRenamingFor(null);
    };

    const handleTogglePin = (sid: string) => {
        setMenuOpenFor(null);
        const next = { ...sessionMeta };
        next[sid] = { ...next[sid], pinned: !next[sid]?.pinned };
        setSessionMeta(next);
        saveSessionMeta(next);
    };

    const handleDeleteSessionById = async (sid: string) => {
        if (deletingId) return;
        setDeletingId(sid);
        setDeletingFor(null);
        setMenuOpenFor(null);
        const previousSummaries = sessionSummaries;
        const previousMeta = sessionMeta;
        const previousSessionId = sessionId;
        setSessionSummaries((prev) => prev.filter((s) => s.session_id !== sid));
        const next = { ...sessionMeta };
        delete next[sid];
        setSessionMeta(next);
        saveSessionMeta(next);
        const wasCurrent = sid === sessionId;
        if (wasCurrent) {
            handleNewSession(useStore.getState().isAnonymousSession, false);
        }
        try {
            await deleteSession(sid);
        } catch (error) {
            console.error('Failed to delete session:', error);
            setSessionSummaries(previousSummaries);
            setSessionMeta(previousMeta);
            saveSessionMeta(previousMeta);
            if (wasCurrent && previousSessionId) {
                setSessionId(previousSessionId);
                persistSessionId(previousSessionId);
            }
            useStore.getState().addToast({
                message: 'Failed to delete session. Please try again.',
                type: 'error',
            });
            fetchSummaries();
        } finally {
            setDeletingId(null);
        }
    };

    // -----------------------------------------------------------------------
    // Derived data — pinned sessions float to the top
    // -----------------------------------------------------------------------

    const sortedSessions = [...sessionSummaries].sort((a, b) => {
        const ap = sessionMeta[a.session_id]?.pinned ? 1 : 0;
        const bp = sessionMeta[b.session_id]?.pinned ? 1 : 0;
        if (bp !== ap) return bp - ap; // pinned float to top
        // Within each group, most recently active first
        const at = a.last_activity_at ? new Date(a.last_activity_at).getTime() : 0;
        const bt = b.last_activity_at ? new Date(b.last_activity_at).getTime() : 0;
        return bt - at;
    });

    // Collapsed: a slim strip that still offers "new session" and the way back.
    if (!isSidebarOpen) {
        return (
            <aside
                className="w-11 flex flex-col items-center gap-1 py-3 shrink-0"
                style={{
                    background: 'var(--c-slate)',
                    borderRight: '1px solid var(--c-glass-border)',
                }}
                aria-label="Sessions (collapsed)"
            >
                <button
                    type="button"
                    onClick={toggleSidebar}
                    title="Expand sessions"
                    aria-label="Expand sessions"
                    className="p-2 rounded-lg transition-colors hover:bg-[var(--c-glass)]"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    <PanelLeftOpen className="w-4 h-4" />
                </button>
                <button
                    type="button"
                    onClick={() => handleNewSession(false)}
                    title="New session"
                    aria-label="New session"
                    className="p-2 rounded-lg transition-colors hover:brightness-110"
                    style={{
                        background: 'var(--c-signal-dim)',
                        color: 'var(--c-signal-text)',
                    }}
                >
                    <Plus className="w-4 h-4" />
                </button>
            </aside>
        );
    }

    return (
        <aside
            className="w-52 flex flex-col shrink-0"
            style={{
                background: 'var(--c-slate)',
                borderRight: '1px solid var(--c-glass-border)',
            }}
        >
            <div
                className="flex items-center justify-between px-3 pt-3"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                <span className="text-[10px] uppercase tracking-widest font-semibold">
                    Sessions
                </span>
                <button
                    type="button"
                    onClick={toggleSidebar}
                    title="Collapse sessions"
                    aria-label="Collapse sessions"
                    className="p-1 rounded-md transition-colors hover:bg-[var(--c-glass)]"
                >
                    <PanelLeftClose className="w-3.5 h-3.5" />
                </button>
            </div>

            {/* New session — split button */}
            <div className="p-3 border-b" style={{ borderColor: 'var(--c-glass-border)' }}>
                <div className="relative" ref={newSessionMenuRef}>
                    <div
                        className="flex rounded-xl overflow-hidden"
                        style={{ border: '1px solid var(--c-signal-dim)' }}
                    >
                        <button
                            type="button"
                            onClick={() => handleNewSession(false)}
                            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium transition-colors hover:brightness-110"
                            style={{
                                background: 'var(--c-signal-dim)',
                                color: 'var(--c-signal-text)',
                            }}
                        >
                            <Plus className="w-4 h-4" />
                            New Session
                        </button>
                        <button
                            type="button"
                            onClick={() => setShowNewSessionMenu((v) => !v)}
                            className="px-2.5 border-l transition-colors hover:brightness-110"
                            style={{
                                borderColor: 'color-mix(in srgb, var(--c-signal) 20%, transparent)',
                                background: 'var(--c-signal-dim)',
                                color: 'var(--c-signal-text)',
                            }}
                            aria-label="More session options"
                        >
                            <ChevronDown className="w-3.5 h-3.5" />
                        </button>
                    </div>
                    {showNewSessionMenu && (
                        <div
                            className="absolute top-full left-0 right-0 mt-1 rounded-xl border z-20 overflow-hidden"
                            style={{
                                background: 'var(--c-slate-2)',
                                borderColor: 'var(--c-glass-border)',
                                boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
                            }}
                        >
                            <button
                                type="button"
                                onClick={() => handleNewSession(false)}
                                className="w-full flex items-center gap-2.5 px-3 py-2.5 text-sm transition-colors hover:bg-white/5"
                                style={{ color: 'var(--c-ink)' }}
                            >
                                <Plus
                                    className="w-3.5 h-3.5"
                                    style={{ color: 'var(--c-signal-text)' }}
                                />
                                New Session
                            </button>
                            <button
                                type="button"
                                onClick={() => handleNewSession(true)}
                                className="w-full flex items-center gap-2.5 px-3 py-2.5 text-sm transition-colors hover:bg-white/5"
                                style={{ color: 'var(--c-ink)' }}
                            >
                                <EyeOff
                                    className="w-3.5 h-3.5"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                />
                                <span>Anonymous Session</span>
                                <span
                                    className="ml-auto text-[10px]"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    no memory
                                </span>
                            </button>
                        </div>
                    )}
                </div>
            </div>

            <div ref={listScrollRef} className="flex-1 overflow-y-auto p-3 space-y-4">
                {/* Workspace navigation — surfaces that are not the chat itself. */}
                <div>
                    <p
                        className="text-[10px] uppercase tracking-widest font-semibold mb-2 px-1"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        Workspace
                    </p>
                    <button
                        onClick={() => setActiveView('ontology')}
                        className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg text-xs transition-colors"
                        style={
                            activeView === 'ontology'
                                ? {
                                      background: 'var(--c-signal-dim)',
                                      color: 'var(--c-signal-text)',
                                  }
                                : { color: 'var(--c-ink-2)' }
                        }
                        title="Define the semantic model the agent answers from"
                    >
                        <Network
                            className="w-3.5 h-3.5 flex-shrink-0"
                            style={{
                                color:
                                    activeView === 'ontology'
                                        ? 'var(--c-signal-text)'
                                        : 'var(--c-mist-fg)',
                            }}
                        />
                        Ontology
                        {ontologyBadge && activeView !== 'ontology' && (
                            <span
                                className="ml-auto w-1.5 h-1.5 rounded-full flex-shrink-0"
                                style={{ background: 'var(--c-signal)' }}
                            />
                        )}
                    </button>
                </div>

                {/* Session history */}
                <div>
                    <p className="text-[10px] mb-2 px-1" style={{ color: 'var(--c-mist-fg)' }}>
                        History
                    </p>
                    <div className="space-y-1.5">
                        {initialLoading && (
                            <div className="text-xs px-1" style={{ color: 'var(--c-mist-fg)' }}>
                                Loading sessions…
                            </div>
                        )}
                        {!initialLoading && sessionSummaries.length === 0 && (
                            <div className="text-xs px-1" style={{ color: 'var(--c-mist-fg)' }}>
                                No tracked sessions yet
                            </div>
                        )}

                        {sortedSessions.map((entry) => {
                            const sid = entry.session_id;
                            const isActive = sid === sessionId;
                            const isExpensive = entry.estimated_cost_usd >= 0.1;
                            const meta = sessionMeta[sid];
                            const displayName = meta?.name || entry.title || 'Untitled session';
                            const isPinned = meta?.pinned ?? false;
                            const isMenuOpen = menuOpenFor === sid;
                            const isRenaming = renamingFor === sid;

                            return (
                                <div
                                    key={sid}
                                    className={`relative group${newSessionIds.has(sid) ? ' session-materialize' : ''}`}
                                >
                                    {isRenaming ? (
                                        <div className="w-full rounded-lg border border-blue-400/40 bg-blue-500/10 px-2 py-2">
                                            <input
                                                ref={renameInputRef}
                                                autoFocus
                                                value={renameValue}
                                                onChange={(e) => setRenameValue(e.target.value)}
                                                onKeyDown={(e) => {
                                                    if (e.key === 'Enter') {
                                                        renameCancelledRef.current = false;
                                                        commitRename(sid);
                                                    }
                                                    if (e.key === 'Escape') {
                                                        renameCancelledRef.current = true;
                                                        setRenamingFor(null);
                                                    }
                                                }}
                                                onBlur={() => {
                                                    if (renameCancelledRef.current) {
                                                        renameCancelledRef.current = false;
                                                        return;
                                                    }
                                                    commitRename(sid);
                                                }}
                                                placeholder="Session name…"
                                                className={`w-full bg-transparent text-[11px] outline-none placeholder:${label} ${text}`}
                                            />
                                        </div>
                                    ) : (
                                        <button
                                            type="button"
                                            onClick={() => handleOpenSession(sid)}
                                            disabled={
                                                openingSessionId !== null || deletingId === sid
                                            }
                                            className="w-full rounded-lg border px-2 py-2 text-left transition-colors disabled:opacity-60"
                                            style={
                                                isActive
                                                    ? {
                                                          background: 'var(--c-signal-dim)',
                                                          borderColor:
                                                              'color-mix(in srgb, var(--c-signal) 30%, transparent)',
                                                      }
                                                    : {
                                                          background: 'var(--c-glass)',
                                                          borderColor: 'var(--c-glass-border)',
                                                      }
                                            }
                                        >
                                            <div className="flex items-center justify-between gap-2 pr-4">
                                                <div className="flex items-center gap-1.5 min-w-0">
                                                    {isPinned ? (
                                                        <Pin
                                                            className="w-2.5 h-2.5 shrink-0"
                                                            style={{
                                                                color: 'var(--c-signal-text)',
                                                            }}
                                                        />
                                                    ) : (
                                                        <Clock3
                                                            className="w-3 h-3 shrink-0"
                                                            style={{ color: 'var(--c-mist-fg)' }}
                                                        />
                                                    )}
                                                    <span
                                                        className="text-[11px] truncate"
                                                        style={{ color: 'var(--c-ink)' }}
                                                    >
                                                        {displayName}
                                                    </span>
                                                </div>
                                                <span
                                                    className="text-[10px] shrink-0"
                                                    style={{ color: 'var(--c-mist-fg)' }}
                                                >
                                                    {entry.request_count} req
                                                </span>
                                            </div>
                                            <div className="mt-1 flex items-center gap-1.5">
                                                <span
                                                    className="text-[10px] px-1.5 py-0.5 rounded"
                                                    style={{
                                                        background: 'var(--c-signal-dim)',
                                                        color: 'var(--c-signal-text)',
                                                        border: '1px solid color-mix(in srgb, var(--c-signal) 20%, transparent)',
                                                    }}
                                                >
                                                    {entry.total_tokens.toLocaleString()} tok
                                                </span>
                                                <span
                                                    className="text-[10px] px-1.5 py-0.5 rounded"
                                                    style={
                                                        isExpensive
                                                            ? {
                                                                  background:
                                                                      'rgba(248,113,113,0.1)',
                                                                  color: '#fca5a5',
                                                                  border: '1px solid rgba(248,113,113,0.3)',
                                                              }
                                                            : {
                                                                  background:
                                                                      'rgba(34,211,163,0.1)',
                                                                  color: '#6ee7b7',
                                                                  border: '1px solid rgba(34,211,163,0.25)',
                                                              }
                                                    }
                                                >
                                                    ${entry.estimated_cost_usd.toFixed(4)}
                                                </span>
                                                {entry.is_anonymous && (
                                                    <span
                                                        className={`flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded border border-slate-400/30 bg-slate-500/10 ${muted}`}
                                                    >
                                                        <EyeOff className="w-2.5 h-2.5" />
                                                        anon
                                                    </span>
                                                )}
                                            </div>
                                        </button>
                                    )}

                                    {!isRenaming && (
                                        <button
                                            type="button"
                                            data-session-menu="true"
                                            onClick={(e) => openMenu(e, sid)}
                                            className={`absolute right-1.5 top-2 w-5 h-5 flex items-center justify-center rounded transition-all hover:bg-white/10 ${
                                                isMenuOpen
                                                    ? 'opacity-100 bg-white/10'
                                                    : 'opacity-0 group-hover:opacity-100'
                                            }`}
                                            style={{ color: 'var(--c-ink-2)' }}
                                        >
                                            <MoreHorizontal className="w-3.5 h-3.5" />
                                        </button>
                                    )}

                                    {newSessionIds.has(sid) && (
                                        <div
                                            className="session-shimmer-overlay"
                                            aria-hidden="true"
                                        />
                                    )}

                                    {isMenuOpen && (
                                        <div
                                            data-session-menu="true"
                                            className="absolute right-0 top-full mt-0.5 z-50 rounded-lg border shadow-xl py-1 w-36"
                                            style={{
                                                background: 'var(--c-slate-2)',
                                                borderColor: 'var(--c-glass-border)',
                                                boxShadow: '0 12px 40px rgba(0,0,0,0.4)',
                                            }}
                                        >
                                            {deletingFor === sid ? (
                                                <div className="px-3 py-2" data-session-menu="true">
                                                    <p
                                                        className="text-[11px] mb-2 leading-snug"
                                                        style={{ color: 'var(--c-ink)' }}
                                                    >
                                                        Delete this session?
                                                    </p>
                                                    <div className="flex gap-1.5">
                                                        <button
                                                            type="button"
                                                            disabled={deletingId === sid}
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                handleDeleteSessionById(sid);
                                                            }}
                                                            className="flex-1 text-[11px] font-medium bg-red-500/20 hover:bg-red-500/30 text-red-400 rounded px-2 py-1 transition-colors disabled:opacity-60"
                                                        >
                                                            {deletingId === sid
                                                                ? 'Deleting…'
                                                                : 'Delete'}
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                setDeletingFor(null);
                                                            }}
                                                            className="flex-1 text-[11px] rounded px-2 py-1 transition-colors hover:bg-white/10"
                                                            style={{
                                                                color: 'var(--c-ink-2)',
                                                                background: 'var(--c-glass)',
                                                            }}
                                                        >
                                                            Cancel
                                                        </button>
                                                    </div>
                                                </div>
                                            ) : (
                                                <>
                                                    <button
                                                        type="button"
                                                        onClick={() => handleStartRename(sid)}
                                                        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors text-left hover:bg-white/5"
                                                        style={{ color: 'var(--c-ink-2)' }}
                                                    >
                                                        <Pencil className="w-3 h-3 shrink-0" />
                                                        Rename
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={() => handleTogglePin(sid)}
                                                        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors text-left hover:bg-white/5"
                                                        style={{ color: 'var(--c-ink-2)' }}
                                                    >
                                                        {isPinned ? (
                                                            <>
                                                                <PinOff className="w-3 h-3 shrink-0" />
                                                                Unpin
                                                            </>
                                                        ) : (
                                                            <>
                                                                <Pin className="w-3 h-3 shrink-0" />
                                                                Pin
                                                            </>
                                                        )}
                                                    </button>
                                                    <div
                                                        className="my-1 border-t"
                                                        style={{
                                                            borderColor: 'var(--c-glass-border)',
                                                        }}
                                                    />
                                                    <button
                                                        type="button"
                                                        onClick={() => setDeletingFor(sid)}
                                                        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-red-400 hover:text-red-300 hover:bg-red-500/10 transition-colors text-left"
                                                    >
                                                        <Trash2 className="w-3 h-3 shrink-0" />
                                                        Delete
                                                    </button>
                                                </>
                                            )}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                        {hasMoreSessions && (
                            <div
                                ref={loadMoreSentinelRef}
                                className="h-6 flex items-center justify-center"
                            >
                                {loadingMore && (
                                    <span
                                        className="text-[10px]"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        Loading more…
                                    </span>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </aside>
    );
}
