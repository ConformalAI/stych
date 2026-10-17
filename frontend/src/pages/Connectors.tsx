import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Loader2, RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import {
    getConnectorCatalog,
    listSavedConnections,
    type ConnectorCatalogEntry,
    type ConnectorId,
    type SavedConnection,
} from '../services/api';
import { useStore } from '../store/useStore';
import ConnectorFilterRail from '../components/connectors/ConnectorFilterRail';
import ConnectorDetailDrawer from '../components/connectors/ConnectorDetailDrawer';
import DeleteConnectionDialog from '../components/connectors/DeleteConnectionDialog';
import { AvailableConnectorCard, ConnectorCard } from '../components/connectors/ConnectorCards';
import { connectorForm } from '../components/connectors/connectorForms';
import {
    EMPTY_FILTERS,
    activeFilterCount,
    filterAndSortConnections,
    tagCounts,
    type ConnectorFilters,
    type SortKey,
} from '../components/connectors/filterConnections';
import {
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    connectorIcon,
    connectorIconColors,
    healthOf,
} from '../components/connectors/connectorMeta';

const SORT_OPTIONS: Array<{ value: SortKey; label: string }> = [
    { value: 'recent', label: 'Recently used' },
    { value: 'name', label: 'Name' },
    { value: 'type', label: 'Type' },
    { value: 'health', label: 'Status' },
];

/**
 * Connector management. Configured connections sit on top, the connectors you
 * could still add are grouped below, and one search spans both — setting up a
 * new source is a normal part of this page rather than a separate dialog.
 */
