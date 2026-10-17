import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import type { VizData } from './types';

interface Props {
    data: VizData;
    onClose: () => void;
}

const DISPLAY_LIMIT = 200;

export default function ChartDataTable({ data, onClose }: Props) {
    const closeRef = useRef<HTMLButtonElement>(null);
    useEffect(() => {
        closeRef.current?.focus();
        const onKey = (event: KeyboardEvent) => {
            if (event.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [onClose]);

    return (
        <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="chart-data-title"
            className="absolute inset-2 z-30 flex flex-col rounded-xl border shadow-2xl overflow-hidden"
            style={{ background: 'var(--c-slate)', borderColor: 'var(--c-glass-border)' }}
        >
            <div
                className="flex items-center justify-between gap-3 px-3 py-2 border-b"
                style={{ borderColor: 'var(--c-glass-border)' }}
            >
                <div>
                    <h2
                        id="chart-data-title"
                        className="text-xs font-semibold"
                        style={{ color: 'var(--c-ink)' }}
                    >
                        Chart data
                    </h2>
                    <p className="text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                        Showing {Math.min(data.rows.length, DISPLAY_LIMIT).toLocaleString()} of{' '}
                        {data.rows.length.toLocaleString()} plotted rows
                    </p>
                </div>
                <button
                    ref={closeRef}
                    type="button"
                    onClick={onClose}
                    aria-label="Close chart data"
                    className="rounded-md p-1"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    <X className="w-4 h-4" />
                </button>
            </div>
            <div className="flex-1 min-h-0 overflow-auto">
                <table className="min-w-full border-collapse text-xs">
                    <thead className="sticky top-0" style={{ background: 'var(--c-slate)' }}>
                        <tr>
                            {data.columns.map((column) => (
                                <th
                                    key={column.name}
                                    scope="col"
                                    className="px-3 py-2 text-left font-semibold border-b whitespace-nowrap"
                                    style={{
                                        color: 'var(--c-ink)',
                                        borderColor: 'var(--c-glass-border)',
                                    }}
                                >
                                    {column.field ?? column.name}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {data.rows.slice(0, DISPLAY_LIMIT).map((row, rowIndex) => (
                            <tr key={rowIndex}>
                                {data.columns.map((column, columnIndex) => (
                                    <td
                                        key={column.name}
                                        className="px-3 py-1.5 border-b whitespace-nowrap"
                                        style={{
                                            color: 'var(--c-ink-2)',
                                            borderColor: 'var(--c-glass-border)',
                                        }}
                                    >
                                        {row[columnIndex] == null ? '—' : String(row[columnIndex])}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
