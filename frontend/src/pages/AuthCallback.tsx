import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { v4 as uuidv4 } from 'uuid';
import {
    completeGoogleSignInOnce,
    getCurrentUser,
    getCurrentUserIfSessionExists,
} from '../services/auth';
import { setSessionId as persistSessionId } from '../services/storage';
import { useAuthStore, type AuthUser } from '../store/useAuthStore';
import { useStore } from '../store/useStore';

/**
 * Handles the Google OAuth frontend callback at /oauth/callback/google.
 *
 * Flow:
 *   Google redirects here with ?code=...&state=...
 *   → completeGoogleSignInOnce() sends the code to the backend via SuperTokens
 *   → backend exchanges it for tokens, syncs local user, sets session headers
 *   → we fetch the user profile and land on /chat
 *
 * Google codes are one-time-use. Strict Mode remounts (and a second tab) can
 * make signInAndUp throw after the session already exists — recover via
 * getCurrentUserIfSessionExists instead of showing a false error.
 */
export default function AuthCallback() {
    const navigate = useNavigate();
    const { setUser } = useAuthStore();
    const resetSession = useStore((s) => s.resetSession);
    const setSessionId = useStore((s) => s.setSessionId);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;

        const enterApp = (user: AuthUser) => {
            if (cancelled) return;
            resetSession();
            const newSessionId = uuidv4();
            persistSessionId(newSessionId);
            setSessionId(newSessionId);
            setUser(user);
            navigate('/chat', { replace: true });
        };

        const resumeIfSessionExists = async (): Promise<boolean> => {
            const user = await getCurrentUserIfSessionExists();
            if (!user || cancelled) return false;
            enterApp(user);
            return true;
        };

        const finish = async () => {
            try {
                const result = await completeGoogleSignInOnce();
                if (cancelled) return;

                if (result.status === 'OK') {
                    const user = await getCurrentUser();
                    enterApp(user);
                    return;
                }

                if (await resumeIfSessionExists()) return;
                if (cancelled) return;

                if (result.status === 'NO_EMAIL_GIVEN_BY_PROVIDER') {
                    setError('Google did not share your email. Please use email/password sign-in.');
                } else {
                    setError('Sign-in was not allowed. Please try again.');
                }
            } catch (err: unknown) {
                console.error('OAuth callback error:', err);
                try {
                    if (await resumeIfSessionExists()) return;
                } catch {
                    // Fall through to the original error.
                }
                if (cancelled) return;
                const message =
                    err instanceof Error ? err.message : 'Authentication failed. Please try again.';
                setError(message);
            }
        };

        void finish();
        return () => {
            cancelled = true;
        };
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const bgStyle = { background: 'var(--c-paper)' };

    if (error) {
        return (
            <div className="min-h-screen flex items-center justify-center px-6" style={bgStyle}>
                <div
                    className="max-w-sm w-full text-center space-y-4 rounded-2xl p-8"
                    style={{
                        background: 'var(--c-slate)',
                        border: '1px solid var(--c-glass-border)',
                        boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
                    }}
                >
                    <p className="text-red-400 text-sm">{error}</p>
                    <button
                        onClick={() => navigate('/login', { replace: true })}
                        className="text-sm underline transition-opacity hover:opacity-80"
                        style={{ color: 'var(--c-signal-text)' }}
                    >
                        Back to sign in
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen flex items-center justify-center" style={bgStyle}>
            <div className="flex flex-col items-center space-y-4">
                <div
                    className="w-8 h-8 border-2 border-t-transparent rounded-full animate-spin"
                    style={{ borderColor: 'var(--c-signal)', borderTopColor: 'transparent' }}
                />
                <p className="text-sm" style={{ color: 'var(--c-ink-2)' }}>
                    Completing sign-in…
                </p>
            </div>
        </div>
    );
}