export default function Connectors() {
    const [connections, setConnections] = useState<SavedConnection[]>([]);
    const [catalog, setCatalog] = useState<ConnectorCatalogEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [filters, setFilters] = useState<ConnectorFilters>(EMPTY_FILTERS);
    const [sort, setSort] = useState<SortKey>('recent');
    const [showFilters, setShowFilters] = useState(true);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    /** Non-null while setting up a new connection of this type, inline. */
    const [connecting, setConnecting] = useState<ConnectorId | null>(null);
    const [pendingDelete, setPendingDelete] = useState<SavedConnection | null>(null);
    const addToast = useStore((s) => s.addToast);

    const notifyError = useCallback(
        (message: string) => addToast({ message, type: 'error' }),
        [addToast]
    );
    const notifySuccess = useCallback(
        (message: string) => addToast({ message, type: 'success' }),
        [addToast]
    );

    const load = useCallback(
        async (mode: 'initial' | 'refresh' = 'refresh') => {
            if (mode === 'refresh') setRefreshing(true);
            try {
                const [rows, entries] = await Promise.all([
                    listSavedConnections(),
                    getConnectorCatalog(),
                ]);
                setConnections(rows);
                setCatalog(entries);
            } catch (err) {
                notifyError(errorMessage(err, 'Could not load connectors'));
            } finally {
                setLoading(false);
                setRefreshing(false);
            }
        },
        [notifyError]
    );

    useEffect(() => {
        load('initial');
    }, [load]);

    const visible = useMemo(
        () => filterAndSortConnections(connections, filters, sort, { catalog }),
        [connections, filters, sort, catalog]
    );
    const allTags = useMemo(() => tagCounts(connections).map((t) => t.tag), [connections]);
    const failing = useMemo(
        () => connections.filter((c) => healthOf(c) === 'error').length,
        [connections]
    );

    // Facets describe saved connections, so while any are active the "available"
    // shelf would just be noise below a narrowed result set.
    const facetsActive = activeFilterCount(filters) > 0;
    const term = filters.query.trim().toLowerCase();
    const availableGroups = useMemo(() => {
        if (facetsActive) return [];
        const matching = catalog.filter((entry) => {
            if (!entry.enabled) return false;
            if (!term) return true;
            return [entry.label, entry.vendor, entry.category, entry.summary]
                .join(' ')
                .toLowerCase()
                .includes(term);
        });
        const grouped = new Map<string, ConnectorCatalogEntry[]>();
        for (const entry of matching) {
            grouped.set(entry.category, [...(grouped.get(entry.category) ?? []), entry]);
        }
        return [...grouped.entries()].sort(
            (a, b) => CATEGORY_ORDER.indexOf(a[0]) - CATEGORY_ORDER.indexOf(b[0])
        );
    }, [catalog, term, facetsActive]);

    const selected = visible.find((c) => c.id === selectedId) ?? null;
    const entryFor = (connection: SavedConnection) =>
        catalog.find((c) => c.id === connection.connector_id);
    const connectingEntry = catalog.find((c) => c.id === connecting);

    // ── Inline setup view: replaces the browse view, no dialog ──
    if (connecting && connectingEntry) {
        const Icon = connectorIcon(connecting);
        const iconColors = connectorIconColors(connecting);
        return (
            <div className="h-full overflow-y-auto">
                <div className="max-w-2xl mx-auto px-6 py-6">
                    <button
                        type="button"
                        onClick={() => setConnecting(null)}
                        className="inline-flex items-center gap-1.5 text-xs font-medium mb-5 rounded-md px-2 py-1 -ml-2 transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <ArrowLeft className="w-3.5 h-3.5" />
                        All connectors
                    </button>

                    <div className="flex items-start gap-3 mb-6">
                        <span
                            aria-hidden="true"
                            className="w-11 h-11 rounded-xl flex items-center justify-center shrink-0"
                            style={{
                                background: iconColors.bg,
                                border: `1px solid ${iconColors.border}`,
                            }}
                        >
                            <Icon className="w-5 h-5" style={{ color: iconColors.fg }} />
                        </span>
                        <div>
                            <h1
                                className="font-display font-semibold text-lg"
                                style={{ color: 'var(--c-ink)' }}
                            >
                                Connect {connectingEntry.label}
                            </h1>
                            <p className="text-xs mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                                {connectingEntry.summary} · Credentials are encrypted and tested
                                before saving.
                            </p>
                        </div>
                    </div>

                    {connectorForm(connecting, {
                        onSaved: (connection) => {
                            setConnecting(null);
                            notifySuccess(`${connection.name} connected`);
                            setSelectedId(connection.id);
                            load('refresh');
                        },
                        onCancel: () => setConnecting(null),
                    })}
                </div>
            </div>
        );
    }

    return (
        <div className="h-full flex flex-col overflow-hidden">
            <header className="px-6 pt-5 pb-4 shrink-0">
                <div className="flex items-start justify-between gap-4">
                    <div>
                        <h1
                            className="font-display font-semibold text-lg"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            Connectors
                        </h1>
                        <p className="text-xs mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                            {loading
                                ? 'Loading…'
                                : `${connections.length} connection${connections.length === 1 ? '' : 's'}`}
                            {failing > 0 && (
                                <span style={{ color: 'var(--c-danger)' }}>
                                    {' '}
                                    · {failing} failing
                                </span>
                            )}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={() => load('refresh')}
                        disabled={refreshing}
                        aria-label="Refresh"
                        className="p-2 rounded-xl transition-colors hover:bg-[var(--c-glass)] disabled:opacity-50"
                        style={{
                            color: 'var(--c-mist-fg)',
                            border: '1px solid var(--c-glass-border)',
                        }}
                    >
                        <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
                    </button>
                </div>

                <div className="flex items-center gap-2 mt-4">
                    <button
                        type="button"
                        onClick={() => setShowFilters((v) => !v)}
                        aria-pressed={showFilters}
                        className="inline-flex items-center gap-1.5 rounded-xl px-2.5 py-2 text-xs font-medium transition-colors hover:bg-[var(--c-glass)]"
                        style={{
                            color: showFilters ? 'var(--c-signal-text)' : 'var(--c-mist-fg)',
                            border: '1px solid var(--c-glass-border)',
                        }}
                    >
                        <SlidersHorizontal className="w-3.5 h-3.5" />
                        Filters
                    </button>

                    <div className="relative flex-1">
                        <Search
                            className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none"
                            style={{ color: 'var(--c-mist-fg)' }}
                        />
                        <input
                            value={filters.query}
                            onChange={(e) => setFilters({ ...filters, query: e.target.value })}
                            placeholder="Search your connectors and everything you can add…"
                            aria-label="Search connectors"
                            className="w-full rounded-xl pl-9 pr-3 py-2 text-sm outline-none"
                            style={{
                                background: 'var(--c-glass)',
                                border: '1px solid var(--c-glass-border)',
                                color: 'var(--c-ink)',
                            }}
                        />
                    </div>

                    <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as SortKey)}
                        aria-label="Sort connectors"
                        className="rounded-xl px-2.5 py-2 text-xs outline-none cursor-pointer"
                        style={{
                            background: 'var(--c-glass)',
                            border: '1px solid var(--c-glass-border)',
                            color: 'var(--c-ink-2)',
                        }}
                    >
                        {SORT_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                                {option.label}
                            </option>
                        ))}
                    </select>
                </div>
            </header>

            <div
                className="flex-1 flex min-h-0"
                style={{ borderTop: '1px solid var(--c-glass-border)' }}
            >
                {showFilters && connections.length > 0 && (
                    <ConnectorFilterRail
                        connections={connections}
                        catalog={catalog}
                        filters={filters}
                        onChange={setFilters}
                        onClear={() => setFilters({ ...EMPTY_FILTERS, query: filters.query })}
                    />
                )}

                <main className="flex-1 min-w-0 overflow-y-auto px-6 py-5">
                    {loading ? (
                        <div className="flex items-center justify-center h-64">
                            <Loader2
                                className="w-5 h-5 animate-spin"
                                style={{ color: 'var(--c-mist-fg)' }}
                            />
                        </div>
                    ) : (
                        <div className="space-y-8">
                            <section>
                                <GroupHeading
                                    title="Your connectors"
                                    count={visible.length}
                                    total={connections.length}
                                />
                                {connections.length === 0 ? (
                                    <p
                                        className="text-xs rounded-xl border border-dashed px-4 py-6 text-center"
                                        style={{
                                            color: 'var(--c-mist-fg)',
                                            borderColor: 'var(--c-glass-border)',
                                        }}
                                    >
                                        Nothing configured yet. Pick a connector below to get
                                        started.
                                    </p>
                                ) : visible.length === 0 ? (
                                    <div
                                        className="text-xs rounded-xl border border-dashed px-4 py-6 text-center"
                                        style={{
                                            color: 'var(--c-mist-fg)',
                                            borderColor: 'var(--c-glass-border)',
                                        }}
                                    >
                                        No connectors match your search or filters.
                                        <button
                                            type="button"
                                            onClick={() => setFilters(EMPTY_FILTERS)}
                                            className="ml-2 font-semibold"
                                            style={{ color: 'var(--c-signal-text)' }}
                                        >
                                            Clear
                                        </button>
                                    </div>
                                ) : (
                                    <CardGrid>
                                        {visible.map((connection) => (
                                            <ConnectorCard
                                                key={connection.id}
                                                connection={connection}
                                                typeLabel={
                                                    entryFor(connection)?.label ??
                                                    connection.connector_id
                                                }
                                                selected={connection.id === selectedId}
                                                onSelect={() =>
                                                    setSelectedId(
                                                        connection.id === selectedId
                                                            ? null
                                                            : connection.id
                                                    )
                                                }
                                            />
                                        ))}
                                    </CardGrid>
                                )}
                            </section>

                            {availableGroups.length > 0 && (
                                <section>
                                    <GroupHeading title="Available" />
                                    <div className="space-y-6">
                                        {availableGroups.map(([category, entries]) => (
                                            <div key={category}>
                                                <SectionHeading
                                                    title={CATEGORY_LABELS[category] ?? category}
                                                />
                                                <CardGrid>
                                                    {entries.map((entry) => (
                                                        <AvailableConnectorCard
                                                            key={entry.id}
                                                            entry={entry}
                                                            onConnect={() =>
                                                                setConnecting(entry.id)
                                                            }
                                                        />
                                                    ))}
                                                </CardGrid>
                                            </div>
                                        ))}
                                    </div>
                                </section>
                            )}

                            {!facetsActive && term && availableGroups.length === 0 && (
                                <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                                    No available connectors match “{filters.query}”.
                                </p>
                            )}
                        </div>
                    )}
                </main>

                {selected && (
                    <ConnectorDetailDrawer
                        connection={selected}
                        entry={entryFor(selected)}
                        allTags={allTags}
                        onClose={() => setSelectedId(null)}
                        onChanged={() => load('refresh')}
                        onDelete={setPendingDelete}
                        onError={notifyError}
                        onSuccess={notifySuccess}
                    />
                )}
            </div>

            {pendingDelete && (
                <DeleteConnectionDialog
                    connection={pendingDelete}
                    onCancel={() => setPendingDelete(null)}
                    onDeleted={() => {
                        notifySuccess(`${pendingDelete.name} deleted`);
                        if (selectedId === pendingDelete.id) setSelectedId(null);
                        setPendingDelete(null);
                        load('refresh');
                    }}
                    onError={notifyError}
                />
            )}
        </div>
    );
}

