import { describe, it, expect } from 'vitest';
import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';
import {
    EMPTY_FILTERS,
    activeFilterCount,
    filterAndSortConnections,
    hasActiveFilters,
    tagCounts,
    toggleFacet,
    type ConnectorFilters,
} from './filterConnections';
import { endpointLabel, healthOf, relativeTime } from './connectorMeta';

const catalog = [
    { id: 'bigquery', label: 'BigQuery', vendor: 'gcp' },
    { id: 'redshift', label: 'Amazon Redshift', vendor: 'aws' },
    { id: 'postgres', label: 'PostgreSQL', vendor: 'postgresql' },
] as ConnectorCatalogEntry[];

const conn = (over: Partial<SavedConnection> & { id: string }): SavedConnection =>
    ({
        connector_id: 'postgres',
        name: 'Conn',
        tags: [],
        hint: {},
        tables: [],
        ...over,
    }) as SavedConnection;

const filters = (over: Partial<ConnectorFilters> = {}): ConnectorFilters => ({
    ...EMPTY_FILTERS,
    ...over,
});

const rows = [
    conn({
        id: 'a',
        name: 'Finance warehouse',
        connector_id: 'bigquery',
        tags: ['prod', 'finance'],
        hint: { project_id: 'acme-analytics' },
        last_test_status: 'ok',
        last_used_at: '2026-08-18T10:00:00',
    }),
    conn({
        id: 'b',
        name: 'Events lake',
        connector_id: 'redshift',
        tags: ['prod'],
        hint: { host: 'redshift.internal', port: 5439, database: 'events' },
        last_test_status: 'error',
        last_used_at: '2026-08-17T10:00:00',
    }),
    conn({
        id: 'c',
        name: 'Local dev',
        connector_id: 'postgres',
        tags: ['dev'],
        hint: { host: 'localhost', port: 5432, database: 'app' },
    }),
];

const ids = (list: SavedConnection[]) => list.map((c) => c.id);

describe('connector filtering', () => {
    it('returns everything when no filters are set', () => {
        // 'name' sorts alphabetically: Events lake, Finance warehouse, Local dev.
        expect(ids(filterAndSortConnections(rows, EMPTY_FILTERS, 'name', { catalog }))).toEqual([
            'b',
            'a',
            'c',
        ]);
    });

    it('searches name, endpoint, type label and tags', () => {
        const find = (query: string) =>
            ids(filterAndSortConnections(rows, filters({ query }), 'name', { catalog }));

        expect(find('finance')).toEqual(['a']); // name + tag
        expect(find('acme-analytics')).toEqual(['a']); // endpoint from hint
        expect(find('redshift')).toEqual(['b']); // type label
        expect(find('dev')).toEqual(['c']); // tag
    });

    it('narrows as more terms are typed', () => {
        const find = (query: string) =>
            ids(filterAndSortConnections(rows, filters({ query }), 'name', { catalog }));
        expect(find('prod')).toEqual(['b', 'a']); // 'prod' tags both...
        expect(find('prod finance')).toEqual(['a']); // ...a second term narrows
        expect(find('lake events')).toEqual(['b']); // terms may appear in any order
    });

    it('supports an explicit tag: scope', () => {
        const find = (query: string) =>
            ids(filterAndSortConnections(rows, filters({ query }), 'name', { catalog }));
        expect(find('tag:prod')).toEqual(['b', 'a']);
        expect(find('tag:dev')).toEqual(['c']);
    });

    it('filters by vendor facet', () => {
        expect(
            ids(filterAndSortConnections(rows, filters({ vendors: ['gcp'] }), 'name', { catalog }))
        ).toEqual(['a']);
        expect(
            ids(
                filterAndSortConnections(rows, filters({ vendors: ['gcp', 'aws'] }), 'name', {
                    catalog,
                })
            )
        ).toEqual(['b', 'a']);
    });

    it('filters by health, treating never-tested as untested', () => {
        const pick = (health: ConnectorFilters['health']) =>
            ids(filterAndSortConnections(rows, filters({ health }), 'name', { catalog }));
        expect(pick(['error'])).toEqual(['b']);
        expect(pick(['untested'])).toEqual(['c']);
        expect(pick(['ok', 'error'])).toEqual(['b', 'a']);
    });

    it('ANDs multiple selected tags', () => {
        expect(
            ids(filterAndSortConnections(rows, filters({ tags: ['prod'] }), 'name', { catalog }))
        ).toEqual(['b', 'a']);
        expect(
            ids(
                filterAndSortConnections(rows, filters({ tags: ['prod', 'finance'] }), 'name', {
                    catalog,
                })
            )
        ).toEqual(['a']);
    });

    it('combines facets with search', () => {
        const result = filterAndSortConnections(
            rows,
            filters({ query: 'prod', tags: ['prod'] }),
            'name',
            { catalog }
        );
        expect(ids(result)).toEqual(['b', 'a']);
    });
});

