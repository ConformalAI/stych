import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { getPreviewPage, PreviewSource, PreviewTextPage } from '../../services/api';

interface TextPreviewProps {
    sessionId: string;
    source: PreviewSource;
}

function prettyPrint(text: string, suffix?: string | null): string {
    if (suffix !== 'json') return text;
    try {
        return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
        return text;
    }
}

export default function TextPreview({ sessionId, source }: TextPreviewProps) {
    const [page, setPageData] = useState<PreviewTextPage | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [reloadTick, setReloadTick] = useState(0);

    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        getPreviewPage(sessionId, { source: 'file', path: source.id })
            .then((response) => {
                if (cancelled) return;
                if (response.kind !== 'text') {
                    setError('Unexpected response for text preview.');
                } else {
                    setPageData(response);
                }
            })
            .catch((err) => {
                if (cancelled) return;
                setError(err?.response?.data?.detail || err?.message || 'Failed to load file.');
            })
            .finally(() => {
                if (!cancelled) setLoading(false);
            });
        return () => {
            cancelled = true;
        };
    }, [sessionId, source.id, reloadTick]);

    if (loading) {
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

    if (!page) return null;

    return (
        <div
            className="flex-1 flex flex-col min-h-0 rounded-lg border overflow-hidden"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            {page.truncated && (
                <div
                    className="px-3 py-1.5 text-[11px] border-b"
                    style={{
                        color: 'var(--c-warning)',
                        borderColor: 'var(--c-glass-border)',
                        background: 'rgba(232,161,58,0.08)',
                    }}
                >
                    Showing the first {(256).toLocaleString()} KB of{' '}
                    {(page.size_bytes / 1024).toLocaleString(undefined, {
                        maximumFractionDigits: 0,
                    })}{' '}
                    KB — download the file for the full contents.
                </div>
            )}
            <pre
                className="flex-1 overflow-auto p-4 text-xs leading-relaxed whitespace-pre-wrap break-words"
                style={{
                    color: 'var(--c-ink-2)',
                    fontFamily: "'JetBrains Mono', monospace",
                }}
            >
                {prettyPrint(page.text, source.suffix)}
            </pre>
        </div>
    );
}
