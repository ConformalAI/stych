import { afterEach, describe, expect, it, vi } from 'vitest';
import { useStore } from '../store/useStore';
import { consumeAgentRunStream } from './agentRunStream';

vi.mock('../services/authHeaders', () => ({
    getAuthHeaders: vi.fn(async () => ({})),
}));

describe('consumeAgentRunStream', () => {
    afterEach(() => {
        vi.unstubAllGlobals();
        useStore.getState().resetSession();
    });

    it('commits a completed replay snapshot after reconnecting', async () => {
        useStore.setState({
            sessionId: 'session-1',
            messages: [
                {
                    role: 'user',
                    content: 'Analyze this',
                    timestamp: new Date(),
                },
            ],
        });
        const snapshot = {
            type: 'run_snapshot',
            run_id: 'run-1',
            session_id: 'session-1',
            status: 'completed',
            sequence: 8,
            streaming_content: 'Partial',
            thinking_content: null,
            parts: [{ id: 'step-1', status: 'completed' }],
            result: {
                type: 'result',
                run_id: 'run-1',
                response: 'Finished analysis',
                status: 'completed',
                metadata: { run_id: 'run-1' },
            },
            terminal_event: null,
        };
        vi.stubGlobal(
            'fetch',
            vi.fn(async () => new Response(`data: ${JSON.stringify(snapshot)}\n\n`))
        );

        const outcome = await consumeAgentRunStream(
            'run-1',
            'session-1',
            new AbortController().signal
        );

        expect(outcome).toBe('completed');
        expect(useStore.getState().messages.at(-1)).toMatchObject({
            role: 'assistant',
            content: 'Finished analysis',
            metadata: { run_id: 'run-1' },
        });
        expect(useStore.getState().streamingContent).toBeNull();
    });

    it('replays visualization and filter side effects from a reconnect snapshot', async () => {
        useStore.setState({ sessionId: 'session-1' });
        const snapshot = {
            type: 'run_snapshot',
            run_id: 'run-1',
            session_id: 'session-1',
            status: 'running',
            sequence: 2,
            artifact_events: [
                {
                    type: 'viz',
                    viz_id: 'viz-1',
                    action: 'created',
                    session_id: 'session-1',
                },
                {
                    type: 'filter',
                    filter_id: 'filter-1',
                    action: 'created',
                    session_id: 'session-1',
                },
            ],
        };
        const terminal = {
            type: 'result',
            run_id: 'run-1',
            response: 'Done',
            status: 'completed',
        };
        vi.stubGlobal(
            'fetch',
            vi.fn(
                async () =>
                    new Response(
                        `data: ${JSON.stringify(snapshot)}\n\ndata: ${JSON.stringify(terminal)}\n\n`
                    )
            )
        );

        await consumeAgentRunStream('run-1', 'session-1', new AbortController().signal);

        expect(useStore.getState().lastVizEvent?.vizId).toBe('viz-1');
        expect(useStore.getState().lastFilterEvent?.filterId).toBe('filter-1');
        expect(useStore.getState().lastFilterEvent?.fromReplay).toBe(true);
    });
});
