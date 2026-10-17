import type { VizData } from './types';

const FORMULA_PREFIX = /^[=+\-@\t\r]/;

function csvCell(value: unknown, spreadsheetSafe: boolean): string {
    if (value == null) return '';
    let text = String(value);
    if (spreadsheetSafe && typeof value === 'string' && FORMULA_PREFIX.test(text)) {
        text = `'${text}`;
    }
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Serialize the materialized chart points as UTF-8, spreadsheet-safe CSV. */
export function buildVizCsv(data: VizData): string {
    const header = data.columns.map((column) => csvCell(column.field ?? column.name, true));
    const rows = data.rows.map((row) => row.map((value) => csvCell(value, true)).join(','));
    return `\uFEFF${[header.join(','), ...rows].join('\r\n')}`;
}

export function safeDownloadStem(value: string, fallback: string): string {
    const withoutControls = Array.from(value, (character) =>
        character.charCodeAt(0) < 32 ? '-' : character
    ).join('');
    const stem = withoutControls
        .trim()
        .replace(/[\\/:*?"<>|]/g, '-')
        .replace(/\s+/g, ' ')
        .slice(0, 100);
    return stem || fallback;
}
