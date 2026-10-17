import { ChevronDown, ChevronUp, Plus, RotateCcw, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import ChartTextureControl from './ChartTextureControl';
import {
    categoricalColorContext,
    colorForValue,
    colorPaletteOf,
    colorValueKey,
    continuousPaletteOf,
    continuousColorBinding,
    displayColorValue,
    effectiveColorMode,
    makeColorConfig,
    singleColorOf,
} from './chartColors';
import type { ColorConfig, ColorMode, ColorValue, TexturePattern, VizData, VizSpec } from './types';

const PRESET_PALETTES = [
    ['#0FB981', '#1FD68F', '#06B6D4', '#3B82F6', '#F59E0B'],
    ['#2563EB', '#06B6D4', '#10B981', '#84CC16', '#EAB308'],
    ['#7C3AED', '#DB2777', '#DC2626', '#EA580C', '#D97706'],
];
const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;
const MAX_COLORS = 12;

type AppearanceTab = 'color' | 'texture';

interface Props {
    spec: VizSpec;
    data?: VizData | null;
    texture?: { enabled: boolean; pattern: TexturePattern };
    onColorChange: (color: ColorConfig | null) => void;
    onTextureChange: (texture: { enabled: boolean; pattern: TexturePattern }) => void;
    onReset: () => void;
}

export function ColorInput({
    color,
    label,
    onChange,
}: {
    color: string;
    label: string;
    onChange: (color: string) => void;
}) {
    const [draft, setDraft] = useState(color.toUpperCase());

    useEffect(() => setDraft(color.toUpperCase()), [color]);

    const commit = () => {
        if (HEX_COLOR.test(draft)) onChange(draft.toUpperCase());
        else setDraft(color.toUpperCase());
    };

    return (
        <>
            <input
                type="color"
                value={color}
                aria-label={`${label} picker`}
                onChange={(event) => onChange(event.target.value.toUpperCase())}
                className="h-7 w-8 cursor-pointer rounded border bg-transparent p-0.5"
                style={{ borderColor: 'var(--c-glass-border)' }}
            />
            <input
                value={draft}
                aria-label={`${label} hex value`}
                onChange={(event) => setDraft(event.target.value)}
                onBlur={commit}
                onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                        commit();
                        event.currentTarget.blur();
                    }
                }}
                className="min-w-0 flex-1 rounded border px-1.5 py-1 font-mono text-[10px] uppercase outline-none"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: HEX_COLOR.test(draft)
                        ? 'var(--c-glass-border)'
                        : 'var(--c-danger)',
                    color: 'var(--c-ink-2)',
                }}
            />
        </>
    );
}

function PaletteEditor({
    colors,
    minimum = 1,
    onChange,
}: {
    colors: string[];
    minimum?: number;
    onChange: (colors: string[]) => void;
}) {
    const replaceColor = (index: number, color: string) =>
        onChange(colors.map((current, currentIndex) => (currentIndex === index ? color : current)));
    const moveColor = (index: number, direction: -1 | 1) => {
        const next = [...colors];
        const target = index + direction;
        [next[index], next[target]] = [next[target], next[index]];
        onChange(next);
    };

    return (
        <div className="flex flex-col gap-2">
            <div className="flex gap-1" aria-label="Palette presets">
                {PRESET_PALETTES.map((preset, index) => (
                    <button
                        key={index}
                        type="button"
                        aria-label={`Use palette ${index + 1}`}
                        onClick={() => onChange([...preset])}
                        className="flex h-5 flex-1 overflow-hidden rounded border"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        {preset.map((color) => (
                            <span
                                key={color}
                                className="h-full flex-1"
                                style={{ background: color }}
                            />
                        ))}
                    </button>
                ))}
            </div>
            {colors.map((color, index) => (
                <div key={index} className="flex items-center gap-1">
                    <ColorInput
                        color={color}
                        label={`Color ${index + 1}`}
                        onChange={(next) => replaceColor(index, next)}
                    />
                    <button
                        type="button"
                        aria-label={`Move color ${index + 1} up`}
                        disabled={index === 0}
                        onClick={() => moveColor(index, -1)}
                        className="disabled:opacity-25"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <ChevronUp className="h-3.5 w-3.5" />
                    </button>
                    <button
                        type="button"
                        aria-label={`Move color ${index + 1} down`}
                        disabled={index === colors.length - 1}
                        onClick={() => moveColor(index, 1)}
                        className="disabled:opacity-25"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <ChevronDown className="h-3.5 w-3.5" />
                    </button>
                    <button
                        type="button"
                        aria-label={`Remove color ${index + 1}`}
                        disabled={colors.length <= minimum}
                        onClick={() => onChange(colors.filter((_, current) => current !== index))}
                        className="disabled:opacity-25"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <X className="h-3.5 w-3.5" />
                    </button>
                </div>
            ))}
            <button
                type="button"
                disabled={colors.length >= MAX_COLORS}
                onClick={() => onChange([...colors, PRESET_PALETTES[0][colors.length % 5]])}
                className="flex items-center justify-center gap-1 rounded border py-1 text-[10px] font-semibold disabled:opacity-40"
                style={{
                    borderColor: 'var(--c-glass-border)',
                    color: 'var(--c-mist-fg)',
                }}
            >
                <Plus className="h-3 w-3" />
                Add color
            </button>
        </div>
    );
}

