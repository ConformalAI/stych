import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const signInAndUp = vi.fn();
const doesSessionExist = vi.fn();

vi.mock('supertokens-auth-react/recipe/thirdparty', () => ({
    getAuthorisationURLWithQueryParamsAndSetState: vi.fn(),
    signInAndUp: (...args: unknown[]) => signInAndUp(...args),
}));

vi.mock('supertokens-auth-react/recipe/emailpassword', () => ({
    signIn: vi.fn(),
    signUp: vi.fn(),
}));

vi.mock('supertokens-auth-react/recipe/session', () => ({
    signOut: vi.fn(),
    doesSessionExist: (...args: unknown[]) => doesSessionExist(...args),
}));

vi.mock('./api', () => ({
    api: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

import { api } from './api';
import {
    completeGoogleSignInOnce,
    getCurrentUserIfSessionExists,
    resetGoogleOAuthExchangeCache,
} from './auth';

describe('completeGoogleSignInOnce', () => {
    beforeEach(() => {
        resetGoogleOAuthExchangeCache();
        signInAndUp.mockReset();
        window.history.replaceState({}, '', '/oauth/callback/google?code=abc&state=s1');
    });

    afterEach(() => {
        resetGoogleOAuthExchangeCache();
    });

    it('shares one SuperTokens exchange across concurrent calls with the same callback URL', async () => {
        let resolveFirst: (value: { status: string }) => void = () => undefined;
        signInAndUp.mockReturnValue(
            new Promise((resolve) => {
                resolveFirst = resolve;
            })
        );

        const first = completeGoogleSignInOnce();
        const second = completeGoogleSignInOnce();

        expect(signInAndUp).toHaveBeenCalledTimes(1);

        resolveFirst({ status: 'OK' });
        await expect(first).resolves.toEqual({ status: 'OK' });
        await expect(second).resolves.toEqual({ status: 'OK' });
        expect(signInAndUp).toHaveBeenCalledTimes(1);
    });

    it('starts a new exchange when the callback code changes', async () => {
        signInAndUp.mockResolvedValueOnce({ status: 'OK' }).mockResolvedValueOnce({
            status: 'OK',
        });

        await completeGoogleSignInOnce();
        window.history.replaceState({}, '', '/oauth/callback/google?code=other&state=s2');
        await completeGoogleSignInOnce();

        expect(signInAndUp).toHaveBeenCalledTimes(2);
    });
});

describe('getCurrentUserIfSessionExists', () => {
    beforeEach(() => {
        doesSessionExist.mockReset();
        vi.mocked(api.get).mockReset();
    });

    it('returns the profile when a SuperTokens session already exists', async () => {
        doesSessionExist.mockResolvedValue(true);
        vi.mocked(api.get).mockResolvedValue({
            data: { id: 'u1', email: 'a@b.com' },
        });

        await expect(getCurrentUserIfSessionExists()).resolves.toEqual({
            id: 'u1',
            email: 'a@b.com',
        });
    });

    it('returns null when no session exists', async () => {
        doesSessionExist.mockResolvedValue(false);

        await expect(getCurrentUserIfSessionExists()).resolves.toBeNull();
        expect(api.get).not.toHaveBeenCalled();
    });

    it('returns null when the profile request fails', async () => {
        doesSessionExist.mockResolvedValue(true);
        vi.mocked(api.get).mockRejectedValue(new Error('boom'));

        await expect(getCurrentUserIfSessionExists()).resolves.toBeNull();
    });
});
