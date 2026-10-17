import type { ReactNode } from 'react';
import type { ConnectorId, SavedConnection } from '../../services/api';
import PostgresConnect from '../PostgresConnect';
import SqlServerConnect from '../SqlServerConnect';
import MongoConnect from '../MongoConnect';
import WarehouseConnect from '../WarehouseConnect';

export interface ConnectorFormProps {
    onSaved: (connection: SavedConnection) => void;
    onCancel: () => void;
    editing?: SavedConnection;
}

/**
 * The credential form for a connector type.
 *
 * These forms already own per-provider validation, secret handling and the
 * test-before-save round trip, so the new Connectors UI drives them rather than
 * reimplementing seven credential flows.
 */
export function connectorForm(connectorId: ConnectorId, props: ConnectorFormProps): ReactNode {
    switch (connectorId) {
        case 'postgres':
            return <PostgresConnect libraryMode {...props} />;
        case 'sqlserver':
            return <SqlServerConnect libraryMode {...props} />;
        case 'mongodb':
            return <MongoConnect libraryMode {...props} />;
        case 'snowflake':
        case 'redshift':
        case 'bigquery':
        case 'databricks':
            return <WarehouseConnect provider={connectorId} libraryMode {...props} />;
        default:
            return null;
    }
}
