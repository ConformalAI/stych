import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
    BIGQUERY_OAUTH_SERVER_ID,
    getBigQueryDatasets,
    getBigQueryOAuthStatus,
    getBigQueryProjects,
    startGoogleOAuth,
} from '../services/api';
import { parseIntegrationOAuthMessage } from '../services/authHeaders';

function errorMessage(error: unknown, fallback: string): string {
    return (
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback
    );
}

export function useBigQueryGoogleAuth(enabled: boolean) {
    const [busy, setBusy] = useState(false);
    const [connected, setConnected] = useState(false);
    const [projectId, setProjectId] = useState('');
    const [projects, setProjects] = useState<string[]>([]);
    const [datasets, setDatasets] = useState<string[]>([]);
    const [status, setStatus] = useState<'idle' | 'success' | 'error'>('idle');
    const [message, setMessage] = useState('');

    useEffect(() => {
        if (!enabled) return;
        let cancelled = false;
        getBigQueryOAuthStatus()
            .then(async (result) => {
                if (cancelled) return;
                setConnected(result.connected);
                if (result.connected) {
                    const listed = await getBigQueryProjects({ auth_mode: 'oauth' });
                    if (!cancelled) setProjects(listed.projects ?? []);
                }
            })
            .catch(() => {
                if (!cancelled) setConnected(false);
            });
        return () => {
            cancelled = true;
        };
    }, [enabled]);

    useEffect(() => {
        if (!enabled || !projectId) {
            setDatasets([]);
            return;
        }
        let cancelled = false;
        getBigQueryDatasets({ auth_mode: 'oauth', project_id: projectId })
            .then((result) => {
                if (!cancelled) setDatasets(result.datasets ?? []);
            })
            .catch(() => {
                if (!cancelled) setDatasets([]);
            });
        return () => {
            cancelled = true;
        };
    }, [enabled, projectId]);

    const connect = async () => {
        setBusy(true);
        setMessage('');
        try {
            const authUrl = await startGoogleOAuth(BIGQUERY_OAUTH_SERVER_ID);
            const popup = window.open(authUrl, 'google_bigquery_oauth', 'width=520,height=640');
            if (!popup) {
                setStatus('error');
                setMessage('Popup blocked — allow popups and try again.');
                return;
            }
            await new Promise<void>((resolve) => {
                let settled = false;
                const finish = () => {
                    if (settled) return;
                    settled = true;
                    window.removeEventListener('message', onMessage);
                    clearInterval(poll);
                    try {
                        popup.close();
                    } catch {
                        /* ignore */
                    }
                    resolve();
                };
                const onMessage = (event: MessageEvent) => {
                    if (event.origin !== window.location.origin) return;
                    const parsed = parseIntegrationOAuthMessage(
                        event.data,
                        BIGQUERY_OAUTH_SERVER_ID
                    );
                    if (parsed || event.data === `${BIGQUERY_OAUTH_SERVER_ID}_connected`) {
                        finish();
                    }
                };
                window.addEventListener('message', onMessage);
                const poll = setInterval(() => {
                    if (popup.closed) finish();
                }, 600);
            });
            const result = await getBigQueryOAuthStatus();
            setConnected(result.connected);
            if (result.connected) {
                const listed = await getBigQueryProjects({ auth_mode: 'oauth' });
                setProjects(listed.projects ?? []);
                setStatus('success');
                setMessage('Google account connected.');
            }
        } catch (error: unknown) {
            setStatus('error');
            setMessage(errorMessage(error, 'Failed to start Google sign-in'));
        } finally {
            setBusy(false);
        }
    };

    return {
        busy,
        connected,
        projectId,
        setProjectId,
        projects,
        datasets,
        status,
        message,
        connect,
    };
}

export function BigQueryGooglePanel({
    busy,
    connected,
    projectId,
    projects,
    onConnect,
    onProject,
}: {
    busy: boolean;
    connected: boolean;
    projectId: string;
    projects: string[];
    onConnect: () => void;
    onProject: (projectId: string) => void;
}) {
    return (
        <div
            className="rounded-xl border p-4 space-y-3"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            <button
                type="button"
                onClick={onConnect}
                disabled={busy}
                className="btn-primary w-full disabled:opacity-50"
            >
                {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                {connected ? 'Reconnect Google' : 'Continue with Google'}
            </button>
            {connected && (
                <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                    Google account connected. Choose a project, then a dataset.
                </p>
            )}
            {projects.length > 0 && (
                <label className="block space-y-1.5 text-sm">
                    <span className="font-medium" style={{ color: 'var(--c-ink)' }}>
                        Project
                    </span>
                    <select
                        value={projectId}
                        onChange={(event) => onProject(event.target.value)}
                        className="input w-full text-sm"
                    >
                        <option value="">Select a project</option>
                        {projects.map((project) => (
                            <option key={project} value={project}>
                                {project}
                            </option>
                        ))}
                    </select>
                </label>
            )}
        </div>
    );
}
