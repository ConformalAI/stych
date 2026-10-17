import type { MarkType, TexturePattern } from './types';

const PATTERNS: Array<{ value: TexturePattern; label: string }> = [
    { value: 'diagonal', label: 'Diagonal' },
    { value: 'horizontal', label: 'Horizontal' },
    { value: 'vertical', label: 'Vertical' },
    { value: 'dots', label: 'Dots' },
    { value: 'grid', label: 'Grid' },
    { value: 'crosshatch', label: 'Crosshatch' },
];

interface Props {
    markType: MarkType;
    texture?: { enabled: boolean; pattern: TexturePattern };
    onChange: (texture: { enabled: boolean; pattern: TexturePattern }) => void;
}

function previewBackground(pattern: TexturePattern): string {
    const line = 'rgba(255,255,255,0.72)';
    switch (pattern) {
        case 'horizontal':
            return `repeating-linear-gradient(0deg, transparent 0 5px, ${line} 5px 7px)`;
        case 'vertical':
            return `repeating-linear-gradient(90deg, transparent 0 5px, ${line} 5px 7px)`;
        case 'dots':
            return `radial-gradient(circle, ${line} 1.5px, transparent 1.5px)`;
        case 'grid':
            return `linear-gradient(${line} 1px, transparent 1px), linear-gradient(90deg, ${line} 1px, transparent 1px)`;
        case 'crosshatch':
            return `repeating-linear-gradient(45deg, transparent 0 6px, ${line} 6px 7px), repeating-linear-gradient(-45deg, transparent 0 6px, ${line} 6px 7px)`;
        case 'diagonal':
            return `repeating-linear-gradient(135deg, transparent 0 5px, ${line} 5px 7px)`;
    }
}

export default function ChartTextureControl({ markType, texture, onChange }: Props) {
    const current = texture ?? { enabled: false, pattern: 'diagonal' };

    return (
        <div className="flex flex-col gap-2">
            <label
                className="flex items-center justify-between text-[11px] font-semibold"
                style={{ color: 'var(--c-ink-2)' }}
            >
                Use texture
                <input
                    type="checkbox"
                    checked={current.enabled}
                    onChange={(event) => onChange({ ...current, enabled: event.target.checked })}
                />
            </label>
            <div className="grid grid-cols-3 gap-1.5">
                {PATTERNS.map(({ value, label }) => (
                    <button
                        key={value}
                        type="button"
                        aria-pressed={current.enabled && current.pattern === value}
                        onClick={() => onChange({ enabled: true, pattern: value })}
                        className="flex flex-col gap-1 rounded-md border p-1 text-[9px] font-medium"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor:
                                current.enabled && current.pattern === value
                                    ? 'var(--c-signal)'
                                    : 'var(--c-glass-border)',
                            color:
                                current.enabled && current.pattern === value
                                    ? 'var(--c-signal-text)'
                                    : 'var(--c-mist-fg)',
                        }}
                    >
                        <span
                            className="h-7 w-full rounded-sm"
                            style={{
                                backgroundColor: '#0f9f73',
                                backgroundImage: previewBackground(value),
                                backgroundSize:
                                    value === 'dots' || value === 'grid' ? '8px 8px' : undefined,
                            }}
                        />
                        {label}
                    </button>
                ))}
            </div>
            {(markType === 'line' || markType === 'scatter') && current.enabled && (
                <p className="text-[9px] leading-tight" style={{ color: 'var(--c-mist-fg)' }}>
                    Texture changes the line or point style for this chart type.
                </p>
            )}
        </div>
    );
}
