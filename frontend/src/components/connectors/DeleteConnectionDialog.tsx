import { useEffect, useState } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import {
    deleteSavedConnection,
    getConnectionUsage,
    type ConnectionUsage,
    type SavedConnection,
} from '../../services/api';
import { forgetRetiredSource } from '../../sourceRemoved';
import { useStore } from '../../store/useStore';

interface DeleteConnectionDialogProps {
    connection: SavedConnection;
    onCancel: () => void;
    onDeleted: () => void;
    onError: (message: string) => void;
}

/**
 * Two-phase delete. The server reports the blast radius first (which sessions,
 * charts and filters depend on this connection) and only accepts the delete with
 * a matching etag, so a connection can't be removed based on stale usage.
 */
export default function DeleteConnectionDialog({
    connection,
    onCancel,
    onDeleted,
    onError,
}: DeleteConnectionDialogProps) {
    const [usage, setUsage] = useState<ConnectionUsage | null>(null);
    const [loading, setLoading] = useState(true);
    const [deleting, setDeleting] = useState(false);
    const sessionId = useStore((s) => s.sessionId);

    useEffect(() => {
        let alive = true;
        getConnectionUsage(connection.id)
            .then((result) => alive && setUsage(result))
            .catch((err) => {
                if (!alive) return;
                onError(errorMessage(err, 'Could not check where this connection is used'));
                onCancel();
            })
            .finally(() => alive && setLoading(false));
        return () => {
            alive = false;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [connection.id]);

    const confirm = async () => {
        if (!usage) return;
        setDeleting(true);
        try {
            const result = await deleteSavedConnection(connection.id, {
                confirm: true,
                etag: usage.etag,
            });
            for (const row of result.detached ?? []) {
                if (!sessionId || row.session_id === sessionId) {
                    forgetRetiredSource(row.file_id);
                }
            }
            onDeleted();
        } catch (err) {
            onError(errorMessage(err, 'Could not delete this connection'));
        } finally {
            setDeleting(false);
        }
    };

    const affected = usage?.sessions ?? [];

    return (
        <div
            className="fixed inset-0 z-[60] flex items-center justify-center p-4"
            style={{ background: 'color-mix(in srgb, #000 55%, transparent)' }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-connection-title"
            onClick={(e) => e.target === e.currentTarget && !deleting && onCancel()}
        >
            <div
                className="w-full max-w-md rounded-2xl border p-5"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: 'var(--c-glass-border)',
                    boxShadow: '0 24px 64px rgba(0,0,0,0.5)',
                }}
            >
                <div className="flex items-start gap-3">
                    <span
                        aria-hidden="true"
                        className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                        style={{
                            background: 'color-mix(in srgb, var(--c-danger) 12%, transparent)',
                        }}
                    >
                        <AlertTriangle className="w-4 h-4" style={{ color: 'var(--c-danger)' }} />
                    </span>
                    <div className="min-w-0">
                        <h2
                            id="delete-connection-title"
                            className="font-display font-semibold text-sm"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            Delete “{connection.name}”?
                        </h2>
                        <p className="text-xs mt-1" style={{ color: 'var(--c-ink-2)' }}>
                            Credentials are removed permanently. This cannot be undone.
                        </p>
                    </div>
                </div>

                <div className="mt-4">
                    {loading ? (
                        <p
                            className="flex items-center gap-2 text-xs"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            Checking where this connection is used…
                        </p>
                    ) : affected.length === 0 ? (
                        <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                            No sessions are currently using this connection.
                        </p>
                    ) : (
                        <div
                            className="rounded-xl p-3"
                            style={{
                                background: 'var(--c-glass)',
                                border: '1px solid var(--c-glass-border)',
                            }}
                        >
                            <p
                                className="text-[11px] font-semibold mb-2"
                                style={{ color: 'var(--c-warning)' }}
                            >
                                {affected.length} session{affected.length === 1 ? '' : 's'} will
                                lose {usage?.table_count ?? 0} table
                                {usage?.table_count === 1 ? '' : 's'}
                                {(usage?.viz_count ?? 0) > 0 &&
                                    `, ${usage?.viz_count} chart${usage?.viz_count === 1 ? '' : 's'}`}
                                {(usage?.filter_count ?? 0) > 0 &&
                                    `, ${usage?.filter_count} filter${usage?.filter_count === 1 ? '' : 's'}`}
                            </p>
                            <ul className="space-y-1 max-h-32 overflow-y-auto">
                                {affected.map((session) => (
                                    <li
                                        key={session.session_id}
                                        className="text-[11px] truncate"
                                        style={{ color: 'var(--c-ink-2)' }}
                                    >
                                        {session.title || 'Untitled session'}
                                        {session.tables.length > 0 && (
                                            <span style={{ color: 'var(--c-mist-fg)' }}>
                                                {' '}
                                                — {session.tables.join(', ')}
                                            </span>
                                        )}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>

                <div className="flex items-center justify-end gap-2 mt-5">
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={deleting}
                        className="rounded-xl px-3 py-2 text-xs font-semibold transition-colors hover:bg-[var(--c-glass)] disabled:opacity-50"
                        style={{
                            color: 'var(--c-ink-2)',
                            border: '1px solid var(--c-glass-border)',
                        }}
                    >
                        Cancel
                    </button>
                    <button
                        type="button"
                        onClick={confirm}
                        disabled={loading || deleting}
                        className="btn-danger px-3 py-2 text-xs"
                    >
                        {deleting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                        Delete connection
                    </button>
                </div>
            </div>
        </div>
    );
}

function errorMessage(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (err instanceof Error && err.message) return err.message;
    return fallback;
}
