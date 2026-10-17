import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';
import { endpointLabel, healthOf, lastUsedValue, type HealthState } from './connectorMeta';

export type SortKey = 'recent' | 'name' | 'type' | 'health';

export interface ConnectorFilters {
    query: string;
    vendors: string[];
    types: string[];
    health: HealthState[];
    tags: string[];
}

export const EMPTY_FILTERS: ConnectorFilters = {
    query: '',
    vendors: [],
    types: [],
    health: [],
    tags: [],
};

export function hasActiveFilters(filters: ConnectorFilters): boolean {
    return (
        filters.query.trim() !== '' ||
        filters.vendors.length > 0 ||
        filters.types.length > 0 ||
        filters.health.length > 0 ||
        filters.tags.length > 0
    );
}

export function activeFilterCount(filters: ConnectorFilters): number {
    return (
        filters.vendors.length + filters.types.length + filters.health.length + filters.tags.length
    );
}

/** Toggle a value in one of the multi-select facets. */
export function toggleFacet<T extends string>(current: T[], value: T): T[] {
    return current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
}

/**
 * Free-text match. Deliberately searches the fields a person would type from
 * memory — the connection's name, what it points at, and its tags — rather than
 * every field, so results stay predictable.
 */
function matchesQuery(connection: SavedConnection, typeLabel: string, rawQuery: string): boolean {
    const query = rawQuery.trim().toLowerCase();
    if (!query) return true;

    // Support `tag:foo` as an explicit scope; bare terms search everything.
    if (query.startsWith('tag:')) {
        const wanted = query.slice(4).trim();
        return wanted ? connection.tags.some((t) => t.includes(wanted)) : true;
    }

    const haystack = [
        connection.name,
        connection.description ?? '',
        typeLabel,
        endpointLabel(connection),
        ...connection.tags,
    ]
        .join(' ')
        .toLowerCase();

    // Every whitespace-separated term must appear, so extra words narrow.
    return query.split(/\s+/).every((term) => haystack.includes(term));
}

export interface FilterContext {
    catalog: ConnectorCatalogEntry[];
}

export function filterAndSortConnections(
    connections: SavedConnection[],
    filters: ConnectorFilters,
    sort: SortKey,
    { catalog }: FilterContext
): SavedConnection[] {
    const byId = new Map(catalog.map((entry) => [entry.id as string, entry]));

    const filtered = connections.filter((connection) => {
        const entry = byId.get(connection.connector_id);
        const typeLabel = entry?.label ?? connection.connector_id;

        if (!matchesQuery(connection, typeLabel, filters.query)) return false;
        if (filters.types.length && !filters.types.includes(connection.connector_id)) return false;
        if (filters.vendors.length) {
            const vendor = entry?.vendor ?? 'other';
            if (!filters.vendors.includes(vendor)) return false;
        }
        if (filters.health.length && !filters.health.includes(healthOf(connection))) return false;
        if (filters.tags.length && !filters.tags.every((tag) => connection.tags.includes(tag))) {
            return false;
        }
        return true;
    });

    const label = (c: SavedConnection) => byId.get(c.connector_id)?.label ?? c.connector_id;
    const healthRank: Record<HealthState, number> = { error: 0, untested: 1, ok: 2 };

    return [...filtered].sort((a, b) => {
        switch (sort) {
            case 'name':
                return a.name.localeCompare(b.name);
            case 'type':
                return label(a).localeCompare(label(b)) || a.name.localeCompare(b.name);
            case 'health':
                // Problems first — this sort exists to surface what needs attention.
                return (
                    healthRank[healthOf(a)] - healthRank[healthOf(b)] ||
                    a.name.localeCompare(b.name)
                );
            case 'recent':
            default:
                return lastUsedValue(b) - lastUsedValue(a) || a.name.localeCompare(b.name);
        }
    });
}

/** Every tag in use, with counts, ordered by frequency then alphabetically. */
export function tagCounts(connections: SavedConnection[]): Array<{ tag: string; count: number }> {
    const counts = new Map<string, number>();
    for (const connection of connections) {
        for (const tag of connection.tags) {
            counts.set(tag, (counts.get(tag) ?? 0) + 1);
        }
    }
    return [...counts.entries()]
        .map(([tag, count]) => ({ tag, count }))
        .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
}

/** Counts per facet value, used for the filter rail's trailing numbers. */
export function facetCounts<T extends string>(
    connections: SavedConnection[],
    valueOf: (connection: SavedConnection) => T
): Map<T, number> {
    const counts = new Map<T, number>();
    for (const connection of connections) {
        const value = valueOf(connection);
        counts.set(value, (counts.get(value) ?? 0) + 1);
    }
    return counts;
}
