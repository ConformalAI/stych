import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Download, Loader2 } from 'lucide-react';
import { PreviewSource } from '../../services/api';

export type FileDownloadFormat = 'parquet' | 'csv';

export default function FileDownloadMenu({
    source,
    downloading,
    onDownload,
}: {
    source: PreviewSource;
    downloading: string | null;
    onDownload: (format: FileDownloadFormat) => void;
}) {
    const [open, setOpen] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const busy = downloading === 'parquet' || downloading === 'csv';

    useEffect(() => {
        if (!open) return;
        const onClickOutside = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', onClickOutside);
        return () => document.removeEventListener('mousedown', onClickOutside);
    }, [open]);

    return (
        <div ref={ref} className="relative">
            <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                disabled={downloading !== null}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-xs font-medium transition-colors hover:bg-white/5 disabled:opacity-50"
                style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
                title={`Download ${source.name}`}
                aria-haspopup="menu"
                aria-expanded={open}
            >
                {busy ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                    <Download className="w-3.5 h-3.5" />
                )}
                Download
                <ChevronDown className="w-3 h-3 opacity-70" />
            </button>
            {open && (
                <div
                    role="menu"
                    className="absolute right-0 mt-1 z-20 min-w-[8.5rem] rounded-md border py-1"
                    style={{
                        borderColor: 'var(--c-glass-border)',
                        background: 'var(--c-slate)',
                        boxShadow: '0 8px 24px rgba(0,0,0,0.28)',
                    }}
                >
                    {(
                        [
                            ['parquet', 'Parquet'],
                            ['csv', 'CSV'],
                        ] as const
                    ).map(([fmt, label]) => (
                        <button
                            key={fmt}
                            type="button"
                            role="menuitem"
                            onClick={() => {
                                setOpen(false);
                                onDownload(fmt);
                            }}
                            className="w-full text-left px-3 py-1.5 text-xs transition-colors hover:bg-white/5"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
