/**
 * Single typed gateway to localStorage. All keys are defined here; call sites
 * must go through these accessors instead of touching localStorage directly.
 * (Exception: vizDraftStorage.ts keeps its own versioned draft keys.)
 */

// ---------------------------------------------------------------------------
// Keys — the only place raw key strings live
// ---------------------------------------------------------------------------

const SESSION_ID_KEY = 'stych_session_id';

function sessionAnonKey(sessionId: string): string {
    return `stych_session_anon_${sessionId}`;
}

const SESSION_META_KEY = 'stych_session_meta';

function chatDraftKey(sessionId: string | null): string {
    return `stych_chat_draft:${sessionId ?? 'pending'}`;
}

function activeRunKey(sessionId: string): string {
    return `stych_active_agent_run:${sessionId}`;
}

const CHAT_THEME_KEY = 'stych_chat_theme';

const RAIL_EXPANDED_KEY = 'stych_rail_expanded';

// ---------------------------------------------------------------------------
// Low-level helpers
// ---------------------------------------------------------------------------

function canUseStorage(): boolean {
    if (typeof window === 'undefined') return false;
    try {
        return Boolean(window.localStorage);
    } catch {
        return false;
    }
}

function readRaw(key: string): string | null {
    if (!canUseStorage()) return null;
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
}

function writeRaw(key: string, value: string): void {
    if (!canUseStorage()) return;
    try {
        localStorage.setItem(key, value);
    } catch {
        // Storage can be unavailable in private mode; callers keep working in memory.
    }
}

function removeRaw(key: string): void {
    if (!canUseStorage()) return;
    try {
        localStorage.removeItem(key);
    } catch {
        // Best effort only.
    }
}

// ---------------------------------------------------------------------------
// Session id
// ---------------------------------------------------------------------------

export function getSessionId(): string | null {
    return readRaw(SESSION_ID_KEY);
}

export function setSessionId(sessionId: string): void {
    writeRaw(SESSION_ID_KEY, sessionId);
}

export function clearSessionId(): void {
    removeRaw(SESSION_ID_KEY);
}

// ---------------------------------------------------------------------------
// Session anonymity flag (per session)
// ---------------------------------------------------------------------------

export function isSessionAnonymous(sessionId: string): boolean {
    return readRaw(sessionAnonKey(sessionId)) === '1';
}

export function markSessionAnonymous(sessionId: string): void {
    writeRaw(sessionAnonKey(sessionId), '1');
}

// ---------------------------------------------------------------------------
// Session meta map (name/pin per session id)
// ---------------------------------------------------------------------------

export interface SessionMeta {
    name?: string;
    pinned?: boolean;
}

export type SessionMetaMap = Record<string, SessionMeta>;

export function loadSessionMeta(): SessionMetaMap {
    try {
        return JSON.parse(readRaw(SESSION_META_KEY) || '{}');
    } catch {
        return {};
    }
}

export function saveSessionMeta(meta: SessionMetaMap): void {
    writeRaw(SESSION_META_KEY, JSON.stringify(meta));
}

// ---------------------------------------------------------------------------
// Chat draft (per session; "pending" before a session exists)
// ---------------------------------------------------------------------------

export function getChatDraft(sessionId: string | null): string {
    return readRaw(chatDraftKey(sessionId)) ?? '';
}

/** Stores the draft; an empty draft clears the stored entry. */
export function setChatDraft(sessionId: string | null, draft: string): void {
    if (draft) writeRaw(chatDraftKey(sessionId), draft);
    else removeRaw(chatDraftKey(sessionId));
}

// ---------------------------------------------------------------------------
// Active agent run id (per session)
// ---------------------------------------------------------------------------

export function getActiveRunId(sessionId: string): string | null {
    return readRaw(activeRunKey(sessionId));
}

export function setActiveRunId(sessionId: string, runId: string): void {
    writeRaw(activeRunKey(sessionId), runId);
}

export function clearActiveRunId(sessionId: string): void {
    removeRaw(activeRunKey(sessionId));
}

// ---------------------------------------------------------------------------
// Chat theme
// ---------------------------------------------------------------------------

export type ChatTheme = 'light' | 'dark';

export function getChatTheme(): ChatTheme {
    return readRaw(CHAT_THEME_KEY) === 'light' ? 'light' : 'dark';
}

export function setChatTheme(theme: ChatTheme): void {
    writeRaw(CHAT_THEME_KEY, theme);
}

// ---------------------------------------------------------------------------
// Primary rail expanded flag
// ---------------------------------------------------------------------------

export function getRailExpanded(): boolean {
    return readRaw(RAIL_EXPANDED_KEY) === 'true';
}

export function setRailExpanded(expanded: boolean): void {
    writeRaw(RAIL_EXPANDED_KEY, String(expanded));
}
