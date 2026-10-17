import { create } from 'zustand';
import { v4 as uuidv4 } from 'uuid';
import type { AvailableModel } from '../services/api';
import { getChatTheme, setChatTheme } from '../services/storage';

export interface Toast {
    id: string;
    message: string;
    type: 'success' | 'error' | 'info';
}

/**
 * A single node in the agent reasoning trace. Stable `id` + optional `parentId`
 * (an orchestrator `call_agent` part's id) give exact, collision-free nesting that
 * is identical live and after persistence. Structurally a superset of `StepRow`,
 * so it renders directly through `StepItem`.
 */
export interface TracePart {
    id: string;
    parentId: string | null;
    agent?: string;
    type?: string;
    step?: number;
    phase: 'start' | 'end' | 'final' | 'pause' | 'permission' | 'error';
    thought?: string;
    action?: string;
    input?: any;
    observation?: string;
}

export interface Message {
    role: 'user' | 'assistant' | 'system' | 'error';
    content: string;
    timestamp: Date;
    status?: string;
    actionPrompt?: {
        prompt_id: string;
        kind: string;
        question: string;
        options?: Array<{ label: string; value: string }>;
        allow_open_text?: boolean;
        context?: Record<string, any>;
    } | null;
    metadata?: Record<string, any>;
}

export interface UploadedFileInfo {
    file_id: string;
    filename: string;
    rows: number;
    columns: number;
}

interface AppState {
    // Session
    sessionId: string | null;
    setSessionId: (id: string | null) => void;

    // Messages
    messages: Message[];
    addMessage: (message: Omit<Message, 'timestamp'>) => void;
    setMessages: (messages: Message[]) => void;
    clearMessages: () => void;
    resolvedActionPromptIds: string[];
    markActionPromptResolved: (promptId: string) => void;

    // File upload — supports multiple files for linking agent
    uploadedFiles: UploadedFileInfo[];
    addUploadedFile: (file: UploadedFileInfo) => void;
    removeUploadedFile: (fileId: string) => void;
    clearUploadedFiles: () => void;
    /** Replace the entire list — used to restore all datasets on session load. */
    setUploadedFiles: (files: UploadedFileInfo[]) => void;

    /**
     * Back-compat setter: replaces the entire list with a single file.
     * Existing code that calls `setUploadedFile(file)` continues to work.
     */
    setUploadedFile: (file: UploadedFileInfo | null) => void;

    // Pending query (set from landing page, consumed by chat page)
    pendingQuery: string | null;
    setPendingQuery: (query: string | null) => void;

    // Saved-filter events: the agent created/updated a filter (SSE 'filter' event).
    // Chat.tsx watches this to badge/flip the Data tab; FilterView consumes openFilterId.
    lastFilterEvent: {
        sessionId: string | null;
        filterId: string | null;
        datasetRef: string | null;
        action: string;
        at: number;
        fromReplay?: boolean;
    } | null;
    setLastFilterEvent: (e: AppState['lastFilterEvent']) => void;
    openFilterId: string | null;
    setOpenFilterId: (id: string | null) => void;

    // MCP integrations
    enabledIntegrations: string[] | null;
    setEnabledIntegrations: (ids: string[] | null) => void;

    // UI state
    isLoading: boolean;
    setIsLoading: (loading: boolean) => void;

    isSidebarOpen: boolean;
    toggleSidebar: () => void;

    error: string | null;
    setError: (error: string | null) => void;

    // Reset all user-scoped session/chat state (call on login, signup, logout)
    resetSession: () => void;

    // App-wide theme — applies to sidebar, header, and chat area
    chatTheme: 'dark' | 'light';
    toggleChatTheme: () => void;

    // Anonymous session — long-term memory (Hindsight) fully disabled
    isAnonymousSession: boolean;
    setIsAnonymousSession: (v: boolean) => void;

    // Real-time agent thinking state (populated by SSE stream)
    thinkingAgent: { agent: string; message: string } | null;
    setThinkingAgent: (state: { agent: string; message: string } | null) => void;

    // Cross-turn conversation compaction — transient "compacting…" indicator
    isCompacting: boolean;
    setCompacting: (v: boolean) => void;

    // Live agent trace — a single ordered, append-only list of parts. Each part has
    // a stable `id` and an optional `parentId` linking a sub-agent's steps to the
    // orchestrator `call_agent` part that spawned them. This replaces the old
    // integer-step-keyed dual arrays (which collided across sequential sub-agents).
    liveTraceParts: TracePart[];
    setLiveTraceParts: (parts: TracePart[]) => void;
    /** Upsert a part by `id`, preserving insertion order (events arrive in order). */
    upsertTracePart: (part: TracePart) => void;

    // In-app toast notifications
    toasts: Toast[];
    addToast: (t: Omit<Toast, 'id'>) => void;
    removeToast: (id: string) => void;

    // Available models — fetched once per login, cleared on logout
    availableModels: AvailableModel[];
    setAvailableModels: (models: AvailableModel[]) => void;

