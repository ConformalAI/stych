import { useState, useEffect } from 'react';
import { Loader2, CheckCircle2, AlertCircle, Table2 } from 'lucide-react';
import {
    createSavedConnection,
    getPostgresStatus,
    testPostgresConnection,
    getPostgresTables,
    loadPostgresTable,
    patchSavedConnection,
    testSavedConnection,
    type PostgresConnectionParams,
    type SavedConnection,
} from '../services/api';
import { useStore } from '../store/useStore';
import { setSessionId as persistSessionId } from '../services/storage';
import { KEEP_SECRET_PLACEHOLDER, savedEndpointLabel } from './savedConnectionHint';

type Step = 'connect' | 'pick-table';

interface PostgresConnectProps {
    onComplete?: () => void;
    libraryMode?: boolean;
    editing?: SavedConnection;
    onSaved?: (connection: SavedConnection) => void;
    onCancel?: () => void;
}

export default function PostgresConnect({
    onComplete,
    libraryMode = false,
    editing,
    onSaved,
    onCancel,
}: PostgresConnectProps) {
    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const chatTheme = useStore((s) => s.chatTheme);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);
    const isDark = chatTheme === 'dark';

    const [defaultAvailable, setDefaultAvailable] = useState(false);
    const [loadingStatus, setLoadingStatus] = useState(true);
    const [customUri, setCustomUri] = useState('');
    const [useCustom, setUseCustom] = useState(() => Boolean(editing && !editing.hint.use_default));
    const [connectionName, setConnectionName] = useState(editing?.name ?? '');

    const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
    const [testMessage, setTestMessage] = useState('');

    const [step, setStep] = useState<Step>('connect');
    const [connectionForUse, setConnectionForUse] = useState<PostgresConnectionParams | null>(null);

    const [schema, setSchema] = useState('public');
    const [tables, setTables] = useState<Array<{ schema: string; table: string }>>([]);
    const [tablesLoading, setTablesLoading] = useState(false);
    const [loadLoading, setLoadLoading] = useState(false);
    const [saveLoading, setSaveLoading] = useState(false);

    useEffect(() => {
        let mounted = true;
        getPostgresStatus()
            .then((r) => {
                if (mounted) setDefaultAvailable(r.default_connection_available);
            })
            .catch(() => {
                if (mounted) setDefaultAvailable(false);
            })
            .finally(() => {
                if (mounted) setLoadingStatus(false);
            });
        return () => {
            mounted = false;
        };
    }, []);

    const getConnection = (customMode: boolean): PostgresConnectionParams | null => {
        if (customMode && customUri.trim()) {
            return { connection_url: customUri.trim() };
        }
        return null;
    };

    const overlay = (customMode: boolean) =>
        customMode
            ? customUri.trim()
                ? { connection_url: customUri.trim() }
                : {}
            : { use_default: true };

    const handleTest = async (customMode: boolean) => {
        setUseCustom(customMode);
        const conn = getConnection(customMode);
        if (!editing && customMode && !conn) return;
        setTestStatus('testing');
        setTestMessage('');
        try {
            const res = editing
                ? await testSavedConnection(editing.id, overlay(customMode))
                : await testPostgresConnection(conn);
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

    const handleUse = (customMode: boolean) => {
        setUseCustom(customMode);
        const conn = getConnection(customMode);
        setConnectionForUse(conn);
        setStep('pick-table');
        setTables([]);
    };

    const handleSave = async (customMode: boolean) => {
        setUseCustom(customMode);
        const conn = getConnection(customMode);
        if (!editing && customMode && !conn) return;
        setSaveLoading(true);
        try {
            const body = overlay(customMode);
            const saved = editing
                ? await patchSavedConnection(editing.id, {
                      name: connectionName.trim() || undefined,
                      connection: body,
                  })
                : await createSavedConnection({
                      connector_id: 'postgres',
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
            const res = await getPostgresTables(schema, connectionForUse);
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
            const res = await loadPostgresTable(
                schema,
                tableName,
                sessionId,
                connectionForUse ?? undefined
            );
            setSessionId(res.session_id);
            persistSessionId(res.session_id);
            addUploadedFile({
                file_id: res.source_id,
                filename: `PostgreSQL: ${res.source_label}`,
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
                    <h2 className={`text-xl font-bold ${isDark ? 'text-white' : 'text-slate-800'}`}>
                        Select table
                    </h2>
                    <button
                        type="button"
                        onClick={() => {
                            setStep('connect');
                            setConnectionForUse(null);
                        }}
                        className={`text-sm ${isDark ? 'text-white/50 hover:text-white' : 'text-slate-500 hover:text-slate-700'}`}
                    >
                        Back
                    </button>
                </div>
                <div className="flex gap-2">
                    <input
                        type="text"
                        value={schema}
                        onChange={(e) => setSchema(e.target.value)}
                        placeholder="Schema (e.g. public)"
                        className={`flex-1 rounded-lg border px-3 py-2 text-sm ${isDark ? 'bg-white/5 border-white/10 text-white placeholder:text-white/30' : 'bg-white border-gray-200 text-slate-800'}`}
                    />
                    <button
                        type="button"
                        onClick={fetchTables}
                        disabled={tablesLoading}
                        className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
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
                    className={`rounded-xl border max-h-64 overflow-auto ${isDark ? 'border-white/10' : 'border-gray-200'}`}
                >
                    {tables.length === 0 && !tablesLoading && (
                        <p className={`p-4 text-sm ${isDark ? 'text-white/40' : 'text-slate-500'}`}>
                            Enter schema and click &quot;Load tables&quot;
                        </p>
                    )}
                    {tables.map(({ table: t }) => (
                        <button
                            key={t}
                            type="button"
                            onClick={() => handleLoadTable(t)}
                            disabled={loadLoading}
                            className={`w-full text-left px-4 py-3 border-b last:border-b-0 flex items-center justify-between ${isDark ? 'border-white/10 hover:bg-white/5' : 'border-gray-100 hover:bg-gray-50'} ${loadLoading ? 'opacity-50 cursor-not-allowed' : ''}`}
                        >
                            <span className={isDark ? 'text-white' : 'text-slate-800'}>{t}</span>
                            {loadLoading ? (
                                <Loader2 className="w-4 h-4 animate-spin text-blue-400" />
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
                <h2 className={`text-xl font-bold ${isDark ? 'text-white' : 'text-slate-800'}`}>
                    {editing ? 'Edit PostgreSQL connection' : 'Connect to PostgreSQL'}
                </h2>
                <p className={`text-sm mt-0.5 ${isDark ? 'text-white/40' : 'text-slate-400'}`}>
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

            {loadingStatus ? (
                <div className="flex items-center gap-2 text-sm text-white/50">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Checking config…
                </div>
            ) : (
                <>
                    {libraryMode && (
                        <input
                            value={connectionName}
                            onChange={(e) => setConnectionName(e.target.value)}
                            placeholder="Connection name (optional)"
                            className={`w-full rounded-lg border px-3 py-2 text-sm ${isDark ? 'bg-white/5 border-white/10 text-white placeholder:text-white/30' : 'bg-white border-gray-200 text-slate-800'}`}
                        />
                    )}
                    {defaultAvailable && (
                        <div
                            className={`rounded-2xl border p-5 ${isDark ? 'bg-white/[0.03] border-white/10' : 'bg-gray-50 border-gray-200'}`}
                        >
                            <p
                                className={`text-sm font-medium mb-3 ${isDark ? 'text-white/80' : 'text-slate-700'}`}
                            >
                                Default connection (from config)
                            </p>
                            <div className="flex flex-wrap items-center gap-2">
                                <button
                                    type="button"
                                    onClick={() => {
                                        handleTest(false);
                                    }}
                                    disabled={testStatus === 'testing'}
                                    className="rounded-lg border px-4 py-2 text-sm font-medium flex items-center gap-2 disabled:opacity-50"
                                    style={
                                        isDark
                                            ? {
                                                  borderColor: 'rgba(255,255,255,0.2)',
                                                  color: 'rgba(255,255,255,0.9)',
                                              }
                                            : {}
                                    }
                                >
                                    {testStatus === 'testing' && !useCustom ? (
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                    ) : null}
                                    Test connection
                                </button>
                                <button
                                    type="button"
                                    onClick={() => {
                                        if (libraryMode) handleSave(false);
                                        else handleUse(false);
                                    }}
                                    className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                                    disabled={saveLoading}
                                >
                                    {libraryMode
                                        ? editing
                                            ? 'Save changes'
                                            : 'Save connection'
                                        : 'Use'}
                                </button>
                                {!useCustom && testStatus === 'success' && (
                                    <span className="flex items-center gap-1 text-sm text-emerald-500">
                                        <CheckCircle2 className="w-4 h-4" /> Connected
                                    </span>
                                )}
                                {!useCustom && testStatus === 'error' && (
                                    <span
                                        className="flex items-center gap-1 text-sm text-red-400"
                                        title={testMessage}
                                    >
                                        <AlertCircle className="w-4 h-4" /> Failed
                                    </span>
                                )}
                            </div>
                        </div>
                    )}

                    <div
                        className={`rounded-2xl border p-5 ${isDark ? 'bg-white/[0.03] border-white/10' : 'bg-gray-50 border-gray-200'}`}
                    >
                        <p
                            className={`text-sm font-medium mb-3 ${isDark ? 'text-white/80' : 'text-slate-700'}`}
                        >
                            Custom connection
                        </p>
                        {editing && !editing.hint.use_default && (
                            <p
                                className={`text-xs mb-3 ${isDark ? 'text-white/40' : 'text-slate-400'}`}
                            >
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
                                    : 'postgresql://user:password@host:5432/database'
                            }
                            className={`w-full rounded-lg border px-3 py-2 text-sm mb-3 ${isDark ? 'bg-white/5 border-white/10 text-white placeholder:text-white/30' : 'bg-white border-gray-200 text-slate-800'}`}
                        />
                        <div className="flex flex-wrap items-center gap-2">
                            <button
                                type="button"
                                onClick={() => {
                                    handleTest(true);
                                }}
                                disabled={
                                    testStatus === 'testing' || (!customUri.trim() && !editing)
                                }
                                className="rounded-lg border px-4 py-2 text-sm font-medium flex items-center gap-2 disabled:opacity-50"
                                style={
                                    isDark
                                        ? {
                                              borderColor: 'rgba(255,255,255,0.2)',
                                              color: 'rgba(255,255,255,0.9)',
                                          }
                                        : {}
                                }
                            >
                                {testStatus === 'testing' && useCustom ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : null}
                                Test connection
                            </button>
                            <button
                                type="button"
                                onClick={() => {
                                    if (libraryMode) handleSave(true);
                                    else handleUse(true);
                                }}
                                disabled={(!customUri.trim() && !editing) || saveLoading}
                                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                            >
                                {libraryMode
                                    ? editing
                                        ? 'Save changes'
                                        : 'Save connection'
                                    : 'Use'}
                            </button>
                            {useCustom && testStatus === 'success' && (
                                <span className="flex items-center gap-1 text-sm text-emerald-500">
                                    <CheckCircle2 className="w-4 h-4" /> Connected
                                </span>
                            )}
                            {useCustom && testStatus === 'error' && (
                                <span
                                    className="flex items-center gap-1 text-sm text-red-400"
                                    title={testMessage}
                                >
                                    <AlertCircle className="w-4 h-4" /> Failed
                                </span>
                            )}
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}
