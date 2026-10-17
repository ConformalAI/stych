import { StrictMode, act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthStore, type AuthUser } from '../store/useAuthStore';

const completeGoogleSignInOnce = vi.fn();
const getCurrentUser = vi.fn();
const getCurrentUserIfSessionExists = vi.fn();
const persistSessionId = vi.fn();
const resetSession = vi.fn();
const setStoreSessionId = vi.fn();

vi.mock('../services/auth', () => ({
    completeGoogleSignInOnce: (...args: unknown[]) => completeGoogleSignInOnce(...args),
    getCurrentUser: (...args: unknown[]) => getCurrentUser(...args),
    getCurrentUserIfSessionExists: (...args: unknown[]) => getCurrentUserIfSessionExists(...args),
}));

vi.mock('../services/storage', () => ({
    setSessionId: (...args: unknown[]) => persistSessionId(...args),
}));

vi.mock('../store/useStore', () => ({
    useStore: (
        selector: (s: { resetSession: () => void; setSessionId: (id: string) => void }) => unknown
    ) => selector({ resetSession, setSessionId: setStoreSessionId }),
}));

vi.mock('uuid', () => ({
    v4: () => 'session-from-oauth',
}));

import AuthCallback from './AuthCallback';

const sampleUser: AuthUser = {
    id: 'u1',
    email: 'user@example.com',
    handle: null,
    name: 'User',
    profile_picture: null,
    is_active: true,
    can_change_password: false,
    created_at: null,
};

describe('AuthCallback', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    const renderCallback = async () => {
        await act(async () =>
            root.render(
                <StrictMode>
                    <MemoryRouter initialEntries={['/oauth/callback/google?code=abc']}>
                        <Routes>
                            <Route path="/oauth/callback/google" element={<AuthCallback />} />
                            <Route path="/chat" element={<div>chat page</div>} />
                            <Route path="/login" element={<div>login page</div>} />
                        </Routes>
                    </MemoryRouter>
                </StrictMode>
            )
        );
    };

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
        useAuthStore.setState({
            user: null,
            isAuthenticated: false,
            isCheckingAuth: false,
        });
        completeGoogleSignInOnce.mockReset();
        getCurrentUser.mockReset();
        getCurrentUserIfSessionExists.mockReset();
        persistSessionId.mockReset();
        resetSession.mockReset();
        setStoreSessionId.mockReset();
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('lands on chat after a successful Google exchange', async () => {
        completeGoogleSignInOnce.mockResolvedValue({ status: 'OK' });
        getCurrentUser.mockResolvedValue(sampleUser);

        await renderCallback();
        await act(async () => {
            await Promise.resolve();
        });

        expect(container.textContent).toContain('chat page');
        expect(useAuthStore.getState().isAuthenticated).toBe(true);
        expect(persistSessionId).toHaveBeenCalledWith('session-from-oauth');
    });

    it('recovers to chat when SuperTokens returns a non-OK status but a session exists', async () => {
        completeGoogleSignInOnce.mockResolvedValue({ status: 'SIGN_IN_UP_NOT_ALLOWED' });
        getCurrentUserIfSessionExists.mockResolvedValue(sampleUser);

        await renderCallback();
        await act(async () => {
            await Promise.resolve();
        });

        expect(container.textContent).toContain('chat page');
        expect(container.textContent).not.toContain('Sign-in was not allowed');
    });

    it('recovers to chat when the exchange throws but a session already exists', async () => {
        completeGoogleSignInOnce.mockRejectedValue(new Error('Auth state verification failed'));
        getCurrentUserIfSessionExists.mockResolvedValue(sampleUser);

        await renderCallback();
        await act(async () => {
            await Promise.resolve();
        });

        expect(container.textContent).toContain('chat page');
        expect(container.textContent).not.toContain('Back to sign in');
        expect(useAuthStore.getState().user?.email).toBe('user@example.com');
    });

    it('shows the error card only when the exchange fails and no session exists', async () => {
        completeGoogleSignInOnce.mockRejectedValue(
            new Error('No valid auth state present in session storage')
        );
        getCurrentUserIfSessionExists.mockResolvedValue(null);

        await renderCallback();
        await act(async () => {
            await Promise.resolve();
        });

        expect(container.textContent).toContain('No valid auth state present in session storage');
        expect(container.textContent).toContain('Back to sign in');
        expect(container.textContent).not.toContain('chat page');
    });
});