/** Top-level grouping ("Your connectors" / "Available") — larger and brand-colored
 *  so the two groups read as distinct sections rather than blending together. */
function GroupHeading({ title, count, total }: { title: string; count?: number; total?: number }) {
    const showCount = count !== undefined && total !== undefined && total > 0;
    return (
        <div className="flex items-baseline gap-2 mb-4">
            <h2
                className="font-display text-base font-semibold"
                style={{ color: 'var(--c-signal-text)' }}
            >
                {title}
            </h2>
            {showCount && (
                <span className="text-xs tabular-nums" style={{ color: 'var(--c-mist-fg)' }}>
                    {count === total ? total : `${count} of ${total}`}
                </span>
            )}
        </div>
    );
}

function SectionHeading({
    title,
    count,
    total,
}: {
    title: string;
    count?: number;
    total?: number;
}) {
    const showCount = count !== undefined && total !== undefined && total > 0;
    return (
        <div className="flex items-baseline gap-2 mb-3">
            <h2
                className="text-[10px] uppercase tracking-widest font-semibold"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {title}
            </h2>
            {showCount && (
                <span className="text-[10px] tabular-nums" style={{ color: 'var(--c-mist-fg)' }}>
                    {count === total ? total : `${count} of ${total}`}
                </span>
            )}
        </div>
    );
}

function CardGrid({ children }: { children: React.ReactNode }) {
    return (
        <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(260px,1fr))] items-stretch">
            {children}
        </div>
    );
}

function errorMessage(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (err instanceof Error && err.message) return err.message;
    return fallback;
}
