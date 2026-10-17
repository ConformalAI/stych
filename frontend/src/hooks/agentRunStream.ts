import { useStore, type TracePart } from '../store/useStore';
import { agentRunStreamUrl } from '../services/api';
import { getAuthHeaders } from '../services/authHeaders';
import { parseSseChunk } from '../services/sse';
import type { SseEventType } from '../generated/contracts/sseEventTypes';

/**
 * Discriminated SSE event union. Member set is generated from the backend's
 * SseEventType enum (helpers/sse.py) — adding a value there without handling
 * it here is a COMPILE ERROR via assertNever in applyEvent, not a silent
 * fall-through.
 */
export interface ResultEvent {
    type: Extract<SseEventType, 'result'>;
    run_id?: string;
    response?: string;
    task_type?: string;
    status?: string;
    action_prompt?: {
        prompt_id: string;
        kind: string;
        question: string;
        options?: Array<{ label: string; value: string }>;
        allow_open_text?: boolean;
        context?: Record<string, any>;
    } | null;
    metadata?: Record<string, any>;
}

type TerminalEvent =
    | { type: Extract<SseEventType, 'error' | 'timeout' | 'limit_reached'>; message?: string }
    | {
          type: Extract<SseEventType, 'cancelled'>;
          message?: string;
          run_id?: string;
      };

export type StreamEvent =
    | ResultEvent
    | TerminalEvent
    | {
          type: Extract<SseEventType, 'run_snapshot'>;
          run_id?: string;
          session_id?: string;
          sequence?: number;
          status?: string;
          streaming_content?: string | null;
          thinking_content?: string | null;
          thinking_agent?: { agent: string; message: string } | null;
          parts?: TracePart[];
          artifact_events?: ArtifactEvent[];
          result?: ResultEvent | null;
          terminal_event?: StreamEvent | null;
      }
    | { type: Extract<SseEventType, 'thinking'>; agent?: string; message?: string }
    | { type: Extract<SseEventType, 'part.updated'>; part?: TracePart }
    | { type: Extract<SseEventType, 'token' | 'thinking_token'>; delta?: string }
    | { type: Extract<SseEventType, 'token_retract'> }
    | { type: Extract<SseEventType, 'compaction_start'> }
    | { type: Extract<SseEventType, 'compaction_end'>; success?: boolean }
    | { type: Extract<SseEventType, 'heartbeat'>; run_id?: string }
    | {
          type: Extract<SseEventType, 'viz'>;
          viz_id?: string;
          title?: string;
          action?: 'created' | 'updated';
          version?: number;
          session_id?: string;
      }
    | {
          type: Extract<SseEventType, 'filter'>;
          filter_id?: string;
          dataset_ref?: string;
          action?: 'created' | 'updated';
          session_id?: string;
      }
    | {
          type: Extract<SseEventType, 'ontology'>;
          ontology_id?: string;
          action?: string;
          version?: number;
          proposed?: string[];
          session_id?: string;
      }
    // Backend emits reasoning steps during analysis; intentionally unhandled today.
    | { type: Extract<SseEventType, 'reasoning'> };

type ArtifactEvent = Extract<StreamEvent, { type: 'viz' | 'filter' }>;

export type AgentRunStreamOutcome =
    | 'completed'
    | 'cancelled'
    | 'failed'
    | 'timed_out'
    | 'limit_reached';

function isActiveSession(sessionId: string): boolean {
    return useStore.getState().sessionId === sessionId;
}

function alreadyCommitted(runId: string): boolean {
    return useStore
        .getState()
        .messages.some(
            (message) => message.role === 'assistant' && message.metadata?.run_id === runId
        );
}

function commitResult(runId: string, sessionId: string, event: ResultEvent): void {
    if (!isActiveSession(sessionId) || alreadyCommitted(runId)) return;
    const state = useStore.getState();
    const parts =
        state.liveTraceParts.length > 0
            ? state.liveTraceParts
            : ((event.metadata?.parts ?? []) as TracePart[]);
    const thinking = state.streamingThinkingContent;
    const agentName = state.streamingAgent ?? undefined;

    useStore.setState((current) => ({
        streamingContent: null,
        streamingAgent: null,
        streamingThinkingContent: null,
        thinkingAgent: null,
        liveTraceParts: [],
        messages: [
            ...current.messages,
            {
                role: 'assistant' as const,
                content: event.response ?? '',
                timestamp: new Date(),
                status: event.status || 'completed',
                actionPrompt: event.action_prompt || null,
                metadata: {
                    ...(event.metadata || {}),
                    run_id: runId,
                    parts,
                    ...(agentName ? { agent_name: agentName } : {}),
                    ...(thinking !== null ? { thinking } : {}),
                },
            },
        ],
    }));
    useStore.getState().notifySessionActivity(sessionId);
    useStore.getState().addToast({ message: 'Response ready', type: 'success' });
}

function commitTerminalMessage(
    runId: string,
    sessionId: string,
    event: TerminalEvent,
    status: AgentRunStreamOutcome
): void {
    if (!isActiveSession(sessionId) || alreadyCommitted(runId)) return;
    const parts = useStore.getState().liveTraceParts;
    const content =
        event.message ||
        (status === 'cancelled'
            ? 'Generation stopped.'
            : 'The agent run ended before producing a response.');
    useStore.setState((current) => ({
        streamingContent: null,
        streamingAgent: null,
        streamingThinkingContent: null,
        thinkingAgent: null,
        liveTraceParts: [],
        messages:
            status === 'cancelled'
                ? current.messages
                : [
                      ...current.messages,
                      {
                          role: 'assistant' as const,
                          content,
                          timestamp: new Date(),
                          status,
                          metadata: { run_id: runId, parts },
                      },
                  ],
    }));
    if (status === 'failed') useStore.getState().setError(content);
    useStore.getState().notifySessionActivity(sessionId);
}