    // Per-user selected model (cached from Settings — global default)
    selectedModel: string | null;
    setSelectedModel: (model: string | null) => void;

    // Per-session model override (only changeable before first message is sent)
    sessionModel: string | null;
    setSessionModel: (model: string | null) => void;

    // Live token stream — non-null while the LLM is writing its final answer
    streamingContent: string | null;
    setStreamingContent: (content: string | null) => void;
    appendStreamingContent: (delta: string) => void;
    // Which agent triggered the current streaming bubble (for the label)
    streamingAgent: string | null;
    setStreamingAgent: (agent: string | null) => void;

    // Live chain-of-thought stream for reasoning models (Qwen3, DeepSeek-R1, etc.)
    // Non-null when the model is actively emitting thinking tokens via reasoning_content.
    streamingThinkingContent: string | null;
    setStreamingThinkingContent: (content: string | null) => void;
    appendStreamingThinkingContent: (delta: string) => void;

    // Latest durable visualization change. Agent events refresh the editor/list;
    // both agent and user events invalidate matching live chat embeds.
    lastVizEvent: {
        sessionId: string;
        vizId: string | null;
        action: 'created' | 'updated';
        source: 'agent' | 'user';
        version: number | null;
        at: number;
    } | null;
    setLastVizEvent: (e: AppState['lastVizEvent']) => void;

    // Signals that a backend operation created or updated durable session state.
    sessionActivityEvent: { sessionId: string; at: number } | null;
    notifySessionActivity: (sessionId: string) => void;

    // Click-through from a chat VizCard: switches Chat to the Visualize tab
    // and selects this viz; VizView consumes and clears it.
    openVizId: string | null;
    setOpenVizId: (id: string | null) => void;
    // Which workspace surface Chat is showing. Lives here rather than in
    // Chat.tsx local state so the sidebar and chat cards can switch tabs.
    activeView: 'chat' | 'data' | 'visualize' | 'ontology';
    setActiveView: (view: AppState['activeView']) => void;

    // Latest agent-authored ontology change (a proposal). Refreshes the
    // Ontology tab and badges it when the user is looking elsewhere.
    lastOntologyEvent: {
        sessionId: string;
        ontologyId: string | null;
        version: number | null;
        action: string;
        proposed: string[];
        at: number;
    } | null;
    setLastOntologyEvent: (e: AppState['lastOntologyEvent']) => void;

    // Unread marker on the sidebar's Ontology entry. Set when an agent proposes
    // definitions while the user is elsewhere; cleared when they open it.
    ontologyBadge: boolean;
    setOntologyBadge: (on: boolean) => void;

    // Click-through from a chat proposal card: opens the Ontology tab with this
    // definition selected; OntologyView consumes and clears it.
    openOntologyName: string | null;
    setOpenOntologyName: (name: string | null) => void;
}

