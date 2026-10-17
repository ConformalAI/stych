import type { VizSpec } from './types';

// Version 1 drafts could be written under the next session during a session
// transition. Do not restore those potentially cross-session editor states.
const STORAGE_VERSION = 2;

export interface StoredVizDraft {
    storageVersion: number;
    activeVizId: string | null;
    baseVersion: number | null;
    spec: VizSpec;
    title: string;
}

function editorKey(sessionId: string): string {
    return `stych_viz_editor_v${STORAGE_VERSION}:${sessionId}`;
}

function draftKey(sessionId: string, vizId: string | null): string {
    return `stych_viz_draft_v${STORAGE_VERSION}:${sessionId}:${vizId ?? 'new'}`;
}

function canUseStorage(): boolean {
    return typeof window !== 'undefined' && Boolean(window.localStorage);
}

export function readActiveVizId(sessionId: string): string | null {
    if (!canUseStorage()) return null;
    try {
        const parsed = JSON.parse(localStorage.getItem(editorKey(sessionId)) ?? 'null');
        return typeof parsed?.activeVizId === 'string' ? parsed.activeVizId : null;
    } catch {
        return null;
    }
}

export function readVizDraft(sessionId: string, vizId: string | null): StoredVizDraft | null {
    if (!canUseStorage()) return null;
    try {
        const parsed = JSON.parse(localStorage.getItem(draftKey(sessionId, vizId)) ?? 'null');
        if (
            parsed?.storageVersion !== STORAGE_VERSION ||
            typeof parsed?.spec !== 'object' ||
            typeof parsed?.spec?.mark !== 'object' ||
            typeof parsed?.spec?.encoding !== 'object'
        ) {
            return null;
        }
        return parsed as StoredVizDraft;
    } catch {
        return null;
    }
}

export function rememberActiveViz(sessionId: string, activeVizId: string | null): void {
    if (!canUseStorage()) return;
    try {
        localStorage.setItem(editorKey(sessionId), JSON.stringify({ activeVizId }));
    } catch {
        // Storage can be unavailable in private mode; the editor still works in memory.
    }
}

export function persistVizDraft(
    sessionId: string,
    draft: Omit<StoredVizDraft, 'storageVersion'>
): void {
    if (!canUseStorage()) return;
    const stored: StoredVizDraft = { storageVersion: STORAGE_VERSION, ...draft };
    try {
        localStorage.setItem(draftKey(sessionId, draft.activeVizId), JSON.stringify(stored));
    } catch {
        return;
    }
    rememberActiveViz(sessionId, draft.activeVizId);
}

export function clearVizDraft(sessionId: string, vizId: string | null): void {
    if (!canUseStorage()) return;
    try {
        localStorage.removeItem(draftKey(sessionId, vizId));
    } catch {
        // Best effort only; a failed removal must not block server operations.
    }
}