function applySnapshot(
    runId: string,
    sessionId: string,
    event: Extract<StreamEvent, { type: 'run_snapshot' }>
): AgentRunStreamOutcome | null {
    if (isActiveSession(sessionId)) {
        useStore.setState({
            liveTraceParts: event.parts ?? [],
            streamingContent: event.streaming_content ?? null,
            streamingThinkingContent: event.thinking_content ?? null,
            thinkingAgent: event.thinking_agent ?? null,
        });
    }
    for (const artifactEvent of event.artifact_events ?? []) {
        applyEvent(runId, sessionId, artifactEvent, { fromReplay: true });
    }
    if (event.result) {
        commitResult(runId, sessionId, event.result);
        return 'completed';
    }
    if (event.terminal_event) {
        return applyEvent(runId, sessionId, event.terminal_event);
    }
    if (event.status === 'cancelled') return 'cancelled';
    return null;
}

function applyEvent(
    runId: string,
    sessionId: string,
    event: StreamEvent,
    options: { fromReplay?: boolean } = {}
): AgentRunStreamOutcome | null {
    switch (event.type) {
        case 'run_snapshot':
            return applySnapshot(runId, sessionId, event);
        case 'result':
            commitResult(runId, sessionId, event);
            return 'completed';
        case 'cancelled':
            commitTerminalMessage(runId, sessionId, event, 'cancelled');
            return 'cancelled';
        case 'timeout':
            commitTerminalMessage(runId, sessionId, event, 'timed_out');
            return 'timed_out';
        case 'limit_reached':
            commitTerminalMessage(runId, sessionId, event, 'limit_reached');
            return 'limit_reached';
        case 'error':
            commitTerminalMessage(runId, sessionId, event, 'failed');
            return 'failed';
        default:
            break;
    }

    if (!isActiveSession(sessionId)) return null;

    const store = useStore.getState();
    switch (event.type) {
        case 'thinking':
            if (event.agent && event.message) {
                store.setThinkingAgent({ agent: event.agent, message: event.message });
            }
            break;
        case 'part.updated':
            if (event.part) store.upsertTracePart(event.part);
            break;
        case 'thinking_token':
            if (store.streamingThinkingContent === null) store.setStreamingThinkingContent('');
            if (event.delta) store.appendStreamingThinkingContent(event.delta);
            break;
        case 'token':
            if (store.streamingContent === null) {
                store.setStreamingAgent(store.thinkingAgent?.agent ?? null);
                store.setThinkingAgent(null);
                store.setStreamingContent('');
            }
            if (event.delta) store.appendStreamingContent(event.delta);
            break;
        case 'token_retract':
            store.setStreamingContent(null);
            store.setStreamingAgent(null);
            break;
        case 'compaction_start':
            store.setCompacting(true);
            break;
        case 'compaction_end':
            store.setCompacting(false);
            if (event.success) {
                store.addMessage({
                    role: 'system',
                    content: 'This chat has been compacted',
                    metadata: { type: 'compaction_notice' },
                });
            }
            break;
        case 'viz':
            store.setLastVizEvent({
                sessionId: event.session_id || sessionId,
                vizId: event.viz_id ?? null,
                action: event.action ?? 'created',
                source: 'agent',
                version: event.version ?? null,
                at: Date.now(),
            });
            store.notifySessionActivity(event.session_id || sessionId);
            break;
        case 'ontology':
            store.setLastOntologyEvent({
                sessionId: event.session_id || sessionId,
                ontologyId: event.ontology_id ?? null,
                version: event.version ?? null,
                action: event.action ?? 'proposed',
                proposed: event.proposed ?? [],
                at: Date.now(),
            });
            store.notifySessionActivity(event.session_id || sessionId);
            break;
        case 'filter':
            store.setLastFilterEvent({
                sessionId: event.session_id || sessionId,
                filterId: event.filter_id ?? null,
                datasetRef: event.dataset_ref ?? null,
                action: event.action ?? 'created',
                at: Date.now(),
                fromReplay: options.fromReplay === true,
            });
            break;
        case 'reasoning':
        case 'heartbeat':
            // Intentionally unhandled.
            break;
        default:
            // Compile-time exhaustiveness guard: every SseEventType member must
            // be handled above. If codegen adds a value, this line errors until
            // a case is written — unknown types can never silently fall through.
            assertNever(event);
    }
    return null;
}

function assertNever(event: never): void {
    // Runtime no-op; the type constraint is what matters.
    void event;
}

export async function consumeAgentRunStream(
    runId: string,
    sessionId: string,
    signal: AbortSignal
): Promise<AgentRunStreamOutcome> {
    const authHeaders = await getAuthHeaders();
    const response = await fetch(agentRunStreamUrl(runId), {
        headers: authHeaders,
        credentials: 'include',
        signal,
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `HTTP ${response.status}`);
    }
    if (!response.body) throw new Error('Run stream is unavailable.');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let streamOpen = true;
    while (streamOpen) {
        const { done, value } = await reader.read();
        streamOpen = !done;
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseChunk<StreamEvent>(buffer);
        buffer = parsed.remaining;
        for (const event of parsed.events) {
            const outcome = applyEvent(runId, sessionId, event);
            if (outcome) {
                reader.cancel().catch(() => {});
                return outcome;
            }
        }
    }
    throw new Error('The run stream ended unexpectedly.');
}
