import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';
import { CellValue, ColumnStats, PreviewColumn } from './types';

const KIND_LABEL: Record<PreviewColumn['kind'], string> = {
    number: '123',
    string: 'abc',
    date: 'date',
    bool: 'T/F',
    other: '···',
};

interface ColumnHeaderProps {
    column: PreviewColumn;
    sortDir: 'asc' | 'desc' | null;
    stats?: ColumnStats | null;
    onToggleSort: () => void;
    sortable?: boolean;
    statsEnabled?: boolean;
}

const STATUS_COLOR: Record<string, string> = {
    added: 'var(--c-success)',
    renamed: '#8b5cf6',
    common: '#8b5cf6',
};

function statValue(value: CellValue | undefined): string {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number') {
        return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
    }
    const text = String(value);
    return text.includes('T') ? text.split('T')[0] : text;
}

export default function ColumnHeader({
    column,
    sortDir,
    stats,
    onToggleSort,
    sortable = true,
    statsEnabled = true,
}: ColumnHeaderProps) {
    const [showStats, setShowStats] = useState(false);
    const containerRef = useRef<HTMLTableCellElement>(null);

    useEffect(() => {
        if (!showStats) return;
        const onClickOutside = (event: MouseEvent) => {
            if (!containerRef.current?.contains(event.target as Node)) {
                setShowStats(false);
            }
        };
        document.addEventListener('mousedown', onClickOutside);
        return () => document.removeEventListener('mousedown', onClickOutside);
    }, [showStats]);

    const SortIcon = sortDir === 'asc' ? ArrowUp : sortDir === 'desc' ? ArrowDown : ArrowUpDown;

    return (
        <th
            ref={containerRef}
            className="relative text-left font-medium px-3 h-9 border-b whitespace-nowrap sticky top-0 z-10"
            style={{
                borderColor: 'var(--c-glass-border)',
                background: 'var(--c-slate)',
                color: 'var(--c-mist-fg)',
            }}
        >
            <div className="flex items-center gap-1.5">
                <button
                    type="button"
                    onClick={sortable ? onToggleSort : undefined}
                    className={`flex items-center gap-1 transition-colors hover:opacity-100 ${
                        sortable ? '' : 'cursor-default'
                    }`}
                    style={{ color: sortDir ? 'var(--c-signal-text)' : 'inherit' }}
                    title={sortable ? `Sort by ${column.name}` : column.name}
                >
                    <span style={{ fontFamily: "'Space Grotesk', sans-serif" }}>
                        {column.name}
                    </span>
                    {sortable && <SortIcon className={`w-3 h-3 ${sortDir ? '' : 'opacity-40'}`} />}
                </button>
                <button
                    type="button"
                    onClick={statsEnabled ? () => setShowStats((v) => !v) : undefined}
                    className={`px-1 py-0.5 rounded text-[9px] font-mono uppercase tracking-wide border transition-colors ${
                        statsEnabled ? 'hover:bg-white/5' : 'cursor-default'
                    }`}
                    style={{
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-mist-fg)',
                    }}
                    title={
                        statsEnabled ? `${column.dtype} — click for column stats` : column.dtype
                    }
                >
                    {KIND_LABEL[column.kind]}
                </button>
                {column.note && (
                    <span
                        className="px-1 py-0.5 rounded text-[9px] font-mono whitespace-nowrap"
                        style={{
                            color: STATUS_COLOR[column.status ?? 'common'],
                            background: `color-mix(in srgb, ${
                                STATUS_COLOR[column.status ?? 'common']
                            } 14%, transparent)`,
                        }}
                        title={column.note}
                    >
                        {column.note}
                    </span>
                )}
            </div>

            {showStats && (
                <div
                    className="absolute left-0 top-full mt-1 z-20 rounded-lg border p-3 text-xs font-normal normal-case shadow-lg min-w-[180px]"
                    style={{
                        borderColor: 'var(--c-glass-border)',
                        background: 'var(--c-slate)',
                        color: 'var(--c-ink-2)',
                    }}
                >
                    <div
                        className="font-medium mb-2 truncate max-w-[220px]"
                        style={{ color: 'var(--c-ink)' }}
                    >
                        {column.name}
                        <span className="ml-2 font-mono text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                            {column.dtype}
                        </span>
                    </div>
                    {stats ? (
                        <dl className="space-y-1">
                            {(
                                [
                                    ['Nulls', stats.nulls.toLocaleString()],
                                    [
                                        'Unique',
                                        stats.unique === null || stats.unique === undefined
                                            ? '—'
                                            : stats.unique.toLocaleString(),
                                    ],
                                    ...(stats.min !== undefined
                                        ? ([['Min', statValue(stats.min)]] as const)
                                        : []),
                                    ...(stats.max !== undefined
                                        ? ([['Max', statValue(stats.max)]] as const)
                                        : []),
                                    ...(stats.mean !== undefined && stats.mean !== null
                                        ? ([['Mean', statValue(stats.mean)]] as const)
                                        : []),
                                ] as Array<readonly [string, string]>
                            ).map(([label, value]) => (
                                <div key={label} className="flex justify-between gap-4">
                                    <dt style={{ color: 'var(--c-mist-fg)' }}>{label}</dt>
                                    <dd className="font-mono">{value}</dd>
                                </div>
                            ))}
                        </dl>
                    ) : (
                        <div style={{ color: 'var(--c-mist-fg)' }}>Stats unavailable</div>
                    )}
                </div>
            )}
        </th>
    );
}
