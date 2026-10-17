import { describe, it, expect } from 'vitest';

import { partsFromLegacy, computeTraceGap } from './AgentTrace';
import type { TracePart } from '../store/useStore';

const part = (o: Partial<TracePart> & Pick<TracePart, 'id' | 'phase'>): TracePart =>
    ({ parentId: null, step: 0, ...o }) as TracePart;

const live = { isLive: true, isStreaming: false };

describe('partsFromLegacy', () => {
    it('nests each sub-agent trace under its orchestrator step by step key', () => {
        const parts = partsFromLegacy({
            orchestrator_reasoning_trace: [
                { step: 1, action: 'call_agent', input: { agent: 'integration' } },
                { step: 2, action: 'call_agent', input: { agent: 'analysis' } },
            ],
            sub_agent_traces: {
                1: [{ step: 1, agent: 'integration', action: 'list_files' }],
                2: [{ step: 1, agent: 'analysis', action: 'run_script' }],
            },
        });

        // 2 orchestrator steps + 1 child each.
        expect(parts).toHaveLength(4);
        expect(parts.find((p) => p.id === 'legacy-orch-1')?.parentId).toBeNull();
        expect(parts.find((p) => p.parentId === 'legacy-orch-1')?.agent).toBe('integration');
        expect(parts.find((p) => p.parentId === 'legacy-orch-2')?.agent).toBe('analysis');
    });

    it('renders a flat sub-agent trace when there is no orchestrator', () => {
        const parts = partsFromLegacy({ reasoning_trace: [{ step: 1, agent: 'analysis' }] });
        expect(parts).toHaveLength(1);
        expect(parts[0].parentId).toBeNull();
    });

    it('returns [] for empty metadata', () => {
        expect(partsFromLegacy(undefined)).toEqual([]);
    });
});

describe('computeTraceGap', () => {
    it('no gap when not live, streaming, or empty', () => {
        const parts = [part({ id: 'orch-1', phase: 'end' })];
        expect(computeTraceGap(parts, { isLive: false, isStreaming: false })).toBeNull();
        expect(computeTraceGap(parts, { isLive: true, isStreaming: true })).toBeNull();
        expect(computeTraceGap([], live)).toBeNull();
    });

    it('orchestrator gap: a finished top-level step ⇒ planner deciding next', () => {
        // orchestrator → orchestrator
        const parts = [part({ id: 'orch-1', phase: 'end', action: 'run_python' })];
        expect(computeTraceGap(parts, live)).toEqual({ level: 'orchestrator' });
    });

    it('orchestrator gap after a sub-agent returns (subagent → orchestrator)', () => {
        // call_agent flipped to 'end'; its children still sit at the array tail.
        const parts = [
            part({ id: 'orch-1', phase: 'end', action: 'call_agent' }),
            part({ id: 'orch-1-1', parentId: 'orch-1', phase: 'end', agent: 'analysis' }),
        ];
        // Must NOT nest inside the finished sub-agent block.
        expect(computeTraceGap(parts, live)).toEqual({ level: 'orchestrator' });
    });

    it('subagent-start gap: call_agent running with no children yet', () => {
        // orchestrator → subagent
        const parts = [
            part({ id: 'orch-1', phase: 'start', action: 'call_agent', input: '{"agent":"analysis"}' }),
        ];
        expect(computeTraceGap(parts, live)).toEqual({
            level: 'subagent-start',
            parentId: 'orch-1',
            agentLabel: 'analysis',
        });
    });

    it('subagent gap: last child finished, sub-agent choosing its next tool', () => {
        // subagent → subagent
        const parts = [
            part({ id: 'orch-1', phase: 'start', action: 'call_agent' }),
            part({ id: 'orch-1-1', parentId: 'orch-1', phase: 'end', agent: 'analysis' }),
        ];
        expect(computeTraceGap(parts, live)).toEqual({
            level: 'subagent',
            parentId: 'orch-1',
            agentLabel: 'analysis',
        });
    });

    it('no gap while a step is actively running', () => {
        // top-level tool running
        expect(
            computeTraceGap([part({ id: 'orch-1', phase: 'start', action: 'run_python' })], live)
        ).toBeNull();
        // sub-agent step running
        expect(
            computeTraceGap(
                [
                    part({ id: 'orch-1', phase: 'start', action: 'call_agent' }),
                    part({ id: 'orch-1-1', parentId: 'orch-1', phase: 'start', agent: 'analysis' }),
                ],
                live
            )
        ).toBeNull();
    });
});