describe('connector sorting', () => {
    it('sorts most-recently-used first, never-used last', () => {
        expect(ids(filterAndSortConnections(rows, EMPTY_FILTERS, 'recent', { catalog }))).toEqual([
            'a',
            'b',
            'c',
        ]);
    });

    it('surfaces failing connections first when sorting by health', () => {
        expect(ids(filterAndSortConnections(rows, EMPTY_FILTERS, 'health', { catalog }))).toEqual([
            'b',
            'c',
            'a',
        ]);
    });

    it('sorts by type label, not connector id', () => {
        expect(ids(filterAndSortConnections(rows, EMPTY_FILTERS, 'type', { catalog }))).toEqual([
            'b', // Amazon Redshift
            'a', // BigQuery
            'c', // PostgreSQL
        ]);
    });
});

describe('filter helpers', () => {
    it('toggles facet values', () => {
        expect(toggleFacet(['a'], 'b')).toEqual(['a', 'b']);
        expect(toggleFacet(['a', 'b'], 'a')).toEqual(['b']);
    });

    it('reports active state and count', () => {
        expect(hasActiveFilters(EMPTY_FILTERS)).toBe(false);
        expect(hasActiveFilters(filters({ query: '  ' }))).toBe(false);
        expect(hasActiveFilters(filters({ tags: ['x'] }))).toBe(true);
        expect(activeFilterCount(filters({ tags: ['x'], vendors: ['gcp'] }))).toBe(2);
    });

    it('counts tags by frequency then name', () => {
        expect(tagCounts(rows)).toEqual([
            { tag: 'prod', count: 2 },
            { tag: 'dev', count: 1 },
            { tag: 'finance', count: 1 },
        ]);
    });
});

describe('connector metadata', () => {
    it('builds endpoint labels per connector shape', () => {
        expect(endpointLabel(rows[0])).toBe('acme-analytics');
        expect(endpointLabel(rows[1])).toBe('redshift.internal:5439/events');
        expect(endpointLabel(conn({ id: 'd', hint: { use_default: true } }))).toBe(
            'Server default connection'
        );
        expect(endpointLabel(conn({ id: 'e' }))).toBe('—');
    });

    it('never leaks a secret-shaped hint value into the endpoint label', () => {
        const label = endpointLabel(
            conn({ id: 'f', hint: { host: 'db.internal', port: 5432, database: 'app' } })
        );
        expect(label).toBe('db.internal:5432/app');
    });

    it('maps test status to health state', () => {
        expect(healthOf(rows[0])).toBe('ok');
        expect(healthOf(rows[1])).toBe('error');
        expect(healthOf(rows[2])).toBe('untested');
    });

    it('renders never-used timestamps as Never', () => {
        expect(relativeTime(null)).toBe('Never');
        expect(relativeTime(undefined)).toBe('Never');
        expect(relativeTime('not-a-date')).toBe('Never');
    });

    it('treats naive backend timestamps as UTC', () => {
        const iso = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
        expect(relativeTime(iso.replace('Z', ''))).toBe('2h ago');
    });
});
