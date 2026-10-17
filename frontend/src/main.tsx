import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import SuperTokens from 'supertokens-auth-react';
import EmailPasswordReact from 'supertokens-auth-react/recipe/emailpassword';
import ThirdPartyReact from 'supertokens-auth-react/recipe/thirdparty';
import SessionReact from 'supertokens-auth-react/recipe/session';
import App from './App';
import ErrorBoundary from './components/ErrorBoundary';
import './index.css';

// Initialise SuperTokens once before the React tree mounts.
// apiDomain = the URL the browser uses for /auth/* routes (proxied via Vite in dev).
// websiteDomain = the URL the browser uses for the frontend.
SuperTokens.init({
    appInfo: {
        appName: 'Stych',
        apiDomain: import.meta.env.VITE_SUPERTOKENS_API_DOMAIN || 'http://localhost:5173',
        websiteDomain: import.meta.env.VITE_SUPERTOKENS_WEBSITE_DOMAIN || 'http://localhost:5173',
        apiBasePath: '/auth',
        websiteBasePath: '/auth',
    },
    recipeList: [
        EmailPasswordReact.init(),
        ThirdPartyReact.init({
            signInAndUpFeature: {
                providers: [{ id: 'google', name: 'Google' }],
            },
        }),
        SessionReact.init({
            // Header-based sessions work correctly through HTTP proxies (Vite/nginx)
            // and across different origins (tunnel, localhost, production) without
            // cookie SameSite / Secure restrictions.
            tokenTransferMethod: 'header',
        }),
    ],
});

const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            refetchOnWindowFocus: false,
            retry: 1,
        },
    },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
        <ErrorBoundary>
            <QueryClientProvider client={queryClient}>
                <App />
            </QueryClientProvider>
        </ErrorBoundary>
    </React.StrictMode>
);
