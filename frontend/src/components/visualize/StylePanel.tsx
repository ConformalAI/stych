/**
 * Style panel: mark type + mark options + chart config toggles.
 * Every control writes through updateSpec — presentation lives in the spec.
 */

import {
    AreaChart,
    BarChart2,
    CircleDot,
    Grid3X3,
    LineChart,
    PieChart,
    BarChartHorizontal,
} from 'lucide-react';
import type { MarkType, VizData, VizSpec } from './types';
import { MARK_CAPABILITIES, transitionMark } from './markCapabilities';
import ChartColorControl, { ColorInput } from './ChartColorControl';

interface Props {
    spec: VizSpec;
    data?: VizData | null;
    updateSpec: (updater: (prev: VizSpec) => VizSpec) => void;
}

const MARKS: Array<{ type: MarkType; label: string; Icon: typeof BarChart2 }> = [
    { type: 'bar', label: 'Bar', Icon: BarChart2 },
    { type: 'line', label: 'Line', Icon: LineChart },
    { type: 'area', label: 'Area', Icon: AreaChart },
    { type: 'scatter', label: 'Scatter', Icon: CircleDot },
    { type: 'pie', label: 'Pie', Icon: PieChart },
    { type: 'histogram', label: 'Histogram', Icon: BarChartHorizontal },
    { type: 'heatmap', label: 'Heatmap', Icon: Grid3X3 },
];

function Toggle({
    label,
    checked,
    onChange,
}: {
    label: string;
    checked: boolean;
    onChange: (v: boolean) => void;
}) {
    return (
        <label
            className="flex items-center justify-between text-[11px] cursor-pointer"
            style={{ color: 'var(--c-ink-2)' }}
        >
            {label}
            <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        </label>
    );
}

