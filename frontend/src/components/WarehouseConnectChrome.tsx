import type { ReactNode } from 'react';
import { ArrowLeft, type LucideIcon } from 'lucide-react';

export function ConnectShell({
    title,
    description,
    onBack,
    backLabel = 'Return',
    children,
}: {
    title: string;
    description: string;
    onBack?: () => void;
    backLabel?: string;
    children: ReactNode;
}) {
    return (
        <div className="space-y-5">
            <div className="flex items-start justify-between gap-3">
                <div>
                    <h2 className="text-xl font-bold" style={{ color: 'var(--c-ink)' }}>
                        {title}
                    </h2>
                    <p className="text-sm mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                        {description}
                    </p>
                </div>
                {onBack && (
                    <button
                        type="button"
                        onClick={onBack}
                        className="flex items-center gap-1.5 text-sm shrink-0"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <ArrowLeft className="w-3.5 h-3.5" />
                        {backLabel}
                    </button>
                )}
            </div>
            {children}
        </div>
    );
}

export function MethodCard({
    title,
    subtitle,
    icon: Icon,
    onClick,
}: {
    title: string;
    subtitle: string;
    icon: LucideIcon;
    onClick: () => void;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            className="w-full text-left rounded-xl border p-4 flex items-start gap-3 transition-colors hover:bg-white/5"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            <span
                className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0"
                style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
            >
                <Icon className="w-5 h-5" />
            </span>
            <span>
                <span className="block text-sm font-semibold" style={{ color: 'var(--c-ink)' }}>
                    {title}
                </span>
                <span className="block text-xs mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                    {subtitle}
                </span>
            </span>
        </button>
    );
}
