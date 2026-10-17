import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { v4 as uuidv4 } from 'uuid';
import { Bot, Sun, Moon } from 'lucide-react';
import { setSessionId as persistSessionId } from '../services/storage';
import { useAuthStore } from '../store/useAuthStore';
import { useStore } from '../store/useStore';
import {
    emailPasswordSignIn,
    emailPasswordSignUp,
    googleSignIn,
    devAuth,
    getCurrentUser,
} from '../services/auth';

type Tab = 'signin' | 'signup';

export default function Login() {
    const navigate = useNavigate();
    const { setUser, isAuthenticated, isCheckingAuth } = useAuthStore();
    const resetSession = useStore((s) => s.resetSession);
    const setSessionId = useStore((s) => s.setSessionId);
    const chatTheme = useStore((s) => s.chatTheme);
    const toggleChatTheme = useStore((s) => s.toggleChatTheme);
    const isDark = chatTheme === 'dark';

    const [tab, setTab] = useState<Tab>('signin');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [error, setError] = useState<string | null>(null);
    const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
    const [isLoading, setIsLoading] = useState(false);

    const isDevMode = import.meta.env.VITE_DEV_MODE === 'true';

    useEffect(() => {
        if (!isCheckingAuth && isAuthenticated) {
            navigate('/chat', { replace: true });
        }
    }, [isCheckingAuth, isAuthenticated, navigate]);

    if (!isCheckingAuth && isAuthenticated) return null;

    const clearErrors = () => {
        setError(null);
        setFieldErrors({});
    };

    const handleEmailPasswordSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        clearErrors();

        if (tab === 'signup' && password !== confirmPassword) {
            setFieldErrors({ confirmPassword: 'Passwords do not match' });
            return;
        }

        setIsLoading(true);
        try {
            const result =
                tab === 'signin'
                    ? await emailPasswordSignIn(email, password)
                    : await emailPasswordSignUp(email, password);

            if (result.status === 'OK') {
                const user = await getCurrentUser();
                resetSession();
                // Pre-create a session ID so Chat never starts with null and the
                // first send always has a valid ID without waiting for a re-render.
                const newSessionId = uuidv4();
                persistSessionId(newSessionId);
                setSessionId(newSessionId);
                setUser(user);
                navigate('/chat', { replace: true });
            } else if (result.status === 'WRONG_CREDENTIALS_ERROR') {
                setError('Incorrect email or password.');
            } else if (result.status === 'FIELD_ERROR') {
                const errs: Record<string, string> = {};
                result.formFields.forEach((f: { id: string; error: string }) => {
                    errs[f.id] = f.error;
                });
                setFieldErrors(errs);
            } else if (
                result.status === 'SIGN_IN_NOT_ALLOWED' ||
                result.status === 'SIGN_UP_NOT_ALLOWED'
            ) {
                setError(
                    (result as any).reason || 'Sign-in is not allowed. Please contact support.'
                );
            } else {
                setError('Something went wrong. Please try again.');
            }
        } catch (err: any) {
            setError(err?.message || 'Authentication failed. Please try again.');
        } finally {
            setIsLoading(false);
        }
    };

    const handleGoogleSignIn = async () => {
        clearErrors();
        setIsLoading(true);
        try {
            await googleSignIn(); // redirects browser — won't reach below
        } catch (err: any) {
            setError(err?.message || 'Could not start Google sign-in.');
            setIsLoading(false);
        }
    };

    const handleDevLogin = async () => {
        clearErrors();
        setIsLoading(true);
        try {
            const authResponse = await devAuth();
            resetSession();
            const newSessionId = uuidv4();
            persistSessionId(newSessionId);
            setSessionId(newSessionId);
            setUser(authResponse.user);
            navigate('/chat', { replace: true });
        } catch (err: any) {
            setError(
                err?.response?.data?.detail || 'Dev auth failed. Is DEV_MODE enabled on backend?'
            );
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div
            className="min-h-screen flex items-center justify-center px-6 transition-colors duration-300 relative"
            style={{ background: 'var(--c-paper)' }}
        >
            {/* Theme toggle — top-right corner */}
            <button
                onClick={toggleChatTheme}
                title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
                className="fixed top-4 right-4 z-50 w-9 h-9 flex items-center justify-center rounded-full border transition-all"
                style={{
                    color: 'var(--c-ink-2)',
                    background: 'var(--c-glass)',
                    borderColor: 'var(--c-glass-border)',
                }}
            >
                {isDark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>

            {/* Subtle dot grid overlay */}
            <div
                className="fixed inset-0 pointer-events-none"
                style={{
                    backgroundImage: `radial-gradient(circle, var(--c-mist-fg) 1px, transparent 1px)`,
                    backgroundSize: '40px 40px',
                    opacity: 0.15,
                }}
            />

            {/* Card */}
            <div
                className="relative max-w-md w-full rounded-2xl p-8"
                style={{
                    background: 'var(--c-slate)',
                    backdropFilter: 'blur(24px)',
                    border: '1px solid var(--c-glass-border)',
                    boxShadow: '0 20px 60px rgba(0,0,0,0.3), 0 0 0 1px var(--c-glass-border)',
                }}
            >
                {/* Top accent stripe */}
                <div
                    className="absolute top-0 left-8 right-8 h-px"
                    style={{
                        background:
                            'linear-gradient(90deg, transparent, var(--c-signal), transparent)',
                    }}
                />

                <div className="relative text-center mb-8">
                    <div
                        className="w-14 h-14 rounded-2xl flex items-center justify-center mx-auto mb-5"
                        style={{
                            background: 'var(--c-signal)',
                            boxShadow: '0 8px 32px var(--c-signal-glow)',
                        }}
                    >
                        <Bot className="w-8 h-8 text-white" />
                    </div>
                    <h1
                        className="text-3xl font-black mb-2"
                        style={{
                            color: 'var(--c-ink)',
                            fontFamily: 'Space Grotesk, sans-serif',
                            letterSpacing: '-0.03em',
                        }}
                    >
                        Welcome to Stych
                    </h1>
                    <p className="text-base" style={{ color: 'var(--c-ink-2)' }}>
                        Sign in to start analyzing your data
                    </p>
                </div>

                {/* Tabs */}
                <div
                    className="relative flex rounded-xl p-1 mb-6"
                    style={{ background: 'var(--c-slate-2)' }}
                >
                    {(['signin', 'signup'] as Tab[]).map((t) => (
                        <button
                            key={t}
                            onClick={() => {
                                setTab(t);
                                clearErrors();
                            }}
                            className="flex-1 py-2 text-sm font-semibold rounded-lg transition-all duration-200"
                            style={
                                tab === t
                                    ? {
                                          background: 'var(--c-signal)',
                                          color: '#fff',
                                          boxShadow: '0 2px 8px var(--c-signal-glow)',
                                      }
                                    : { color: 'var(--c-ink-2)' }
                            }
                        >
                            {t === 'signin' ? 'Sign In' : 'Sign Up'}
                        </button>
                    ))}
                </div>

                {error && (
                    <div className="mb-5 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-400 text-sm text-center">
                        {error}
                    </div>
                )}

                {isLoading ? (
                    <div className="flex items-center justify-center space-x-3 py-8">
                        <div
                            className="w-5 h-5 border-2 border-t-transparent rounded-full animate-spin"
                            style={{
                                borderColor: 'var(--c-signal)',
                                borderTopColor: 'transparent',
                            }}
                        />
                        <span style={{ color: 'var(--c-ink-2)' }}>
                            {tab === 'signin' ? 'Signing in…' : 'Creating account…'}
                        </span>
                    </div>
                ) : (
                    <div className="relative flex flex-col space-y-4">
                        {/* Dev login — dev mode only */}
                        {isDevMode && (
                            <>
                                <button
                                    onClick={handleDevLogin}
                                    className="w-full px-4 py-3 text-white font-semibold rounded-full transition-all duration-200 shadow-lg flex items-center justify-center gap-2"
                                    style={{
                                        background: 'linear-gradient(135deg, #0fb981, #1fd68f)',
                                        boxShadow: '0 4px 20px rgba(15,185,129,0.3)',
                                    }}
                                >
                                    <span className="text-lg">🚀</span>
                                    <span>Dev Login (Skip Auth)</span>
                                </button>
                                <div className="relative">
                                    <div className="absolute inset-0 flex items-center">
                                        <div
                                            className="w-full border-t"
                                            style={{ borderColor: 'var(--c-mist)' }}
                                        />
                                    </div>
                                    <div className="relative flex justify-center text-sm">
                                        <span
                                            className="px-2"
                                            style={{
                                                background: 'var(--c-slate)',
                                                color: 'var(--c-ink-2)',
                                            }}
                                        >
                                            or
                                        </span>
                                    </div>
                                </div>
                            </>
                        )}

                        {/* Email + password form */}
                        <form onSubmit={handleEmailPasswordSubmit} className="space-y-3">
                            <div>
                                <input
                                    type="email"
                                    placeholder="Email address"
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    required
                                    autoComplete="email"
                                    className="input text-sm"
                                />
                                {fieldErrors.email && (
                                    <p className="mt-1 text-red-400 text-xs">{fieldErrors.email}</p>
                                )}
                            </div>

                            <div>
                                <input
                                    type="password"
                                    placeholder="Password"
                                    value={password}
                                    onChange={(e) => setPassword(e.target.value)}
                                    required
                                    autoComplete={
                                        tab === 'signin' ? 'current-password' : 'new-password'
                                    }
                                    className="input text-sm"
                                />
                                {fieldErrors.password && (
                                    <p className="mt-1 text-red-400 text-xs">
                                        {fieldErrors.password}
                                    </p>
                                )}
                            </div>

                            {tab === 'signup' && (
                                <div>
                                    <input
                                        type="password"
                                        placeholder="Confirm password"
                                        value={confirmPassword}
                                        onChange={(e) => setConfirmPassword(e.target.value)}
                                        required
                                        autoComplete="new-password"
                                        className="input text-sm"
                                    />
                                    {fieldErrors.confirmPassword && (
                                        <p className="mt-1 text-red-400 text-xs">
                                            {fieldErrors.confirmPassword}
                                        </p>
                                    )}
                                </div>
                            )}

                            <button
                                type="submit"
                                className="w-full py-3 text-white font-semibold rounded-full transition-all duration-200 text-sm"
                                style={{
                                    background: 'var(--c-signal)',
                                    boxShadow: '0 4px 20px var(--c-signal-glow)',
                                }}
                            >
                                {tab === 'signin' ? 'Sign In' : 'Create Account'}
                            </button>
                        </form>

                        {/* Divider */}
                        <div className="relative">
                            <div className="absolute inset-0 flex items-center">
                                <div
                                    className="w-full border-t"
                                    style={{ borderColor: 'var(--c-mist)' }}
                                />
                            </div>
                            <div className="relative flex justify-center text-sm">
                                <span
                                    className="px-2"
                                    style={{
                                        background: 'var(--c-slate)',
                                        color: 'var(--c-ink-2)',
                                    }}
                                >
                                    or continue with
                                </span>
                            </div>
                        </div>

                        {/* Google sign-in */}
                        <button
                            onClick={handleGoogleSignIn}
                            className="w-full flex items-center justify-center gap-3 px-4 py-3 rounded-full text-sm font-medium transition-all duration-200"
                            style={{
                                background: 'var(--c-glass)',
                                border: '1px solid var(--c-glass-border)',
                                color: 'var(--c-ink)',
                            }}
                        >
                            {/* Google logo */}
                            <svg
                                width="18"
                                height="18"
                                viewBox="0 0 18 18"
                                xmlns="http://www.w3.org/2000/svg"
                            >
                                <path
                                    d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"
                                    fill="#4285F4"
                                />
                                <path
                                    d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"
                                    fill="#34A853"
                                />
                                <path
                                    d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"
                                    fill="#FBBC05"
                                />
                                <path
                                    d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"
                                    fill="#EA4335"
                                />
                            </svg>
                            Sign in with Google
                        </button>
                    </div>
                )}

                <p
                    className="relative text-sm text-center mt-8"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    By signing in, you agree to our Terms of Service
                </p>
            </div>
        </div>
    );
}
