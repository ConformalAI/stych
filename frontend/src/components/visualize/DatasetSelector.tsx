export interface DatasetSelectProps {
    datasets: Array<{ file_id: string; filename: string }>;
    activeSource: string;
    onSelect: (source: string) => void;
}

export default function DatasetSelector({ datasets, activeSource, onSelect }: DatasetSelectProps) {
    return (
        <div className="flex flex-col gap-1">
            <label
                htmlFor="viz-dataset-select"
                className="text-[10px] font-semibold uppercase tracking-wider"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                Data source
            </label>
            <select
                id="viz-dataset-select"
                aria-label="Visualization data source"
                value={activeSource}
                onChange={(event) => onSelect(event.target.value)}
                disabled={datasets.length < 2}
                className="w-full min-w-0 rounded-md border px-2 py-1.5 text-xs font-medium disabled:opacity-70"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: 'var(--c-glass-border)',
                    color: 'var(--c-ink)',
                }}
            >
                {datasets.map((dataset) => (
                    <option key={dataset.file_id} value={dataset.file_id}>
                        {dataset.filename}
                    </option>
                ))}
            </select>
        </div>
    );
}
