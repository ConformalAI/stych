import { CellValue, PreviewColumn } from './types';

export interface FormattedCell {
    text: string;
    /** Tailwind classes for the td. */
    className: string;
    /** Render dimmed (nulls, booleans). */
    muted: boolean;
    /** Full value for the title tooltip when the cell truncates. */
    title?: string;
}

const NUMBER_FORMAT: Intl.NumberFormatOptions = { maximumFractionDigits: 4 };

function formatIsoDate(value: string): string {
    // Backend sends ISO strings ("2026-01-02T00:00:00"); show time only when non-midnight.
    const [date, time] = value.split('T');
    if (!time || time.startsWith('00:00:00')) return date;
    return `${date} ${time.slice(0, 5)}`;
}

export function formatCell(value: CellValue, kind: PreviewColumn['kind']): FormattedCell {
    if (value === null || value === undefined) {
        return { text: '—', className: 'text-left', muted: true };
    }
    if (kind === 'number' && typeof value === 'number') {
        return {
            text: value.toLocaleString(undefined, NUMBER_FORMAT),
            className: 'text-right font-mono tabular-nums',
            muted: false,
        };
    }
    if (kind === 'bool' || typeof value === 'boolean') {
        return { text: String(value), className: 'text-left font-mono', muted: true };
    }
    if (kind === 'date' && typeof value === 'string') {
        return { text: formatIsoDate(value), className: 'text-left font-mono', muted: false };
    }
    const text = String(value);
    return {
        text,
        className: 'text-left truncate max-w-[280px]',
        muted: false,
        title: text.length > 40 ? text : undefined,
    };
}
