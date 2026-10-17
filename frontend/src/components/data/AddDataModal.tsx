import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, Check, Database, Loader2, Plug, Search, Upload, X } from 'lucide-react';
import {
    attachConnectionTable,
    attachSavedTable,
    getConnectorCatalog,
    listRemoteTables,
    listSavedConnections,
    type ConnectorCatalogEntry,
    type DatasourceLoadResponse,
    type SavedConnection,
} from '../../services/api';
import { useStore } from '../../store/useStore';
import { setSessionId as persistSessionId } from '../../services/storage';
import FileUpload from '../FileUpload';
import {
    connectorIcon,
    connectorIconColors,
    endpointLabel,
    healthColors,
    healthOf,
    relativeTime,
} from '../connectors/connectorMeta';
import NamespacePicker from '../connectors/NamespacePicker';
import { initialNamespace } from '../connectors/namespaceUtils';

interface AddDataModalProps {
    onClose: () => void;
}

type Pane = 'upload' | 'connectors';

/**
 * The single way data enters a session: upload a file, or attach a table from a
 * connector that is already configured. Configuring connectors themselves is a
 * separate concern and lives on the Connectors page — this modal only consumes
 * them, which is why it has no credential forms.
 */
export default function AddDataModal({ onClose }: AddDataModalProps) {
    const [pane, setPane] = useState<Pane>('upload');
    const [connections, setConnections] = useState<SavedConnection[]>([]);
    const [catalog, setCatalog] = useState<ConnectorCatalogEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [active, setActive] = useState<SavedConnection | null>(null);
    const [query, setQuery] = useState('');

    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);

    useEffect(() => {
        let alive = true;
        Promise.all([listSavedConnections(), getConnectorCatalog()])
            .then(([rows, entries]) => {
                if (!alive) return;
                setConnections(rows);
                setCatalog(entries);
                // Land on whichever pane the user can actually act in.
                if (rows.length > 0) setPane('connectors');
            })
            .catch(() => alive && setConnections([]))
            .finally(() => alive && setLoading(false));
        return () => {
            alive = false;
        };
    }, []);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onClose]);

    const applyAttach = useCallback(
        (result: DatasourceLoadResponse, label: string) => {
            persistSessionId(result.session_id);
            setSessionId(result.session_id);
            addUploadedFile({
                file_id: result.source_id,
                filename: `${label}: ${result.source_label}`,
                rows: result.rows,
                columns: result.columns,
            });
            notifySessionActivity(result.session_id);
            onClose();
        },
        [setSessionId, addUploadedFile, notifySessionActivity, onClose]
    );

    const term = query.trim().toLowerCase();
    const filtered = useMemo(
        () =>
            connections.filter((c) =>
                term
                    ? [c.name, endpointLabel(c), ...c.tags].join(' ').toLowerCase().includes(term)
                    : true
            ),
        [connections, term]
    );

    const entryFor = (connection: SavedConnection) =>
        catalog.find((c) => c.id === connection.connector_id);

    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            style={{ background: 'color-mix(in srgb, #000 55%, transparent)' }}
            role="dialog"
            aria-modal="true"
            aria-label="Add data"
            onClick={(e) => e.target === e.currentTarget && onClose()}
        >
            <div
                className="w-full max-w-2xl h-[560px] max-h-[85vh] flex flex-col rounded-2xl border overflow-hidden"
                style={{
                    background: 'var(--c-slate)',
                    borderColor: 'var(--c-glass-border)',
                    boxShadow: '0 24px 64px rgba(0,0,0,0.5)',
                }}
            >
                <div
                    className="flex items-center gap-3 px-5 py-4 shrink-0"
                    style={{ borderBottom: '1px solid var(--c-glass-border)' }}
                >
                    {active && (
                        <button
                            type="button"
                            onClick={() => setActive(null)}
                            aria-label="Back to connections"
                            className="p-1.5 rounded-md transition-colors hover:bg-[var(--c-glass)]"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            <ArrowLeft className="w-4 h-4" />
                        </button>
                    )}
                    <div className="flex-1 min-w-0">
                        <h2
                            className="font-display font-semibold text-sm"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            {active ? active.name : 'Add data'}
                        </h2>
                        <p className="text-[11px] mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                            {active
                                ? endpointLabel(active)
                                : 'Bring a file or a connected table into this session.'}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close"
                        className="p-1.5 rounded-md transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {!active && (
                    <div
                        className="flex items-center gap-1 px-4 py-2 shrink-0"
                        style={{ borderBottom: '1px solid var(--c-glass-border)' }}
                        role="tablist"
                    >
                        {(
                            [
                                ['upload', 'Upload file', Upload],
                                ['connectors', 'From connector', Database],
                            ] as const
                        ).map(([id, label, Icon]) => (
                            <button
                                key={id}
                                role="tab"
                                aria-selected={pane === id}
                                onClick={() => setPane(id)}
                                className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors"
                                style={
                                    pane === id
                                        ? {
                                              background: 'var(--c-signal-dim)',
                                              color: 'var(--c-signal-text)',
                                          }
                                        : { color: 'var(--c-mist-fg)' }
                                }
                            >
                                <Icon className="w-3.5 h-3.5" />
                                {label}
                                {id === 'connectors' && connections.length > 0 && (
                                    <span
                                        className="text-[10px] tabular-nums"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        {connections.length}
                                    </span>
                                )}
                            </button>
                        ))}
                    </div>
                )}

                <div className="flex-1 overflow-y-auto p-5">
                    {active ? (
                        <TablePicker
                            connection={active}
                            entry={entryFor(active)}
                            sessionId={sessionId}
                            onAttached={applyAttach}
                            onError={(m) => setError(m)}
                            label={entryFor(active)?.label ?? active.connector_id}
                        />
                    ) : pane === 'upload' ? (
                        <FileUpload onComplete={onClose} />
                    ) : loading ? (
                        <div className="flex items-center justify-center h-40">
                            <Loader2
                                className="w-5 h-5 animate-spin"
                                style={{ color: 'var(--c-mist-fg)' }}
                            />
                        </div>
                    ) : connections.length === 0 ? (
                        <NoConnectors />
                    ) : (
                        <>
                            <div className="relative mb-3">
                                <Search
                                    className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                />
                                <input
                                    value={query}
                                    onChange={(e) => setQuery(e.target.value)}
                                    placeholder="Search connections…"
                                    aria-label="Search connections"
                                    className="w-full rounded-xl pl-9 pr-3 py-2 text-sm outline-none"
                                    style={{
                                        background: 'var(--c-glass)',
                                        border: '1px solid var(--c-glass-border)',
                                        color: 'var(--c-ink)',
                                    }}
                                />
                            </div>

                            <ul className="space-y-1.5">
                                {filtered.map((connection) => {
                                    const Icon = connectorIcon(connection.connector_id);
                                    const iconColors = connectorIconColors(connection.connector_id);
                                    const colors = healthColors(healthOf(connection));
                                    return (
                                        <li key={connection.id}>
                                            <button
                                                type="button"
                                                onClick={() => setActive(connection)}
                                                className="w-full flex items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors hover:bg-[var(--c-glass)]"
                                                style={{
                                                    borderColor: 'var(--c-glass-border)',
                                                    background: 'var(--c-glass)',
                                                }}
                                            >
                                                <span
                                                    aria-hidden="true"
                                                    className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
                                                    style={{
                                                        background: iconColors.bg,
                                                        border: `1px solid ${iconColors.border}`,
                                                    }}
                                                >
                                                    <Icon
                                                        className="w-4 h-4"
                                                        style={{ color: iconColors.fg }}
                                                    />
                                                </span>
                                                <span className="min-w-0 flex-1">
                                                    <span className="flex items-center gap-1.5">
                                                        <span
                                                            aria-hidden="true"
                                                            className="w-1.5 h-1.5 rounded-full shrink-0"
                                                            style={{ background: colors.fg }}
                                                        />
                                                        <span
                                                            className="text-xs font-semibold truncate"
                                                            style={{ color: 'var(--c-ink)' }}
                                                        >
                                                            {connection.name}
                                                        </span>
                                                    </span>
                                                    <span
                                                        className="block text-[10px] font-mono truncate mt-0.5"
                                                        style={{ color: 'var(--c-mist-fg)' }}
                                                    >
                                                        {endpointLabel(connection)}
                                                    </span>
                                                </span>
                                                <span
                                                    className="text-[10px] shrink-0"
                                                    style={{ color: 'var(--c-mist-fg)' }}
                                                >
                                                    {connection.tables.length > 0
                                                        ? `${connection.tables.length} pinned`
                                                        : relativeTime(connection.last_used_at)}
                                                </span>
                                            </button>
                                        </li>
                                    );
                                })}
                            </ul>

                            <Link
                                to="/connectors"
                                onClick={onClose}
                                className="inline-flex items-center gap-1.5 mt-4 text-xs font-semibold"
                                style={{ color: 'var(--c-signal-text)' }}
                            >
                                <Plug className="w-3.5 h-3.5" />
                                Manage connectors
                            </Link>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}

function NoConnectors() {
    return (
        <div className="flex flex-col items-center justify-center text-center py-12">
            <span
                aria-hidden="true"
                className="w-12 h-12 rounded-2xl flex items-center justify-center mb-4"
                style={{ background: 'var(--c-signal-dim)' }}
            >
                <Plug className="w-5 h-5" style={{ color: 'var(--c-signal-text)' }} />
            </span>
            <h3 className="font-display font-semibold text-sm" style={{ color: 'var(--c-ink)' }}>
                No connectors configured
            </h3>
            <p className="text-xs mt-1.5 max-w-xs" style={{ color: 'var(--c-mist-fg)' }}>
                Set up a database or warehouse once, then attach its tables to any session.
            </p>
            <Link
                to="/connectors"
                className="mt-5 inline-flex items-center gap-1.5 rounded-xl px-4 py-2 text-xs font-semibold"
                style={{
                    background: 'var(--c-signal)',
                    color: '#fff',
                    boxShadow: '0 4px 16px var(--c-signal-glow)',
                }}
            >
                <Plug className="w-3.5 h-3.5" />
                Go to Connectors
            </Link>
        </div>
    );
}

function TablePicker({
    connection,
    entry,
    sessionId,
    label,
    onAttached,
    onError,
}: {
    connection: SavedConnection;
    entry?: ConnectorCatalogEntry;
    sessionId: string | null;
    label: string;
    onAttached: (result: DatasourceLoadResponse, label: string) => void;
    onError: (message: string) => void;
}) {
    const noun = entry?.relation_noun ?? 'table';
    const schemaLabel = entry?.schema_label ?? 'Schema';
    const [schema, setSchema] = useState(() => initialNamespace(connection, entry));
    const [remote, setRemote] = useState<Array<{ schema: string; table: string }> | null>(null);
    const [listing, setListing] = useState(false);
    const [busy, setBusy] = useState<string | null>(null);

    const browse = async () => {
        setListing(true);
        setRemote(null);
        try {
            const result = await listRemoteTables(connection.id, schema);
            setRemote(
                result.tables?.length > 0
                    ? result.tables
                    : (result.collections ?? []).map((name) => ({ schema, table: name }))
            );
        } catch (err) {
            onError(errorMessage(err, `Could not list ${noun}s`));
        } finally {
            setListing(false);
        }
    };

    const attachPin = async (tableId: string) => {
        setBusy(tableId);
        try {
            onAttached(await attachSavedTable(connection.id, tableId, sessionId), label);
        } catch (err) {
            onError(errorMessage(err, `Could not add ${noun} to this session`));
        } finally {
            setBusy(null);
        }
    };

    const attachRemote = async (schemaName: string, tableName: string) => {
        setBusy(`${schemaName}.${tableName}`);
        try {
            onAttached(
                await attachConnectionTable(connection.id, {
                    sessionId,
                    schema_name: schemaName,
                    table_name: tableName,
                }),
                label
            );
        } catch (err) {
            onError(errorMessage(err, `Could not add ${noun} to this session`));
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="space-y-5">
            {connection.tables.length > 0 && (
                <section>
                    <h4
                        className="text-[10px] uppercase tracking-widest font-semibold mb-2"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        Pinned {noun}s
                    </h4>
                    <ul className="space-y-1">
                        {connection.tables.map((table) => (
                            <li key={table.id}>
                                <button
                                    type="button"
                                    disabled={busy === table.id}
                                    onClick={() => attachPin(table.id)}
                                    className="w-full flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-left transition-colors hover:bg-[var(--c-glass)] disabled:opacity-50"
                                    style={{
                                        background: 'var(--c-glass)',
                                        border: '1px solid var(--c-glass-border)',
                                    }}
                                >
                                    <span
                                        className="text-xs font-medium truncate"
                                        style={{ color: 'var(--c-ink)' }}
                                    >
                                        {table.display_name}
                                    </span>
                                    {busy === table.id ? (
                                        <Loader2
                                            className="w-3.5 h-3.5 animate-spin shrink-0"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        />
                                    ) : (
                                        <Check
                                            className="w-3.5 h-3.5 shrink-0"
                                            style={{ color: 'var(--c-signal-text)' }}
                                        />
                                    )}
                                </button>
                            </li>
                        ))}
                    </ul>
                </section>
            )}

            <section>
                <h4
                    className="text-[10px] uppercase tracking-widest font-semibold mb-2"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Browse {noun}s
                </h4>
                <div className="flex items-start gap-2">
                    <NamespacePicker
                        connectionId={connection.id}
                        label={schemaLabel}
                        value={schema}
                        onChange={setSchema}
                        onSubmit={browse}
                    />
                    <button
                        type="button"
                        onClick={browse}
                        disabled={listing}
                        className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-50"
                        style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
                    >
                        {listing ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                            <Search className="w-3.5 h-3.5" />
                        )}
                        List
                    </button>
                </div>

                {remote && remote.length === 0 && (
                    <p className="text-xs mt-3" style={{ color: 'var(--c-mist-fg)' }}>
                        No {noun}s found in “{schema || schemaLabel}”.
                    </p>
                )}

                {remote && remote.length > 0 && (
                    <ul className="mt-3 space-y-1 max-h-56 overflow-y-auto pr-1">
                        {remote.map(({ schema: s, table }) => {
                            const key = `${s}.${table}`;
                            return (
                                <li key={key}>
                                    <button
                                        type="button"
                                        disabled={busy === key}
                                        onClick={() => attachRemote(s, table)}
                                        className="w-full flex items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left transition-colors hover:bg-[var(--c-glass)] disabled:opacity-50"
                                        style={{ background: 'var(--c-glass)' }}
                                    >
                                        <span
                                            className="text-xs font-mono truncate"
                                            style={{ color: 'var(--c-ink-2)' }}
                                        >
                                            {table}
                                        </span>
                                        {busy === key && (
                                            <Loader2
                                                className="w-3.5 h-3.5 animate-spin shrink-0"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            />
                                        )}
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                )}
            </section>
        </div>
    );
}

function errorMessage(err: unknown, fallback: string): string {
    const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (err instanceof Error && err.message) return err.message;
    return fallback;
}
