import { useEffect } from 'react';
import { CheckCircle, XCircle, Info, X } from 'lucide-react';
import { useStore } from '../store/useStore';

export default function Toast() {
    const toasts = useStore((s) => s.toasts);
    const removeToast = useStore((s) => s.removeToast);

    return (
        <div className="fixed bottom-6 right-6 z-[9999] flex flex-col gap-2 pointer-events-none">
            {toasts.map((toast) => (
                <ToastItem key={toast.id} toast={toast} onRemove={removeToast} />
            ))}
        </div>
    );
}

interface ToastData {
    id: string;
    message: string;
    type: 'success' | 'error' | 'info';
}

function ToastItem({ toast, onRemove }: { toast: ToastData; onRemove: (id: string) => void }) {
    useEffect(() => {
        const timer = setTimeout(() => onRemove(toast.id), 4000);
        return () => clearTimeout(timer);
    }, [toast.id, onRemove]);

    const icons = {
        success: <CheckCircle className="w-4 h-4 shrink-0 text-emerald-400" />,
        error: <XCircle className="w-4 h-4 shrink-0 text-red-400" />,
        info: <Info className="w-4 h-4 shrink-0 text-blue-400" />,
    };

    const borderColors = {
        success: 'rgba(52,211,153,0.35)',
        error: 'rgba(248,113,113,0.35)',
        info: 'color-mix(in srgb, var(--c-signal) 30%, transparent)',
    };

    return (
        <div
            className="pointer-events-auto flex items-center gap-3 px-4 py-3 rounded-xl backdrop-blur-sm shadow-2xl text-sm min-w-[220px] max-w-[340px] animate-fade-in-up"
            style={{
                background: 'var(--c-slate)',
                border: `1px solid ${borderColors[toast.type]}`,
                boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
                color: 'var(--c-ink)',
            }}
        >
            {icons[toast.type]}
            <span className="flex-1 leading-snug">{toast.message}</span>
            <button
                onClick={() => onRemove(toast.id)}
                className="ml-1 transition-colors hover:opacity-80"
                style={{ color: 'var(--c-mist-fg)' }}
                aria-label="Dismiss"
            >
                <X className="w-3.5 h-3.5" />
            </button>
        </div>
    );
}
