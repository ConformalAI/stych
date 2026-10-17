import { useEffect, useRef } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { doesSessionExist } from 'supertokens-auth-react/recipe/session';
import Landing from './pages/Landing';
import Login from './pages/Login';
import Chat from './pages/Chat';
import Connectors from './pages/Connectors';
import AuthCallback from './pages/AuthCallback';
import Settings from './pages/Settings';
import ProtectedRoute from './components/ProtectedRoute';
import AppShell from './components/shell/AppShell';
import { useAuthStore } from './store/useAuthStore';
import { getCurrentUser } from './services/auth';
import { getAvailableModels } from './services/api';
import { useStore } from './store/useStore';

function App() {
    const { setUser, clearUser, setIsCheckingAuth, isAuthenticated } = useAuthStore();
    const setAvailableModels = useStore((s) => s.setAvailableModels);
    const setSelectedModel = useStore((s) => s.setSelectedModel);
    const chatTheme = useStore((s) => s.chatTheme);

    // Sync chatTheme to <html> data-theme attribute and legacy dark class
    useEffect(() => {
        const root = document.documentElement;
        root.setAttribute('data-theme', chatTheme);
        // Keep legacy dark class for any Tailwind dark: utilities still in use
        if (chatTheme === 'dark') {
            root.classList.add('dark');
        } else {
            root.classList.remove('dark');
        }
    }, [chatTheme]);

    // Keep a ref so the interval callback sees the latest isAuthenticated value
    // without being listed as a useEffect dependency (which would re-run the
    // entire effect — and flash the auth spinner — every time auth state changes).
    const isAuthenticatedRef = useRef(isAuthenticated);
    useEffect(() => {
        isAuthenticatedRef.current = isAuthenticated;
    }, [isAuthenticated]);

    useEffect(() => {
        // On app load: ask SuperTokens if a valid session cookie exists.
        // If yes, fetch the user profile from /api/auth/me.
        // If no, clear local auth state so ProtectedRoute redirects to /login.
        const checkAuth = async (silent = false) => {
            try {
                if (!silent) setIsCheckingAuth(true);
                const sessionActive = await doesSessionExist();
                if (sessionActive) {
                    const user = await getCurrentUser();
                    setUser(user);
                } else {
                    clearUser();
                }
            } catch (err: unknown) {
                // On the initial (non-silent) check a network error should not
                // sign the user out — we just don't know yet. On silent background
                // refreshes we also leave auth state unchanged so a transient
                // network hiccup does not redirect to /login.
                if (!silent) {
                    const isNetworkErr =
                        err instanceof TypeError &&
                        (err.message.includes('Failed to fetch') ||
                            err.message.includes('NetworkError'));
                    if (!isNetworkErr) {
                        clearUser();
                    }
                }
            } finally {
                if (!silent) setIsCheckingAuth(false);
            }
        };
        checkAuth(false);

        const onFocus = () => {
            checkAuth(true);
        };
        window.addEventListener('focus', onFocus);
        const timer = window.setInterval(() => {
            // Keep UI auth state in sync; only poll when authenticated.
            // Uses a ref so this interval does not force effect re-runs.
            if (isAuthenticatedRef.current) checkAuth(true);
        }, 15000);

        return () => {
            window.removeEventListener('focus', onFocus);
            window.clearInterval(timer);
        };
    }, [setUser, clearUser, setIsCheckingAuth]);

    // Fetch available models once per login; clear them on logout.
    // Runs whenever auth state flips — not on every session or page navigation.
    useEffect(() => {
        if (isAuthenticated) {
            getAvailableModels()
                .then((data) => {
                    setAvailableModels(data.models);
                    // Sync the user's preferred model so the chat uses it by
                    // default without requiring the user to visit Settings first.
                    if (data.preferred_model) setSelectedModel(data.preferred_model);
                })
                .catch((err) => {
                    console.warn('Failed to load available models:', err);
                });
        } else {
            setAvailableModels([]);
            setSelectedModel(null);
        }
    }, [isAuthenticated, setAvailableModels, setSelectedModel]);

    return (
        <BrowserRouter>
            <Routes>
                <Route path="/" element={<Landing />} />
                <Route path="/login" element={<Login />} />
                {/* Google OAuth frontend callback handoff */}
                <Route path="/oauth/callback/google" element={<AuthCallback />} />
                {/* Signed-in surfaces share one frame: primary rail + header. */}
                <Route
                    element={
                        <ProtectedRoute>
                            <AppShell />
                        </ProtectedRoute>
                    }
                >
                    <Route path="/chat" element={<Chat />} />
                    <Route path="/connectors" element={<Connectors />} />
                </Route>
                <Route
                    path="/settings"
                    element={
                        <ProtectedRoute>
                            <Settings />
                        </ProtectedRoute>
                    }
                />
                <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
        </BrowserRouter>
    );
}

export default App;
