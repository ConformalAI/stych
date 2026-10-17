import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';

const listSavedConnections = vi.fn();
const getConnectorCatalog = vi.fn();
const listRemoteNamespaces = vi.fn();

vi.mock('../../services/api', () => ({
    listSavedConnections: (...args: unknown[]) => listSavedConnections(...args),
    getConnectorCatalog: (...args: unknown[]) => getConnectorCatalog(...args),
    listRemoteNamespaces: (...args: unknown[]) => listRemoteNamespaces(...args),
    listRemoteTables: vi.fn(),
    attachSavedTable: vi.fn(),
    attachConnectionTable: vi.fn(),
    uploadFile: vi.fn(),
    deleteSource: vi.fn(),
}));

import AddDataModal from './AddDataModal';

const catalog: ConnectorCatalogEntry[] = [
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

const connection = {
    id: 'a',
    connector_id: 'postgres',
    name: 'Warehouse',
    tags: [],
    hint: { host: 'db.internal', port: 5432, database: 'analytics' },
    last_test_status: 'ok',
    tables: [{ id: 't1', schema_name: 'public', table_name: 'orders', display_name: 'orders' }],
} as SavedConnection;

describe('AddDataModal', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    // FileUpload uses react-query; the app supplies this provider in main.tsx.
    const render = async () => {
        await act(async () =>
            root.render(
                <QueryClientProvider client={new QueryClient()}>
                    <MemoryRouter>
                        <AddDataModal onClose={vi.fn()} />
                    </MemoryRouter>
                </QueryClientProvider>
            )
        );
    };

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
        getConnectorCatalog.mockResolvedValue(catalog);
        listRemoteNamespaces.mockResolvedValue({
            default_namespace: 'public',
            namespaces: ['public'],
            complete: true,
        });
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.clearAllMocks();
    });

    it('routes to the Connectors page when nothing is configured', async () => {
        listSavedConnections.mockResolvedValue([]);
        await render();

        // With no connectors, the upload pane is the only actionable one...
        expect(container.textContent).toContain('Upload file');

        const connectorTab = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('From connector')
        );
        await act(async () => {
            connectorTab?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(container.textContent).toContain('No connectors configured');
        const cta = Array.from(container.querySelectorAll('a')).find((a) =>
            a.textContent?.includes('Go to Connectors')
        );
        expect(cta?.getAttribute('href')).toBe('/connectors');
    });

    it('opens on the connector pane and lists configured connections', async () => {
        listSavedConnections.mockResolvedValue([connection]);
        await render();

        expect(container.textContent).toContain('Warehouse');
        expect(container.textContent).toContain('db.internal:5432/analytics');
        expect(container.textContent).toContain('1 pinned');
    });

    it('drills into a connection to offer its pinned tables', async () => {
        listSavedConnections.mockResolvedValue([connection]);
        await render();

        const row = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('Warehouse')
        );
        await act(async () => {
            row?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(container.textContent).toContain('Pinned tables');
        expect(container.textContent).toContain('orders');
        expect(container.textContent).toContain('Browse tables');
    });

    it('offers no credential forms — configuring connectors lives elsewhere', async () => {
        listSavedConnections.mockResolvedValue([connection]);
        await render();

        expect(container.textContent).not.toContain('Private key');
        expect(container.textContent).not.toContain('Connection URL');
        expect(container.textContent).toContain('Manage connectors');
    });

    it('starts from the saved namespace and offers discovered choices without blocking typing', async () => {
        const mongoCatalog: ConnectorCatalogEntry[] = [
            {
                ...catalog[0],
                id: 'mongodb',
                label: 'MongoDB',
                vendor: 'mongodb',
                maturity: 'beta',
                schema_label: 'Database',
                default_schema: 'admin',
                relation_noun: 'collection',
            },
        ];
        const mongoConnection = {
            ...connection,
            connector_id: 'mongodb',
            name: 'Documents',
            hint: { host: 'mongo.internal', port: 27017, database: 'cafe_sales' },
            tables: [],
        } as SavedConnection;
        getConnectorCatalog.mockResolvedValue(mongoCatalog);
        listSavedConnections.mockResolvedValue([mongoConnection]);
        listRemoteNamespaces.mockResolvedValue({
            default_namespace: 'cafe_sales',
            namespaces: ['admin', 'cafe_sales', 'reporting'],
            complete: true,
        });
        await render();

        const row = Array.from(container.querySelectorAll('button')).find((node) =>
            node.textContent?.includes('Documents')
        );
        await act(async () => row?.dispatchEvent(new MouseEvent('click', { bubbles: true })));

        const input = container.querySelector<HTMLInputElement>('input[aria-label="Database"]');
        expect(input?.value).toBe('cafe_sales');
        const dropdown = container.querySelector<HTMLButtonElement>(
            'button[aria-label="Show available databases"]'
        );
        await act(async () => {
            dropdown?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            await Promise.resolve();
        });

        expect(listRemoteNamespaces).toHaveBeenCalledWith('a');
        expect(container.textContent).toContain('reporting');

        await act(async () => {
            if (!input) return;
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                'value'
            )?.set;
            setter?.call(input, 'manual_db');
            input.dispatchEvent(new Event('input', { bubbles: true }));
        });
        expect(input?.value).toBe('manual_db');
    });
});
