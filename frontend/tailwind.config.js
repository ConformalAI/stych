/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    darkMode: 'class',
    theme: {
        extend: {
            /* ── Stych palette (references CSS custom properties) ── */
            colors: {
                // Semantic tokens — always use these over raw Tailwind colours
                paper:  'var(--c-paper)',
                slate2: 'var(--c-slate-2)',
                mist:   'var(--c-mist)',
                ink:    'var(--c-ink)',
                signal: 'var(--c-signal)',

                // Legacy palette — kept for backward-compat with existing classes
                primary: {
                    50:  '#eef2ff',
                    100: '#e0e7ff',
                    200: '#c7d2fe',
                    300: '#a5b4fc',
                    400: '#818cf8',
                    500: '#6366f1',
                    600: '#4f46e5',
                    700: '#4338ca',
                    800: '#3730a3',
                    900: '#312e81',
                },
                navy: {
                    50:  '#e8edf4',
                    100: '#c5d0e0',
                    200: '#8fa5c2',
                    300: '#5a7aa4',
                    400: '#2d5489',
                    500: '#1a3a5c',
                    600: '#132f4c',
                    700: '#0f2238',
                    800: '#0c1929',
                    900: '#07101a',
                },
                background: {
                    DEFAULT: '#08090d',
                    surface: '#10121a',
                    card: 'var(--c-glass)',
                },
                text: {
                    primary:   'var(--c-ink)',
                    secondary: 'var(--c-ink-2)',
                    muted:     'var(--c-mist-fg)',
                    dark:      '#1e293b',
                },
            },
            fontFamily: {
                sans:     ['Hanken Grotesk', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
                display:  ['Space Grotesk',  'Hanken Grotesk', 'sans-serif'],
                mono:     ['JetBrains Mono', 'Fira Code', 'Menlo', 'monospace'],
            },
            borderRadius: {
                'pill': '48px',
            },
            boxShadow: {
                'signal': '0 6px 24px var(--c-signal-glow)',
                'blue':   '0 6px 24px rgba(99, 102, 241, 0.30)',
                'card':   '0 20px 40px rgba(0, 0, 0, 0.45)',
                'glass':  '0 8px 32px rgba(0, 0, 0, 0.36)',
            },
        },
    },
    plugins: [],
}
