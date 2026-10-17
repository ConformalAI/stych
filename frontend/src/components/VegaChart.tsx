import { useEffect, useRef, useState } from 'react';
import embed from 'vega-embed';

export type VegaSpec = Record<string, unknown>;

interface VegaChartProps {
    spec: VegaSpec;
    className?: string;
    ariaLabel?: string;
}

export default function VegaChart({ spec, className, ariaLabel }: VegaChartProps) {
    const containerRef = useRef<HTMLDivElement>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        const el = containerRef.current;
        if (!el || !spec) return;

        let cancelled = false;
        let finalize: (() => void) | undefined;
        setError(null);
        el.replaceChildren();

        embed(el, spec as Parameters<typeof embed>[1], {
            actions: false,
            renderer: 'canvas',
            tooltip: true,
        })
            .then((result) => {
                if (cancelled) {
                    result.finalize();
                    return;
                }
                finalize = () => result.finalize();
            })
            .catch((err: unknown) => {
                if (cancelled) return;
                const message = err instanceof Error ? err.message : 'Failed to render chart';
                setError(message);
            });

        return () => {
            cancelled = true;
            finalize?.();
        };
    }, [spec]);

    return (
        <div className={className} role="img" aria-label={ariaLabel || 'Linkage chart'}>
            {error && <p className="text-xs text-red-500 px-2 py-1">{error}</p>}
            <div ref={containerRef} className="w-full overflow-auto" />
        </div>
    );
}
