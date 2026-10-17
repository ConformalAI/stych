import { useState } from 'react';
import { Loader2, CheckCircle2, AlertCircle, Layers } from 'lucide-react';
import {
    createSavedConnection,
    testMongoConnection,
    getMongoCollections,
    loadMongoCollection,
    patchSavedConnection,
    testSavedConnection,
    type MongoConnectionParams,
    type SavedConnection,
} from '../services/api';
import { useStore } from '../store/useStore';
import { setSessionId as persistSessionId } from '../services/storage';
import { KEEP_SECRET_PLACEHOLDER, hintString, savedEndpointLabel } from './savedConnectionHint';

type Step = 'connect' | 'pick-collection';

interface MongoConnectProps {
    onComplete?: () => void;
    libraryMode?: boolean;
    editing?: SavedConnection;
    onSaved?: (connection: SavedConnection) => void;
    onCancel?: () => void;
}

export default function MongoConnect({
    onComplete,
    libraryMode = false,
    editing,
    onSaved,
    onCancel,
}: MongoConnectProps) {
    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);

    const [customUri, setCustomUri] = useState('');
    const [database, setDatabase] = useState(hintString(editing?.hint, 'database'));
    const [connectionName, setConnectionName] = useState(editing?.name ?? '');
    const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
    const [testMessage, setTestMessage] = useState('');

    const [step, setStep] = useState<Step>('connect');
    const [connectionForUse, setConnectionForUse] = useState<MongoConnectionParams | null>(null);
    const [databaseForUse, setDatabaseForUse] = useState('admin');

    const [collections, setCollections] = useState<string[]>([]);
    const [collectionsLoading, setCollectionsLoading] = useState(false);
    const [loadLoading, setLoadLoading] = useState(false);
    const [saveLoading, setSaveLoading] = useState(false);

    const getConnection = (): MongoConnectionParams | null => {
        if (customUri.trim()) {
            return { connection_url: customUri.trim() };
        }
        return null;
    };

    const getDbName = () => database.trim() || 'admin';

    const handleTest = async () => {
        const conn = getConnection();
        if (!editing && !conn) return;
        setTestStatus('testing');
        setTestMessage('');
        try {
            const res = editing
                ? await testSavedConnection(editing.id, {
                      ...(conn ?? {}),
                      database: getDbName(),
                  })
                : await testMongoConnection(conn!, getDbName());
            setTestStatus(res.success ? 'success' : 'error');
            setTestMessage(res.message);
        } catch (e: unknown) {
            const msg =
                (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                'Test failed';
            setTestStatus('error');
            setTestMessage(msg);
        }
    };

    const handleUse = () => {
        const conn = getConnection();
        const db = getDbName();
        setConnectionForUse(conn);
        setDatabaseForUse(db);
        setStep('pick-collection');
        setCollections([]);
    };

    const handleSave = async () => {
        const conn = getConnection();
        if (!editing && !conn) return;
        setSaveLoading(true);
        try {
            const body = { ...(conn ?? {}), database: getDbName() };
            const saved = editing
                ? await patchSavedConnection(editing.id, {
                      name: connectionName.trim() || undefined,
                      connection: body,
                  })
                : await createSavedConnection({
                      connector_id: 'mongodb',
                      name: connectionName.trim() || undefined,
                      connection: body,
                  });
            onSaved?.(saved);
        } catch (e: unknown) {
            const msg =
                (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                'Failed to save connection';
            setError(msg);
        } finally {
            setSaveLoading(false);
        }
    };

    const fetchCollections = async () => {
        setCollectionsLoading(true);
        try {
            const res = await getMongoCollections(databaseForUse, connectionForUse);
            setCollections(res.collections ?? []);
        } catch (e: unknown) {
            const msg =
                (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                'Failed to list collections';
            setError(msg);
        } finally {
            setCollectionsLoading(false);
        }
    };

    const handleLoadCollection = async (collectionName: string) => {
        setLoadLoading(true);
        try {
            const res = await loadMongoCollection(
                databaseForUse,
                collectionName,
                sessionId,
                connectionForUse ?? undefined
            );
            setSessionId(res.session_id);
            persistSessionId(res.session_id);
            addUploadedFile({
                file_id: res.source_id,
                filename: `MongoDB: ${res.source_label}`,
                rows: res.rows,
                columns: res.columns,
            });
            notifySessionActivity(res.session_id);
            if (onComplete) onComplete();
        } catch (e: unknown) {
            const msg =
                (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                'Load failed';
            setError(msg);
        } finally {
            setLoadLoading(false);
        }
    };

    if (step === 'pick-collection') {
        return (
            <div className="space-y-5">
                <div className="flex items-center justify-between">
                    <h2 className="text-xl font-bold" style={{ color: 'var(--c-ink)' }}>
                        Select collection
                    </h2>
                    <button
                        type="button"
                        onClick={() => {
                            setStep('connect');
                            setConnectionForUse(null);
                        }}
                        className="text-sm transition-colors"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        Back
                    </button>
                </div>
                <div className="flex gap-2">
                    <div
                        className="flex-1 rounded-lg border px-3 py-2 text-sm"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink-2)',
                        }}
                    >
                        Database:{' '}
                        <span style={{ color: 'var(--c-ink)' }} className="font-medium">
                            {databaseForUse}
                        </span>
                    </div>
                    <button
                        type="button"
                        onClick={fetchCollections}
                        disabled={collectionsLoading}
                        className="rounded-lg px-4 py-2 text-sm font-medium text-white flex items-center gap-2 disabled:opacity-50"
                        style={{ background: 'var(--c-signal)' }}
                    >
                        {collectionsLoading ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Layers className="w-4 h-4" />
                        )}
                        List collections
                    </button>
                </div>
                <div
                    className="rounded-xl border max-h-64 overflow-auto"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    {collections.length === 0 && !collectionsLoading && (
                        <p className="p-4 text-sm" style={{ color: 'var(--c-mist-fg)' }}>
                            Click &quot;List collections&quot; to browse your database
                        </p>
                    )}
                    {collections.map((col) => (
                        <button
                            key={col}
                            type="button"
                            onClick={() => handleLoadCollection(col)}
                            disabled={loadLoading}
                            className="w-full text-left px-4 py-3 border-b last:border-b-0 flex items-center justify-between hover:bg-white/5 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                        >
                            <span>{col}</span>
                            {loadLoading ? (
                                <Loader2
                                    className="w-4 h-4 animate-spin"
                                    style={{ color: 'var(--c-signal)' }}
                                />
                            ) : null}
                        </button>
                    ))}
                </div>
            </div>
        );
    }

    return (
        <div className="space-y-6">
            <div>
                <h2 className="text-xl font-bold" style={{ color: 'var(--c-ink)' }}>
                    {editing ? 'Edit MongoDB connection' : 'Connect to MongoDB'}
                </h2>
                <p className="text-sm mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                    {editing
                        ? 'The saved URI is not shown. Leave it blank to keep it, or paste a new one to rotate.'
                        : 'Use a collection as your data source — documents are flattened into a table.'}
                </p>
                {libraryMode && onCancel && (
                    <button
                        type="button"
                        className="text-sm mt-2"
                        style={{ color: 'var(--c-mist-fg)' }}
                        onClick={onCancel}
                    >
                        Back
                    </button>
                )}
            </div>

            <div
                className="rounded-2xl border p-5 space-y-4"
                style={{ background: 'var(--c-glass)', borderColor: 'var(--c-glass-border)' }}
            >
                <div>
                    <p className="text-sm font-medium mb-1" style={{ color: 'var(--c-ink-2)' }}>
                        Connection URI
                    </p>
                    <p className="text-xs mb-2" style={{ color: 'var(--c-mist-fg)' }}>
                        Format:{' '}
                        <code style={{ color: 'var(--c-ink-2)' }}>
                            mongodb://user:password@host:27017/database
                        </code>
                    </p>
                    {editing && (
                        <p className="text-xs mb-2" style={{ color: 'var(--c-mist-fg)' }}>
                            Saved {savedEndpointLabel(editing.hint)}. {KEEP_SECRET_PLACEHOLDER}.
                        </p>
                    )}
                    <input
                        type="text"
                        value={customUri}
                        onChange={(e) => {
                            setCustomUri(e.target.value);
                            setTestStatus('idle');
                        }}
                        placeholder={
                            editing ? KEEP_SECRET_PLACEHOLDER : 'mongodb://localhost:27017'
                        }
                        className="input w-full text-sm"
                    />
                </div>

                <div>
                    <p className="text-sm font-medium mb-1" style={{ color: 'var(--c-ink-2)' }}>
                        Database name
                    </p>
                    <input
                        type="text"
                        value={database}
                        onChange={(e) => setDatabase(e.target.value)}
                        placeholder="admin"
                        className="input w-full text-sm"
                    />
                </div>

                {libraryMode && (
                    <input
                        value={connectionName}
                        onChange={(e) => setConnectionName(e.target.value)}
                        placeholder="Connection name (optional)"
                        className="input w-full text-sm"
                    />
                )}

                <div className="flex flex-wrap items-center gap-2 pt-1">
                    <button
                        type="button"
                        onClick={handleTest}
                        disabled={testStatus === 'testing' || (!customUri.trim() && !editing)}
                        className="rounded-lg border px-4 py-2 text-sm font-medium flex items-center gap-2 disabled:opacity-50 transition-colors hover:bg-white/5"
                        style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                    >
                        {testStatus === 'testing' ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : null}
                        Test connection
                    </button>
                    <button
                        type="button"
                        onClick={libraryMode ? () => void handleSave() : handleUse}
                        disabled={(!customUri.trim() && !editing) || saveLoading}
                        className="rounded-lg px-4 py-2 text-sm font-medium text-white disabled:opacity-50 transition-colors"
                        style={{ background: 'var(--c-signal)' }}
                    >
                        {libraryMode ? (editing ? 'Save changes' : 'Save connection') : 'Use'}
                    </button>
                    {testStatus === 'success' && (
                        <span
                            className="flex items-center gap-1 text-sm"
                            style={{ color: 'var(--c-signal-text)' }}
                        >
                            <CheckCircle2 className="w-4 h-4" /> Connected
                        </span>
                    )}
                    {testStatus === 'error' && (
                        <span
                            className="flex items-center gap-1 text-sm text-red-400"
                            title={testMessage}
                        >
                            <AlertCircle className="w-4 h-4" /> Failed
                        </span>
                    )}
                </div>
                {testStatus === 'error' && testMessage && (
                    <p className="text-xs text-red-400 break-words">{testMessage}</p>
                )}
            </div>
        </div>
    );
}
