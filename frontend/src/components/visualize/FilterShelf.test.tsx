import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useState } from 'react';
import FilterShelf from './FilterShelf';
import { emptySpec, type VizSpec } from './types';

let currentSpec: VizSpec;

function Harness() {
    const [spec, setSpec] = useState<VizSpec>({
        ...emptySpec(),
        transform: [
            {
                op: 'filter_any',
                clauses: [[{ op: 'filter', field: 'region', predicate: 'eq', value: 'EU' }]],
            },
        ],
    });
    currentSpec = spec;
    return <FilterShelf spec={spec} catalog={null} updateSpec={(update) => setSpec(update)} />;
}

describe('FilterShelf exact selections', () => {
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

    it('shows and clears exact multi-mark selection filters', async () => {
        await act(async () => root.render(<Harness />));

        expect(container.textContent).toContain('Exact selection — 1 group');
        expect(container.textContent).not.toContain('No filters — full dataset');

        await act(async () => {
            container
                .querySelector<HTMLButtonElement>('[aria-label="Clear exact selection filter"]')
                ?.click();
        });

        expect(currentSpec.transform).toEqual([]);
        expect(container.textContent).toContain('No filters — full dataset');
    });
});
