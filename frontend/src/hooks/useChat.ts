import { useCallback, useEffect, useRef, useState } from 'react';
import { v4 as uuidv4 } from 'uuid';
import {
    cancelAgentRun,
    createAgentRun,
    getHistory,
    getLatestAgentRun,
    type QueryRequest,
} from '../services/api';
import { useStore } from '../store/useStore';
import { clearActiveRunId, setActiveRunId } from '../services/storage';
import type { TaskType } from '../generated/contracts/taskTypes';
import { consumeAgentRunStream } from './agentRunStream';

const activeStatuses = new Set(['queued', 'running']);

function normalizeHistory(history: any) {
    return (history?.messages || []).map((message: any) => ({
        role: message.role,
        content: message.content,
        timestamp: new Date(message.timestamp),
        metadata: message.metadata || {},
        actionPrompt: message.metadata?.action_prompt || null,
        status: message.metadata?.status || 'completed',
    }));
}

function friendlyError(error: any): string {
    const raw = error?.response?.data?.detail || error?.message || 'Failed to send message';
    if (raw.includes('422')) {
        return "I couldn't process that request — the message format was invalid. Please try rephrasing.";
    }
    if (raw.includes('401') || raw.toLowerCase().includes('unauthorized')) {
        return 'Your session has expired. Redirecting to login...';
    }
    if (raw.includes('403') || raw.toLowerCase().includes('forbidden')) {
        return "You don't have permission to perform this action.";
    }
    if (raw.includes('404') || raw.toLowerCase().includes('not found')) {
        return 'The active run is no longer available. Its completed messages are still saved.';
    }
    if (
        raw.toLowerCase().includes('already has a query running') ||
        raw.toLowerCase().includes('concurrent queries') ||
        raw.toLowerCase().includes('too many concurrent')
    ) {
        return 'This chat already has work running. Reopen it to follow the response.';
    }
    if (raw.includes('429') || raw.toLowerCase().includes('rate limit')) {
        return 'Too many requests — please wait a moment and try again.';
    }
    if (raw.includes('502') || raw.includes('503') || raw.toLowerCase().includes('unavailable')) {
        return 'The AI service is currently unavailable. Please try again shortly.';
    }
    if (
        raw.includes('504') ||
        raw.toLowerCase().includes('timed out') ||
        raw.toLowerCase().includes('timeout')
    ) {
        return 'Analysis took too long. Try a simpler question or a smaller dataset.';
    }
    if (
        raw.toLowerCase().includes('network') ||
        raw.toLowerCase().includes('fetch') ||
        raw.toLowerCase().includes('load failed')
    ) {
        return 'Network error — the agent is still running in the background. Reopen this chat to reconnect.';
    }
    return raw;
}

