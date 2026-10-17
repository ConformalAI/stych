import { useState } from 'react';
import { ChevronDown } from 'lucide-react';

type WarehouseProvider = 'snowflake' | 'redshift' | 'bigquery' | 'databricks';

const GUIDES: Record<WarehouseProvider, { title: string; steps: string[] }> = {
    snowflake: {
        title: 'How to connect',
        steps: [
            'In Snowflake, create a TYPE=SERVICE user (no password).',
            'Generate an RSA key pair locally. Keep the private key as PEM.',
            'Run ALTER USER … SET RSA_PUBLIC_KEY=… with the matching public key.',
            'Grant USAGE on the warehouse, database, and schema you want to read.',
            'In Stych, paste account, user, warehouse, database, and the PEM private key.',
        ],
    },
    redshift: {
        title: 'How to connect',
        steps: [
            'Create an IAM user or role with redshift:GetClusterCredentials (or Serverless GetCredentials).',
            'Create a matching database user and grant SELECT on the tables you need.',
            'Copy the cluster host from the AWS console (standard *.redshift.amazonaws.com hosts infer region).',
            'Create an access key for the IAM principal.',
            'In Stych, enter host, database, database user, access key ID, and secret access key.',
        ],
    },
    bigquery: {
        title: 'How to connect',
        steps: [
            'Quick: click Continue with Google, approve BigQuery read access, then pick a project and dataset.',
            'Service account: in Google Cloud, create a service account with BigQuery Job User and Data Viewer.',
            'Keys → Add key → JSON, then paste that file into Stych. Project ID is optional if it is already in the JSON.',
            'Choose a dataset, load its tables, and pick the table to analyze (up to 10,000 rows).',
        ],
    },
    databricks: {
        title: 'How to connect',
        steps: [
            'Sign up for Databricks Free Edition (no credit card) or use an existing workspace.',
            'Start the Serverless Starter Warehouse and wait until it is Running.',
            'Open the warehouse Connection details tab. Copy Server hostname and HTTP path (/sql/1.0/warehouses/…).',
            'Settings → Developer → Access tokens → Generate new token. Copy it once (BI Tools scope if shown).',
            'Catalog is usually workspace on Free Edition, or main on paid workspaces. Schema is often default.',
            'The PAT needs CREATE SCHEMA and CREATE TABLE on a scratch schema (default {catalog}.stych_temp) so Stych can clone the table for cleaning.',
        ],
    },
};

export default function WarehouseHowToConnect({ provider }: { provider: WarehouseProvider }) {
    const [open, setOpen] = useState(false);
    const guide = GUIDES[provider];

    return (
        <div
            className="rounded-xl border overflow-hidden"
            style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
        >
            <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                className="w-full flex items-center justify-between px-4 py-3 text-left"
            >
                <span className="text-sm font-medium" style={{ color: 'var(--c-ink)' }}>
                    {guide.title}
                </span>
                <ChevronDown
                    className={`w-4 h-4 transition-transform ${open ? 'rotate-180' : ''}`}
                    style={{ color: 'var(--c-mist-fg)' }}
                />
            </button>
            {open && (
                <ol
                    className="px-4 pb-4 space-y-2 text-sm list-decimal list-inside"
                    style={{ color: 'var(--c-ink-2)' }}
                >
                    {guide.steps.map((step) => (
                        <li key={step} className="leading-relaxed">
                            {step}
                        </li>
                    ))}
                </ol>
            )}
        </div>
    );
}
