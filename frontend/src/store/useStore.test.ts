import { describe, it, expect, beforeEach } from 'vitest';

import { useStore, type TracePart } from './useStore';

const part = (over: Partial<TracePart>): TracePart => ({
    id: 'x',
    parentId: null,
    phase: 'start',
    ...over,
});

describe('liveTraceParts upsert', () => {
    beforeEach(() => useStore.getState().setLiveTraceParts([]));

    it('appends new parts in arrival order', () => {
        useStore.getState().upsertTracePart(part({ id: 'orch-1', agent: 'orchestrator', step: 1 }));
        useStore
            .getState()
            .upsertTracePart(part({ id: 'orch-1-1', parentId: 'orch-1', agent: 'analysis', step: 1 }));
        expect(useStore.getState().liveTraceParts.map((p) => p.id)).toEqual(['orch-1', 'orch-1-1']);
    });

    it('replaces an existing part in place (start → end) without reordering', () => {
        useStore.getState().upsertTracePart(part({ id: 'orch-1', phase: 'start' }));
        useStore.getState().upsertTracePart(part({ id: 'orch-2', phase: 'start' }));
        useStore
            .getState()
            .upsertTracePart(part({ id: 'orch-1', phase: 'end', observation: 'done' }));

        const parts = useStore.getState().liveTraceParts;
        expect(parts.map((p) => p.id)).toEqual(['orch-1', 'orch-2']);
        expect(parts[0].phase).toBe('end');
        expect(parts[0].observation).toBe('done');
    });

    it('keeps two sub-agents that reuse local step=1 distinct (core regression)', () => {
        useStore.getState().upsertTracePart(
            part({ id: 'orch-1-1', parentId: 'orch-1', agent: 'integration', step: 1, observation: 'int' })
        );
        useStore.getState().upsertTracePart(
            part({ id: 'orch-2-1', parentId: 'orch-2', agent: 'analysis', step: 1, observation: 'ana' })
        );

        const parts = useStore.getState().liveTraceParts;
        expect(parts).toHaveLength(2);
        expect(parts.find((p) => p.id === 'orch-1-1')?.observation).toBe('int');
        expect(parts.find((p) => p.id === 'orch-2-1')?.observation).toBe('ana');
    });
});

describe('session activity notifications', () => {
    it('records the session that needs a sidebar refresh', () => {
        useStore.getState().notifySessionActivity('session-1');

        expect(useStore.getState().sessionActivityEvent).toMatchObject({
            sessionId: 'session-1',
        });
        expect(useStore.getState().sessionActivityEvent?.at).toEqual(expect.any(Number));
    });
});

describe('primary uploaded file', () => {
    beforeEach(() => useStore.getState().clearUploadedFiles());

    const file = (id: string) => ({ file_id: id, filename: `${id}.csv`, rows: 10, columns: 3 });

    // Regression: `uploadedFile` used to be a `get uploadedFile()` accessor on the
    // store object. Zustand merges with `Object.assign({}, state, partial)`, which
    // invokes the accessor and copies its *return value* as a plain property — so
    // the first `set()` of any kind froze it at null forever.
    it('stays live after an unrelated set()', () => {
        useStore.getState().setIsLoading(true);

        useStore.getState().addUploadedFile(file('a'));

        expect(useStore.getState().uploadedFiles).toHaveLength(1);
        expect(useStore.getState().uploadedFiles[0] ?? null).toMatchObject({ file_id: 'a' });
    });

    it('tracks the first file as more are added and removed', () => {
        useStore.getState().addUploadedFile(file('a'));
        useStore.getState().addUploadedFile(file('b'));
        expect(useStore.getState().uploadedFiles[0] ?? null).toMatchObject({ file_id: 'a' });

        useStore.getState().removeUploadedFile('a');
        expect(useStore.getState().uploadedFiles[0] ?? null).toMatchObject({ file_id: 'b' });

        useStore.getState().removeUploadedFile('b');
        expect(useStore.getState().uploadedFiles[0] ?? null).toBeNull();
    });
});
