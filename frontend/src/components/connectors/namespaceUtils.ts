import type { ConnectorCatalogEntry, SavedConnection } from '../../services/api';

export function initialNamespace(
    connection: SavedConnection,
    entry?: ConnectorCatalogEntry
): string {
    const label = (entry?.schema_label ?? 'Schema').toLowerCase();
    const hinted = connection.hint[label];
    return typeof hinted === 'string' && hinted.trim()
        ? hinted.trim()
        : (entry?.default_schema ?? '');
}
