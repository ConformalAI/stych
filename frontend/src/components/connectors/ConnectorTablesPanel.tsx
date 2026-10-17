import { useState } from 'react';
import { Loader2, Pin, PinOff, Search } from 'lucide-react';
import {
    listRemoteTables,
    pinSavedTable,
    unpinSavedTable,
    type ConnectorCatalogEntry,
    type SavedConnection,
} from '../../services/api';
import NamespacePicker from './NamespacePicker';
import { initialNamespace } from './namespaceUtils';

interface ConnectorTablesPanelProps {
    connection: SavedConnection;
    entry?: ConnectorCatalogEntry;
    onChanged: () => void;
    onError: (message: string) => void;
}

/**
 * Pinned-table management for one connection. Pins are bookmarks, not
 * attachments — loading a table into a session happens from the agent
 * workspace's data picker, which is where session context exists.
 */
export default function ConnectorTablesPanel({
    connection,
    entry,
    onChanged,
    onError,
}: ConnectorTablesPanelProps) {
    const schemaLabel = entry?.schema_label ?? 'Schema';
    const noun = entry?.relation_noun ?? 'table';
    const [schema, setSchema] = useState(() => initialNamespace(connection, entry));
    const [remote, setRemote] = useState<Array<{ schema: string; table: string }> | null>(null);
    const [loading, setLoading] = useState(false);
    const [busy, setBusy] = useState<string | null>(null);

    const pinnedKeys = new Set(
        connection.tables.map((t) => `${t.schema_name ?? ''}.${t.table_name ?? ''}`)
    );

    const browse = async () => {
        setLoading(true);
        setRemote(null);
        try {
            const result = await listRemoteTables(connection.id, schema);
            const tables =
                result.tables?.length > 0
                    ? result.tables
                    : (result.collections ?? []).map((name) => ({ schema, table: name }));
            setRemote(tables);
        } catch (err) {
            onError(errorMessage(err, `Could not list ${noun}s`));
        } finally {
            setLoading(false);
        }
    };

    const pin = async (schemaName: string, tableName: string) => {
        setBusy(`${schemaName}.${tableName}`);
        try {
            await pinSavedTable(connection.id, {
                schema_name: schemaName,
                table_name: tableName,
                display_name: tableName,
            });
            onChanged();
        } catch (err) {
            onError(errorMessage(err, 'Could not pin'));
        } finally {
            setBusy(null);
        }
    };

    const unpin = async (tableId: string) => {
        setBusy(tableId);
        try {
            await unpinSavedTable(connection.id, tableId);
            onChanged();
        } catch (err) {
            onError(errorMessage(err, 'Could not unpin'));
        } finally {
            setBusy(null);
        }
    };

    return (
        <div className="space-y-5">
            <section>
                <h4
                    className="text-[10px] uppercase tracking-widest font-semibold mb-2"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Pinned {noun}s
                </h4>
                {connection.tables.length === 0 ? (
                    <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        No pins yet. Pinned {noun}s appear first when adding data to a session.
                    </p>
                ) : (
                    <ul className="space-y-1">
                        {connection.tables.map((table) => (
                            <li
                                key={table.id}
                                className="flex items-center justify-between gap-2 rounded-lg px-2.5 py-2"
                                style={{
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                }}
                            >
                                <span className="min-w-0">
                                    <span
                                        className="block text-xs font-medium truncate"
                                        style={{ color: 'var(--c-ink)' }}
                                    >
                                        {table.display_name}
                                    </span>
                                    {table.schema_name && (
                                        <span
                                            className="block text-[10px] font-mono truncate"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {table.schema_name}.{table.table_name}
                                        </span>
                                    )}
                                </span>
                                <button
                                    type="button"
                                    disabled={busy === table.id}
                                    onClick={() => unpin(table.id)}
                                    title="Unpin"
                                    className="p-1.5 rounded-md transition-colors hover:bg-[var(--c-glass)] disabled:opacity-50"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    <PinOff className="w-3.5 h-3.5" />
                                </button>
                            </li>
                        ))}
                    </ul>
                )}
            </section>

            <section>
                <h4
                    className="text-[10px] uppercase tracking-widest font-semibold mb-2"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    Browse
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
                        disabled={loading}
                        className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-50"
                        style={{
                            background: 'var(--c-signal-dim)',
                            color: 'var(--c-signal-text)',
                        }}
                    >
                        {loading ? (
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
                    <ul className="mt-3 space-y-1 max-h-64 overflow-y-auto pr-1">
                        {remote.map(({ schema: s, table }) => {
                            const key = `${s}.${table}`;
                            const isPinned = pinnedKeys.has(key);
                            return (
                                <li
                                    key={key}
                                    className="flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5"
                                    style={{ background: 'var(--c-glass)' }}
                                >
                                    <span
                                        className="text-xs font-mono truncate"
                                        style={{ color: 'var(--c-ink-2)' }}
                                    >
                                        {table}
                                    </span>
                                    <button
                                        type="button"
                                        disabled={isPinned || busy === key}
                                        onClick={() => pin(s, table)}
                                        title={isPinned ? 'Already pinned' : 'Pin'}
                                        className="p-1.5 rounded-md transition-colors hover:bg-[var(--c-glass)] disabled:opacity-40"
                                        style={{
                                            color: isPinned
                                                ? 'var(--c-signal-text)'
                                                : 'var(--c-mist-fg)',
                                        }}
                                    >
                                        <Pin className="w-3.5 h-3.5" />
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