export default function StylePanel({ spec, data, updateSpec }: Props) {
    const mark = spec.mark;
    const config = spec.config ?? {};
    const capabilities = MARK_CAPABILITIES[mark.type];

    const setMark = (p: Partial<VizSpec['mark']>) =>
        updateSpec((prev) => ({ ...prev, mark: { ...prev.mark, ...p } }));
    const setConfig = (p: Partial<NonNullable<VizSpec['config']>>) =>
        updateSpec((prev) => ({ ...prev, config: { ...prev.config, ...p } }));
    const changeMark = (type: MarkType) => updateSpec((prev) => transitionMark(prev, type));

    return (
        <div className="flex flex-col gap-2">
            <p
                className="text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                Chart type
            </p>
            <div className="grid grid-cols-4 gap-1">
                {MARKS.map(({ type, label, Icon }) => (
                    <button
                        key={type}
                        type="button"
                        title={label}
                        onClick={() => changeMark(type)}
                        aria-pressed={mark.type === type}
                        className="flex flex-col items-center gap-0.5 rounded-md border px-1 py-1.5 text-[9px] font-medium transition-colors"
                        style={
                            mark.type === type
                                ? {
                                      background: 'var(--c-signal-dim)',
                                      borderColor:
                                          'color-mix(in srgb, var(--c-signal) 35%, transparent)',
                                      color: 'var(--c-signal-text)',
                                  }
                                : {
                                      background: 'var(--c-glass)',
                                      borderColor: 'var(--c-glass-border)',
                                      color: 'var(--c-mist-fg)',
                                  }
                        }
                    >
                        <Icon className="w-3.5 h-3.5" />
                        {label}
                    </button>
                ))}
            </div>

            <ChartColorControl
                spec={spec}
                data={data}
                texture={config.texture}
                onColorChange={(color) => setConfig({ color })}
                onTextureChange={(texture) => setConfig({ texture })}
                onReset={() =>
                    setConfig({
                        color: null,
                        palette: null,
                        texture: { enabled: false, pattern: 'diagonal' },
                    })
                }
            />

            <div className="flex flex-col gap-1.5 mt-1">
                {capabilities.stacked && (
                    <Toggle
                        label="Stacked"
                        checked={Boolean(mark.stacked)}
                        onChange={(v) => setMark({ stacked: v })}
                    />
                )}
                {(capabilities.smooth || capabilities.points) && (
                    <>
                        {capabilities.smooth && (
                            <Toggle
                                label="Smooth"
                                checked={Boolean(mark.smooth)}
                                onChange={(v) => setMark({ smooth: v })}
                            />
                        )}
                        {capabilities.points && (
                            <Toggle
                                label="Points"
                                checked={mark.point !== false}
                                onChange={(v) => setMark({ point: v })}
                            />
                        )}
                    </>
                )}
                {capabilities.legend && (
                    <Toggle
                        label="Legend"
                        checked={config.legend !== false}
                        onChange={(v) => setConfig({ legend: v })}
                    />
                )}
                {capabilities.grid && (
                    <Toggle
                        label="Grid lines"
                        checked={config.grid !== false}
                        onChange={(v) => setConfig({ grid: v })}
                    />
                )}
                {capabilities.labels && (
                    <>
                        <Toggle
                            label="Value labels"
                            checked={Boolean(config.labels)}
                            onChange={(v) => setConfig({ labels: v })}
                        />
                        {config.labels && (
                            <>
                                <label className="flex items-center justify-between gap-2 text-[11px]">
                                    <span style={{ color: 'var(--c-ink-2)' }}>Label size</span>
                                    <span className="flex items-center gap-1.5">
                                        <input
                                            aria-label="Label text size"
                                            type="range"
                                            min={8}
                                            max={32}
                                            value={
                                                config.labelFontSize ??
                                                (mark.type === 'heatmap'
                                                    ? 12
                                                    : mark.type === 'pie'
                                                      ? 11
                                                      : 10)
                                            }
                                            onChange={(event) =>
                                                setConfig({
                                                    labelFontSize: Number(event.target.value),
                                                })
                                            }
                                            className="w-20"
                                        />
                                        <span
                                            className="w-8 text-right text-[10px]"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {config.labelFontSize ??
                                                (mark.type === 'heatmap'
                                                    ? 12
                                                    : mark.type === 'pie'
                                                      ? 11
                                                      : 10)}
                                            px
                                        </span>
                                    </span>
                                </label>
                                <div className="flex items-start justify-between gap-2 text-[11px]">
                                    <span style={{ color: 'var(--c-ink-2)' }}>Label color</span>
                                    <div className="flex w-36 flex-col items-end gap-1">
                                        <div className="flex gap-1">
                                            <button
                                                type="button"
                                                aria-pressed={!config.labelColor}
                                                onClick={() => setConfig({ labelColor: null })}
                                                className="rounded border px-1.5 py-1 text-[9px] font-semibold"
                                                style={{
                                                    borderColor: !config.labelColor
                                                        ? 'var(--c-signal)'
                                                        : 'var(--c-glass-border)',
                                                    color: !config.labelColor
                                                        ? 'var(--c-signal-text)'
                                                        : 'var(--c-mist-fg)',
                                                }}
                                            >
                                                Auto
                                            </button>
                                            {!config.labelColor && (
                                                <button
                                                    type="button"
                                                    onClick={() =>
                                                        setConfig({ labelColor: '#111827' })
                                                    }
                                                    className="rounded border px-1.5 py-1 text-[9px] font-semibold"
                                                    style={{
                                                        borderColor: 'var(--c-glass-border)',
                                                        color: 'var(--c-mist-fg)',
                                                    }}
                                                >
                                                    Custom
                                                </button>
                                            )}
                                        </div>
                                        {config.labelColor && (
                                            <div className="flex w-full items-center gap-1">
                                                <ColorInput
                                                    color={config.labelColor}
                                                    label="Label color"
                                                    onChange={(labelColor) =>
                                                        setConfig({ labelColor })
                                                    }
                                                />
                                            </div>
                                        )}
                                    </div>
                                </div>
                            </>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}
