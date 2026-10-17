import type { FieldInfo, VizData } from './types';

interface Props {
    field: FieldInfo;
    data: VizData | null;
    loading: boolean;
}

function display(value: unknown): string {
    if (value == null) return '—';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
}

export default function VizIntermediateTable({ field, data, loading }: Props) {
    const fieldIndex = data?.columns.findIndex((column) => column.field === field.name) ?? -1;
    const countIndex =
        data?.columns.findIndex((column) => column.field === null && column.kind === 'measure') ??
        -1;
    const materializedRows =
        data && fieldIndex >= 0
            ? data.rows
                  .slice(0, 20)
                  .map((row) => [row[fieldIndex], countIndex >= 0 ? row[countIndex] : undefined])
            : [];
    const rows =
        materializedRows.length > 0
            ? materializedRows
            : (field.samples ?? []).slice(0, 20).map((sample) => [sample, undefined]);
    const showCount = countIndex >= 0 && materializedRows.length > 0;

    return (
        <div className="flex flex-1 min-h-0 flex-col p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
                <div>
                    <p className="text-sm font-semibold" style={{ color: 'var(--c-ink)' }}>
                        {field.name}
                    </p>
                    <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        Add another field to build the chart.
                    </p>
                </div>
                {loading && (
                    <span className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        updating…
                    </span>
                )}
            </div>
            <div className="min-h-0 overflow-auto rounded-lg border border-[var(--c-glass-border)]">
                <table className="w-full border-collapse text-left text-xs">
                    <thead className="sticky top-0" style={{ background: 'var(--c-slate)' }}>
                        <tr>
                            <th className="px-3 py-2 font-semibold">{field.name}</th>
                            {showCount && <th className="px-3 py-2 font-semibold">Count</th>}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((row, index) => (
                            <tr
                                key={`${display(row[0])}-${index}`}
                                className="border-t border-[var(--c-glass-border)]"
                            >
                                <td className="px-3 py-2">{display(row[0])}</td>
                                {showCount && <td className="px-3 py-2">{display(row[1])}</td>}
                            </tr>
                        ))}
                        {rows.length === 0 && (
                            <tr className="border-t border-[var(--c-glass-border)]">
                                <td
                                    className="px-3 py-6 text-center"
                                    colSpan={showCount ? 2 : 1}
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    No preview values available.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
