import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useState } from 'react';
import ChartColorControl from './ChartColorControl';
import { emptySpec, type ColorConfig, type TexturePattern, type VizData } from './types';

let currentColor: ColorConfig | null | undefined;
let currentTexture: { enabled: boolean; pattern: TexturePattern } | undefined;

const chartData: VizData = {
    columns: [
        { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
        { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
    ],
    rows: [
        ['East', 10],
        ['West', 20],
    ],
    meta: { row_count: 2, truncated: false, engine: 'pandas' },
};

function Harness({
    initialColor = null,
    initialTexture = { enabled: false, pattern: 'diagonal' as const },
}: {
    initialColor?: ColorConfig | null;
    initialTexture?: { enabled: boolean; pattern: TexturePattern };
}) {
    const [color, setColor] = useState<ColorConfig | null>(initialColor);
    const [texture, setTexture] = useState(initialTexture);
    currentColor = color;
    currentTexture = texture;
    const spec = {
        ...emptySpec(),
        encoding: {
            x: { field: 'region', type: 'nominal' as const },
            y: {
                field: 'revenue',
                type: 'quantitative' as const,
                aggregate: 'sum' as const,
            },
        },
        config: { ...emptySpec().config, color, texture },
    };
    return (
        <ChartColorControl
            spec={spec}
            data={chartData}
            texture={texture}
            onColorChange={setColor}
            onTextureChange={setTexture}
            onReset={() => {
                setColor(null);
                setTexture({ enabled: false, pattern: 'diagonal' });
            }}
        />
    );
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
    const match = Array.from(container.querySelectorAll('button')).find(
        (candidate) => candidate.textContent?.trim() === label
    );
    if (!match) throw new Error(`Button "${label}" not found`);
    return match;
}

describe('ChartColorControl', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        currentColor = undefined;
        currentTexture = undefined;
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('switches a bar chart from one color to stable per-value assignments', async () => {
        await act(async () => root.render(<Harness />));

        expect(button(container, 'Single')).toBeTruthy();
        expect(button(container, 'By value')).toBeTruthy();
        await act(async () => button(container, 'By value').click());

        expect(currentColor).toMatchObject({
            mode: 'categorical',
            binding: { channel: 'x', field: 'region' },
        });
        expect(container.textContent).toContain('Assign colors by region');

        const east = container.querySelector('input[aria-label="East picker"]') as HTMLInputElement;
        await act(async () => {
            east.value = '#DC2626';
            east.dispatchEvent(new Event('change', { bubbles: true }));
        });

        expect(currentColor?.assignments).toContainEqual({
            value: 'East',
            color: '#DC2626',
        });
    });

    it('collapses and expands the entire Color section', async () => {
        await act(async () => root.render(<Harness />));

        const panel = container.querySelector('details') as HTMLDetailsElement;
        const summary = container.querySelector('summary') as HTMLElement;
        expect(panel.open).toBe(true);

        await act(async () => summary.click());
        expect(panel.open).toBe(false);

        await act(async () => summary.click());
        expect(panel.open).toBe(true);
    });

    it('turns textures on, selects a pattern, and turns them off', async () => {
        await act(async () => root.render(<Harness />));
        await act(async () => button(container, 'Texture').click());
        await act(async () => button(container, 'Dots').click());

        expect(currentTexture).toEqual({ enabled: true, pattern: 'dots' });

        const toggle = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
        await act(async () => toggle.click());
        expect(currentTexture).toEqual({ enabled: false, pattern: 'dots' });
    });

    it('reverts explicit color and texture settings to defaults', async () => {
        await act(async () =>
            root.render(
                <Harness
                    initialColor={{ mode: 'single', value: '#2563EB' }}
                    initialTexture={{ enabled: true, pattern: 'grid' }}
                />
            )
        );

        await act(async () => button(container, 'Revert to default').click());

        expect(currentColor).toBeNull();
        expect(currentTexture).toEqual({ enabled: false, pattern: 'diagonal' });
    });
});
