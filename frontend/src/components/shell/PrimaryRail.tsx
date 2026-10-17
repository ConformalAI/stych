import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { NAV_SECTIONS, activeNavId, type NavItem } from './navItems';

interface PrimaryRailProps {
    expanded: boolean;
    onToggle: () => void;
}

/**
 * The platform's top-level navigation: a thin, always-present icon rail that
 * expands to show labels. Collapsed is the default — the rail should cost as
 * little horizontal space as possible next to the session sidebar.
 */
export default function PrimaryRail({ expanded, onToggle }: PrimaryRailProps) {
    const { pathname } = useLocation();
    const active = activeNavId(pathname);

    return (
        <nav
            aria-label="Primary"
            className="flex flex-col shrink-0 transition-[width] duration-200 ease-out"
            style={{
                width: expanded ? 208 : 56,
                background: 'var(--c-slate)',
                borderRight: '1px solid var(--c-glass-border)',
            }}
        >
            <div className="flex-1 overflow-y-auto overflow-x-hidden py-3 space-y-4">
                {NAV_SECTIONS.map((section) => (
                    <div key={section.id}>
                        {expanded && section.label && (
                            <p
                                className="text-[10px] uppercase tracking-widest font-semibold px-4 mb-1.5"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                {section.label}
                            </p>
                        )}
                        <ul className="space-y-0.5 px-2">
                            {section.items.map((item) => (
                                <li key={item.id}>
                                    <RailLink
                                        item={item}
                                        expanded={expanded}
                                        active={active === item.id}
                                    />
                                </li>
                            ))}
                        </ul>
                    </div>
                ))}
            </div>

            <div className="p-2" style={{ borderTop: '1px solid var(--c-glass-border)' }}>
                <button
                    type="button"
                    onClick={onToggle}
                    aria-expanded={expanded}
                    title={expanded ? 'Collapse sidebar' : 'Expand sidebar'}
                    className="w-full flex items-center gap-3 rounded-lg px-3 py-2 transition-colors hover:bg-[var(--c-glass)]"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    {expanded ? (
                        <PanelLeftClose className="w-4 h-4 shrink-0" />
                    ) : (
                        <PanelLeftOpen className="w-4 h-4 shrink-0" />
                    )}
                    {expanded && <span className="text-xs font-medium">Collapse</span>}
                </button>
            </div>
        </nav>
    );
}

function RailLink({
    item,
    expanded,
    active,
}: {
    item: NavItem;
    expanded: boolean;
    active: boolean;
}) {
    // Collapsed icons need their label somewhere reachable. A hover card beats a
    // native title tooltip here: it appears instantly and can carry the hint line.
    const [hovered, setHovered] = useState(false);
    const Icon = item.icon;
    const showTooltip = hovered && !expanded;

    return (
        <div className="relative">
            <Link
                to={item.path}
                aria-current={active ? 'page' : undefined}
                onMouseEnter={() => setHovered(true)}
                onMouseLeave={() => setHovered(false)}
                onBlur={() => setHovered(false)}
                className="relative flex items-center rounded-xl transition-colors"
                style={{
                    gap: expanded ? 12 : 0,
                    // The feature item gets real height and its own raised surface so
                    // it reads as the product's centre of gravity, not a peer link.
                    padding: item.feature
                        ? expanded
                            ? '14px 12px'
                            : '14px 8px'
                        : expanded
                          ? '8px 12px'
                          : '8px',
                    justifyContent: expanded ? 'flex-start' : 'center',
                    background: active
                        ? 'var(--c-signal-dim)'
                        : item.feature
                          ? 'var(--c-slate-2)'
                          : 'transparent',
                    border: item.feature
                        ? `1px solid ${
                              active
                                  ? 'color-mix(in srgb, var(--c-signal) 40%, transparent)'
                                  : 'var(--c-glass-border)'
                          }`
                        : '1px solid transparent',
                    boxShadow: item.feature
                        ? active
                            ? '0 2px 12px var(--c-signal-glow)'
                            : '0 1px 3px rgba(0,0,0,0.18)'
                        : 'none',
                    color: active ? 'var(--c-signal-text)' : 'var(--c-ink)',
                }}
                onMouseOver={(e) => {
                    if (active) return;
                    e.currentTarget.style.background = item.feature
                        ? 'var(--c-mist)'
                        : 'var(--c-glass)';
                }}
                onMouseOut={(e) => {
                    if (active) return;
                    e.currentTarget.style.background = item.feature
                        ? 'var(--c-slate-2)'
                        : 'transparent';
                }}
            >
                {active && !item.feature && (
                    <span
                        aria-hidden="true"
                        className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full"
                        style={{ background: 'var(--c-signal)' }}
                    />
                )}
                <Icon
                    className={item.feature ? 'w-[22px] h-[22px] shrink-0' : 'w-4 h-4 shrink-0'}
                    style={item.feature && !active ? { color: 'var(--c-signal-text)' } : undefined}
                />
                {expanded && (
                    <span
                        className={item.feature ? 'text-sm truncate' : 'text-sm truncate'}
                        style={{
                            fontWeight: item.feature ? 600 : 500,
                            color: item.feature || active ? undefined : 'var(--c-ink-2)',
                        }}
                    >
                        {item.label}
                    </span>
                )}
            </Link>

            {showTooltip && (
                <div
                    role="tooltip"
                    className="absolute left-full top-0 ml-2 z-50 pointer-events-none rounded-lg border px-3 py-2 whitespace-nowrap"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                        boxShadow: '0 12px 32px rgba(0,0,0,0.35)',
                    }}
                >
                    <p className="text-xs font-semibold" style={{ color: 'var(--c-ink)' }}>
                        {item.label}
                    </p>
                    <p className="text-[11px] mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                        {item.hint}
                    </p>
                </div>
            )}
        </div>
    );
}
