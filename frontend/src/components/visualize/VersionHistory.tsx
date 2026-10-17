import { useEffect, useRef, useState } from 'react';
import { History, RotateCcw, X } from 'lucide-react';
import { listVizVersions, restoreVizVersion } from '../../services/viz';
import type { VizVersion } from './types';

interface Props {
    vizId: string;
    currentVersion: number;
    onClose: () => void;
    onRestored: () => Promise<void>;
}

export default function VersionHistory({ vizId, currentVersion, onClose, onRestored }: Props) {
    const closeRef = useRef<HTMLButtonElement>(null);
    const [versions, setVersions] = useState<VizVersion[]>([]);
    const [loading, setLoading] = useState(true);
    const [restoring, setRestoring] = useState<number | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        closeRef.current?.focus();
        const onKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', onKeyDown);
        listVizVersions(vizId)
            .then(setVersions)
            .catch(() => setError('Could not load chart history.'))
            .finally(() => setLoading(false));
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [onClose, vizId]);

    const restore = async (version: number) => {
        setRestoring(version);
        setError(null);
        try {
            await restoreVizVersion(vizId, version, currentVersion);
            await onRestored();
            onClose();
        } catch (cause) {
            const status = (cause as { response?: { status?: number } })?.response?.status;
            setError(
                status === 409
                    ? 'This chart changed elsewhere. Reload it before restoring a version.'
                    : 'Could not restore this version.'
            );
        } finally {
            setRestoring(null);
        }
    };

    return (
        <div
            className="absolute inset-0 z-40 flex justify-end rounded-xl bg-black/30"
            role="dialog"
            aria-modal="true"
            aria-label="Chart version history"
            onMouseDown={(event) => {
                if (event.target === event.currentTarget) onClose();
            }}
        >
            <section
                className="h-full w-full max-w-sm overflow-y-auto border-l p-4 shadow-2xl"
                style={{ background: 'var(--c-slate)', borderColor: 'var(--c-glass-border)' }}
            >
                <header className="mb-4 flex items-center gap-2">
                    <History className="h-4 w-4" />
                    <h2 className="flex-1 text-sm font-semibold">Version history</h2>
                    <button
                        ref={closeRef}
                        type="button"
                        onClick={onClose}
                        aria-label="Close history"
                    >
                        <X className="h-4 w-4" />
                    </button>
                </header>
                {error && (
                    <p role="alert" className="mb-3 text-xs" style={{ color: 'var(--c-danger)' }}>
                        {error}
                    </p>
                )}
                {loading ? (
                    <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        Loading history…
                    </p>
                ) : (
                    <ol className="space-y-2">
                        {versions.map((version) => {
                            const isCurrent = version.version === currentVersion;
                            return (
                                <li
                                    key={version.version}
                                    className="rounded-lg border p-3 text-xs"
                                    style={{ borderColor: 'var(--c-glass-border)' }}
                                >
                                    <div className="flex items-center gap-2">
                                        <strong>Version {version.version}</strong>
                                        {isCurrent && (
                                            <span style={{ color: 'var(--c-signal-text)' }}>
                                                Current
                                            </span>
                                        )}
                                        <span
                                            className="ml-auto"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {version.author === 'agent' ? 'AI' : 'You'}
                                        </span>
                                    </div>
                                    {version.created_at && (
                                        <p className="mt-1" style={{ color: 'var(--c-mist-fg)' }}>
                                            {new Date(version.created_at).toLocaleString()}
                                        </p>
                                    )}
                                    {!isCurrent && (
                                        <button
                                            type="button"
                                            onClick={() => restore(version.version)}
                                            disabled={restoring !== null}
                                            className="mt-2 flex items-center gap-1 font-semibold disabled:opacity-50"
                                            style={{ color: 'var(--c-signal-text)' }}
                                        >
                                            <RotateCcw className="h-3 w-3" />
                                            {restoring === version.version
                                                ? 'Restoring…'
                                                : 'Restore'}
                                        </button>
                                    )}
                                </li>
                            );
                        })}
                    </ol>
                )}
            </section>
        </div>
    );
}
