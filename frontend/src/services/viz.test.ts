import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({
    api: {
        put: vi.fn().mockResolvedValue({ data: {} }),
        post: vi.fn().mockResolvedValue({ data: { visualization: {} } }),
    },
}));

import { api } from './api';
import { restoreVizVersion, updateVisualization } from './viz';
import { emptySpec } from '../components/visualize/types';

beforeEach(() => {
    vi.mocked(api.put).mockClear();
    vi.mocked(api.post).mockClear();
});

describe('restoreVizVersion', () => {
    it('protects restores from overwriting a newer chart version', async () => {
        await restoreVizVersion('viz-1', 2, 7);

        expect(api.post).toHaveBeenCalledWith('/api/viz/visualizations/viz-1/restore/2', null, {
            params: { expected_version: 7 },
        });
    });
});

describe('updateVisualization', () => {
    it('sends the loaded version so a stale editor cannot overwrite newer work', async () => {
        await updateVisualization('viz-1', emptySpec(), 'Revenue', 7);

        expect(api.put).toHaveBeenCalledWith('/api/viz/visualizations/viz-1', {
            spec: emptySpec(),
            title: 'Revenue',
            expected_version: 7,
        });
    });
});
