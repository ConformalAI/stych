import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { VizData, VizSpec } from './types';

const echarts = vi.hoisted(() => {
    const chart = {
        dispose: vi.fn(),
        getDataURL: vi.fn(),
        getZr: vi.fn(() => ({ on: vi.fn() })),
        on: vi.fn(),
        resize: vi.fn(),
        setOption: vi.fn(),
    };
    return { chart, init: vi.fn(() => chart), use: vi.fn() };
});

vi.mock('echarts/core', () => echarts);
vi.mock('echarts/charts', () => ({
    BarChart: {},
    HeatmapChart: {},
    LineChart: {},
    PieChart: {},
    ScatterChart: {},
}));
vi.mock('echarts/components', () => ({
    AriaComponent: {},
    BrushComponent: {},
    GridComponent: {},
    LegendComponent: {},
    TitleComponent: {},
    ToolboxComponent: {},
    TooltipComponent: {},
    VisualMapComponent: {},
}));
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }));
vi.mock('../../store/useStore', () => ({
    useStore: (selector: (state: { chatTheme: string }) => unknown) =>
        selector({ chatTheme: 'light' }),
}));
vi.mock('./compileSpec', () => ({
    compileSpec: vi.fn(() => ({ series: [{ data: [{ __row: 0, value: [1, 2] }] }] })),
    themeFromMode: vi.fn(),
}));
vi.mock('./selection', () => ({
    applySelectionStyle: (option: Record<string, unknown>) => option,
    rowsFromEventParams: vi.fn(() => []),
}));

import EChartsRenderer from './EChartsRenderer';

const spec: VizSpec = {
    mark: { type: 'scatter' },
    encoding: {
        x: { field: 'x', type: 'quantitative' },
        y: { field: 'y', type: 'quantitative' },
    },
};
const data: VizData = {
    columns: [],
    rows: [],
    meta: { row_count: 1, truncated: false, engine: 'pandas' },
};

describe('EChartsRenderer brush selection', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;
    let onSelectRows: (rows: number[], additive: boolean) => void;

    beforeEach(() => {
        vi.stubGlobal(
            'ResizeObserver',
            class {
                observe() {}
                disconnect() {}
            }
        );
        echarts.chart.setOption.mockClear();
        onSelectRows = vi.fn<(rows: number[], additive: boolean) => void>();
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.unstubAllGlobals();
    });

    it('does not recreate the brush when brushed rows update', async () => {
        await act(async () => {
            root.render(<EChartsRenderer spec={spec} data={data} onSelectRows={onSelectRows} />);
        });
        echarts.chart.setOption.mockClear();

        await act(async () => {
            root.render(
                <EChartsRenderer
                    spec={spec}
                    data={data}
                    onSelectRows={onSelectRows}
                    selectedRows={new Set([0])}
                />
            );
        });

        expect(echarts.chart.setOption).toHaveBeenCalledWith({
            series: [{ data: [{ __row: 0, value: [1, 2] }] }],
        });
        expect(echarts.chart.setOption).not.toHaveBeenCalledWith(
            expect.objectContaining({ brush: expect.anything() }),
            expect.anything()
        );
    });
});
