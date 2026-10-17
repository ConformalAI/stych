import { X } from 'lucide-react';
import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';
import {
    HEALTH_LABELS,
    VENDOR_LABELS,
    healthColors,
    healthOf,
    type HealthState,
} from './connectorMeta';
import {
    activeFilterCount,
    facetCounts,
    tagCounts,
    toggleFacet,
    type ConnectorFilters,
} from './filterConnections';

interface ConnectorFilterRailProps {
    connections: SavedConnection[];
    catalog: ConnectorCatalogEntry[];
    filters: ConnectorFilters;
    onChange: (next: ConnectorFilters) => void;
    onClear: () => void;
}

const HEALTH_ORDER: HealthState[] = ['ok', 'error', 'untested'];

/**
 * Faceted filters for the connector list. Counts come from the unfiltered set so
 * a facet never silently reads zero just because another facet is applied.
 */
export default function ConnectorFilterRail({
    connections,
    catalog,
    filters,
    onChange,
    onClear,
}: ConnectorFilterRailProps) {
    const vendorOf = new Map(catalog.map((entry) => [entry.id as string, entry.vendor]));
    const labelOf = new Map(catalog.map((entry) => [entry.id as string, entry.label]));

    const healthFacets = facetCounts(connections, healthOf);
    const vendorFacets = facetCounts(connections, (c) => vendorOf.get(c.connector_id) ?? 'other');
    const typeFacets = facetCounts(connections, (c) => c.connector_id as string);
    const tags = tagCounts(connections);
    const activeCount = activeFilterCount(filters);

    return (
        <aside
            className="w-56 shrink-0 overflow-y-auto p-4 space-y-6"
            style={{ borderRight: '1px solid var(--c-glass-border)' }}
            aria-label="Filters"
        >
            <div className="flex items-center justify-between">
                <h2
                    className="text-[10px] uppercase tracking-widest font-semibold"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Filters
                </h2>
                {activeCount > 0 && (
                    <button
                        type="button"
                        onClick={onClear}
                        className="inline-flex items-center gap-1 text-[11px] font-medium rounded-md px-1.5 py-0.5 transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-signal-text)' }}
                    >
                        <X className="w-3 h-3" />
                        Clear {activeCount}
                    </button>
                )}
            </div>

            <FacetGroup label="Status">
                {HEALTH_ORDER.map((state) => {
                    const colors = healthColors(state);
                    return (
                        <FacetRow
                            key={state}
                            checked={filters.health.includes(state)}
                            count={healthFacets.get(state) ?? 0}
                            onToggle={() =>
                                onChange({ ...filters, health: toggleFacet(filters.health, state) })
                            }
                        >
                            <span
                                aria-hidden="true"
                                className="w-1.5 h-1.5 rounded-full shrink-0"
                                style={{ background: colors.fg }}
                            />
                            {HEALTH_LABELS[state]}
                        </FacetRow>
                    );
                })}
            </FacetGroup>

            {vendorFacets.size > 0 && (
                <FacetGroup label="Provider">
                    {[...vendorFacets.keys()].sort().map((vendor) => (
                        <FacetRow
                            key={vendor}
                            checked={filters.vendors.includes(vendor)}
                            count={vendorFacets.get(vendor) ?? 0}
                            onToggle={() =>
                                onChange({
                                    ...filters,
                                    vendors: toggleFacet(filters.vendors, vendor),
                                })
                            }
                        >
                            {VENDOR_LABELS[vendor] ?? vendor}
                        </FacetRow>
                    ))}
                </FacetGroup>
            )}

            {typeFacets.size > 0 && (
                <FacetGroup label="Type">
                    {[...typeFacets.keys()]
                        .sort((a, b) => (labelOf.get(a) ?? a).localeCompare(labelOf.get(b) ?? b))
                        .map((type) => (
                            <FacetRow
                                key={type}
                                checked={filters.types.includes(type)}
                                count={typeFacets.get(type) ?? 0}
                                onToggle={() =>
                                    onChange({
                                        ...filters,
                                        types: toggleFacet(filters.types, type),
                                    })
                                }
                            >
                                {labelOf.get(type) ?? type}
                            </FacetRow>
                        ))}
                </FacetGroup>
            )}

            {tags.length > 0 && (
                <FacetGroup label="Tags">
                    {tags.map(({ tag, count }) => (
                        <FacetRow
                            key={tag}
                            checked={filters.tags.includes(tag)}
                            count={count}
                            onToggle={() =>
                                onChange({ ...filters, tags: toggleFacet(filters.tags, tag) })
                            }
                        >
                            <span className="truncate">{tag}</span>
                        </FacetRow>
                    ))}
                </FacetGroup>
            )}
        </aside>
    );
}

function FacetGroup({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div>
            <p
                className="text-[10px] uppercase tracking-widest font-semibold mb-1.5"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {label}
            </p>
            <ul className="space-y-0.5">{children}</ul>
        </div>
    );
}

function FacetRow({
    checked,
    count,
    onToggle,
    children,
}: {
    checked: boolean;
    count: number;
    onToggle: () => void;
    children: React.ReactNode;
}) {
    return (
        <li>
            <button
                type="button"
                onClick={onToggle}
                aria-pressed={checked}
                className="w-full flex items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-[var(--c-glass)]"
                style={{ color: checked ? 'var(--c-ink)' : 'var(--c-ink-2)' }}
            >
                <span
                    aria-hidden="true"
                    className="w-3.5 h-3.5 rounded border shrink-0 flex items-center justify-center"
                    style={{
                        borderColor: checked ? 'var(--c-signal)' : 'var(--c-glass-border)',
                        background: checked ? 'var(--c-signal)' : 'transparent',
                    }}
                >
                    {checked && (
                        <svg viewBox="0 0 10 8" className="w-2.5 h-2" fill="none">
                            <path
                                d="M1 4L3.5 6.5L9 1"
                                stroke="#fff"
                                strokeWidth="1.8"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                            />
                        </svg>
                    )}
                </span>
                <span className="flex items-center gap-1.5 min-w-0 flex-1 text-left">
                    {children}
                </span>
                <span className="text-[10px] tabular-nums" style={{ color: 'var(--c-mist-fg)' }}>
                    {count}
                </span>
            </button>
        </li>
    );
}