export const useStore = create<AppState>((set) => ({
    // Session
    sessionId: null,
    setSessionId: (id) => set({ sessionId: id }),

    // Messages
    messages: [],
    addMessage: (message) =>
        set((state) => ({
            messages: [...state.messages, { ...message, timestamp: new Date() }],
        })),
    setMessages: (messages) => set({ messages }),
    clearMessages: () => set({ messages: [] }),
    resolvedActionPromptIds: [],
    markActionPromptResolved: (promptId) =>
        set((state) => ({
            resolvedActionPromptIds: (() => {
                const MAX_RESOLVED_ACTION_PROMPTS = 200;
                if (state.resolvedActionPromptIds.includes(promptId)) {
                    return state.resolvedActionPromptIds;
                }
                const updated = [...state.resolvedActionPromptIds, promptId];
                return updated.slice(-MAX_RESOLVED_ACTION_PROMPTS);
            })(),
        })),

    // File upload — multiple files
    uploadedFiles: [],
    addUploadedFile: (file) =>
        set((state) => {
            // Avoid duplicates by file_id
            if (state.uploadedFiles.some((f) => f.file_id === file.file_id)) {
                return state;
            }
            return { uploadedFiles: [...state.uploadedFiles, file] };
        }),
    removeUploadedFile: (fileId) =>
        set((state) => ({
            uploadedFiles: state.uploadedFiles.filter((f) => f.file_id !== fileId),
        })),
    clearUploadedFiles: () => set({ uploadedFiles: [] }),
    setUploadedFiles: (files) => set({ uploadedFiles: files }),

    setUploadedFile: (file) => set({ uploadedFiles: file ? [file] : [] }),

    // Pending query
    pendingQuery: null,
    setPendingQuery: (query) => set({ pendingQuery: query }),

    // Saved-filter events
    lastFilterEvent: null,
    setLastFilterEvent: (e) => set({ lastFilterEvent: e }),
    openFilterId: null,
    setOpenFilterId: (id) => set({ openFilterId: id }),

    // UI state
    isLoading: false,
    setIsLoading: (loading) => set({ isLoading: loading }),

    isSidebarOpen: true,
    toggleSidebar: () => set((state) => ({ isSidebarOpen: !state.isSidebarOpen })),

    error: null,
    setError: (error) => set({ error: error }),

    // MCP integrations
    enabledIntegrations: null,
    setEnabledIntegrations: (ids) => set({ enabledIntegrations: ids }),

    resetSession: () =>
        set({
            sessionId: null,
            messages: [],
            uploadedFiles: [],
            resolvedActionPromptIds: [],
            pendingQuery: null,
            lastFilterEvent: null,
            openFilterId: null,
            isLoading: false,
            error: null,
            thinkingAgent: null,
            isCompacting: false,
            liveTraceParts: [],
            streamingContent: null,
            streamingAgent: null,
            streamingThinkingContent: null,
            lastVizEvent: null,
            sessionActivityEvent: null,
            openVizId: null,
            activeView: 'chat',
            lastOntologyEvent: null,
            ontologyBadge: false,
            openOntologyName: null,
            toasts: [],
            sessionModel: null,
            isAnonymousSession: false,
            enabledIntegrations: null,
            // Clear model list so a subsequent login always re-fetches for
            // the new user rather than inheriting the previous user's models.
            availableModels: [],
            selectedModel: null,
        }),

    chatTheme: getChatTheme(),
    toggleChatTheme: () =>
        set((state) => {
            const next = state.chatTheme === 'dark' ? 'light' : 'dark';
            setChatTheme(next);
            return { chatTheme: next };
        }),

    isAnonymousSession: false,
    setIsAnonymousSession: (v) => set({ isAnonymousSession: v }),

    thinkingAgent: null,
    setThinkingAgent: (state) => set({ thinkingAgent: state }),

    isCompacting: false,
    setCompacting: (v) => set({ isCompacting: v }),

    liveTraceParts: [],
    setLiveTraceParts: (parts) => set({ liveTraceParts: parts }),
    upsertTracePart: (part) =>
        set((state) => {
            const idx = state.liveTraceParts.findIndex((p) => p.id === part.id);
            if (idx === -1) {
                return { liveTraceParts: [...state.liveTraceParts, part] };
            }
            // Full replace — the backend sends the complete part on every emission.
            const next = [...state.liveTraceParts];
            next[idx] = part;
            return { liveTraceParts: next };
        }),

    streamingContent: null,
    setStreamingContent: (content) => set({ streamingContent: content }),
    appendStreamingContent: (delta) =>
        set((state) => ({
            streamingContent:
                state.streamingContent !== null ? state.streamingContent + delta : delta,
        })),

    streamingAgent: null,
    setStreamingAgent: (agent) => set({ streamingAgent: agent }),

    streamingThinkingContent: null,
    setStreamingThinkingContent: (content) => set({ streamingThinkingContent: content }),
    appendStreamingThinkingContent: (delta) =>
        set((state) => ({
            streamingThinkingContent:
                state.streamingThinkingContent !== null
                    ? state.streamingThinkingContent + delta
                    : delta,
        })),

    lastVizEvent: null,
    setLastVizEvent: (e) => set({ lastVizEvent: e }),
    sessionActivityEvent: null,
    notifySessionActivity: (sessionId) =>
        set({ sessionActivityEvent: { sessionId, at: Date.now() } }),

    openVizId: null,
    setOpenVizId: (id) => set({ openVizId: id }),

    activeView: 'chat',
    setActiveView: (view) => set({ activeView: view }),

    lastOntologyEvent: null,
    setLastOntologyEvent: (e) => set({ lastOntologyEvent: e }),

    ontologyBadge: false,
    setOntologyBadge: (on) => set({ ontologyBadge: on }),

    openOntologyName: null,
    setOpenOntologyName: (name) => set({ openOntologyName: name }),

    toasts: [],
    addToast: (t) =>
        set((state) => ({
            toasts: [...state.toasts, { ...t, id: uuidv4() }],
        })),
    removeToast: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),

    availableModels: [],
    setAvailableModels: (models) => set({ availableModels: models }),

    selectedModel: null,
    setSelectedModel: (model) => set({ selectedModel: model }),

    sessionModel: null,
    setSessionModel: (model) => set({ sessionModel: model }),
}));

/**
 * The session's primary dataset — the first entry in `uploadedFiles`, or null.
 *
 * This replaces what used to be a `get uploadedFile()` accessor on the store
 * object. Zustand merges updates with `Object.assign({}, state, partial)`,
 * which *invokes* an accessor on the source and copies its return value as a
 * plain property. The getter was therefore evaluated once — while
 * `uploadedFiles` was still empty — and frozen at `null` by the first `set()`
 * of any kind, so every consumer gated on it was permanently dead. Reading
 * through a selector keeps the value live.
 */
export const usePrimaryFile = (): UploadedFileInfo | null =>
    useStore((state) => state.uploadedFiles[0] ?? null);
