import { LogOut, Sun, Moon, Settings } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useStore } from '../store/useStore';
import { useAuthStore } from '../store/useAuthStore';
import { logout } from '../services/auth';
import { archiveWorkspace } from '../services/api';
import { clearSessionId } from '../services/storage';

interface HeaderProps {
    isConnected: boolean;
}

export default function Header({ isConnected }: HeaderProps) {
    const navigate = useNavigate();
    const chatTheme = useStore((s) => s.chatTheme);
    const toggleChatTheme = useStore((s) => s.toggleChatTheme);
    const sessionId = useStore((s) => s.sessionId);
    const resetSession = useStore((s) => s.resetSession);
    const isDark = chatTheme === 'dark';
    const { user, clearUser } = useAuthStore();

    const handleLogout = async () => {
        // Archive workspace before signing out so files survive the session end.
        if (sessionId) {
            archiveWorkspace(sessionId).catch((err) =>
                console.warn('Workspace archive failed (non-blocking):', err)
            );
        }
        try {
            await logout();
        } catch (err) {
            console.error('Logout error:', err);
        } finally {
            clearSessionId();
            resetSession();
            clearUser();
            navigate('/login');
        }
    };

    return (
        <header
            className="border-b px-5 py-3 flex items-center justify-between shrink-0"
            style={{
                background: 'var(--c-slate)',
                borderColor: 'var(--c-glass-border)',
                boxShadow: '0 1px 0 var(--c-glass-border)',
            }}
        >
            {/* Left: brand */}
            <div className="flex items-center gap-3">
                <span
                    className="text-lg font-black tracking-tight"
                    style={{
                        fontFamily: 'Space Grotesk, sans-serif',
                        letterSpacing: '-0.03em',
                        background:
                            'linear-gradient(135deg, var(--c-signal) 0%, var(--c-ink) 100%)',
                        WebkitBackgroundClip: 'text',
                        WebkitTextFillColor: 'transparent',
                        backgroundClip: 'text',
                    }}
                >
                    Stych
                </span>
                <div className="flex items-center gap-1.5 ml-2">
                    <span
                        className="w-1.5 h-1.5 rounded-full"
                        style={{
                            background: isConnected ? 'var(--c-success)' : 'var(--c-mist-fg)',
                        }}
                    />
                    <span className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                        {isConnected ? 'Connected' : 'Offline'}
                    </span>
                </div>
            </div>

            {/* Right: actions */}
            <div className="flex items-center gap-3">
                {/* Theme toggle */}
                <button
                    onClick={toggleChatTheme}
                    title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
                    className="w-8 h-8 flex items-center justify-center rounded-xl transition-colors"
                    style={{
                        color: 'var(--c-ink-2)',
                        background: 'var(--c-glass)',
                        border: '1px solid var(--c-glass-border)',
                    }}
                >
                    {isDark ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
                </button>

                {user && (
                    <button
                        type="button"
                        onClick={() => navigate('/settings')}
                        className="flex items-center gap-2 rounded-xl px-2 py-1 transition-colors hover:bg-white/5"
                        title="Account settings"
                    >
                        {user.profile_picture ? (
                            <img
                                src={user.profile_picture}
                                alt={user.name || user.email}
                                className="w-7 h-7 rounded-full ring-1 ring-white/20"
                                referrerPolicy="no-referrer"
                            />
                        ) : (
                            <div
                                className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold"
                                style={{
                                    background: 'var(--c-signal-dim)',
                                    color: 'var(--c-signal-text)',
                                }}
                            >
                                {(user.name || user.email || '?')[0].toUpperCase()}
                            </div>
                        )}
                        <span
                            className="text-sm font-medium hidden sm:block"
                            style={{ color: 'var(--c-ink)' }}
                        >
                            {user.name || user.email}
                        </span>
                        <Settings
                            className="w-3.5 h-3.5 hidden sm:block"
                            style={{ color: 'var(--c-mist-fg)' }}
                        />
                    </button>
                )}

                <button
                    onClick={handleLogout}
                    className="flex items-center gap-1.5 text-xs hover:text-red-400 transition-colors"
                    style={{ color: 'var(--c-mist-fg)' }}
                    title="Logout"
                >
                    <LogOut className="w-4 h-4" />
                    <span className="hidden sm:block">Logout</span>
                </button>
            </div>
        </header>
    );
}
