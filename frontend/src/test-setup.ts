import { vi } from 'vitest';

// Under Node's experimental global localStorage, `localStorage.getItem` is not a
// usable method, which breaks modules that read it at import time (e.g. the store's
// chatTheme init). Provide a minimal in-memory polyfill for jsdom-based tests.
if (typeof globalThis.localStorage?.getItem !== 'function') {
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
        getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
        setItem: (k: string, v: string) => void store.set(k, String(v)),
        removeItem: (k: string) => void store.delete(k),
        clear: () => store.clear(),
        key: (i: number) => Array.from(store.keys())[i] ?? null,
        get length() {
            return store.size;
        },
    });
}