export default function ChartColorControl({
    spec,
    data,
    texture,
    onColorChange,
    onTextureChange,
    onReset,
}: Props) {
    const [activeTab, setActiveTab] = useState<AppearanceTab>('color');
    const mode = effectiveColorMode(spec, data);
    const categorical = categoricalColorContext(spec, data);
    const continuous = continuousColorBinding(spec, data);
    const modes: Array<[ColorMode, string]> = [
        ['single', 'Single'],
        ...(categorical ? ([['categorical', 'By value']] as Array<[ColorMode, string]>) : []),
        ...(continuous ? ([['continuous', 'Gradient']] as Array<[ColorMode, string]>) : []),
    ];
    const colors = mode === 'continuous' ? continuousPaletteOf(spec) : colorPaletteOf(spec);

    const changeMode = (nextMode: ColorMode) => {
        const next = makeColorConfig(spec, nextMode, data);
        if (next) onColorChange(next);
    };
    const changePalette = (palette: string[]) => {
        const base = makeColorConfig(spec, mode, data);
        if (!base) return;
        onColorChange(
            mode === 'single'
                ? { ...base, mode: 'single', value: palette[0] }
                : { ...base, palette }
        );
    };
    const assign = (value: ColorValue, color: string) => {
        const base = makeColorConfig(spec, 'categorical', data);
        if (!base) return;
        const assignments = (base.assignments ?? []).filter(
            (candidate) => colorValueKey(candidate.value) !== colorValueKey(value)
        );
        onColorChange({ ...base, assignments: [...assignments, { value, color }] });
    };

    return (
        <details
            open
            className="overflow-hidden rounded-lg border"
            style={{
                background: 'var(--c-glass)',
                borderColor: 'var(--c-glass-border)',
            }}
        >
            <summary
                className="cursor-pointer px-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: 'var(--c-ink-2)' }}
            >
                Color
            </summary>
            <div
                className="flex flex-col gap-2 border-t px-2 pb-2 pt-2"
                style={{ borderColor: 'var(--c-glass-border)' }}
            >
                <div className="flex justify-end">
                    <button
                        type="button"
                        onClick={onReset}
                        className="flex items-center gap-1 text-[9px] font-semibold"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <RotateCcw className="h-3 w-3" />
                        Revert to default
                    </button>
                </div>
                <div className="grid grid-cols-2 gap-1">
                    {(
                        [
                            ['color', 'Color'],
                            ['texture', 'Texture'],
                        ] as Array<[AppearanceTab, string]>
                    ).map(([value, label]) => (
                        <button
                            key={value}
                            type="button"
                            aria-pressed={activeTab === value}
                            onClick={() => setActiveTab(value)}
                            className="rounded-md border px-1 py-1 text-[10px] font-semibold"
                            style={{
                                background:
                                    activeTab === value ? 'var(--c-signal-dim)' : 'var(--c-glass)',
                                borderColor:
                                    activeTab === value
                                        ? 'var(--c-signal)'
                                        : 'var(--c-glass-border)',
                                color:
                                    activeTab === value
                                        ? 'var(--c-signal-text)'
                                        : 'var(--c-mist-fg)',
                            }}
                        >
                            {label}
                        </button>
                    ))}
                </div>

                {activeTab === 'color' && (
                    <div className="flex flex-col gap-2">
                        <div
                            className="grid gap-1"
                            style={{
                                gridTemplateColumns: `repeat(${modes.length}, minmax(0, 1fr))`,
                            }}
                        >
                            {modes.map(([value, label]) => (
                                <button
                                    key={value}
                                    type="button"
                                    aria-pressed={mode === value}
                                    onClick={() => changeMode(value)}
                                    className="rounded border px-1 py-1 text-[9px] font-semibold"
                                    style={{
                                        borderColor:
                                            mode === value
                                                ? 'var(--c-signal)'
                                                : 'var(--c-glass-border)',
                                        color:
                                            mode === value
                                                ? 'var(--c-signal-text)'
                                                : 'var(--c-mist-fg)',
                                    }}
                                >
                                    {label}
                                </button>
                            ))}
                        </div>

                        {mode === 'single' ? (
                            <div className="flex items-center gap-1">
                                <ColorInput
                                    color={singleColorOf(spec)}
                                    label="Chart color"
                                    onChange={(value) => {
                                        const base = makeColorConfig(spec, 'single', data);
                                        if (base) onColorChange({ ...base, value });
                                    }}
                                />
                            </div>
                        ) : (
                            <PaletteEditor
                                colors={colors}
                                minimum={mode === 'continuous' ? 2 : 1}
                                onChange={changePalette}
                            />
                        )}

                        {mode === 'categorical' && categorical && (
                            <div className="max-h-56 overflow-y-auto border-t pt-2">
                                <p
                                    className="mb-1.5 text-[9px]"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Assign colors by {categorical.binding.field}
                                </p>
                                <div className="flex flex-col gap-1.5">
                                    {categorical.values.map((value, index) => (
                                        <div
                                            key={colorValueKey(value)}
                                            className="flex items-center gap-1"
                                        >
                                            <span
                                                className="min-w-0 flex-1 truncate text-[10px]"
                                                title={displayColorValue(value)}
                                                style={{ color: 'var(--c-ink-2)' }}
                                            >
                                                {displayColorValue(value)}
                                            </span>
                                            <ColorInput
                                                color={colorForValue(
                                                    spec,
                                                    categorical.binding,
                                                    value,
                                                    index
                                                )}
                                                label={displayColorValue(value)}
                                                onChange={(next) => assign(value, next)}
                                            />
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {activeTab === 'texture' && (
                    <ChartTextureControl
                        markType={spec.mark.type}
                        texture={texture}
                        onChange={onTextureChange}
                    />
                )}
            </div>
        </details>
    );
}
