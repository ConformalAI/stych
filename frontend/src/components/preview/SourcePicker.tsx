import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Database, FileText, Trash2, X } from 'lucide-react';
import { PreviewSource } from '../../services/api';

const ORIGIN_LABEL: Record<string, string> = {
    upload: 'Upload',
    postgres: 'Postgres',
    sqlserver: 'SQL Server',
    mongodb: 'MongoDB',
    snowflake: 'Snowflake',
    redshift: 'Redshift',
    bigquery: 'BigQuery',
    databricks: 'Databricks',
};

function humanSize(bytes?: number | null): string {
    if (bytes === null || bytes === undefined) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface SourcePickerProps {
    sources: PreviewSource[];
    selected: PreviewSource | null;
    onSelect: (source: PreviewSource) => void;
    /** When set, each row shows a delete control (inline two-step confirm). */
    onDelete?: (source: PreviewSource) => void;
    /** id:kind of the source currently being deleted (shows a spinner, disables). */
    deletingKey?: string | null;
}

function Badge({ children }: { children: React.ReactNode }) {
    return (
        <span
            className="px-1.5 py-0.5 rounded text-[9px] font-mono uppercase tracking-wide border flex-shrink-0"
            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
        >
            {children}
        </span>
    );
}

function DeleteControl({
    source,
    onDelete,
    busy,
}: {
    source: PreviewSource;
    onDelete: (source: PreviewSource) => void;
    busy: boolean;
}) {
    const [confirming, setConfirming] = useState(false);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(
        () => () => {
            if (timerRef.current) clearTimeout(timerRef.current);
        },
        []
    );

    const arm = (e: React.MouseEvent) => {
        e.stopPropagation();
        setConfirming(true);
        timerRef.current = setTimeout(() => setConfirming(false), 3500);
    };
    const cancel = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (timerRef.current) clearTimeout(timerRef.current);
        setConfirming(false);
    };
    const confirm = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (timerRef.current) clearTimeout(timerRef.current);
        setConfirming(false);
        onDelete(source);
    };

    if (busy) {
        return (
            <span className="flex-shrink-0 w-4 h-4 flex items-center justify-center">
                <span
                    className="w-3 h-3 rounded-full border-2 border-transparent animate-spin"
                    style={{
                        borderTopColor: 'var(--c-danger)',
                        borderRightColor: 'var(--c-danger)',
                    }}
                />
            </span>
        );
    }

    if (confirming) {
        return (
            <span className="flex items-center gap-1 flex-shrink-0">
                <button
                    type="button"
                    onClick={confirm}
                    title="Confirm delete"
                    className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                    style={{ color: '#fff', background: 'var(--c-danger)' }}
                >
                    Delete
                </button>
                <button
                    type="button"
                    onClick={cancel}
                    title="Cancel"
                    className="w-4 h-4 flex items-center justify-center rounded hover:bg-white/10"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    <X className="w-3 h-3" />
                </button>
            </span>
        );
    }

    return (
        <button
            type="button"
            onClick={arm}
            title="Delete"
            className="w-5 h-5 flex items-center justify-center rounded flex-shrink-0 transition-colors hover:bg-red-500/10"
            style={{ color: 'var(--c-mist-fg)' }}
        >
            <Trash2 className="w-3.5 h-3.5" />
        </button>
    );
}

function SourceRow({
    source,
    isSelected,
    onSelect,
    onDelete,
    deleting,
}: {
    source: PreviewSource;
    isSelected: boolean;
    onSelect: () => void;
    onDelete?: (source: PreviewSource) => void;
    deleting: boolean;
}) {
    return (
        <div
            role="button"
            tabIndex={0}
            onClick={onSelect}
            onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect()}
            className="w-full flex items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-white/5 cursor-pointer"
            style={{ color: 'var(--c-ink)' }}
        >
            {source.kind === 'dataset' || source.kind === 'sql' ? (
                <Database
                    className="w-3.5 h-3.5 flex-shrink-0"
                    style={{ color: 'var(--c-signal-text)' }}
                />
            ) : (
                <FileText
                    className="w-3.5 h-3.5 flex-shrink-0"
                    style={{ color: 'var(--c-mist-fg)' }}
                />
            )}
            <span className="min-w-0 flex-1 flex flex-col">
                <span className="truncate">{source.name}</span>
                {source.capability_summary && (
                    <span
                        className="truncate text-[9px]"
                        style={{ color: 'var(--c-mist-fg)' }}
                        title={source.capability_summary}
                    >
                        {source.capability_summary}
                    </span>
                )}
            </span>
            {source.kind === 'dataset' || source.kind === 'sql' ? (
                <>
                    <Badge>{ORIGIN_LABEL[source.origin ?? 'upload'] ?? source.origin}</Badge>
                    {source.kind === 'sql' && source.role === 'working' && (
                        <span
                            className="px-1.5 py-0.5 rounded text-[9px] font-mono uppercase tracking-wide border flex-shrink-0"
                            style={{
                                color: 'var(--c-warning)',
                                borderColor: 'rgba(232,161,58,0.3)',
                                background: 'rgba(232,161,58,0.08)',
                            }}
                            title="The agent's working copy of this table"
                        >
                            working copy
                        </span>
                    )}
                    {typeof source.rows === 'number' && (
                        <span
                            className="text-[10px] flex-shrink-0"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            {source.rows.toLocaleString()} rows
                        </span>
                    )}
                </>
            ) : (
                <>
                    {source.suffix && <Badge>{source.suffix}</Badge>}
                    <span
                        className="text-[10px] flex-shrink-0"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {humanSize(source.size_bytes)}
                    </span>
                </>
            )}
            {source.is_active && (
                <span
                    className="text-[9px] px-1.5 py-0.5 rounded-full flex-shrink-0"
                    style={{
                        color: 'var(--c-signal-text)',
                        background: 'var(--c-signal-dim)',
                    }}
                >
                    active
                </span>
            )}
            {isSelected && (
                <Check
                    className="w-3.5 h-3.5 flex-shrink-0"
                    style={{ color: 'var(--c-signal-text)' }}
                />
            )}
            {onDelete && !(source.kind === 'sql' && source.role === 'working') && (
                <DeleteControl source={source} onDelete={onDelete} busy={deleting} />
            )}
        </div>
    );
}

