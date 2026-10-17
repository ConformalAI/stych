import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import WarehouseConnect from './WarehouseConnect';

describe('WarehouseConnect', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('shows Snowflake-specific credentials, how-to help, and the live analysis scope', async () => {
        await act(async () => root.render(<WarehouseConnect provider="snowflake" />));

        expect(container.textContent).toContain('Connect to Snowflake');
        expect(container.textContent).toContain('Full-data analysis runs in Snowflake');
        expect(container.textContent).toContain('default STYCH_TEMP');
        expect(container.textContent).toContain('How to connect');
        expect(container.textContent).toContain('Account');
        expect(container.textContent).toContain('Private key (PEM)');
    });

    it('shows Redshift-specific credentials without Snowflake fields', async () => {
        await act(async () => root.render(<WarehouseConnect provider="redshift" />));

        expect(container.textContent).toContain('Connect to Amazon Redshift');
        expect(container.textContent).toContain('Full-data analysis runs in Redshift');
        expect(container.textContent).toContain('default stych_temp');
        expect(container.textContent).toContain('How to connect');
        expect(container.textContent).toContain('Host');
        expect(container.textContent).toContain('Port');
        expect(container.textContent).toContain('Access key ID');
        expect(container.textContent).toContain('Secret access key');
        expect(container.textContent).not.toContain('Private key (PEM)');
        expect(container.textContent).not.toContain('Password');
        expect(container.querySelector('input[type="password"]')).not.toBeNull();
    });

    it('starts BigQuery with the service-account connection form', async () => {
        await act(async () => root.render(<WarehouseConnect provider="bigquery" />));

        expect(container.textContent).toContain('Connect to BigQuery');
        expect(container.textContent).toContain('Service account JSON');
        expect(container.textContent).toContain('Project ID (optional)');
        expect(container.textContent).not.toContain('Access token');
        expect(container.textContent).not.toContain('Continue with Google');
    });

    it('shows Databricks PAT fields', async () => {
        await act(async () => root.render(<WarehouseConnect provider="databricks" />));

        expect(container.textContent).toContain('Connect to Databricks');
        expect(container.textContent).toContain('How to connect');
        expect(container.textContent).toContain('Server hostname');
        expect(container.textContent).toContain('HTTP path');
        expect(container.textContent).toContain('Catalog');
        expect(container.textContent).toContain('Access token');
        expect(container.textContent).toContain('Scratch schema');
        expect(container.textContent).toContain('Scratch catalog');
        expect(container.textContent).not.toContain('10,000');
        expect(container.textContent).not.toContain('Password');
        expect(container.querySelector('input[type="password"]')).not.toBeNull();
    });

    it('prefills non-secret fields when editing and leaves the key blank', async () => {
        await act(async () =>
            root.render(
                <WarehouseConnect
                    provider="snowflake"
                    libraryMode
                    editing={{
                        id: 'c1',
                        connector_id: 'snowflake',
                        name: 'Analytics',
                        tags: [],
                        hint: {
                            account: 'org-account',
                            user: 'analyst',
                            warehouse: 'COMPUTE_WH',
                            database: 'DB',
                        },
                        tables: [],
                    }}
                    onSaved={() => undefined}
                    onCancel={() => undefined}
                />
            )
        );

        expect(container.textContent).toContain('Edit Snowflake connection');
        expect(container.textContent).toContain('Tokens and keys stay hidden');
        const account = Array.from(container.querySelectorAll('input')).find(
            (input) => input.value === 'org-account'
        );
        expect(account).toBeTruthy();
        const key = container.querySelector('textarea');
        expect(key?.value).toBe('');
        expect(key?.placeholder).toContain('Leave blank to keep the saved value');
        expect(container.textContent).toContain('Save changes');
    });
});
