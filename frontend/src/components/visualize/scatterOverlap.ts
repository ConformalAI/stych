import type { VizData, VizSpec } from './types';

export function rawScatterOverlapSummary(spec: VizSpec, data: VizData): string | null {
    if (spec.mark.type !== 'scatter' || spec.encoding.x?.aggregate || spec.encoding.y?.aggregate) {
        return null;
    }
    const xIndex = data.columns.findIndex((column) => column.name === 'x');
    const yIndex = data.columns.findIndex((column) => column.name === 'y');
    if (xIndex < 0 || yIndex < 0 || data.rows.length < 2) return null;

    const positions = new Set(
        data.rows.map((row) => JSON.stringify([row[xIndex] ?? null, row[yIndex] ?? null]))
    );
    if (positions.size === data.rows.length) return null;
    return `${data.rows.length.toLocaleString()} plotted rows occupy ${positions.size.toLocaleString()} positions — overlapping marks may hide legend colors`;
}
