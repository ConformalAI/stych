import type {
    BigQueryConnectionParams,
    DatabricksConnectionParams,
    RedshiftConnectionParams,
    SnowflakeConnectionParams,
} from '../services/api';
import { KEEP_SECRET_PLACEHOLDER } from './savedConnectionHint';

interface SnowflakeFieldsProps {
    value: SnowflakeConnectionParams;
    onChange: (value: SnowflakeConnectionParams) => void;
    keepSecrets?: boolean;
}

export function SnowflakeFields({ value, onChange, keepSecrets = false }: SnowflakeFieldsProps) {
    return (
        <>
            <Field
                label="Account"
                hint="org-account or xy12345.us-east-1"
                value={value.account}
                onChange={(account) => onChange({ ...value, account })}
            />
            <Field
                label="User"
                value={value.user}
                onChange={(user) => onChange({ ...value, user })}
            />
            <Field
                label="Warehouse"
                value={value.warehouse}
                onChange={(warehouse) => onChange({ ...value, warehouse })}
            />
            <Field
                label="Database"
                value={value.database}
                onChange={(database) => onChange({ ...value, database })}
            />
            <Field
                label="Role (optional)"
                value={value.role ?? ''}
                onChange={(role) => onChange({ ...value, role })}
            />
            <Field
                label="Key passphrase (optional)"
                type="password"
                value={value.private_key_passphrase ?? ''}
                onChange={(private_key_passphrase) =>
                    onChange({ ...value, private_key_passphrase })
                }
                placeholder={keepSecrets ? KEEP_SECRET_PLACEHOLDER : undefined}
            />
            <TextAreaField
                label="Private key (PEM)"
                value={value.private_key}
                onChange={(private_key) => onChange({ ...value, private_key })}
                placeholder={
                    keepSecrets
                        ? KEEP_SECRET_PLACEHOLDER
                        : '-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----'
                }
            />
        </>
    );
}

interface RedshiftFieldsProps {
    value: RedshiftConnectionParams;
    onChange: (value: RedshiftConnectionParams) => void;
    keepSecrets?: boolean;
}

export function RedshiftFields({ value, onChange, keepSecrets = false }: RedshiftFieldsProps) {
    return (
        <>
            <Field
                label="Host"
                hint="cluster.region.redshift.amazonaws.com"
                value={value.host}
                onChange={(host) => onChange({ ...value, host })}
            />
            <Field
                label="Port"
                type="number"
                value={String(value.port)}
                onChange={(port) => onChange({ ...value, port: Number(port) || 5439 })}
            />
            <Field
                label="Database"
                value={value.database}
                onChange={(database) => onChange({ ...value, database })}
            />
            <Field
                label="User"
                hint="Database user, not the IAM user name"
                value={value.user}
                onChange={(user) => onChange({ ...value, user })}
            />
            <Field
                label="Access key ID"
                value={value.access_key_id}
                onChange={(access_key_id) => onChange({ ...value, access_key_id })}
            />
            <Field
                label="Secret access key"
                type="password"
                value={value.secret_access_key}
                onChange={(secret_access_key) => onChange({ ...value, secret_access_key })}
                placeholder={keepSecrets ? KEEP_SECRET_PLACEHOLDER : undefined}
            />
            <Field
                label="Session token (optional)"
                type="password"
                value={value.session_token ?? ''}
                onChange={(session_token) => onChange({ ...value, session_token })}
                placeholder={keepSecrets ? KEEP_SECRET_PLACEHOLDER : undefined}
            />
            <Field
                label="Region (optional)"
                value={value.region ?? ''}
                onChange={(region) => onChange({ ...value, region })}
            />
            <Field
                label="Cluster identifier (optional)"
                hint="Required only for custom hostnames"
                value={value.cluster_identifier ?? ''}
                onChange={(cluster_identifier) => onChange({ ...value, cluster_identifier })}
            />
        </>
    );
}

