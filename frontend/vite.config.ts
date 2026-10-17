import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, process.cwd(), '')

    return {
        plugins: [react()],
        resolve: {
            alias: {
                '@': path.resolve(__dirname, './src'),
            },
        },
        server: {
            port: 5173,
            host: true,
            allowedHosts: env.VITE_ALLOWED_HOSTS
                ? env.VITE_ALLOWED_HOSTS.split(',').map(h => h.trim())
                : [],
            proxy: {
                // Application API
                '/api': {
                    target: env.VITE_PROXY_TARGET || 'http://localhost:8000',
                    changeOrigin: true,
                },
                // SuperTokens Core routes — must be proxied so session cookies
                // are set on the frontend origin (avoids cross-origin cookie issues)
                '/auth': {
                    target: env.VITE_PROXY_TARGET || 'http://localhost:8000',
                    changeOrigin: true,
                },
            },
        },
    }
})
