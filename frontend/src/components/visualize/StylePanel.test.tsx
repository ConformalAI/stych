import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useState } from 'react';
import StylePanel from './StylePanel';
import { emptySpec, type VizData, type VizSpec } from './types';

const data: VizData = {
    columns: [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'segment', kind: 'dimension', type: 'nominal' },
        { name: 'color', field: 'revenue', kind: 'measure', type: 'quantitative' },
    ],
    rows: [['East', 'Consumer', 10]],
    meta: { row_count: 1, truncated: false, engine: 'pandas' },
};

let currentSpec: VizSpec;

function Harness() {
    const [spec, setSpec] = useState<VizSpec>({
        ...emptySpec(),
        mark: { type: 'heatmap' },
        encoding: {
            x: { field: 'region', type: 'nominal' },
            y: { field: 'segment', type: 'nominal' },
            color: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
        },
        config: { ...emptySpec().config, labels: true },
    });
    currentSpec = spec;
    return <StylePanel spec={spec} data={data} updateSpec={(update) => setSpec(update)} />;
}

describe('StylePanel label color', () => {
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

    it('uses automatic contrast by default and supports a custom label color', async () => {
        await act(async () => root.render(<Harness />));
        const custom = Array.from(container.querySelectorAll('button')).find(
            (button) => button.textContent?.trim() === 'Custom'
        ) as HTMLButtonElement;

        expect(currentSpec.config?.labelColor).toBeNull();
        await act(async () => custom.click());
        expect(currentSpec.config?.labelColor).toBe('#111827');

        const picker = container.querySelector(
            'input[aria-label="Label color picker"]'
        ) as HTMLInputElement;
        await act(async () => {
            picker.value = '#F9FAFB';
            picker.dispatchEvent(new Event('change', { bubbles: true }));
        });
        expect(currentSpec.config?.labelColor).toBe('#F9FAFB');
    });
});
