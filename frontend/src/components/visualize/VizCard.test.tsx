import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore } from '../../store/useStore';
import type { VizData, VizSpec } from './types';

const services = vi.hoisted(() => ({
    getVisualizationData: vi.fn(),
}));

vi.mock('../../services/viz', () => services);
vi.mock('./EChartsRenderer', () => ({ default: () => null }));

import { VizCard } from './VizCard';

const spec: VizSpec = {
    title: 'Sales',
    mark: { type: 'bar' },
    encoding: {
        x: { field: 'region', type: 'nominal' },
        y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
    },
};
const data: VizData = {
    columns: [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
    ],
    rows: [['EU', 100]],
    meta: { row_count: 1, truncated: false, engine: 'pandas' },
};

describe('VizCard materialization lifecycle', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        services.getVisualizationData.mockResolvedValue({ spec, data });
        useStore.setState({ lastVizEvent: null, openVizId: null });
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.clearAllMocks();
    });

    async function render(vizId: string) {
        await act(async () => {
            root.render(<VizCard vizId={vizId} />);
            await Promise.resolve();
        });
    }

    async function remount(vizId: string) {
        await act(async () => root.unmount());
        root = createRoot(container);
        await render(vizId);
    }

    it('consumes each matching viz event once across unrelated events and remounts', async () => {
        const vizId = 'viz-event-version-cache';
        useStore.getState().setLastVizEvent({
            sessionId: 'session-1',
            vizId,
            action: 'created',
            source: 'agent',
            version: 1,
            at: 100,
        });

        await render(vizId);
        expect(services.getVisualizationData).toHaveBeenCalledTimes(1);

        await act(async () => {
            useStore.getState().setLastVizEvent({
                sessionId: 'session-1',
                vizId: 'another-viz',
                action: 'updated',
                source: 'agent',
                version: 2,
                at: 150,
            });
            await Promise.resolve();
        });
        expect(services.getVisualizationData).toHaveBeenCalledTimes(1);
        expect(container.textContent).not.toContain('Loading chart');

        await remount(vizId);
        expect(services.getVisualizationData).toHaveBeenCalledTimes(1);

        await act(async () => {
            useStore.getState().setLastVizEvent({
                sessionId: 'session-1',
                vizId,
                action: 'updated',
                source: 'agent',
                version: 2,
                at: 200,
            });
            await Promise.resolve();
        });
        expect(services.getVisualizationData).toHaveBeenCalledTimes(2);

        await remount(vizId);
        expect(services.getVisualizationData).toHaveBeenCalledTimes(2);
    });

    it('refreshes an embedded chart after the user saves that visualization', async () => {
        const vizId = 'saved-by-user';
        await render(vizId);
        expect(services.getVisualizationData).toHaveBeenCalledTimes(1);

        await act(async () => {
            useStore.getState().setLastVizEvent({
                sessionId: 'session-1',
                vizId,
                action: 'updated',
                source: 'user',
                version: 2,
                at: 300,
            });
            await Promise.resolve();
        });

        expect(services.getVisualizationData).toHaveBeenCalledTimes(2);
    });

    it('shows a stable empty state when the dataset was removed', async () => {
        const err = {
            response: {
                status: 410,
                data: { detail: { code: 'source_removed', message: 'gone' } },
            },
        };
        services.getVisualizationData.mockRejectedValueOnce(err);
        await render('viz-gone');
        expect(container.textContent).toContain('This dataset was removed from the chat.');
        expect(container.textContent).not.toContain('gone');
    });
});
