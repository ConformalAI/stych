import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({
    api: { get: vi.fn().mockResolvedValue({ data: {} }) },
}));

import { api } from './api';
import { getFilterPage } from './filters';

beforeEach(() => {
    vi.mocked(api.get).mockClear();
});

const BASE = { filter_id: 'f1', source: 'file' as const };

describe('getFilterPage rules serialization', () => {
    it('omits the rules param when no rule toggle is active', async () => {
        await getFilterPage('s1', { ...BASE });
        const params = vi.mocked(api.get).mock.calls[0][1]?.params;
        expect(params.rules).toBeUndefined();
    });

    it('sends an explicit empty array when every rule is disabled', async () => {
        // Regression: disabling the only (or every) rule in a filter used to collapse
        // to `undefined` here, silently re-enabling everything server-side instead of
        // selecting zero rows — the toggle looked like it worked (chip grayed out) but
        // had no effect on the grid.
        await getFilterPage('s1', { ...BASE, rules: [] });
        const params = vi.mocked(api.get).mock.calls[0][1]?.params;
        expect(params.rules).toBe('[]');
    });

    it('sends the enabled rule ids as a JSON array when some rules are disabled', async () => {
        await getFilterPage('s1', { ...BASE, rules: ['r1', 'r2'] });
        const params = vi.mocked(api.get).mock.calls[0][1]?.params;
        expect(params.rules).toBe('["r1","r2"]');
    });
});
