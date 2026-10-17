import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PreviewSource } from '../../services/api';
import SourcePicker from './SourcePicker';

describe('SourcePicker capability disclosure', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('shows the brief capability summary and keeps the full matrix out of the UI', async () => {
        const source: PreviewSource = {
            id: 'mongo-1::base',
            kind: 'sql',
            name: 'analytics.events',
            renderer: 'table',
            origin: 'mongodb',
            role: 'base',
            supports_stats: false,
            capability_summary: 'Live preview · Analysis uses up to 10K documents',
            is_active: true,
        };
        await act(async () => {
            root.render(<SourcePicker sources={[source]} selected={source} onSelect={vi.fn()} />);
        });

        const trigger = container.querySelector('button');
        expect(trigger?.title).toBe(source.capability_summary);
        expect(trigger?.textContent).toContain(source.capability_summary);
        await act(async () => trigger?.dispatchEvent(new MouseEvent('click', { bubbles: true })));

        expect(container.textContent).toContain(source.capability_summary);
        expect(container.textContent).not.toContain('Primitive capabilities');
    });
});
