import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConnectorCatalogEntry, SavedConnection } from '../services/api';

const listSavedConnections = vi.fn();
const getConnectorCatalog = vi.fn();

vi.mock('../services/api', () => ({
    listSavedConnections: (...args: unknown[]) => listSavedConnections(...args),
    getConnectorCatalog: (...args: unknown[]) => getConnectorCatalog(...args),
    listRemoteTables: vi.fn(),
    pinSavedTable: vi.fn(),
    unpinSavedTable: vi.fn(),
    patchSavedConnection: vi.fn(),
    testSavedConnection: vi.fn(),
    getConnectionUsage: vi.fn(),
    deleteSavedConnection: vi.fn(),
    // Reached once the inline setup form mounts a real provider component.
    getPostgresStatus: vi.fn().mockResolvedValue({ default_connection_available: false }),
    testPostgresConnection: vi.fn(),
    getPostgresTables: vi.fn(),
    loadPostgresTable: vi.fn(),
    createSavedConnection: vi.fn(),
}));

import Connectors from './Connectors';

const catalog: ConnectorCatalogEntry[] = [
    {
        id: 'bigquery',
        label: 'BigQuery',
        category: 'cloud_warehouse',
        vendor: 'gcp',
        maturity: 'beta',
        summary: 'Live preview',
        enabled: true,
        schema_label: 'Dataset',
        default_schema: '',
        relation_noun: 'table',
        configured_count: 1,
    },
    {
        id: 'postgres',
        label: 'PostgreSQL',
        category: 'database',
        vendor: 'postgresql',
        maturity: 'production',
        summary: 'Live · Full-data analysis',
        enabled: true,
        schema_label: 'Schema',
        default_schema: 'public',
        relation_noun: 'table',
        configured_count: 1,
    },
];

const connections = [
    {
        id: 'a',
        connector_id: 'bigquery',
        name: 'Finance warehouse',
        description: 'Revenue marts',
        tags: ['prod', 'finance'],
        hint: { project_id: 'acme-analytics' },
        last_test_status: 'ok',
        last_used_at: '2026-08-18T10:00:00',
        tables: [],
    },
    {
        id: 'b',
        connector_id: 'postgres',
        name: 'Local dev',
        tags: ['dev'],
        hint: { host: 'localhost', port: 5432, database: 'app' },
        last_test_status: 'error',
        last_test_error: 'password authentication failed',
        tables: [],
    },
] as SavedConnection[];

describe('Connectors page', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    const render = async () => {
        await act(async () =>
            root.render(
                <MemoryRouter>
                    <Connectors />
                </MemoryRouter>
            )
        );
    };

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
        listSavedConnections.mockResolvedValue(connections);
        getConnectorCatalog.mockResolvedValue(catalog);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.clearAllMocks();
    });

    it('lists connections with type, endpoint and tags', async () => {
        await render();

        expect(container.textContent).toContain('Finance warehouse');
        expect(container.textContent).toContain('BigQuery');
        expect(container.textContent).toContain('acme-analytics');
        expect(container.textContent).toContain('finance');
        expect(container.textContent).toContain('localhost:5432/app');
    });

    it('surfaces how many connections are failing', async () => {
        await render();
        expect(container.textContent).toContain('2 connections');
        expect(container.textContent).toContain('1 failing');
    });

    it('offers vendor, status and tag facets built from the data', async () => {
        await render();

        expect(container.textContent).toContain('Google Cloud');
        expect(container.textContent).toContain('PostgreSQL');
        expect(container.textContent).toContain('Healthy');
        expect(container.textContent).toContain('Failing');
        expect(container.textContent).toContain('prod');
    });

    it('filters the table when a facet is selected', async () => {
        await render();

        const gcpFacet = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('Google Cloud')
        );
        await act(async () => {
            gcpFacet?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(container.textContent).toContain('Finance warehouse');
        expect(container.textContent).not.toContain('Local dev');
    });

    it('opens a detail drawer showing the recorded failure reason', async () => {
        await render();

        const row = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('Local dev')
        );
        await act(async () => {
            row?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(container.textContent).toContain('password authentication failed');
        expect(container.textContent).toContain('Overview');
        expect(container.textContent).toContain('Settings');
    });

    it('prompts to pick from the available shelf when nothing is configured', async () => {
        listSavedConnections.mockResolvedValue([]);
        await render();

        expect(container.textContent).toContain('Nothing configured yet');
        // The available connectors are on the page itself, not behind a dialog.
        expect(container.textContent).toContain('Available');
        expect(container.textContent).toContain('BigQuery');
        expect(container.textContent).toContain('Connect');
    });

    it('lists available connectors below the configured ones, grouped by category', async () => {
        await render();

        const text = container.textContent ?? '';
        expect(text).toContain('Your connectors');
        expect(text).toContain('Available');
        expect(text).toContain('Cloud warehouse');
        expect(text).toContain('Database');
        // Configured section precedes the available shelf.
        expect(text.indexOf('Your connectors')).toBeLessThan(text.indexOf('Available'));
        // Within the available shelf, cloud warehouse is grouped before database.
        expect(text.indexOf('Cloud warehouse')).toBeLessThan(text.lastIndexOf('Database'));
    });

    it('runs one search across both configured and available connectors', async () => {
        await render();

        const search = container.querySelector(
            'input[aria-label="Search connectors"]'
        ) as HTMLInputElement;
        const setValue = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype,
            'value'
        )!.set!;
        await act(async () => {
            setValue.call(search, 'postgres');
            search.dispatchEvent(new Event('input', { bubbles: true }));
        });

        // Matches the configured Postgres connection and the available type.
        expect(container.textContent).toContain('Local dev');
        expect(container.textContent).toContain('PostgreSQL');
        expect(container.textContent).not.toContain('Finance warehouse');
    });

    it('opens the setup form inline rather than in a dialog', async () => {
        listSavedConnections.mockResolvedValue([]);
        await render();

        const connectCard = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('PostgreSQL')
        );
        await act(async () => {
            connectCard?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(container.textContent).toContain('Connect PostgreSQL');
        expect(container.textContent).toContain('All connectors');
        expect(container.querySelector('[role="dialog"]')).toBeNull();
    });
});
