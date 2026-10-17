import { describe, expect, it, vi } from 'vitest';
import type { PreviewSource } from '../../services/api';

vi.mock('../../services/filters', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../../services/filters')>();
    return { ...actual, getFilterPage: vi.fn() };
});

import { getFilterPage } from '../../services/filters';
import { makeFilterPreviewProvider } from './filterProvider';

const SOURCE: PreviewSource = {
    id: 'cafe.csv',
    kind: 'file',
    name: 'cafe.csv',
    renderer: 'table',
    is_active: true,
} as PreviewSource;

const PAGE = {
    kind: 'filter' as const,
    columns: [
        { name: 'id', dtype: 'int64', kind: 'number' },
        { name: 'dup_score', dtype: 'float64', kind: 'number', origin: 'annotation' },
    ],
    rows: [[1, 0.95]],
    cell_meta: [[null, { k: 'danger' as const, l: 'dup', t: 'matches row 2' }]],
    row_meta: [{ g: 0, s: 'danger' as const, m: true }],
    total_rows: 10,
    filtered_rows: 4,
    page: 1,
    total_pages: 1,
    summary: {
        matched_rows: 4,
        total_rows: 10,
        approx: true,
        rule_counts: { r1: 4 },
        group_count: 2,
        identity_resolved: 'key:id',
        annotations: [],
    },
};

describe('makeFilterPreviewProvider', () => {
    it('encodes all view state in the provider key', () => {
        const a = makeFilterPreviewProvider('s1', SOURCE, 'f1', false, null, false);
        const b = makeFilterPreviewProvider('s1', SOURCE, 'f1', true, ['r1'], false);
        expect(a.key).not.toEqual(b.key);
        expect(a.key).toContain('filter:s1:file:cafe.csv:f1');
    });

    it('disables sorting while grouping is active', () => {
        expect(
            makeFilterPreviewProvider('s1', SOURCE, 'f1', false, null, true).capabilities?.sort
        ).toBe(false);
        expect(
            makeFilterPreviewProvider('s1', SOURCE, 'f1', false, null, false).capabilities?.sort
        ).toBe(true);
    });

    it('maps cell_meta/row_meta into grid channels and reports the summary', async () => {
        vi.mocked(getFilterPage).mockResolvedValue(PAGE);
        const onPageInfo = vi.fn();
        const provider = makeFilterPreviewProvider(
            's1',
            SOURCE,
            'f1',
            true,
            ['r1'],
            false,
            onPageInfo
        );

        const result = await provider.fetchPage({ offset: 0, limit: 100 });

        expect(vi.mocked(getFilterPage).mock.calls[0][1]).toMatchObject({
            filter_id: 'f1',
            source: 'file',
            path: 'cafe.csv',
            page: 1,
            matches_only: true,
            rules: ['r1'],
        });
        expect(result.cellMeta?.[0]?.[1]).toEqual({
            severity: 'danger',
            label: 'dup',
            tooltip: 'matches row 2',
        });
        expect(result.rowMeta?.[0]).toMatchObject({ group: 0, severity: 'danger', matched: true });
        expect(result.columns[1].note).toBe('filter annotation');
        expect(result.approx).toBe(true);
        expect(onPageInfo).toHaveBeenCalledWith(PAGE.summary);
    });
});
