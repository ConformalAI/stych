import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import VizIntermediateTable from './VizIntermediateTable';
import type { FieldInfo, VizData } from './types';

const field: FieldInfo = {
    name: 'region',
    type: 'nominal',
    role: 'dimension',
    samples: ['EU', 'NA'],
};

describe('VizIntermediateTable', () => {
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

    it('shows catalog samples without treating a single field as an error', async () => {
        await act(async () =>
            root.render(<VizIntermediateTable field={field} data={null} loading={false} />)
        );

        expect(container.textContent).toContain('Add another field to build the chart');
        expect(container.textContent).toContain('EU');
        expect(container.querySelector('[role="alert"]')).toBeNull();
    });

    it('shows the server-derived count when materialized data is available', async () => {
        const data: VizData = {
            columns: [
                { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                { name: 'y', field: null, kind: 'measure', type: 'quantitative' },
            ],
            rows: [['EU', 12]],
            meta: { row_count: 1, truncated: false, engine: 'sql' },
        };
        await act(async () =>
            root.render(<VizIntermediateTable field={field} data={data} loading={false} />)
        );

        expect(container.textContent).toContain('Count');
        expect(container.textContent).toContain('12');
    });
});
