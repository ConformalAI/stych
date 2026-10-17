import { describe, expect, it } from 'vitest';
import type { PreviewSource } from '../../services/api';
import { resolveFilterEventSource } from './sourceIdentity';

const source = (overrides: Partial<PreviewSource>): PreviewSource => ({
    id: 'dbx::base',
    kind: 'sql',
    name: 'table',
    renderer: 'table',
    is_active: false,
    ...overrides,
});

describe('resolveFilterEventSource', () => {
    it('prefers an exact dataset identity', () => {
        const expected = source({ id: 'dbx::copy:clean', role: 'working' });
        expect(resolveFilterEventSource([expected], expected.id)).toBe(expected);
    });

    it('maps a legacy temp event to the active named working copy', () => {
        const base = source({ id: 'dbx::base', file_id: 'dbx', role: 'base' });
        const active = source({
            id: 'dbx::copy:clean',
            file_id: 'dbx',
            role: 'working',
            is_active: true,
        });
        expect(resolveFilterEventSource([base, active], 'dbx::temp')).toBe(active);
    });

    it('does not map temp across dataset boundaries', () => {
        const active = source({
            id: 'other::copy:clean',
            file_id: 'other',
            role: 'working',
            is_active: true,
        });
        expect(resolveFilterEventSource([active], 'dbx::temp')).toBeUndefined();
    });
});
