import { useState } from 'react';
import { AlertCircle, CheckCircle2, Loader2, Table2 } from 'lucide-react';
import {
    getBigQueryTables,
    getDatabricksTables,
    getRedshiftTables,
    getSnowflakeTables,
    loadBigQueryTable,
    loadDatabricksTable,
    loadRedshiftTable,
    loadSnowflakeTable,
    testBigQueryConnection,
    testDatabricksConnection,
    testRedshiftConnection,
    testSnowflakeConnection,
    type BigQueryConnectionParams,
    type DatabricksConnectionParams,
    type RedshiftConnectionParams,
    type SnowflakeConnectionParams,
    createSavedConnection,
    patchSavedConnection,
    testSavedConnection,
    type SavedConnection,
} from '../services/api';
import { useStore } from '../store/useStore';
import { BigQueryGooglePanel, useBigQueryGoogleAuth } from './WarehouseBigQueryOauth';
import { ConnectShell } from './WarehouseConnectChrome';
import {
    BigQueryFields,
    DatabricksFields,
    RedshiftFields,
    SnowflakeFields,
} from './WarehouseConnectionFields';
import WarehouseHowToConnect from './WarehouseHowToConnect';
import { hintNumber, hintString } from './savedConnectionHint';
import { setSessionId as persistSessionId } from '../services/storage';

export type WarehouseProvider = 'snowflake' | 'redshift' | 'bigquery' | 'databricks';
type Step = 'connect' | 'pick-table';
type BigQueryMethod = 'oauth' | 'service_account';

interface WarehouseConnectProps {
    provider: WarehouseProvider;
    onComplete?: () => void;
    libraryMode?: boolean;
    editing?: SavedConnection;
    onSaved?: (connection: SavedConnection) => void;
    onCancel?: () => void;
}

const DETAILS = {
    snowflake: {
        label: 'Snowflake',
        defaultSchema: 'PUBLIC',
        schemaLabel: 'Schema',
        description:
            'RSA key-pair auth. Full-data analysis runs in Snowflake; writes stay in a scratch schema (default STYCH_TEMP).',
    },
    redshift: {
        label: 'Amazon Redshift',
        defaultSchema: 'public',
        schemaLabel: 'Schema',
        description:
            'IAM access keys. Full-data analysis runs in Redshift; writes stay in a scratch schema (default stych_temp).',
    },
    bigquery: {
        label: 'BigQuery',
        defaultSchema: '',
        schemaLabel: 'Dataset',
        description: 'Sign in with Google or paste a service-account key.',
    },
    databricks: {
        label: 'Databricks',
        defaultSchema: 'default',
        schemaLabel: 'Schema',
        description:
            'Personal access token. Full-data analysis runs in Databricks; Stych needs CREATE on a scratch schema (default stych_temp).',
    },
} as const;

function errorMessage(error: unknown, fallback: string): string {
    return (
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? fallback
    );
}

