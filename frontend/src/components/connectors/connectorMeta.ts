import type { ComponentType, CSSProperties } from 'react';
import { Database as DatabaseIcon } from 'lucide-react';
import { DiMsqlServer } from 'react-icons/di';
import { FaAws } from 'react-icons/fa';
import {
    SiDatabricks,
    SiGooglebigquery,
    SiMongodb,
    SiPostgresql,
    SiSnowflake,
} from 'react-icons/si';
import type { ConnectorId, SavedConnection } from '../../services/api';

/** Any icon component (lucide or react-icons) that can be sized/colored via className+style. */
export type ConnectorIconComponent = ComponentType<{
    className?: string;
    style?: CSSProperties;
}>;

/** Real brand logo per connector type. Keyed by id so a new connector needs one line here. */
export const CONNECTOR_ICONS: Record<ConnectorId, ConnectorIconComponent> = {
    postgres: SiPostgresql,
    sqlserver: DiMsqlServer,
    mongodb: SiMongodb,
    snowflake: SiSnowflake,
    // Redshift has no dedicated brand mark in available icon sets; the AWS logo
    // is the closest real, recognizable identifier for the service.
    redshift: FaAws,
    bigquery: SiGooglebigquery,
    databricks: SiDatabricks,
};

export function connectorIcon(id: string): ConnectorIconComponent {
    return CONNECTOR_ICONS[id as ConnectorId] ?? DatabaseIcon;
}

/** Official brand color per connector, used to tint its icon everywhere it appears. */
export const CONNECTOR_COLORS: Record<ConnectorId, string> = {
    postgres: '#336791',
    sqlserver: '#CC2927',
    mongodb: '#47A248',
    snowflake: '#29B5E8',
    redshift: '#FF9900',
    bigquery: '#4285F4',
    databricks: '#FF3621',
};

const DEFAULT_ICON_COLOR = 'var(--c-ink-2)';

/** Tinted chip tokens for a connector's icon: colored glyph on a soft matching background. */
export function connectorIconColors(id: string): { fg: string; bg: string; border: string } {
    const hex = CONNECTOR_COLORS[id as ConnectorId];
    if (!hex) {
        return { fg: DEFAULT_ICON_COLOR, bg: 'var(--c-slate-2)', border: 'var(--c-glass-border)' };
    }
    return {
        fg: hex,
        bg: `color-mix(in srgb, ${hex} 14%, transparent)`,
        border: `color-mix(in srgb, ${hex} 28%, transparent)`,
    };
}

/** Human labels for the vendor facet served by the catalog. */
export const VENDOR_LABELS: Record<string, string> = {
    aws: 'AWS',
    gcp: 'Google Cloud',
    microsoft: 'Microsoft',
    snowflake: 'Snowflake',
    databricks: 'Databricks',
    postgresql: 'PostgreSQL',
    mongodb: 'MongoDB',
    other: 'Other',
};

export const CATEGORY_LABELS: Record<string, string> = {
    database: 'Database',
    document_database: 'Document store',
    cloud_warehouse: 'Cloud warehouse',
    file: 'File',
};

/** Display order for category subgroups under "Available". */
export const CATEGORY_ORDER: string[] = [
    'cloud_warehouse',
    'document_database',
    'database',
    'file',
];

export type HealthState = 'ok' | 'error' | 'untested';

export function healthOf(connection: SavedConnection): HealthState {
    if (connection.last_test_status === 'ok') return 'ok';
    if (connection.last_test_status === 'error') return 'error';
    return 'untested';
}

export const HEALTH_LABELS: Record<HealthState, string> = {
    ok: 'Healthy',
    error: 'Failing',
    untested: 'Untested',
};

/** Token trio per health state, so badges and dots stay consistent everywhere. */
export function healthColors(state: HealthState): { fg: string; bg: string; border: string } {
    if (state === 'ok') {
        return {
            fg: 'var(--c-success)',
            bg: 'color-mix(in srgb, var(--c-success) 12%, transparent)',
            border: 'color-mix(in srgb, var(--c-success) 32%, transparent)',
        };
    }
    if (state === 'error') {
        return {
            fg: 'var(--c-danger)',
            bg: 'color-mix(in srgb, var(--c-danger) 12%, transparent)',
            border: 'color-mix(in srgb, var(--c-danger) 32%, transparent)',
        };
    }
    return {
        fg: 'var(--c-mist-fg)',
        bg: 'var(--c-glass)',
        border: 'var(--c-glass-border)',
    };
}

/**
 * The endpoint a connection points at, drawn from the redacted hint the server
 * returns. Never contains secrets — the backend strips them before serialization.
 */
export function endpointLabel(connection: SavedConnection): string {
    const hint = connection.hint || {};
    const str = (key: string): string => {
        const value = hint[key];
        return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
    };

    if (hint.use_default === true) return 'Server default connection';

    const project = str('project_id');
    if (project) {
        const dataset = str('database');
        return dataset ? `${project} · ${dataset}` : project;
    }

    const account = str('account');
    if (account) {
        const warehouse = str('warehouse');
        return warehouse ? `${account} · ${warehouse}` : account;
    }

    const serverHostname = str('server_hostname');
    if (serverHostname) {
        const catalog = str('catalog');
        return catalog ? `${serverHostname} · ${catalog}` : serverHostname;
    }

    const host = str('host');
    if (host) {
        const port = str('port');
        const database = str('database');
        const authority = port ? `${host}:${port}` : host;
        return database ? `${authority}/${database}` : authority;
    }

    return str('cluster_identifier') || str('region') || '—';
}

/** "3 hours ago" style stamp. Absolute date once it stops being useful as relative. */
export function relativeTime(iso?: string | null): string {
    if (!iso) return 'Never';
    // Backend timestamps are naive UTC; mark them as such so the delta is correct.
    const normalized = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
    const then = new Date(normalized).getTime();
    if (Number.isNaN(then)) return 'Never';

    const seconds = Math.round((Date.now() - then) / 1000);
    if (seconds < 60) return 'Just now';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    return new Date(normalized).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Sort key for "last used", treating never-used as oldest. */
export function lastUsedValue(connection: SavedConnection): number {
    const iso = connection.last_used_at;
    if (!iso) return 0;
    const normalized = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
    const value = new Date(normalized).getTime();
    return Number.isNaN(value) ? 0 : value;
}
