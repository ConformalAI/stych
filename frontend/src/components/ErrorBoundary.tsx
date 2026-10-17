import { Component, ErrorInfo, ReactNode } from 'react';

interface Props {
    children: ReactNode;
    fallback?: ReactNode;
}

interface State {
    hasError: boolean;
    error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
    constructor(props: Props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error: Error): State {
        return { hasError: true, error };
    }

    componentDidCatch(error: Error, info: ErrorInfo) {
        console.error('Uncaught render error:', error, info.componentStack);
    }

    render() {
        if (this.state.hasError) {
            if (this.props.fallback) {
                return this.props.fallback;
            }
            return (
                <div className="flex min-h-screen items-center justify-center bg-gray-950 text-white">
                    <div className="max-w-md rounded-xl border border-gray-700 bg-gray-900 p-8 text-center shadow-xl">
                        <h1 className="mb-2 text-xl font-semibold text-red-400">
                            Something went wrong
                        </h1>
                        <p className="mb-6 text-sm text-gray-400">
                            An unexpected error occurred. Refreshing the page usually fixes it.
                        </p>
                        <button
                            onClick={() => window.location.reload()}
                            className="rounded-lg px-5 py-2 text-sm font-medium text-white transition"
                            style={{ background: 'var(--c-signal)' }}
                        >
                            Reload page
                        </button>
                    </div>
                </div>
            );
        }

        return this.props.children;
    }
}

export default ErrorBoundary;
