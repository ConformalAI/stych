export function hintString(
    hint: Record<string, unknown> | undefined,
    key: string,
    fallback = ''
): string {
    const value = hint?.[key];
    return typeof value === 'string' ? value : fallback;
}

export function hintNumber(
    hint: Record<string, unknown> | undefined,
    key: string,
    fallback: number
): number {
    const value = hint?.[key];
    return typeof value === 'number' ? value : fallback;
}

export function savedEndpointLabel(hint: Record<string, unknown>): string {
    if (hint.use_default) return 'Default connection from server config';
    const user = hintString(hint, 'user');
    const host =
        hintString(hint, 'host') ||
        hintString(hint, 'server_hostname') ||
        hintString(hint, 'account');
    const database =
        hintString(hint, 'database') ||
        hintString(hint, 'catalog') ||
        hintString(hint, 'project_id');
    const left = user && host ? `${user}@${host}` : host || user;
    const port = hintNumber(hint, 'port', 0);
    const withPort = left && port ? `${left}:${port}` : left;
    return [withPort, database].filter(Boolean).join('/') || 'Saved connection';
}

export const KEEP_SECRET_PLACEHOLDER = 'Leave blank to keep the saved value';

export function isTableInChat(
    filename: string,
    label: string,
    schema: string,
    table: string
): boolean {
    if (!filename.startsWith(`${label}: `)) return false;
    const rest = filename.slice(label.length + 2);
    return rest === table || rest === `${schema}.${table}` || rest.endsWith(`.${schema}.${table}`);
}
