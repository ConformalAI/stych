import { useState } from 'react';
import { Loader2, CheckCircle2, AlertCircle, Table2 } from 'lucide-react';
import {
    createSavedConnection,
    testSqlServerConnection,
    getSqlServerTables,
    loadSqlServerTable,
    patchSavedConnection,
    testSavedConnection,
    type SavedConnection,
    type SqlServerConnectionParams,
} from '../services/api';
import { useStore } from '../store/useStore';
import { setSessionId as persistSessionId } from '../services/storage';
import { KEEP_SECRET_PLACEHOLDER, savedEndpointLabel } from './savedConnectionHint';

type Step = 'connect' | 'pick-table';

interface SqlServerConnectProps {
    onComplete?: () => void;
    libraryMode?: boolean;
    editing?: SavedConnection;
    onSaved?: (connection: SavedConnection) => void;
    onCancel?: () => void;
}

export default function SqlServerConnect({
    onComplete,
    libraryMode = false,
    editing,
    onSaved,
    onCancel,
}: SqlServerConnectProps) {
    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);

    const [customUri, setCustomUri] = useState('');
    const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
    const [testMessage, setTestMessage] = useState('');

    const [step, setStep] = useState<Step>('connect');
    const [connectionForUse, setConnectionForUse] = useState<SqlServerConnectionParams | null>(
        null
    );

    const [schema, setSchema] = useState('dbo');
    const [tables, setTables] = useState<Array<{ schema: string; table: string }>>([]);
    const [tablesLoading, setTablesLoading] = useState(false);
    const [loadLoading, setLoadLoading] = useState(false);
    const [connectionName, setConnectionName] = useState(editing?.name ?? '');
    const [saveLoading, setSaveLoading] = useState(false);

    const getConnection = (): SqlServerConnectionParams | null => {
        if (customUri.trim()) {
            return { connection_url: customUri.trim() };
        }
        return null;
    };

    const handleTest = async () => {
        const conn = getConnection();
        if (!editing && !conn) return;
        setTestStatus('testing');
        setTestMessage('');
        try {
            const res = editing
                ? await testSavedConnection(editing.id, conn ?? {})
                : await testSqlServerConnection(conn!);
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
        setConnectionForUse(conn);
        setStep('pick-table');
        setTables([]);
    };

    const handleSave = async () => {
        const conn = getConnection();
        if (!editing && !conn) return;
        setSaveLoading(true);
        try {
            const saved = editing
                ? await patchSavedConnection(editing.id, {
                      name: connectionName.trim() || undefined,
                      connection: conn ?? {},
                  })
                : await createSavedConnection({
                      connector_id: 'sqlserver',
                      name: connectionName.trim() || undefined,
                      connection: conn ?? {},
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

    const fetchTables = async () => {
        setTablesLoading(true);
        try {
            const res = await getSqlServerTables(schema, connectionForUse);
            setTables(res.tables ?? []);
        } catch (e: unknown) {
            const msg =
                (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
                'Failed to list tables';
            setError(msg);
        } finally {
            setTablesLoading(false);
        }
    };

    const handleLoadTable = async (tableName: string) => {
        setLoadLoading(true);
        try {
            const res = await loadSqlServerTable(
                schema,
                tableName,
                sessionId,
                connectionForUse ?? undefined
            );
            setSessionId(res.session_id);
            persistSessionId(res.session_id);
            addUploadedFile({
                file_id: res.source_id,
                filename: `SQL Server: ${res.source_label}`,
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

    if (step === 'pick-table') {
        return (
            <div className="space-y-5">
                <div className="flex items-center justify-between">
                    <h2 className="text-xl font-bold" style={{ color: 'var(--c-ink)' }}>
                        Select table
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
                    <input
                        type="text"
                        value={schema}
                        onChange={(e) => setSchema(e.target.value)}
                        placeholder="Schema (e.g. dbo)"
                        className="input flex-1 text-sm"
                    />
                    <button
                        type="button"
                        onClick={fetchTables}
                        disabled={tablesLoading}
                        className="rounded-lg px-4 py-2 text-sm font-medium text-white flex items-center gap-2 disabled:opacity-50"
                        style={{ background: 'var(--c-signal)' }}
                    >
                        {tablesLoading ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                            <Table2 className="w-4 h-4" />
                        )}
                        Load tables
                    </button>
                </div>
                <div
                    className="rounded-xl border max-h-64 overflow-auto"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    {tables.length === 0 && !tablesLoading && (
                        <p className="p-4 text-sm" style={{ color: 'var(--c-mist-fg)' }}>
                            Enter schema and click &quot;Load tables&quot;
                        </p>
                    )}
                    {tables.map(({ table: t }) => (
                        <button
                            key={t}
                            type="button"
                            onClick={() => handleLoadTable(t)}
                            disabled={loadLoading}
                            className="w-full text-left px-4 py-3 border-b last:border-b-0 flex items-center justify-between hover:bg-white/5 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                        >
                            <span>{t}</span>
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
                    {editing ? 'Edit SQL Server connection' : 'Connect to SQL Server'}
                </h2>
                <p className="text-sm mt-0.5" style={{ color: 'var(--c-mist-fg)' }}>
                    {editing
                        ? 'The saved URI is not shown. Leave it blank to keep it, or paste a new one to rotate.'
                        : 'Use a table as your data source — then ask questions like with a CSV.'}
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
                className="rounded-2xl border p-5"
                style={{ background: 'var(--c-glass)', borderColor: 'var(--c-glass-border)' }}
            >
                <p className="text-sm font-medium mb-1" style={{ color: 'var(--c-ink-2)' }}>
                    Connection URL
                </p>
                <p className="text-xs mb-3" style={{ color: 'var(--c-mist-fg)' }}>
                    Format:{' '}
                    <code style={{ color: 'var(--c-ink-2)' }}>
                        mssql+pymssql://user:password@host:1433/database
                    </code>
                </p>
                {editing && (
                    <p className="text-xs mb-3" style={{ color: 'var(--c-mist-fg)' }}>
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
                        editing
                            ? KEEP_SECRET_PLACEHOLDER
                            : 'mssql+pymssql://sa:password@localhost:1433/MyDB'
                    }
                    className="input w-full text-sm mb-3"
                />
                {libraryMode && (
                    <input
                        value={connectionName}
                        onChange={(e) => setConnectionName(e.target.value)}
                        placeholder="Connection name (optional)"
                        className="input w-full text-sm mb-3"
                    />
                )}
                <div className="flex flex-wrap items-center gap-2">
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
                    <p className="text-xs mt-2 text-red-400 break-words">{testMessage}</p>
                )}
            </div>
        </div>
    );
}
