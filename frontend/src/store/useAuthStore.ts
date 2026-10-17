import { create } from 'zustand';

export interface AuthUser {
    id: string;
    email: string;
    handle: string | null;
    name: string | null;
    profile_picture: string | null;
    is_active: boolean;
    can_change_password: boolean;
    created_at: string | null;
}

interface AuthState {
    // User data
    user: AuthUser | null;
    isAuthenticated: boolean;

    // Loading state (for initial auth check)
    isCheckingAuth: boolean;

    // Actions
    setUser: (user: AuthUser) => void;
    clearUser: () => void;
    setIsCheckingAuth: (checking: boolean) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
    user: null,
    isAuthenticated: false,
    isCheckingAuth: true, // true initially - we check on app load

    setUser: (user) =>
        set({
            user,
            isAuthenticated: true,
            isCheckingAuth: false,
        }),

    clearUser: () =>
        set({
            user: null,
            isAuthenticated: false,
            isCheckingAuth: false,
        }),

    setIsCheckingAuth: (checking) => set({ isCheckingAuth: checking }),
}));
