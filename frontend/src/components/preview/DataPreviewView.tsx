import { useEffect, useMemo, useState } from 'react';
import {
    Download,
    FolderDown,
    GitCompareArrows,
    ListFilter,
    Loader2,
    RefreshCw,
    Table2,
    X,
} from 'lucide-react';
import { isAxiosError } from 'axios';
import {
    deleteSource,
    exportCleaningWorkspaceFile,
    exportPreviewSource,
    exportWorkspaceArchive,
    getPreviewSources,
    PreviewSource,
} from '../../services/api';
import { forgetRetiredSource } from '../../sourceRemoved';
import { useStore } from '../../store/useStore';
import { makeApiPreviewProvider } from './provider';
import DiffView from './DiffView';
import FilterView from './FilterView';
import FileDownloadMenu from './FileDownloadMenu';
import SourcePicker from './SourcePicker';
import SpreadsheetGrid from './SpreadsheetGrid';
import TextPreview from './TextPreview';
import { resolveFilterEventSource } from './sourceIdentity';

interface DataPreviewViewProps {
    sessionId: string;
    onExplain?: (prompt: string) => void;
}

type DownloadKind = 'parquet' | 'csv' | 'file' | 'all';

export default function DataPreviewView({ sessionId, onExplain }: DataPreviewViewProps) {
    const [sources, setSources] = useState<PreviewSource[]>([]);
    const [selected, setSelected] = useState<PreviewSource | null>(null);
    const [mode, setMode] = useState<'browse' | 'diff' | 'filter'>('browse');
    const openFilterId = useStore((s) => s.openFilterId);
    const lastFilterEvent = useStore((s) => s.lastFilterEvent);

    // Agent created/updated a filter — jump straight into filter mode.
    useEffect(() => {
        if (openFilterId) setMode('filter');
    }, [openFilterId]);

    // The filter the agent just saved may belong to a different sheet than the
    // one currently open — follow it there so FilterView (scoped to `selected`)
    // can actually find and open it.
    useEffect(() => {
        if (!lastFilterEvent || lastFilterEvent.sessionId !== sessionId) return;
        const ref = lastFilterEvent.datasetRef;
        if (!ref) return;
        setSelected((prev) => {
            if (prev?.id === ref) return prev;
            const match = resolveFilterEventSource(sources, ref);
            return match ?? prev;
        });
    }, [lastFilterEvent, sessionId, sources]);
    const [loading, setLoading] = useState(true);
    const [downloading, setDownloading] = useState<DownloadKind | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [reloadTick, setReloadTick] = useState(0);
    const [deletingKey, setDeletingKey] = useState<string | null>(null);

    const addToast = useStore((s) => s.addToast);
    const uploadedFiles = useStore((s) => s.uploadedFiles);
    // Refetch sources whenever the shared file list changes (upload, or a delete
    // from the add-data popup) so the dropdown never goes stale behind the store.
    const uploadedIdsKey = uploadedFiles.map((f) => f.file_id).join(',');

    const handleDelete = async (source: PreviewSource) => {
        const key = `${source.kind}:${source.id}`;
        if (deletingKey) return;
        setDeletingKey(key);
        try {
            await deleteSource(sessionId, {
                kind: source.kind,
                id: source.id,
                file_id: source.file_id,
            });
            // Keep the store's file list (top pills + add-data popup) in sync.
            if (source.file_id) forgetRetiredSource(source.file_id);
            const droppedCurrent =
                selected && selected.kind === source.kind && selected.id === source.id;
            setSelected((prev) =>
                prev && prev.kind === source.kind && prev.id === source.id ? null : prev
            );
            if (droppedCurrent) setMode('browse');
            setReloadTick((t) => t + 1);
            addToast({ message: `Deleted ${source.name}.`, type: 'success' });
        } catch (err: unknown) {
            const detail = isAxiosError<{ detail?: string }>(err)
                ? err.response?.data?.detail
                : err instanceof Error
                  ? err.message
                  : undefined;
            addToast({
                message: detail || 'Delete failed.',
                type: 'error',
            });
        } finally {
            setDeletingKey(null);
        }
    };

    /**
     * An oversized table is refused with 413 rather than truncated silently, so
     * the user decides whether a capped download is what they wanted.
     */
    const downloadSourceTable = async (sourceId: string, format: 'csv' | 'parquet') => {
        try {
            const { truncated } = await exportPreviewSource(sessionId, sourceId, format);
            if (truncated) {
                addToast({ message: 'Download complete (row limit reached).', type: 'info' });
            }
            return;
        } catch (err) {
            if (!isAxiosError(err) || err.response?.status !== 413) throw err;
            const raw = err.response.data;
            const body = raw instanceof Blob ? await raw.text() : JSON.stringify(raw ?? '');
            let detail = 'This table is larger than the export limit.';
            try {
                detail = JSON.parse(body).detail ?? detail;
            } catch {
                /* keep the fallback message */
            }
            if (!window.confirm(`${detail}\n\nDownload the capped rows instead?`)) return;
            await exportPreviewSource(sessionId, sourceId, format, true);
            addToast({ message: 'Download complete (row limit reached).', type: 'info' });
        }
    };

    const download = async (what: DownloadKind) => {
        if (downloading) return;
        setDownloading(what);
        try {
            if (what === 'all') {
                await exportWorkspaceArchive(sessionId);
            } else if (selected?.kind === 'sql' && (what === 'csv' || what === 'parquet')) {
                await downloadSourceTable(selected.id, what);
            } else if (selected?.kind === 'file') {
                const format = what === 'csv' || what === 'parquet' ? what : undefined;
                await exportCleaningWorkspaceFile(sessionId, selected.name, selected.id, format);
            }
        } catch (err) {
            const detail =
                (isAxiosError(err) && (err.response?.data as { detail?: string })?.detail) ||
                'Download failed.';
            addToast({ message: detail, type: 'error' });
        } finally {
            setDownloading(null);
        }
    };

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        getPreviewSources(sessionId)
            .then((result) => {
                if (cancelled) return;
                setSources(result);
                setSelected((prev) => {
                    // Keep the current selection if it still exists after a refresh.
                    const kept =
                        prev && result.find((s) => s.kind === prev.kind && s.id === prev.id);
                    if (kept) return kept;
                    return result.find((s) => s.is_active) ?? result[0] ?? null;
                });
            })
            .catch((err) => {
                if (cancelled) return;
                setError(err?.response?.data?.detail || err?.message || 'Failed to load sources.');
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sessionId, reloadTick, uploadedIdsKey]);

    const provider = useMemo(
        () =>
            selected && selected.renderer === 'table'
                ? makeApiPreviewProvider(sessionId, selected)
                : null,
        [sessionId, selected]
    );

    if (loading && sources.length === 0) {
        return (
            <div className="flex-1 flex items-center justify-center">
                <Loader2
                    className="w-5 h-5 animate-spin"
                    style={{ color: 'var(--c-signal-text)' }}
                />
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex-1 flex flex-col items-center justify-center gap-3 text-sm">
                <span style={{ color: 'var(--c-danger)' }}>{error}</span>
                <button
                    type="button"
                    onClick={() => setReloadTick((t) => t + 1)}
                    className="px-3 py-1.5 rounded-md border text-xs transition-colors hover:bg-white/5"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                >
                    Retry
                </button>
            </div>
        );
    }

    if (sources.length === 0) {
        return (
            <div
                className="flex-1 flex flex-col items-center justify-center gap-2 text-sm"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                <Table2 className="w-8 h-8 opacity-50" />
                <span>No data to preview yet — upload a file or connect a data source.</span>
            </div>
        );
    }

    if (mode === 'diff' || mode === 'filter') {
        return (
            <div className="flex-1 flex flex-col min-h-0 p-3 sm:p-4 gap-3">
                <div className="flex items-center gap-2">
                    <span
                        className="flex items-center gap-1.5 text-xs font-medium"
                        style={{ color: 'var(--c-ink)' }}
                    >
                        {mode === 'diff' ? (
                            <GitCompareArrows
                                className="w-4 h-4"
                                style={{ color: 'var(--c-signal-text)' }}
                            />
                        ) : (
                            <ListFilter
                                className="w-4 h-4"
                                style={{ color: 'var(--c-signal-text)' }}
                            />
                        )}
                        {mode === 'diff' ? 'Compare spreadsheets' : 'Custom filters'}
                    </span>
                    <button
                        type="button"
                        onClick={() => setMode('browse')}
                        className="ml-auto flex items-center gap-1 px-2 py-1 rounded-md border text-xs transition-colors hover:bg-white/5"
                        style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
                    >
                        <X className="w-3.5 h-3.5" />
                        Close
                    </button>
                </div>
                {mode === 'diff' ? (
                    <DiffView sessionId={sessionId} sources={sources} onExplain={onExplain} />
                ) : (
                    <FilterView sessionId={sessionId} selected={selected} />
                )}
            </div>
        );
    }

    return (
        <div className="flex-1 flex flex-col min-h-0 p-3 sm:p-4 gap-3">
            <div className="flex items-center gap-2">
                <SourcePicker
                    sources={sources}
                    selected={selected}
                    onSelect={setSelected}
                    onDelete={handleDelete}
                    deletingKey={deletingKey}
                />
                <button
                    type="button"
                    onClick={() => setReloadTick((t) => t + 1)}
                    className="p-1.5 rounded-md border transition-colors hover:bg-white/5"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
                    title="Refresh file list"
                    aria-label="Refresh file list"
                >
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                </button>
                <button
                    type="button"
                    onClick={() => setMode('diff')}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
                    title="Compare two spreadsheets"
                >
                    <GitCompareArrows
                        className="w-3.5 h-3.5"
                        style={{ color: 'var(--c-signal-text)' }}
                    />
                    Compare
                </button>
                <button
                    type="button"
                    onClick={() => setMode('filter')}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
                    title="Apply saved highlight filters"
                >
                    <ListFilter className="w-3.5 h-3.5" style={{ color: 'var(--c-signal-text)' }} />
                    Filters
                </button>
                <div className="flex items-center gap-2 ml-auto">
                    {(selected?.kind === 'file' || selected?.kind === 'sql') &&
                        selected.renderer === 'table' && (
                            <FileDownloadMenu
                                source={selected}
                                downloading={downloading}
                                onDownload={(format) => download(format)}
                            />
                        )}
                    {selected?.kind === 'file' && selected.renderer === 'text' && (
                        <button
                            type="button"
                            onClick={() => download('file')}
                            disabled={downloading !== null}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5 disabled:opacity-50"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                color: 'var(--c-ink-2)',
                            }}
                            title={`Download ${selected.name}`}
                        >
                            {downloading === 'file' ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                                <Download className="w-3.5 h-3.5" />
                            )}
                            Download
                        </button>
                    )}
                    <button
                        type="button"
                        onClick={() => download('all')}
                        disabled={downloading !== null}
                        className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5 disabled:opacity-50"
                        style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
                        title="Download the whole workspace as a zip"
                    >
                        {downloading === 'all' ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                            <FolderDown className="w-3.5 h-3.5" />
                        )}
                        Download all
                    </button>
                </div>
            </div>

            {selected &&
                (selected.renderer === 'text' ? (
                    <TextPreview
                        key={`${selected.kind}:${selected.id}`}
                        sessionId={sessionId}
                        source={selected}
                    />
                ) : provider ? (
                    <SpreadsheetGrid provider={provider} />
                ) : null)}
        </div>
    );
}