export default function SourcePicker({
    sources,
    selected,
    onSelect,
    onDelete,
    deletingKey,
}: SourcePickerProps) {
    const [open, setOpen] = useState(false);
    const containerRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) return;
        const onClickOutside = (event: MouseEvent) => {
            if (!containerRef.current?.contains(event.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', onClickOutside);
        return () => document.removeEventListener('mousedown', onClickOutside);
    }, [open]);

    const datasets = sources.filter((s) => s.kind === 'dataset');
    const files = sources.filter((s) => s.kind === 'file');
    const sqlTables = sources.filter((s) => s.kind === 'sql');

    return (
        <div ref={containerRef} className="relative">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                className="flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs transition-colors hover:bg-white/5 min-w-[220px]"
                style={{
                    borderColor: 'var(--c-glass-border)',
                    background: 'var(--c-glass)',
                    color: 'var(--c-ink)',
                }}
                title={selected?.capability_summary ?? undefined}
            >
                {selected ? (
                    <>
                        {selected.kind === 'dataset' || selected.kind === 'sql' ? (
                            <Database
                                className="w-3.5 h-3.5"
                                style={{ color: 'var(--c-signal-text)' }}
                            />
                        ) : (
                            <FileText
                                className="w-3.5 h-3.5"
                                style={{ color: 'var(--c-mist-fg)' }}
                            />
                        )}
                        <span className="min-w-0 flex-1 flex flex-col text-left">
                            <span className="truncate">{selected.name}</span>
                            {selected.capability_summary && (
                                <span
                                    className="truncate text-[9px]"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    {selected.capability_summary}
                                </span>
                            )}
                        </span>
                    </>
                ) : (
                    <span className="flex-1 text-left" style={{ color: 'var(--c-mist-fg)' }}>
                        Select a file or dataset…
                    </span>
                )}
                <ChevronDown className="w-3.5 h-3.5" style={{ color: 'var(--c-mist-fg)' }} />
            </button>

            {open && (
                <div
                    className="absolute left-0 top-full mt-1 z-30 w-[340px] max-h-80 overflow-y-auto rounded-lg border shadow-lg"
                    style={{
                        borderColor: 'var(--c-glass-border)',
                        background: 'var(--c-slate)',
                    }}
                >
                    {datasets.length > 0 && (
                        <div
                            className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            Active data
                        </div>
                    )}
                    {datasets.map((source) => (
                        <SourceRow
                            key={`${source.kind}:${source.id}`}
                            source={source}
                            isSelected={
                                selected?.id === source.id && selected?.kind === source.kind
                            }
                            onSelect={() => {
                                onSelect(source);
                                setOpen(false);
                            }}
                            onDelete={onDelete}
                            deleting={deletingKey === `${source.kind}:${source.id}`}
                        />
                    ))}
                    {files.length > 0 && (
                        <div
                            className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide border-t"
                            style={{
                                color: 'var(--c-mist-fg)',
                                borderColor: 'var(--c-glass-border)',
                            }}
                        >
                            Workspace files
                        </div>
                    )}
                    {files.map((source) => (
                        <SourceRow
                            key={`${source.kind}:${source.id}`}
                            source={source}
                            isSelected={
                                selected?.id === source.id && selected?.kind === source.kind
                            }
                            onSelect={() => {
                                onSelect(source);
                                setOpen(false);
                            }}
                            onDelete={onDelete}
                            deleting={deletingKey === `${source.kind}:${source.id}`}
                        />
                    ))}
                    {sqlTables.length > 0 && (
                        <div
                            className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide border-t"
                            style={{
                                color: 'var(--c-mist-fg)',
                                borderColor: 'var(--c-glass-border)',
                            }}
                        >
                            Database tables
                        </div>
                    )}
                    {sqlTables.map((source) => (
                        <SourceRow
                            key={`${source.kind}:${source.id}`}
                            source={source}
                            isSelected={
                                selected?.id === source.id && selected?.kind === source.kind
                            }
                            onSelect={() => {
                                onSelect(source);
                                setOpen(false);
                            }}
                            onDelete={onDelete}
                            deleting={deletingKey === `${source.kind}:${source.id}`}
                        />
                    ))}
                </div>
            )}
        </div>
    );
}
