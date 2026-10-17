import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import type { RefObject } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { EChartsRendererHandle } from './EChartsRenderer';
import type { UseVizSpec } from './useVizSpecTypes';
import type { VizData } from './types';
import VizToolbar from './VizToolbar';

const data: VizData = {
    columns: [{ name: 'x', field: 'region', kind: 'dimension', type: 'nominal' }],
    rows: [['EU']],
    meta: { row_count: 1, truncated: false, engine: 'pandas' },
};

function viz(): UseVizSpec {
    return {
        catalog: null,
        catalogError: null,
        calcErrors: [],
        saved: [],
        activeSource: 'dataset-1',
        activeVizId: null,
        activeVersion: null,
        spec: {} as UseVizSpec['spec'],
        renderedSpec: null,
        title: 'Sales',
        setTitle: vi.fn(),
        data,
        loading: false,
        saving: false,
        dirty: false,
        conflict: null,
        specErrors: [],
        queryError: null,
        updateSpec: vi.fn(),
        undo: vi.fn(),
        redo: vi.fn(),
        canUndo: false,
        canRedo: false,
        newViz: vi.fn(),
        selectSource: vi.fn(),
        selectViz: vi.fn(),
        save: vi.fn(),
        saveAsCopy: vi.fn(),
        reloadRemote: vi.fn(),
        keepDraft: vi.fn(),
        discardDraft: vi.fn(),
        remove: vi.fn(),
        refresh: vi.fn(),
        refreshSaved: vi.fn(),
    };
}

describe('VizToolbar exports', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;
    let createObjectUrl: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        createObjectUrl = vi.fn(() => 'blob:chart-data');
        Object.defineProperty(URL, 'createObjectURL', {
            configurable: true,
            value: createObjectUrl,
        });
        Object.defineProperty(URL, 'revokeObjectURL', {
            configurable: true,
            value: vi.fn(),
        });
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.restoreAllMocks();
    });

    it('confirms successful PNG and CSV exports', async () => {
        const chartRef = {
            current: { exportPng: vi.fn(() => 'data:image/png;base64,chart') },
        } as unknown as RefObject<EChartsRendererHandle>;
        await act(async () => {
            root.render(
                <VizToolbar
                    viz={viz()}
                    chartRef={chartRef}
                    fieldsCollapsed={false}
                    onShowFields={vi.fn()}
                    onShowHistory={vi.fn()}
                />
            );
        });

        await act(async () => {
            container.querySelector<HTMLButtonElement>('button[title="Export PNG"]')?.click();
        });
        expect(container.textContent).toContain('PNG exported.');

        await act(async () => {
            container.querySelector<HTMLButtonElement>('button[title="Export CSV"]')?.click();
        });
        expect(container.textContent).toContain('CSV exported.');
        expect(createObjectUrl).toHaveBeenCalledTimes(1);
    });

    it('shows an error when the chart renderer is not ready for PNG export', async () => {
        const chartRef = {
            current: { exportPng: vi.fn(() => null) },
        } as unknown as RefObject<EChartsRendererHandle>;
        await act(async () => {
            root.render(
                <VizToolbar
                    viz={viz()}
                    chartRef={chartRef}
                    fieldsCollapsed={false}
                    onShowFields={vi.fn()}
                    onShowHistory={vi.fn()}
                />
            );
        });

        await act(async () => {
            container.querySelector<HTMLButtonElement>('button[title="Export PNG"]')?.click();
        });
        expect(container.textContent).toContain('PNG export was not ready.');
    });
});
