import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        // jsdom provides browser globals (FormData, File, Blob) so axios's
        // FormData handling behaves as it does in the real app.
        environment: 'jsdom',
        globals: true,
        setupFiles: ['src/test-setup.ts'],
        include: ['src/**/*.{test,spec}.{ts,tsx}'],
    },
});
