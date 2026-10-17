/**
 * Version history for the ontology.
 *
 * Every write is a full-spec snapshot, so restoring is exact rather than a
 * replayed diff. Agent-authored versions are labelled — seeing which entries
 * the agent wrote is the audit trail that makes proposals safe to accept.
 */

import { useEffect, useState } from 'react';
import { Bot, User, X } from 'lucide-react';
import { listOntologyVersions } from '../../services/ontology';
import type { OntologyVersion } from './types';

interface Props {
    ontologyId: string;
    currentVersion: number;
    onRestore: (versionN: number) => void;
    onClose: () => void;
}

export default function VersionHistory({ ontologyId, currentVersion, onRestore, onClose }: Props) {
    const [versions, setVersions] = useState<OntologyVersion[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        listOntologyVersions(ontologyId)
            .then((rows) => {
                if (!cancelled) setVersions(rows);
            })
            .catch(() => {
                if (!cancelled) setError('Could not load version history.');
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [ontologyId]);

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            style={{ background: 'color-mix(in srgb, var(--c-paper) 75%, transparent)' }}
            onClick={onClose}
        >
            <div
                className="w-full max-w-md rounded-xl border overflow-hidden"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: 'var(--c-glass-border)',
                    boxShadow: 'var(--c-shadow-lg)',
                }}
                onClick={(e) => e.stopPropagation()}
            >
                <div
                    className="flex items-center justify-between px-4 py-2.5 border-b"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    <span className="text-sm font-medium" style={{ color: 'var(--c-ink)' }}>
                        Version history
                    </span>
                    <button
                        type="button"
                        onClick={onClose}
                        className="p-1 rounded transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                        aria-label="Close"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="max-h-96 overflow-y-auto">
                    {loading && (
                        <p className="px-4 py-3 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                            Loading…
                        </p>
                    )}
                    {error && (
                        <p className="px-4 py-3 text-xs" style={{ color: 'var(--c-danger)' }}>
                            {error}
                        </p>
                    )}
                    {versions.map((v) => {
                        const isCurrent = v.version === currentVersion;
                        const Icon = v.author === 'agent' ? Bot : User;
                        return (
                            <div
                                key={v.version}
                                className="flex items-center gap-2 px-4 py-2 border-b"
                                style={{ borderColor: 'var(--c-glass-border)' }}
                            >
                                <Icon
                                    className="w-3.5 h-3.5 flex-shrink-0"
                                    style={{
                                        color:
                                            v.author === 'agent'
                                                ? 'var(--c-signal-text)'
                                                : 'var(--c-mist-fg)',
                                    }}
                                />
                                <div className="min-w-0 flex-1">
                                    <p
                                        className="text-xs truncate"
                                        style={{ color: 'var(--c-ink)' }}
                                    >
                                        v{v.version}
                                        {v.summary ? ` — ${v.summary}` : ''}
                                    </p>
                                    <p
                                        className="text-[10px] truncate"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        {v.created_at
                                            ? new Date(v.created_at).toLocaleString()
                                            : ''}
                                        {v.proposed.length > 0 && ` · ${v.proposed.join(', ')}`}
                                    </p>
                                </div>
                                {isCurrent ? (
                                    <span
                                        className="text-[10px] px-1.5 py-0.5 rounded flex-shrink-0"
                                        style={{
                                            background: 'var(--c-signal-dim)',
                                            color: 'var(--c-signal-text)',
                                        }}
                                    >
                                        current
                                    </span>
                                ) : (
                                    <button
                                        type="button"
                                        onClick={() => onRestore(v.version)}
                                        className="text-[10px] px-1.5 py-0.5 rounded flex-shrink-0 transition-colors hover:bg-[var(--c-glass)]"
                                        style={{ color: 'var(--c-ink-2)' }}
                                    >
                                        Restore
                                    </button>
                                )}
                            </div>
                        );
                    })}
                    {!loading && !error && versions.length === 0 && (
                        <p className="px-4 py-3 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                            No history yet.
                        </p>
                    )}
                </div>
            </div>
        </div>
    );
}
