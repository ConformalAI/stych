import { Plus, Table2 } from 'lucide-react';
import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';
import {
    HEALTH_LABELS,
    VENDOR_LABELS,
    connectorIcon,
    connectorIconColors,
    endpointLabel,
    healthColors,
    healthOf,
    relativeTime,
} from './connectorMeta';

/** A connection the user has already configured. */
export function ConnectorCard({
    connection,
    typeLabel,
    selected,
    onSelect,
}: {
    connection: SavedConnection;
    typeLabel: string;
    selected: boolean;
    onSelect: () => void;
}) {
    const Icon = connectorIcon(connection.connector_id);
    const iconColors = connectorIconColors(connection.connector_id);
    const health = healthOf(connection);
    const colors = healthColors(health);

    return (
        <button
            type="button"
            onClick={onSelect}
            aria-pressed={selected}
            className="w-full h-full flex flex-col text-left rounded-xl border p-3.5 transition-all"
            style={{
                background: selected ? 'var(--c-signal-dim)' : 'var(--c-glass)',
                borderColor: selected
                    ? 'color-mix(in srgb, var(--c-signal) 40%, transparent)'
                    : 'var(--c-glass-border)',
            }}
            onMouseOver={(e) => {
                if (!selected) e.currentTarget.style.background = 'var(--c-slate-2)';
            }}
            onMouseOut={(e) => {
                if (!selected) e.currentTarget.style.background = 'var(--c-glass)';
            }}
        >
            <div className="flex items-start gap-2.5">
                <span
                    aria-hidden="true"
                    className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                    style={{
                        background: iconColors.bg,
                        border: `1px solid ${iconColors.border}`,
                    }}
                >
                    <Icon className="w-4 h-4" style={{ color: iconColors.fg }} />
                </span>
                <span className="min-w-0 flex-1">
                    <span
                        className="block text-sm font-semibold truncate"
                        style={{ color: 'var(--c-ink)' }}
                    >
                        {connection.name}
                    </span>
                    <span className="block text-[11px] mt-0.5" style={{ color: 'var(--c-ink-2)' }}>
                        {typeLabel}
                    </span>
                </span>
                <span
                    className="inline-flex items-center gap-1 text-[10px] font-semibold rounded-md px-1.5 py-0.5 border shrink-0"
                    style={{ color: colors.fg, background: colors.bg, borderColor: colors.border }}
                    title={HEALTH_LABELS[health]}
                >
                    <span
                        aria-hidden="true"
                        className="w-1.5 h-1.5 rounded-full"
                        style={{ background: colors.fg }}
                    />
                    {HEALTH_LABELS[health]}
                </span>
            </div>

            <p
                className="text-[11px] font-mono truncate mt-3"
                style={{ color: 'var(--c-mist-fg)' }}
                title={endpointLabel(connection)}
            >
                {endpointLabel(connection)}
            </p>

            {connection.description && (
                <p className="text-[11px] mt-1.5 line-clamp-2" style={{ color: 'var(--c-ink-2)' }}>
                    {connection.description}
                </p>
            )}

            {connection.tags.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-2.5">
                    {connection.tags.slice(0, 4).map((tag) => (
                        <span
                            key={tag}
                            className="text-[10px] font-medium rounded px-1.5 py-0.5 border"
                            style={{
                                color: 'var(--c-ink-2)',
                                background: 'var(--c-slate-2)',
                                borderColor: 'var(--c-glass-border)',
                            }}
                        >
                            {tag}
                        </span>
                    ))}
                    {connection.tags.length > 4 && (
                        <span className="text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                            +{connection.tags.length - 4}
                        </span>
                    )}
                </div>
            )}

            <div
                className="flex items-center gap-3 mt-auto pt-3 text-[10px]"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {connection.tables.length > 0 && (
                    <span className="inline-flex items-center gap-1">
                        <Table2 className="w-3 h-3" />
                        {connection.tables.length} pinned
                    </span>
                )}
                <span className="ml-auto">Used {relativeTime(connection.last_used_at)}</span>
            </div>
        </button>
    );
}

/** A connector type the platform ships that the user can set up. */
export function AvailableConnectorCard({
    entry,
    onConnect,
}: {
    entry: ConnectorCatalogEntry;
    onConnect: () => void;
}) {
    const Icon = connectorIcon(entry.id);
    const iconColors = connectorIconColors(entry.id);

    return (
        <button
            type="button"
            onClick={onConnect}
            className="group w-full h-full flex flex-col text-left rounded-xl border p-3.5 transition-all"
            style={{ background: 'transparent', borderColor: 'var(--c-glass-border)' }}
            onMouseOver={(e) => {
                e.currentTarget.style.background = 'var(--c-glass)';
                e.currentTarget.style.borderColor =
                    'color-mix(in srgb, var(--c-signal) 30%, transparent)';
            }}
            onMouseOut={(e) => {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.borderColor = 'var(--c-glass-border)';
            }}
        >
            <div className="flex items-start gap-2.5">
                <span
                    aria-hidden="true"
                    className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                    style={{ background: iconColors.bg, border: `1px solid ${iconColors.border}` }}
                >
                    <Icon className="w-4 h-4" style={{ color: iconColors.fg }} />
                </span>
                <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                        <span
                            className="text-sm font-semibold truncate"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            {entry.label}
                        </span>
                        {entry.maturity !== 'production' && (
                            <span
                                className="text-[9px] font-bold uppercase px-1 py-0.5 rounded shrink-0"
                                style={{
                                    color: 'var(--c-warning)',
                                    background:
                                        'color-mix(in srgb, var(--c-warning) 15%, transparent)',
                                }}
                            >
                                {entry.maturity}
                            </span>
                        )}
                    </span>
                    <span className="block text-[11px] mt-0.5" style={{ color: 'var(--c-ink-2)' }}>
                        {VENDOR_LABELS[entry.vendor] ?? entry.vendor}
                        {entry.configured_count > 0 && ` · ${entry.configured_count} configured`}
                    </span>
                </span>
            </div>

            <p className="text-[11px] mt-3" style={{ color: 'var(--c-mist-fg)' }}>
                {entry.summary}
            </p>

            <span
                className="inline-flex items-center gap-1 mt-auto pt-3 text-[11px] font-semibold"
                style={{ color: 'var(--c-signal-text)' }}
            >
                <Plus className="w-3.5 h-3.5" />
                Connect
            </span>
        </button>
    );
}
