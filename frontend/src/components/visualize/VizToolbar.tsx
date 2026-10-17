import { useState, type RefObject } from 'react';
import {
    Download,
    FileDown,
    FileJson,
    History,
    PanelLeftOpen,
    Redo2,
    RefreshCw,
    RotateCcw,
    Save,
    Trash2,
    Undo2,
} from 'lucide-react';
import { listVizVersions } from '../../services/viz';
import type { EChartsRendererHandle } from './EChartsRenderer';
import { buildVizCsv, safeDownloadStem } from './csvExport';
import type { UseVizSpec } from './useVizSpec';

interface Props {
    viz: UseVizSpec;
    chartRef: RefObject<EChartsRendererHandle>;
    fieldsCollapsed: boolean;
    onShowFields: () => void;
    onShowHistory: () => void;
}

const buttonStyle = (disabled = false) =>
    ({
        color: disabled ? 'var(--c-mist-fg)' : 'var(--c-ink-2)',
        opacity: disabled ? 0.5 : 1,
    }) as const;

export default function VizToolbar({
    viz,
    chartRef,
    fieldsCollapsed,
    onShowFields,
    onShowHistory,
}: Props) {
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [exportingJson, setExportingJson] = useState(false);
    const [exportStatus, setExportStatus] = useState<{
        kind: 'success' | 'error';
        message: string;
    } | null>(null);
    const blocked =
        !viz.data ||
        viz.loading ||
        Boolean(viz.queryError) ||
        viz.specErrors.length > 0 ||
        viz.calcErrors.length > 0;

    const download = (href: string, extension: string, fallback: string) => {
        const anchor = document.createElement('a');
        anchor.href = href;
        anchor.download = `${safeDownloadStem(viz.title, fallback)}.${extension}`;
        anchor.click();
    };

    const exportPng = () => {
        try {
            const url = chartRef.current?.exportPng();
            if (!url) {
                setExportStatus({ kind: 'error', message: 'PNG export was not ready.' });
                return;
            }
            download(url, 'png', 'chart');
            setExportStatus({ kind: 'success', message: 'PNG exported.' });
        } catch {
            setExportStatus({ kind: 'error', message: 'PNG export failed.' });
        }
    };

    const exportCsv = () => {
        if (!viz.data) return;
        try {
            const url = URL.createObjectURL(
                new Blob([buildVizCsv(viz.data)], { type: 'text/csv;charset=utf-8;' })
            );
            download(url, 'csv', 'chart-data');
            window.setTimeout(() => URL.revokeObjectURL(url), 0);
            setExportStatus({ kind: 'success', message: 'CSV exported.' });
        } catch {
            setExportStatus({ kind: 'error', message: 'CSV export failed.' });
        }
    };

    const exportSpec = async () => {
        if (!viz.activeVizId) return;
        setExportingJson(true);
        try {
            const versions = await listVizVersions(viz.activeVizId);
            const agentVersions = versions.filter((version) => version.author === 'agent');
            if (agentVersions.length === 0) {
                setExportStatus({ kind: 'error', message: 'This chart has no AI-generated version.' });
                return;
            }
            const latestAgentVersion = agentVersions.reduce((latest, version) =>
                version.version > latest.version ? version : latest
            );
            const url = URL.createObjectURL(
                new Blob([JSON.stringify(latestAgentVersion.spec, null, 2)], {
                    type: 'application/json',
                })
            );
            download(url, 'json', 'chart-spec');
            window.setTimeout(() => URL.revokeObjectURL(url), 0);
            setExportStatus({ kind: 'success', message: 'Spec exported.' });
        } catch {
            setExportStatus({ kind: 'error', message: 'Spec export failed.' });
        } finally {
            setExportingJson(false);
        }
    };

    const remove = async () => {
        if (!confirmDelete) {
            setConfirmDelete(true);
            window.setTimeout(() => setConfirmDelete(false), 2500);
            return;
        }
        setConfirmDelete(false);
        await viz.remove();
    };

    return (
        <div className="flex flex-wrap items-center gap-2">
            {fieldsCollapsed && (
                <button
                    type="button"
                    onClick={onShowFields}
                    title="Show fields"
                    className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md border shadow-sm"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                        color: 'var(--c-mist-fg)',
                    }}
                >
                    <PanelLeftOpen className="h-3.5 w-3.5" />
                </button>
            )}
            <input
                type="text"
                value={viz.title}
                onChange={(event) => viz.setTitle(event.target.value)}
                placeholder="Chart title"
                aria-label="Chart title"
                className="min-w-0 flex-1 rounded-md border px-2.5 py-1.5 text-xs font-medium"
                style={{
                    background: 'var(--c-glass)',
                    borderColor: 'var(--c-glass-border)',
                    color: 'var(--c-ink)',
                }}
            />
            <div className="flex flex-shrink-0 flex-wrap items-center gap-1.5">
                <button type="button" onClick={viz.undo} disabled={!viz.canUndo} title="Undo">
                    <Undo2 className="h-4 w-4" style={buttonStyle(!viz.canUndo)} />
                </button>
                <button type="button" onClick={viz.redo} disabled={!viz.canRedo} title="Redo">
                    <Redo2 className="h-4 w-4" style={buttonStyle(!viz.canRedo)} />
                </button>
                <button type="button" onClick={viz.refresh} title="Refresh data">
                    <RefreshCw className="h-4 w-4" style={buttonStyle()} />
                </button>
                <button type="button" onClick={exportPng} disabled={blocked} title="Export PNG">
                    <Download className="h-4 w-4" style={buttonStyle(blocked)} />
                </button>
                <button type="button" onClick={exportCsv} disabled={blocked} title="Export CSV">
                    <FileDown className="h-4 w-4" style={buttonStyle(blocked)} />
                </button>
                {viz.activeVizId && (
                    <button
                        type="button"
                        onClick={() => exportSpec().catch(() => undefined)}
                        disabled={exportingJson}
                        title="Download AI-generated chart spec (JSON)"
                    >
                        <FileJson className="h-4 w-4" style={buttonStyle(exportingJson)} />
                    </button>
                )}
                {viz.activeVizId && viz.activeVersion != null && (
                    <button
                        type="button"
                        onClick={onShowHistory}
                        disabled={viz.dirty}
                        title={
                            viz.dirty
                                ? 'Save or discard changes to view history'
                                : 'Version history'
                        }
                    >
                        <History className="h-4 w-4" style={buttonStyle(viz.dirty)} />
                    </button>
                )}
                {viz.activeVizId && (
                    <button
                        type="button"
                        onClick={() => remove().catch(() => undefined)}
                        title={confirmDelete ? 'Click again to delete' : 'Delete chart'}
                        style={{ color: confirmDelete ? 'var(--c-danger)' : 'var(--c-mist-fg)' }}
                    >
                        <Trash2 className="h-4 w-4" />
                    </button>
                )}
                {viz.dirty && (
                    <button
                        type="button"
                        onClick={() => viz.discardDraft().catch(() => undefined)}
                        title="Discard unsaved changes"
                        aria-label="Discard unsaved changes"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <RotateCcw className="h-4 w-4" />
                    </button>
                )}
                <button
                    type="button"
                    onClick={() => viz.save().catch(() => undefined)}
                    disabled={viz.saving || blocked}
                    className="flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs font-semibold transition-colors disabled:opacity-50"
                    style={{
                        background: viz.dirty ? 'var(--c-signal-dim)' : 'var(--c-glass)',
                        borderColor: viz.dirty
                            ? 'color-mix(in srgb, var(--c-signal) 35%, transparent)'
                            : 'var(--c-glass-border)',
                        color: viz.dirty ? 'var(--c-signal-text)' : 'var(--c-mist-fg)',
                    }}
                >
                    <Save className="h-3.5 w-3.5" />
                    {viz.saving ? 'Saving…' : viz.activeVizId ? 'Save' : 'Save chart'}
                    {viz.activeVersion != null && !viz.dirty && (
                        <span style={{ color: 'var(--c-mist-fg)' }}>v{viz.activeVersion}</span>
                    )}
                </button>
                {exportStatus && (
                    <span
                        role="status"
                        aria-live="polite"
                        className="text-xs font-medium"
                        style={{
                            color:
                                exportStatus.kind === 'error'
                                    ? 'var(--c-danger)'
                                    : 'var(--c-signal-text)',
                        }}
                    >
                        {exportStatus.message}
                    </span>
                )}
            </div>
        </div>
    );
}