export const useChat = (sessionId: string | null) => {
    const [isLoading, setIsLoading] = useState(false);
    const viewerAbortRef = useRef<AbortController | null>(null);
    const viewerSessionRef = useRef<string | null>(null);
    const activeRunRef = useRef<string | null>(null);
    const attachingRef = useRef(false);

    const attachRun = useCallback(async (runId: string, runSessionId: string) => {
        viewerAbortRef.current?.abort();
        const controller = new AbortController();
        viewerAbortRef.current = controller;
        viewerSessionRef.current = runSessionId;
        activeRunRef.current = runId;
        attachingRef.current = true;

        if (useStore.getState().sessionId === runSessionId) {
            setIsLoading(true);
            useStore.getState().setError(null);
        }

        let attempt = 0;
        try {
            while (attempt <= 2) {
                try {
                    await consumeAgentRunStream(runId, runSessionId, controller.signal);
                    clearActiveRunId(runSessionId);
                    return;
                } catch (error: any) {
                    if (error?.name === 'AbortError') return;
                    if (attempt >= 2) throw error;
                    await new Promise((resolve) => window.setTimeout(resolve, 1000 * 2 ** attempt));
                    attempt += 1;
                }
            }
        } catch (error: any) {
            if (error?.name === 'AbortError') return;
            const message = friendlyError(error);
            if (useStore.getState().sessionId === runSessionId) {
                useStore.getState().setError(message);
                useStore.getState().addMessage({ role: 'error', content: message });
            }
            if (
                message.toLowerCase().includes('session has expired') &&
                window.location.pathname !== '/login'
            ) {
                window.location.href = '/login';
            }
        } finally {
            if (viewerAbortRef.current === controller) {
                viewerAbortRef.current = null;
                viewerSessionRef.current = null;
                attachingRef.current = false;
                activeRunRef.current = null;
                setIsLoading(false);
                if (useStore.getState().sessionId === runSessionId) {
                    useStore.getState().setThinkingAgent(null);
                    useStore.getState().setCompacting(false);
                    useStore.getState().setStreamingContent(null);
                    useStore.getState().setStreamingAgent(null);
                    useStore.getState().setStreamingThinkingContent(null);
                }
            }
        }
    }, []);

    const sendMessage = useCallback(
        async (
            message: string,
            taskType: TaskType = 'auto',
            options?: {
                userAction?: {
                    prompt_id: string;
                    kind: string;
                    value: string;
                    label?: string;
                    text?: string;
                };
                userDisplayMessage?: string;
                suppressUserMessage?: boolean;
                targetFileId?: string;
            }
        ) => {
            const activeSessionId = useStore.getState().sessionId ?? sessionId;
            if (!activeSessionId) {
                useStore.getState().setError('No session ID available');
                return false;
            }
            if (attachingRef.current && viewerSessionRef.current === activeSessionId) return false;

            // Leaving another session only detaches its viewer. Its server-owned run continues.
            viewerAbortRef.current?.abort();
            useStore.getState().setError(null);
            useStore.getState().setThinkingAgent(null);
            useStore.getState().setCompacting(false);
            if (!options?.userAction) useStore.getState().setLiveTraceParts([]);
            setIsLoading(true);

            const store = useStore.getState();
            const request: QueryRequest = {
                session_id: activeSessionId,
                message: options?.userAction ? '__user_action__' : message,
                task_type: taskType,
                model_id: store.sessionModel ?? store.selectedModel ?? undefined,
                is_anonymous: store.isAnonymousSession || undefined,
                user_action: options?.userAction,
                enabled_integrations: store.enabledIntegrations ?? undefined,
                client_request_id: uuidv4(),
                target_file_id: options?.targetFileId,
            };

            try {
                const run = await createAgentRun(request);
                if (!options?.suppressUserMessage) {
                    useStore.getState().addMessage({
                        role: 'user',
                        content: options?.userDisplayMessage || message,
                    });
                }
                setActiveRunId(activeSessionId, run.run_id);
                await attachRun(run.run_id, activeSessionId);
                return true;
            } catch (error: any) {
                if (error?.response?.status === 409) {
                    const existing = await getLatestAgentRun(activeSessionId).catch(() => null);
                    if (existing && activeStatuses.has(existing.status)) {
                        setActiveRunId(activeSessionId, existing.run_id);
                        await attachRun(existing.run_id, activeSessionId);
                        return true;
                    }
                }
                const errorMessage = friendlyError(error);
                if (useStore.getState().sessionId === activeSessionId) {
                    useStore.getState().addMessage({ role: 'error', content: errorMessage });
                    useStore.getState().setError(errorMessage);
                }
                if (
                    errorMessage.toLowerCase().includes('session has expired') &&
                    window.location.pathname !== '/login'
                ) {
                    window.location.href = '/login';
                }
                setIsLoading(false);
                return false;
            }
        },
        [attachRun, sessionId]
    );

    const stopGeneration = useCallback(async () => {
        const runId = activeRunRef.current;
        const runSessionId = viewerSessionRef.current;
        if (runId) {
            await cancelAgentRun(runId).catch(() => {});
        }
        viewerAbortRef.current?.abort();
        if (runSessionId) clearActiveRunId(runSessionId);
        setIsLoading(false);
        useStore.getState().setThinkingAgent(null);
        useStore.getState().setCompacting(false);
        useStore.getState().setStreamingContent(null);
        useStore.getState().setStreamingAgent(null);
        useStore.getState().setStreamingThinkingContent(null);
        useStore.getState().setLiveTraceParts([]);
    }, []);

    useEffect(() => {
        if (!sessionId) return;
        let disposed = false;

        // A session switch closes only the old SSE viewer. The backend task is unaffected.
        if (viewerSessionRef.current && viewerSessionRef.current !== sessionId) {
            viewerAbortRef.current?.abort();
        }

        getLatestAgentRun(sessionId)
            .then(async (run) => {
                if (disposed || !run) return;
                if (activeStatuses.has(run.status)) {
                    setActiveRunId(sessionId, run.run_id);
                    await attachRun(run.run_id, sessionId);
                    return;
                }

                clearActiveRunId(sessionId);
                const history = await getHistory(sessionId, 100);
                if (!disposed && useStore.getState().sessionId === sessionId) {
                    useStore.getState().setMessages(normalizeHistory(history));
                }
            })
            .catch(() => {
                // A process restart legitimately loses the in-process run registry.
                clearActiveRunId(sessionId);
            });

        return () => {
            disposed = true;
            if (viewerSessionRef.current === sessionId) viewerAbortRef.current?.abort();
        };
    }, [attachRun, sessionId]);

    return { sendMessage, isLoading, stopGeneration };
};
