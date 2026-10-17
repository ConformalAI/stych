import { useEffect, useState } from 'react';
import { AlertTriangle, Check, Loader2, Trash2, X, Zap } from 'lucide-react';
import {
    patchSavedConnection,
    testSavedConnection,
    type ConnectorCatalogEntry,
    type SavedConnection,
} from '../../services/api';
import {
    CATEGORY_LABELS,
    HEALTH_LABELS,
    VENDOR_LABELS,
    connectorIcon,
    connectorIconColors,
    endpointLabel,
    healthColors,
    healthOf,
    relativeTime,
} from './connectorMeta';
import ConnectorTablesPanel from './ConnectorTablesPanel';
import TagEditor from './TagEditor';

type DrawerTab = 'overview' | 'tables' | 'settings';

interface ConnectorDetailDrawerProps {
    connection: SavedConnection;
    entry?: ConnectorCatalogEntry;
    allTags: string[];
    onClose: () => void;
    onChanged: () => void;
    onDelete: (connection: SavedConnection) => void;
    onError: (message: string) => void;
    onSuccess: (message: string) => void;
}

export default function ConnectorDetailDrawer({
    connection,
    entry,
    allTags,
    onClose,
    onChanged,
    onDelete,
    onError,
    onSuccess,
}: ConnectorDetailDrawerProps) {
    const [tab, setTab] = useState<DrawerTab>('overview');
    const [testing, setTesting] = useState(false);
    const [name, setName] = useState(connection.name);
    const [description, setDescription] = useState(connection.description ?? '');
    const [tags, setTags] = useState<string[]>(connection.tags);
    const [saving, setSaving] = useState(false);

    // Re-seed the editable fields whenever a different connection is opened.
    useEffect(() => {
        setName(connection.name);
        setDescription(connection.description ?? '');
        setTags(connection.tags);
        setTab('overview');
    }, [connection.id, connection.name, connection.description, connection.tags]);

    useEffect(() => {
        const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onClose]);

    const Icon = connectorIcon(connection.connector_id);
    const iconColors = connectorIconColors(connection.connector_id);
    const health = healthOf(connection);
    const colors = healthColors(health);
    const dirty =
        name !== connection.name ||
        description !== (connection.description ?? '') ||
        tags.join(',') !== connection.tags.join(',');
    const joinCapability = entry?.same_source_join;
    const exactJoin = joinCapability?.strategies.find((item) => item.strategy === 'exact');
    const lexicalJoins =
        joinCapability?.strategies.filter((item) => item.strategy !== 'exact') ?? [];

    const runTest = async () => {
        setTesting(true);
        try {
            await testSavedConnection(connection.id);
            onSuccess('Connection is healthy');
        } catch (err) {
            onError(errorMessage(err, 'Connection test failed'));
        } finally {
            setTesting(false);
            onChanged();
        }
    };

    const save = async () => {
        setSaving(true);
        try {
            await patchSavedConnection(connection.id, { name, description, tags });
            onSuccess('Changes saved');
            onChanged();
        } catch (err) {
            onError(errorMessage(err, 'Could not save changes'));
        } finally {
            setSaving(false);
        }
    };

    return (
        <aside
            role="complementary"
            aria-label={`${connection.name} details`}
            className="w-[380px] shrink-0 flex flex-col overflow-hidden"
            style={{
                background: 'var(--c-slate)',
                borderLeft: '1px solid var(--c-glass-border)',
            }}
        >
            <div className="p-4" style={{ borderBottom: '1px solid var(--c-glass-border)' }}>
                <div className="flex items-start gap-3">
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
                    <div className="min-w-0 flex-1">
                        <h2
                            className="font-display font-semibold text-sm truncate"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            {connection.name}
                        </h2>
                        <p
                            className="text-[11px] font-mono truncate mt-0.5"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            {endpointLabel(connection)}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close details"
                        className="p-1.5 rounded-md transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <div className="flex items-center gap-2 mt-3">
                    <span
                        className="inline-flex items-center gap-1.5 text-[11px] font-semibold rounded-md px-2 py-1 border"
                        style={{
                            color: colors.fg,
                            background: colors.bg,
                            borderColor: colors.border,
                        }}
                    >
                        <span
                            aria-hidden="true"
                            className="w-1.5 h-1.5 rounded-full"
                            style={{ background: colors.fg }}
                        />
                        {HEALTH_LABELS[health]}
                    </span>
                    <button
                        type="button"
                        onClick={runTest}
                        disabled={testing}
                        className="inline-flex items-center gap-1.5 text-[11px] font-semibold rounded-md px-2 py-1 transition-colors disabled:opacity-50 hover:bg-[var(--c-glass)]"
                        style={{
                            color: 'var(--c-ink-2)',
                            border: '1px solid var(--c-glass-border)',
                        }}
                    >
                        {testing ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                            <Zap className="w-3 h-3" />
                        )}
                        Test
                    </button>
                </div>
            </div>

            <div
                className="flex items-center gap-1 px-3 py-2"
                style={{ borderBottom: '1px solid var(--c-glass-border)' }}
                role="tablist"
            >
                {(
                    [
                        ['overview', 'Overview'],
                        ['tables', 'Tables'],
                        ['settings', 'Settings'],
                    ] as const
                ).map(([id, label]) => (
                    <button
                        key={id}
                        role="tab"
                        aria-selected={tab === id}
                        onClick={() => setTab(id)}
                        className="rounded-md px-2.5 py-1 text-xs font-medium transition-colors"
                        style={
                            tab === id
                                ? {
                                      background: 'var(--c-signal-dim)',
                                      color: 'var(--c-signal-text)',
                                  }
                                : { color: 'var(--c-mist-fg)' }
                        }
                    >
                        {label}
                    </button>
                ))}
            </div>

            <div className="flex-1 overflow-y-auto p-4">
                {tab === 'overview' && (
                    <div className="space-y-4">
                        {health === 'error' && connection.last_test_error && (
                            <div
                                className="rounded-lg p-3 flex gap-2"
                                style={{
                                    background: colors.bg,
                                    border: `1px solid ${colors.border}`,
                                }}
                            >
                                <AlertTriangle
                                    className="w-3.5 h-3.5 shrink-0 mt-0.5"
                                    style={{ color: colors.fg }}
                                />
                                <p className="text-[11px]" style={{ color: 'var(--c-ink-2)' }}>
                                    {connection.last_test_error}
                                </p>
                            </div>
                        )}

                        {connection.description && (
                            <p className="text-xs" style={{ color: 'var(--c-ink-2)' }}>
                                {connection.description}
                            </p>
                        )}

                        <dl className="space-y-2.5">
                            <Row label="Type" value={entry?.label ?? connection.connector_id} />
                            <Row
                                label="Provider"
                                value={VENDOR_LABELS[entry?.vendor ?? ''] ?? '—'}
                            />
                            <Row
                                label="Category"
                                value={CATEGORY_LABELS[entry?.category ?? ''] ?? '—'}
                            />
                            {entry?.summary && <Row label="Analysis" value={entry.summary} />}
                            <Row label="Pinned tables" value={String(connection.tables.length)} />
                            <Row label="Last used" value={relativeTime(connection.last_used_at)} />
                            <Row
                                label="Last tested"
                                value={relativeTime(connection.last_tested_at)}
                            />
                            <Row label="Created" value={relativeTime(connection.created_at)} />
                        </dl>

                        {joinCapability && exactJoin && (
                            <div
                                className="rounded-lg p-3 space-y-2"
                                style={{
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                }}
                            >
                                <p
                                    className="text-[10px] uppercase tracking-widest font-semibold"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Same-source joins
                                </p>
                                <p className="text-[11px]" style={{ color: 'var(--c-ink-2)' }}>
                                    Exact ·{' '}
                                    {exactJoin.executor === 'source' ? 'runs in source' : 'local'} ·{' '}
                                    {exactJoin.scope}/{exactJoin.freshness}
                                </p>
                                {lexicalJoins.length > 0 && (
                                    <p
                                        className="text-[11px] leading-relaxed"
                                        style={{ color: 'var(--c-ink-2)' }}
                                        title={lexicalJoins.map((item) => item.strategy).join(', ')}
                                    >
                                        Lexical · full-input stream ·{' '}
                                        {lexicalJoins.map((item) => item.strategy).join(', ')}
                                    </p>
                                )}
                                <p className="text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                                    {joinCapability.join_types.join(' · ')}
                                </p>
                            </div>
                        )}

                        {connection.tags.length > 0 && (
                            <div>
                                <p
                                    className="text-[10px] uppercase tracking-widest font-semibold mb-1.5"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Tags
                                </p>
                                <div className="flex flex-wrap gap-1">
                                    {connection.tags.map((tag) => (
                                        <span
                                            key={tag}
                                            className="text-[10px] font-medium rounded px-1.5 py-0.5 border"
                                            style={{
                                                color: 'var(--c-ink-2)',
                                                background: 'var(--c-glass)',
                                                borderColor: 'var(--c-glass-border)',
                                            }}
                                        >
                                            {tag}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                    </div>
                )}

                {tab === 'tables' && (
                    <ConnectorTablesPanel
                        connection={connection}
                        entry={entry}
                        onChanged={onChanged}
                        onError={onError}
                    />
                )}

                {tab === 'settings' && (
                    <div className="space-y-4">
                        <Field label="Name">
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                className="w-full rounded-xl px-3 py-2 text-xs outline-none"
                                style={{
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                    color: 'var(--c-ink)',
                                }}
                            />
                        </Field>

                        <Field label="Description">
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                rows={2}
                                placeholder="What lives in this connection?"
                                className="w-full rounded-xl px-3 py-2 text-xs outline-none resize-none"
                                style={{
                                    background: 'var(--c-glass)',
                                    border: '1px solid var(--c-glass-border)',
                                    color: 'var(--c-ink)',
                                }}
                            />
                        </Field>

                        <Field label="Tags">
                            <TagEditor tags={tags} suggestions={allTags} onChange={setTags} />
                        </Field>

                        <button
                            type="button"
                            onClick={save}
                            disabled={!dirty || saving}
                            className="w-full inline-flex items-center justify-center gap-1.5 rounded-xl px-3 py-2 text-xs font-semibold transition-all disabled:opacity-50"
                            style={{ background: 'var(--c-signal)', color: '#fff' }}
                        >
                            {saving ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                                <Check className="w-3.5 h-3.5" />
                            )}
                            Save changes
                        </button>

                        <div
                            className="pt-4"
                            style={{ borderTop: '1px solid var(--c-glass-border)' }}
                        >
                            <p
                                className="text-[10px] uppercase tracking-widest font-semibold mb-2"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                Danger zone
                            </p>
                            <button
                                type="button"
                                onClick={() => onDelete(connection)}
                                className="btn-danger w-full px-3 py-2 text-xs"
                            >
                                <Trash2 className="w-3.5 h-3.5" />
                                Delete connection
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </aside>
    );
}

function Row({ label, value }: { label: string; value: string }) {
    return (
        <div className="flex items-baseline justify-between gap-3">
            <dt className="text-[11px] shrink-0" style={{ color: 'var(--c-mist-fg)' }}>
                {label}
            </dt>
            <dd className="text-[11px] text-right truncate" style={{ color: 'var(--c-ink-2)' }}>
                {value}
            </dd>
        </div>
    );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div>
            <label
                className="block text-[10px] uppercase tracking-widest font-semibold mb-1.5"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {label}
            </label>
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
