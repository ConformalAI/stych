import { signIn, signUp } from 'supertokens-auth-react/recipe/emailpassword';
import {
    getAuthorisationURLWithQueryParamsAndSetState,
    signInAndUp,
} from 'supertokens-auth-react/recipe/thirdparty';
import { doesSessionExist, signOut } from 'supertokens-auth-react/recipe/session';
import { api } from './api';
import type { AuthUser } from '../store/useAuthStore';

type GoogleSignInResult = Awaited<ReturnType<typeof signInAndUp>>;

/**
 * Google authorization codes are one-time-use. React 18 Strict Mode remounts
 * the callback page in dev and would otherwise POST /auth/signinup twice.
 * Share one in-flight promise per callback query string.
 */
let inFlightGoogleExchange: { key: string; promise: Promise<GoogleSignInResult> } | null = null;

function googleCallbackKey(): string {
    return typeof window === 'undefined' ? '' : window.location.search;
}

/** Test-only: drop the in-flight exchange so cases don't leak across tests. */
export function resetGoogleOAuthExchangeCache(): void {
    inFlightGoogleExchange = null;
}

interface AuthResponse {
    message: string;
    user: AuthUser;
}

interface UpdateProfilePayload {
    handle?: string;
    name?: string;
    profile_picture?: string;
}

export interface UsageByModel {
    model: string;
    request_count: number;
    total_tokens: number;
    estimated_cost_usd: number;
    credits_consumed: number;
}

export interface UsageEvent {
    id: string;
    created_at: string | null;
    provider: string;
    model: string;
    agent_name: string | null;
    session_id: string | null;
    latency_ms: number | null;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_cost_usd: number;
    credits_consumed: number;
}

export interface UsageSummary {
    window_days: number;
    request_count: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    estimated_cost_usd: number;
    credits_consumed: number;
    by_model: UsageByModel[];
    recent_events: UsageEvent[];
}

export interface UsageSessionSummary {
    session_id: string;
    request_count: number;
    total_tokens: number;
    estimated_cost_usd: number;
    credits_consumed: number;
    first_event_at: string | null;
    last_event_at: string | null;
    is_anonymous?: boolean;
    title?: string | null;
}

export interface UsageSessionList {
    window_days: number;
    sessions: UsageSessionSummary[];
}

export interface SessionSummary {
    session_id: string;
    title: string | null;
    created_at: string | null;
    last_activity_at: string | null;
    request_count: number;
    total_tokens: number;
    estimated_cost_usd: number;
    credits_consumed: number;
    is_anonymous?: boolean;
}

export interface SessionList {
    window_days?: number | null;
    sessions: SessionSummary[];
    has_more?: boolean;
    next_before?: string | null;
    next_before_id?: string | null;
}

export type UsageEventSortBy =
    | 'created_at'
    | 'total_tokens'
    | 'credits_consumed'
    | 'estimated_cost_usd';
export type SortOrder = 'asc' | 'desc';

export interface UsageEventsPage {
    window_days: number;
    page: number;
    page_size: number;
    total_count: number;
    items: UsageEvent[];
}

/**
 * Email + password sign-in via SuperTokens.
 * Returns the ST result directly — caller handles status ("OK" / "WRONG_CREDENTIALS_ERROR" etc.)
 */
export const emailPasswordSignIn = (email: string, password: string) =>
    signIn({
        formFields: [
            { id: 'email', value: email },
            { id: 'password', value: password },
        ],
    });

/**
 * Email + password sign-up via SuperTokens.
 * Returns the ST result — caller handles status ("OK" / "FIELD_ERROR" etc.)
 */
export const emailPasswordSignUp = (email: string, password: string) =>
    signUp({
        formFields: [
            { id: 'email', value: email },
            { id: 'password', value: password },
        ],
    });

/**
 * Start Google OAuth flow.
 * Redirects the browser to Google; frontend handoff lands at /oauth/callback/google.
 */
export const googleSignIn = async (): Promise<void> => {
    const authUrl = await getAuthorisationURLWithQueryParamsAndSetState({
        thirdPartyId: 'google',
        // Keep this path outside /auth so Vite's /auth proxy does not swallow
        // the frontend callback route during local development.
        frontendRedirectURI: `${window.location.origin}/oauth/callback/google`,
    });
    window.location.assign(authUrl);
};

