import Session from 'supertokens-auth-react/recipe/session';

/**
 * Auth headers for SuperTokens header-based sessions (st-access-token).
 * Use on raw fetch() calls; axios uses the request interceptor in api.ts.
 */
export async function getAuthHeaders(): Promise<Record<string, string>> {
    const headers: Record<string, string> = {};
    try {
        const exists = await Session.doesSessionExist();
        if (!exists) return headers;
        const token = await Session.getAccessToken();
        if (token) {
            headers['st-access-token'] = token;
        }
    } catch {
        // Not signed in or SDK not ready — caller handles 401.
    }
    return headers;
}

export type IntegrationOAuthMessage = {
    type: 'integration_oauth';
    serverId: string;
    status: 'success' | 'error';
    message: string;
};

export function parseIntegrationOAuthMessage(
    data: unknown,
    serverId: string
): IntegrationOAuthMessage | null {
    if (!data || typeof data !== 'object') return null;
    const msg = data as Partial<IntegrationOAuthMessage>;
    if (msg.type !== 'integration_oauth' || msg.serverId !== serverId) return null;
    if (msg.status !== 'success' && msg.status !== 'error') return null;
    return {
        type: 'integration_oauth',
        serverId: msg.serverId,
        status: msg.status,
        message: typeof msg.message === 'string' ? msg.message : '',
    };
}