export default function WarehouseConnect({
    provider,
    onComplete,
    libraryMode = false,
    editing,
    onSaved,
    onCancel,
}: WarehouseConnectProps) {
    const details = DETAILS[provider];
    const hint = editing?.hint;
    const keepSecrets = Boolean(editing);
    const sessionId = useStore((s) => s.sessionId);
    const setSessionId = useStore((s) => s.setSessionId);
    const addUploadedFile = useStore((s) => s.addUploadedFile);
    const setError = useStore((s) => s.setError);
    const notifySessionActivity = useStore((s) => s.notifySessionActivity);
    const [step, setStep] = useState<Step>('connect');
    // BigQuery is service-account only: Google sign-in returns a read-only token,
    // which cannot create the scratch dataset that cleaning, filters and diff need.
    // Existing OAuth connections stay editable so they are not orphaned.
    const [bigqueryMethod] = useState<BigQueryMethod>(() => {
        if (provider !== 'bigquery' || !editing) return 'service_account';
        return hintString(editing.hint, 'auth_mode') === 'oauth' ? 'oauth' : 'service_account';
    });
    const [schema, setSchema] = useState<string>(details.defaultSchema);
    const [tables, setTables] = useState<Array<{ schema: string; table: string }>>([]);
    const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
    const [testMessage, setTestMessage] = useState('');
    const [tablesLoading, setTablesLoading] = useState(false);
    const [loadLoading, setLoadLoading] = useState(false);
    const [connectionName, setConnectionName] = useState(editing?.name ?? '');
    const [saveLoading, setSaveLoading] = useState(false);
    const googleAuth = useBigQueryGoogleAuth(provider === 'bigquery' && bigqueryMethod === 'oauth');
    const [snowflake, setSnowflake] = useState<SnowflakeConnectionParams>({
        account: hintString(hint, 'account'),
        user: hintString(hint, 'user'),
        private_key: '',
        warehouse: hintString(hint, 'warehouse'),
        database: hintString(hint, 'database'),
        role: hintString(hint, 'role'),
        private_key_passphrase: '',
    });
    const [redshift, setRedshift] = useState<RedshiftConnectionParams>({
        host: hintString(hint, 'host'),
        port: hintNumber(hint, 'port', 5439),
        database: hintString(hint, 'database'),
        user: hintString(hint, 'user'),
        access_key_id: hintString(hint, 'access_key_id'),
        secret_access_key: '',
        session_token: '',
        region: hintString(hint, 'region'),
        cluster_identifier: hintString(hint, 'cluster_identifier'),
    });
    const [bigquery, setBigquery] = useState<BigQueryConnectionParams>({
        service_account_json: '',
        project_id: hintString(hint, 'project_id'),
    });
    const [databricks, setDatabricks] = useState<DatabricksConnectionParams>({
        server_hostname: hintString(hint, 'server_hostname'),
        http_path: hintString(hint, 'http_path'),
        access_token: '',
        catalog: hintString(hint, 'catalog'),
        scratch_catalog: hintString(hint, 'scratch_catalog') || undefined,
        scratch_schema: hintString(hint, 'scratch_schema') || undefined,
    });

    const oauthConnection: BigQueryConnectionParams = {
        auth_mode: 'oauth',
        project_id: googleAuth.projectId || hintString(hint, 'project_id'),
    };
    const activeBigquery =
        provider === 'bigquery' && bigqueryMethod === 'oauth' ? oauthConnection : bigquery;
    const datasets = googleAuth.datasets;

    const complete =
        provider === 'snowflake'
            ? Boolean(
                  snowflake.account &&
                  snowflake.user &&
                  snowflake.warehouse &&
                  snowflake.database &&
                  (snowflake.private_key || keepSecrets)
              )
            : provider === 'redshift'
              ? Boolean(
                    redshift.host &&
                    redshift.database &&
                    redshift.user &&
                    (keepSecrets || (redshift.access_key_id && redshift.secret_access_key))
                )
              : provider === 'bigquery' && bigqueryMethod === 'oauth'
                ? Boolean(oauthConnection.project_id && (keepSecrets || googleAuth.connected))
                : provider === 'bigquery'
                  ? Boolean(bigquery.service_account_json || keepSecrets)
                  : Boolean(
                        databricks.server_hostname &&
                        databricks.http_path &&
                        databricks.catalog &&
                        (databricks.access_token || keepSecrets)
                    );

    const handleTest = async () => {
        if (!complete) return;
        setTestStatus('testing');
        setTestMessage('');
        try {
            const result = editing
                ? await testSavedConnection(editing.id, { ...activeConnection() })
                : provider === 'snowflake'
                  ? await testSnowflakeConnection(snowflake)
                  : provider === 'redshift'
                    ? await testRedshiftConnection(redshift)
                    : provider === 'bigquery'
                      ? await testBigQueryConnection(activeBigquery)
                      : await testDatabricksConnection(databricks);
            setTestStatus(result.success ? 'success' : 'error');
            setTestMessage(result.message);
        } catch (error: unknown) {
            setTestStatus('error');
            setTestMessage(errorMessage(error, 'Connection test failed'));
        }
    };

    const activeConnection = () =>
        provider === 'snowflake'
            ? snowflake
            : provider === 'redshift'
              ? redshift
              : provider === 'bigquery'
                ? activeBigquery
                : databricks;

    const handleSave = async () => {
        if (!complete) return;
        setSaveLoading(true);
        try {
            const saved = editing
                ? await patchSavedConnection(editing.id, {
                      name: connectionName.trim() || undefined,
                      connection: { ...activeConnection() },
                  })
                : await createSavedConnection({
                      connector_id: provider,
                      name: connectionName.trim() || undefined,
                      connection: { ...activeConnection() },
                  });
            onSaved?.(saved);
        } catch (error: unknown) {
            setError(errorMessage(error, 'Failed to save connection'));
        } finally {
            setSaveLoading(false);
        }
    };

    const fetchTables = async () => {
        setTablesLoading(true);
        try {
            const result =
                provider === 'snowflake'
                    ? await getSnowflakeTables(schema, snowflake)
                    : provider === 'redshift'
                      ? await getRedshiftTables(schema, redshift)
                      : provider === 'bigquery'
                        ? await getBigQueryTables(schema, activeBigquery)
                        : await getDatabricksTables(schema, databricks);
            setTables(result.tables ?? []);
        } catch (error: unknown) {
            setError(errorMessage(error, 'Failed to list tables'));
        } finally {
            setTablesLoading(false);
        }
    };

    const loadTable = async (table: string) => {
        setLoadLoading(true);
        try {
            const result =
                provider === 'snowflake'
                    ? await loadSnowflakeTable(schema, table, sessionId, snowflake)
                    : provider === 'redshift'
                      ? await loadRedshiftTable(schema, table, sessionId, redshift)
                      : provider === 'bigquery'
                        ? await loadBigQueryTable(schema, table, sessionId, activeBigquery)
                        : await loadDatabricksTable(schema, table, sessionId, databricks);
            setSessionId(result.session_id);
            persistSessionId(result.session_id);
            addUploadedFile({
                file_id: result.source_id,
                filename: `${details.label}: ${result.source_label}`,
                rows: result.rows,
                columns: result.columns,
            });
            notifySessionActivity(result.session_id);
            onComplete?.();
        } catch (error: unknown) {
            setError(errorMessage(error, 'Load failed'));
        } finally {
            setLoadLoading(false);
        }
    };

    if (step === 'pick-table') {
        return (
            <ConnectShell
                title="Select table"
                description={`Choose a ${details.schemaLabel.toLowerCase()} and load a table from ${details.label}.`}
                onBack={() => setStep('connect')}
                backLabel="Return"
            >
                <div className="flex gap-2">
                    {datasets.length > 0 ? (
                        <select
                            value={schema}
                            onChange={(event) => setSchema(event.target.value)}
                            className="input flex-1 text-sm"
                        >
                            <option value="">Select {details.schemaLabel.toLowerCase()}</option>
                            {datasets.map((name) => (
                                <option key={name} value={name}>
                                    {name}
                                </option>
                            ))}
                        </select>
                    ) : (
                        <input
                            value={schema}
                            onChange={(event) => setSchema(event.target.value)}
                            placeholder={details.schemaLabel}
                            className="input flex-1 text-sm"
                        />
                    )}
                    <button
                        type="button"
                        onClick={fetchTables}
                        disabled={tablesLoading || !schema}
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
                    style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
                >
                    {tables.length === 0 && !tablesLoading && (
                        <p className="p-4 text-sm" style={{ color: 'var(--c-mist-fg)' }}>
                            Enter a {details.schemaLabel.toLowerCase()} and load its tables.
                        </p>
                    )}
                    {tables.map(({ schema: tableSchema, table }) => (
                        <button
                            key={`${tableSchema}.${table}`}
                            type="button"
                            onClick={() => loadTable(table)}
                            disabled={loadLoading}
                            className="w-full text-left px-4 py-3 border-b last:border-b-0 flex items-center justify-between hover:bg-white/5 disabled:opacity-50"
                            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink)' }}
                        >
                            <span>{table}</span>
                            {loadLoading && <Loader2 className="w-4 h-4 animate-spin" />}
                        </button>
                    ))}
                </div>
            </ConnectShell>
        );
    }

    return (
        <ConnectShell
            title={editing ? `Edit ${details.label} connection` : `Connect to ${details.label}`}
            description={
                editing
                    ? 'Host and catalog are shown. Tokens and keys stay hidden — leave them blank to keep the saved values.'
                    : details.description
            }
            onBack={libraryMode ? onCancel : undefined}
            backLabel="Return"
        >
            <WarehouseHowToConnect provider={provider} />
            {provider === 'bigquery' && bigqueryMethod === 'oauth' ? (
                <BigQueryGooglePanel
                    busy={googleAuth.busy}
                    connected={googleAuth.connected}
                    projectId={googleAuth.projectId}
                    projects={googleAuth.projects}
                    onConnect={() => void googleAuth.connect()}
                    onProject={googleAuth.setProjectId}
                />
            ) : (
                <div
                    className="grid grid-cols-2 gap-3 rounded-xl border p-4"
                    style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
                >
                    {provider === 'snowflake' && (
                        <SnowflakeFields
                            value={snowflake}
                            onChange={setSnowflake}
                            keepSecrets={keepSecrets}
                        />
                    )}
                    {provider === 'redshift' && (
                        <RedshiftFields
                            value={redshift}
                            onChange={setRedshift}
                            keepSecrets={keepSecrets}
                        />
                    )}
                    {provider === 'bigquery' && (
                        <BigQueryFields
                            value={bigquery}
                            onChange={setBigquery}
                            keepSecrets={keepSecrets}
                        />
                    )}
                    {provider === 'databricks' && (
                        <DatabricksFields
                            value={databricks}
                            onChange={setDatabricks}
                            keepSecrets={keepSecrets}
                        />
                    )}
                </div>
            )}
            <div className="flex flex-wrap items-center gap-3">
                {libraryMode && (
                    <input
                        value={connectionName}
                        onChange={(event) => setConnectionName(event.target.value)}
                        placeholder="Connection name (optional)"
                        className="input flex-1 text-sm min-w-[12rem]"
                    />
                )}
                <button
                    type="button"
                    onClick={handleTest}
                    disabled={!complete || testStatus === 'testing'}
                    className="btn-secondary disabled:opacity-50"
                >
                    {testStatus === 'testing' && <Loader2 className="w-4 h-4 animate-spin" />}
                    Test connection
                </button>
                {libraryMode ? (
                    <button
                        type="button"
                        onClick={() => void handleSave()}
                        disabled={!complete || saveLoading}
                        className="btn-primary disabled:opacity-50"
                    >
                        {saveLoading && <Loader2 className="w-4 h-4 animate-spin" />}
                        Save {editing ? 'changes' : 'connection'}
                    </button>
                ) : (
                    <button
                        type="button"
                        onClick={() => setStep('pick-table')}
                        disabled={!complete}
                        className="btn-primary disabled:opacity-50"
                    >
                        Choose table
                    </button>
                )}
                {testStatus === 'success' && <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
                {testStatus === 'error' && <AlertCircle className="w-4 h-4 text-red-400" />}
                {(testMessage || googleAuth.message) && (
                    <span className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        {testMessage || googleAuth.message}
                    </span>
                )}
            </div>
        </ConnectShell>
    );
}
