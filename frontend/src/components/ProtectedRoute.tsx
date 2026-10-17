import { useEffect } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore } from '../store/useAuthStore';
import { getCurrentUser } from '../services/auth';

interface ProtectedRouteProps {
    children: React.ReactNode;
}

/**
 * Route wrapper that redirects to /login if user is not authenticated.
 * Shows a loading spinner while the initial auth check is in progress.
 */
export default function ProtectedRoute({ children }: ProtectedRouteProps) {
    const { isAuthenticated, isCheckingAuth, clearUser, setUser } = useAuthStore();

    useEffect(() => {
        if (!isAuthenticated) return;
        let mounted = true;
        let inFlight = false;

        const revalidateSession = async () => {
            if (!mounted || inFlight) return;
            inFlight = true;
            try {
                const user = await getCurrentUser();
                if (!mounted) return;
                setUser(user);
            } catch {
                if (!mounted) return;
                clearUser();
                // Hard redirect clears stale protected UI immediately.
                window.location.replace('/login');
            } finally {
                inFlight = false;
            }
        };

        // Validate immediately, on focus, and periodically so backend restart
        // is reflected in UI without waiting for a user action.
        revalidateSession();
        const onFocus = () => {
            revalidateSession();
        };
        window.addEventListener('focus', onFocus);
        const interval = window.setInterval(revalidateSession, 30000);

        return () => {
            mounted = false;
            window.removeEventListener('focus', onFocus);
            window.clearInterval(interval);
        };
    }, [isAuthenticated, clearUser, setUser]);

    // Keep full-screen loader only for the initial auth bootstrap.
    // Revalidation runs silently in background to avoid visible UI refresh.
    if (isCheckingAuth) {
        return (
            <div className="min-h-screen flex items-center justify-center">
                <div className="flex flex-col items-center space-y-4">
                    <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
                    <p className="text-white/40 text-sm">Validating session...</p>
                </div>
            </div>
        );
    }

    if (!isAuthenticated) {
        return <Navigate to="/login" replace />;
    }

    return <>{children}</>;
}