/**
 * Complete the Google OAuth callback — call this at /oauth/callback/google.
 * SuperTokens reads the ?code= param, exchanges it with the backend, and creates a session.
 */
export const completeGoogleSignIn = () => signInAndUp();

/**
 * Like completeGoogleSignIn, but concurrent calls with the same callback URL
 * share one SuperTokens exchange. A new ?code= starts a fresh exchange.
 */
export function completeGoogleSignInOnce(): Promise<GoogleSignInResult> {
    const key = googleCallbackKey();
    if (inFlightGoogleExchange && inFlightGoogleExchange.key === key) {
        return inFlightGoogleExchange.promise;
    }
    const promise = signInAndUp();
    inFlightGoogleExchange = { key, promise };
    return promise;
}

/**
 * Fetch current user profile from Stych backend (requires active ST session cookie).
 */
export const getCurrentUser = async (): Promise<AuthUser> => {
    const response = await api.get('/api/auth/me');
    return response.data;
};

/**
 * If SuperTokens already has tokens (first of two callback exchanges, or
 * another tab finished login), return the profile; otherwise null.
 */
export async function getCurrentUserIfSessionExists(): Promise<AuthUser | null> {
    try {
        if (!(await doesSessionExist())) return null;
        return await getCurrentUser();
    } catch {
        return null;
    }
}

/**
 * Update editable profile fields for the authenticated user.
 */
export const updateCurrentUser = async (payload: UpdateProfilePayload): Promise<AuthUser> => {
    const response = await api.patch('/api/auth/me', payload);
    return response.data;
};

/**
 * Upload a profile image file. The backend resizes it to 256×256 and stores
 * the result as a base64 data URI in profile_picture.
 */
export const uploadAvatar = async (file: File): Promise<AuthUser> => {
    const form = new FormData();
    form.append('file', file);
    const response = await api.post('/api/auth/avatar', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
    });
    return response.data;
};

export const changePassword = async (
    currentPassword: string,
    newPassword: string
): Promise<{ message: string }> => {
    const response = await api.post('/api/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
    });
    return response.data;
};

export const getUsageSummary = async (days = 30): Promise<UsageSummary> => {
    const response = await api.get('/api/auth/usage/summary', { params: { days } });
    return response.data;
};

export const getUsageSessionSummaries = async (
    days = 30,
    limit = 20
): Promise<UsageSessionList> => {
    const response = await api.get('/api/auth/usage/sessions', { params: { days, limit } });
    return response.data;
};

export const getSessionSummaries = async (params?: {
    days?: number;
    limit?: number;
    before?: string;
    before_id?: string;
}): Promise<SessionList> => {
    const response = await api.get('/api/sessions', { params });
    return response.data;
};

export const getUsageEvents = async (params?: {
    days?: number;
    page?: number;
    page_size?: number;
    sort_by?: UsageEventSortBy;
    sort_order?: SortOrder;
}): Promise<UsageEventsPage> => {
    const response = await api.get('/api/auth/usage/events', { params });
    return response.data;
};

export const getUsageEvent = async (eventId: string): Promise<UsageEvent> => {
    const response = await api.get(`/api/auth/usage/events/item/${eventId}`);
    return response.data;
};

export const exportUsageEventsCsv = async (params?: {
    days?: number;
    sort_by?: UsageEventSortBy;
    sort_order?: SortOrder;
}): Promise<void> => {
    const response = await api.get('/api/auth/usage/events/export.csv', {
        params,
        responseType: 'blob',
    });
    const contentDisposition = response.headers['content-disposition'] || '';
    const filenameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
    const filename = filenameMatch?.[1] ?? 'usage_events.csv';

    const url = URL.createObjectURL(new Blob([response.data]));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
};

/**
 * Sign out — revokes the SuperTokens session (clears session cookies).
 */
export const logout = async (): Promise<void> => {
    await signOut();
};

/**
 * Development-only instant login (DEV_MODE=true on backend only).
 * Creates a SuperTokens session server-side; ST middleware sets cookies on the response.
 */
export const devAuth = async (): Promise<AuthResponse> => {
    const response = await api.post('/api/auth/dev');
    return response.data;
};
