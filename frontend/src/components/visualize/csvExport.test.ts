import { describe, expect, it } from 'vitest';
import { buildVizCsv, safeDownloadStem } from './csvExport';
import type { VizData } from './types';

function data(headers: string[], rows: VizData['rows']): VizData {
    return {
        columns: headers.map((field, index) => ({
            name: index === 0 ? 'x' : 'y',
            field,
            kind: index === 0 ? 'dimension' : 'measure',
            type: index === 0 ? 'nominal' : 'quantitative',
        })),
        rows,
        meta: { row_count: rows.length, truncated: false, engine: 'pandas' },
    };
}

describe('buildVizCsv', () => {
    it('quotes headers and values containing CSV delimiters', () => {
        const csv = buildVizCsv(data(['Last, First', 'He said "value"'], [['Ada\r\nLovelace', 3]]));
        expect(csv).toBe('\uFEFF"Last, First","He said ""value"""\r\n"Ada\r\nLovelace",3');
    });

    it('neutralizes spreadsheet formulas without changing numeric negatives', () => {
        const csv = buildVizCsv(data(['name', 'amount'], [['=HYPERLINK("x")', -2]]));
        expect(csv).toContain('"\'=HYPERLINK(""x"")",-2');
    });
});

it('creates a filesystem-safe export stem', () => {
    expect(safeDownloadStem(' Revenue / Region: 2026 ', 'chart')).toBe('Revenue - Region- 2026');
    expect(safeDownloadStem('   ', 'chart')).toBe('chart');
});