interface BigQueryFieldsProps {
    value: BigQueryConnectionParams;
    onChange: (value: BigQueryConnectionParams) => void;
    keepSecrets?: boolean;
}

export function BigQueryFields({ value, onChange, keepSecrets = false }: BigQueryFieldsProps) {
    return (
        <>
            <Field
                label="Project ID (optional)"
                hint="Leave blank to use the project on the JSON key"
                value={value.project_id ?? ''}
                onChange={(project_id) => onChange({ ...value, project_id })}
            />
            <TextAreaField
                label="Service account JSON"
                value={value.service_account_json ?? ''}
                onChange={(service_account_json) => onChange({ ...value, service_account_json })}
                placeholder={
                    keepSecrets
                        ? KEEP_SECRET_PLACEHOLDER
                        : '{"type": "service_account", "project_id": "...", ...}'
                }
            />
        </>
    );
}

interface DatabricksFieldsProps {
    value: DatabricksConnectionParams;
    onChange: (value: DatabricksConnectionParams) => void;
    keepSecrets?: boolean;
}

export function DatabricksFields({ value, onChange, keepSecrets = false }: DatabricksFieldsProps) {
    return (
        <>
            <Field
                label="Server hostname"
                hint="dbc-xxxx.cloud.databricks.com — no https://"
                value={value.server_hostname}
                onChange={(server_hostname) => onChange({ ...value, server_hostname })}
            />
            <Field
                label="HTTP path"
                hint="/sql/1.0/warehouses/…"
                value={value.http_path}
                onChange={(http_path) => onChange({ ...value, http_path })}
            />
            <Field
                label="Catalog"
                hint="workspace on Free Edition, often main otherwise"
                value={value.catalog}
                onChange={(catalog) => onChange({ ...value, catalog })}
            />
            <Field
                label="Access token"
                type="password"
                value={value.access_token}
                onChange={(access_token) => onChange({ ...value, access_token })}
                placeholder={keepSecrets ? KEEP_SECRET_PLACEHOLDER : undefined}
            />
            <Field
                label="Scratch catalog"
                hint="optional — defaults to the connected catalog"
                value={value.scratch_catalog ?? ''}
                onChange={(scratch_catalog) => onChange({ ...value, scratch_catalog })}
            />
            <Field
                label="Scratch schema"
                hint="optional — Stych creates stych_temp here for working copies"
                value={value.scratch_schema ?? ''}
                onChange={(scratch_schema) => onChange({ ...value, scratch_schema })}
            />
        </>
    );
}

function Field({
    label,
    value,
    onChange,
    type = 'text',
    hint,
    placeholder,
}: {
    label: string;
    value: string;
    onChange: (value: string) => void;
    type?: string;
    hint?: string;
    placeholder?: string;
}) {
    return (
        <label className="space-y-1.5 text-sm" style={{ color: 'var(--c-ink-2)' }}>
            <span className="block font-medium" style={{ color: 'var(--c-ink)' }}>
                {label}
            </span>
            <input
                type={type}
                value={value}
                onChange={(event) => onChange(event.target.value)}
                placeholder={placeholder}
                className="input w-full text-sm"
                autoComplete={type === 'password' ? 'new-password' : 'off'}
            />
            {hint && (
                <span className="block text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                    {hint}
                </span>
            )}
        </label>
    );
}

function TextAreaField({
    label,
    value,
    onChange,
    placeholder,
}: {
    label: string;
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
}) {
    return (
        <label className="space-y-1.5 text-sm col-span-2" style={{ color: 'var(--c-ink-2)' }}>
            <span className="block font-medium" style={{ color: 'var(--c-ink)' }}>
                {label}
            </span>
            <textarea
                value={value}
                onChange={(event) => onChange(event.target.value)}
                placeholder={placeholder}
                rows={6}
                spellCheck={false}
                autoComplete="off"
                className="input w-full text-xs font-mono"
            />
        </label>
    );
}
