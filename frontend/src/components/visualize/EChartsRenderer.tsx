/**
 * Thin ECharts wrapper: init/resize/dispose lifecycle, theme-aware re-render,
 * PNG export, and selection interactivity (click / shift-click on any mark;
 * rectangle brush on scatter). The option object comes exclusively from
 * compileSpec.ts — no ECharts types leak out of this directory. Selection is
 * expressed as tidy-table row indices via the `__row` datum tags.
 */

import { useEffect, useImperativeHandle, useRef, forwardRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
    AriaComponent,
    BrushComponent,
    GridComponent,
    LegendComponent,
    TitleComponent,
    ToolboxComponent,
    TooltipComponent,
    VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { useStore } from '../../store/useStore';
import { compileSpec, themeFromMode } from './compileSpec';
import { applySelectionStyle, rowsFromEventParams } from './selection';
import type { VizData, VizSpec } from './types';

echarts.use([
    BarChart,
    LineChart,
    PieChart,
    ScatterChart,
    HeatmapChart,
    GridComponent,
    TitleComponent,
    TooltipComponent,
    LegendComponent,
    VisualMapComponent,
    BrushComponent,
    ToolboxComponent,
    AriaComponent,
    CanvasRenderer,
]);

export interface EChartsRendererHandle {
    /** PNG data URL of the current chart (2x pixel ratio). */
    exportPng: () => string | null;
}

interface Props {
    spec: VizSpec;
    data: VizData;
    className?: string;
    /** Draw the title inside the canvas. Off where a surrounding header shows it. */
    showTitle?: boolean;
    /** Tidy-row indices currently selected (dim everything else). */
    selectedRows?: ReadonlySet<number>;
    /** Marks were clicked/brushed; additive = shift/cmd held. Empty = clear. */
    onSelectRows?: (rows: number[], additive: boolean) => void;
}

const EMPTY_SELECTION: ReadonlySet<number> = new Set();

const EChartsRenderer = forwardRef<EChartsRendererHandle, Props>(function EChartsRenderer(
    { spec, data, className, showTitle, selectedRows, onSelectRows },
    ref
) {
    const chatTheme = useStore((state) => state.chatTheme);
    const containerRef = useRef<HTMLDivElement>(null);
    const chartRef = useRef<echarts.ECharts | null>(null);
    const optionRef = useRef<Record<string, any> | null>(null);
    const onSelectRef = useRef<Props['onSelectRows']>(onSelectRows);
    const selectedRowsRef = useRef<ReadonlySet<number>>(selectedRows ?? EMPTY_SELECTION);
    onSelectRef.current = onSelectRows;
    selectedRowsRef.current = selectedRows ?? EMPTY_SELECTION;

    useImperativeHandle(ref, () => ({
        exportPng: () =>
            chartRef.current?.getDataURL({
                type: 'png',
                pixelRatio: 2,
                backgroundColor: chatTheme === 'dark' ? '#0b1524' : '#ffffff',
            }) ?? null,
    }));

    useEffect(() => {
        if (!containerRef.current) return;
        const chart = echarts.init(containerRef.current);
        chartRef.current = chart;

        chart.on('click', (params: any) => {
            const rows = rowsFromEventParams(params);
            if (rows.length === 0) return;
            const native = params.event?.event;
            const additive = Boolean(native?.shiftKey || native?.metaKey || native?.ctrlKey);
            onSelectRef.current?.(rows, additive);
        });
        // Click on empty canvas clears the selection.
        chart.getZr().on('click', (e: any) => {
            if (!e.target) onSelectRef.current?.([], false);
        });
        chart.on('brushSelected', (params: any) => {
            const batch = params?.batch?.[0];
            if (!batch) return;
            if (!(batch.areas?.length > 0)) {
                onSelectRef.current?.([], false);
                return;
            }
            const rows: number[] = [];
            const series = optionRef.current?.series;
            const seriesList = Array.isArray(series) ? series : [series];
            for (const sel of batch.selected ?? []) {
                const seriesData = seriesList[sel.seriesIndex]?.data;
                if (!Array.isArray(seriesData)) continue;
                for (const di of sel.dataIndex ?? []) {
                    const row = seriesData[di]?.__row;
                    if (typeof row === 'number') rows.push(row);
                }
            }
            onSelectRef.current?.(rows, false);
        });

        const observer = new ResizeObserver(() => chart.resize());
        observer.observe(containerRef.current);
        return () => {
            observer.disconnect();
            chart.dispose();
            chartRef.current = null;
        };
    }, []);

    useEffect(() => {
        const chart = chartRef.current;
        if (!chart) return;
        const option = compileSpec(spec, data, themeFromMode(chatTheme === 'dark'), { showTitle });
        if (spec.mark.type === 'scatter' && onSelectRows) {
            // Rectangle brush for region selection where clicks are impractical.
            option.brush = {
                toolbox: ['rect', 'clear'],
                xAxisIndex: 'all',
                throttleType: 'debounce',
                throttleDelay: 150,
                brushStyle: {
                    borderWidth: 1,
                    borderColor: chatTheme === 'dark' ? '#7dd3fc' : '#0f766e',
                    color:
                        chatTheme === 'dark'
                            ? 'rgba(125, 211, 252, 0.12)'
                            : 'rgba(15, 118, 110, 0.10)',
                },
                outOfBrush: { colorAlpha: 0.2 },
            };
            option.toolbox = {
                show: true,
                right: 8,
                top: 4,
                itemSize: 13,
                feature: { brush: { type: ['rect', 'clear'] } },
                iconStyle: { borderColor: chatTheme === 'dark' ? '#94a3b8' : '#64748b' },
            };
        }
        optionRef.current = option;
        chart.setOption(applySelectionStyle(option, selectedRowsRef.current), {
            notMerge: true,
        });
    }, [spec, data, chatTheme, showTitle, onSelectRows]);

    useEffect(() => {
        const chart = chartRef.current;
        const option = optionRef.current;
        if (!chart || !option) return;

        // A brush selection changes selectedRows. Replacing the full option here
        // would recreate the brush component and erase its rectangle, so only
        // refresh the series styles while ECharts retains the active brush area.
        const styled = applySelectionStyle(option, selectedRows ?? EMPTY_SELECTION);
        chart.setOption({ series: styled.series });
    }, [selectedRows]);

    const chartLabel = `${spec.title?.trim() || `${spec.mark.type} chart`}. ${data.meta.row_count.toLocaleString()} plotted points.`;
    return (
        <div
            ref={containerRef}
            className={className ?? 'w-full h-full min-h-[240px]'}
            role="img"
            aria-label={chartLabel}
            tabIndex={0}
        />
    );
});

export default EChartsRenderer;
