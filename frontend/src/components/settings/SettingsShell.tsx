import { type ComponentType, type ReactNode } from 'react';
import { ArrowLeft, Settings2 } from 'lucide-react';

export interface SettingsShellTab {
    id: string;
    label: string;
    icon: ComponentType<{ className?: string }>;
    implemented: boolean;
}

interface SettingsShellProps {
    tabs: SettingsShellTab[];
    activeTab: string;
    onSelect: (id: string) => void;
    onBack: () => void;
    /** Section title shown in the sticky pane header. */
    title: string;
    /** One-line section description under the title. */
    description?: string;
    /** Right-aligned actions in the sticky pane header (stay visible while the body scrolls). */
    headerActions?: ReactNode;
    /** The focused section — rendered inside the single internally-scrolling region. */
    children: ReactNode;
}

/**
 * Unified settings surface: a thin labeled nav rail fused to a content pane.
 * The rail and the pane's sticky header stay fixed; only the pane body scrolls,
 * so a focused section never grows the page vertically.
 *
 * Palette comes entirely from the existing --c-* design tokens (index.css).
 */
export default function SettingsShell({
    tabs,
    activeTab,
    onSelect,
    onBack,
    title,
    description,
    headerActions,
    children,
}: SettingsShellProps) {
    return (
        <div className="flex-1 min-h-0 px-4 sm:px-6 py-4 sm:py-6">
            <div
                className="h-full max-w-6xl mx-auto flex rounded-2xl border overflow-hidden"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: 'var(--c-glass-border)',
                    boxShadow: '0 20px 60px -30px rgba(0,0,0,0.45)',
                }}
            >
                {/* ── Thin nav rail ─────────────────────────────────────────── */}
                <nav
                    className="w-[210px] shrink-0 hidden md:flex flex-col border-r"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    {/* Rail header */}
                    <div
                        className="px-4 py-4 flex items-center gap-2.5 border-b"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        <div
                            className="w-8 h-8 rounded-lg grid place-items-center shrink-0"
                            style={{
                                background: 'var(--c-signal-dim)',
                                color: 'var(--c-signal-text)',
                            }}
                        >
                            <Settings2 className="w-4 h-4" />
                        </div>
                        <div className="min-w-0">
                            <p
                                className="text-sm font-semibold leading-tight"
                                style={{
                                    color: 'var(--c-ink)',
                                    fontFamily: "'Space Grotesk', sans-serif",
                                }}
                            >
                                Settings
                            </p>
                            <p
                                className="text-[11px] leading-tight"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                Workspace
                            </p>
                        </div>
                    </div>

                    {/* Nav items */}
                    <div className="flex-1 min-h-0 overflow-y-auto no-scrollbar p-2 space-y-0.5">
                        {tabs.map((tab) => {
                            const Icon = tab.icon;
                            const selected = tab.id === activeTab;
                            return (
                                <button
                                    key={tab.id}
                                    type="button"
                                    onClick={() => onSelect(tab.id)}
                                    className="group relative w-full flex items-center justify-between gap-2 rounded-lg pl-3 pr-2.5 py-2 text-sm transition-colors"
                                    style={{
                                        background: selected
                                            ? 'var(--c-signal-dim)'
                                            : 'transparent',
                                        color: selected ? 'var(--c-signal-text)' : 'var(--c-ink-2)',
                                    }}
                                    onMouseEnter={(e) => {
                                        if (!selected)
                                            e.currentTarget.style.background = 'var(--c-glass)';
                                    }}
                                    onMouseLeave={(e) => {
                                        if (!selected)
                                            e.currentTarget.style.background = 'transparent';
                                    }}
                                >
                                    {/* Active accent bar */}
                                    <span
                                        className="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-[3px] rounded-r-full transition-opacity"
                                        style={{
                                            background: 'var(--c-signal)',
                                            opacity: selected ? 1 : 0,
                                        }}
                                    />
                                    <span className="inline-flex items-center gap-2.5 min-w-0">
                                        <Icon className="w-4 h-4 shrink-0" />
                                        <span className="truncate font-medium">{tab.label}</span>
                                    </span>
                                    {!tab.implemented && (
                                        <span className="text-[9px] px-1.5 py-0.5 rounded border border-amber-400/30 text-amber-300 bg-amber-500/10">
                                            WIP
                                        </span>
                                    )}
                                </button>
                            );
                        })}
                    </div>

                    {/* Rail footer — Back to chat */}
                    <div className="p-2 border-t" style={{ borderColor: 'var(--c-glass-border)' }}>
                        <button
                            type="button"
                            onClick={onBack}
                            className="w-full inline-flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
                            style={{ color: 'var(--c-ink-2)' }}
                            onMouseEnter={(e) => {
                                e.currentTarget.style.background = 'var(--c-glass)';
                            }}
                            onMouseLeave={(e) => {
                                e.currentTarget.style.background = 'transparent';
                            }}
                        >
                            <ArrowLeft className="w-4 h-4 shrink-0" />
                            Back to chat
                        </button>
                    </div>
                </nav>

                {/* ── Content pane ──────────────────────────────────────────── */}
                <section className="flex-1 min-w-0 flex flex-col">
                    {/* Sticky pane header */}
                    <div
                        className="shrink-0 flex items-start justify-between gap-4 px-5 sm:px-6 py-4 border-b"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        <div className="min-w-0">
                            {/* Mobile back — rail is hidden under md */}
                            <button
                                type="button"
                                onClick={onBack}
                                className="md:hidden inline-flex items-center gap-1.5 text-xs mb-1.5"
                                style={{ color: 'var(--c-ink-2)' }}
                            >
                                <ArrowLeft className="w-3.5 h-3.5" />
                                Back
                            </button>
                            <h1
                                className="text-lg font-semibold leading-tight truncate"
                                style={{
                                    color: 'var(--c-ink)',
                                    fontFamily: "'Space Grotesk', sans-serif",
                                }}
                            >
                                {title}
                            </h1>
                            {description && (
                                <p className="text-sm mt-0.5" style={{ color: 'var(--c-ink-2)' }}>
                                    {description}
                                </p>
                            )}
                        </div>
                        {headerActions && (
                            <div className="shrink-0 flex items-center gap-2">{headerActions}</div>
                        )}
                    </div>

                    {/* Mobile tab switcher — rail is hidden under md */}
                    <div
                        className="md:hidden shrink-0 flex gap-1 overflow-x-auto no-scrollbar px-4 py-2 border-b"
                        style={{ borderColor: 'var(--c-glass-border)' }}
                    >
                        {tabs.map((tab) => {
                            const Icon = tab.icon;
                            const selected = tab.id === activeTab;
                            return (
                                <button
                                    key={tab.id}
                                    type="button"
                                    onClick={() => onSelect(tab.id)}
                                    className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium whitespace-nowrap transition-colors"
                                    style={{
                                        background: selected
                                            ? 'var(--c-signal-dim)'
                                            : 'transparent',
                                        color: selected ? 'var(--c-signal-text)' : 'var(--c-ink-2)',
                                    }}
                                >
                                    <Icon className="w-3.5 h-3.5" />
                                    {tab.label}
                                </button>
                            );
                        })}
                    </div>

                    {/* Scrollable body — the ONLY scroller */}
                    <div className="flex-1 min-h-0 overflow-y-auto px-5 sm:px-6 py-5">
                        {children}
                    </div>
                </section>
            </div>
        </div>
    );
}
