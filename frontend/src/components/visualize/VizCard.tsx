/**
 * Live chat embed for a persisted visualization (```viz fence).
 *
 * Fetches spec + freshly materialized data by viz_id — nothing is baked into
 * the message, so the chart reflects the current dataset. Clicking the header
 * opens the chart in the Visualize tab for editing.
 */

import { useEffect, useRef, useState } from 'react';
import { ExternalLink, RefreshCw } from 'lucide-react';
import { useStore } from '../../store/useStore';
import { getVisualizationData } from '../../services/viz';
import { sourceRemovedDisplay } from '../../sourceRemoved';
import EChartsRenderer from './EChartsRenderer';
import type { VizData, VizSpec } from './types';

interface VizCardProps {
    vizId: string;
}

type LoadedViz = { spec: VizSpec; data: VizData };
const CACHE_TTL_MS = 30_000;
const materializationCache = new Map<
    string,
    { at: number; eventVersion: number; request: Promise<LoadedViz> }
>();

function materialize(vizId: string, eventVersion = 0): Promise<LoadedViz> {
    const cached = materializationCache.get(vizId);
    if (cached && cached.eventVersion >= eventVersion && Date.now() - cached.at < CACHE_TTL_MS) {
        return cached.request;
    }
    const request = getVisualizationData(vizId).catch((error) => {
        if (materializationCache.get(vizId)?.request === request) {
            materializationCache.delete(vizId);
        }
        throw error;
    });
    materializationCache.set(vizId, { at: Date.now(), eventVersion, request });
    return request;
}

function useNearViewport(ref: React.RefObject<HTMLElement>): boolean {
    const [nearViewport, setNearViewport] = useState(
        () => typeof IntersectionObserver === 'undefined'
    );
    useEffect(() => {
        const node = ref.current;
        if (!node || nearViewport || typeof IntersectionObserver === 'undefined') return;
        const observer = new IntersectionObserver(
            (entries) => {
                if (entries.some((entry) => entry.isIntersecting)) {
                    setNearViewport(true);
                    observer.disconnect();
                }
            },
            { rootMargin: '300px 0px' }
        );
        observer.observe(node);
        return () => observer.disconnect();
    }, [nearViewport, ref]);
    return nearViewport;
}

export function VizCard({ vizId }: VizCardProps) {
    const setOpenVizId = useStore((state) => state.setOpenVizId);
    const matchingEventAt = useStore((state) =>
        state.lastVizEvent?.vizId === vizId ? state.lastVizEvent.at : null
    );
    const cardRef = useRef<HTMLDivElement>(null);
    const lastRequestRef = useRef<{ vizId: string; eventVersion: number } | null>(null);
    const nearViewport = useNearViewport(cardRef);
    const [spec, setSpec] = useState<VizSpec | null>(null);
    const [data, setData] = useState<VizData | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!nearViewport) return;
        const previous =
            lastRequestRef.current?.vizId === vizId ? lastRequestRef.current.eventVersion : 0;
        const eventVersion = matchingEventAt ?? previous;
        if (
            lastRequestRef.current?.vizId === vizId &&
            lastRequestRef.current.eventVersion === eventVersion
        ) {
            return;
        }
        lastRequestRef.current = { vizId, eventVersion };
        let mounted = true;
        setLoading(true);
        materialize(vizId, eventVersion)
            .then((res) => {
                if (!mounted) return;
                setSpec(res.spec);
                setData(res.data);
                setError(null);
            })
            .catch((err) => {
                if (!mounted) return;
                setError(sourceRemovedDisplay(err));
            })
            .finally(() => mounted && setLoading(false));
        return () => {
            mounted = false;
        };
        // Re-fetch once when the agent updates this same viz mid-conversation.
    }, [nearViewport, vizId, matchingEventAt]);

    return (
        <div
            ref={cardRef}
            className="my-3 rounded-xl border overflow-hidden"
            style={{ background: 'var(--c-glass)', borderColor: 'var(--c-glass-border)' }}
        >
            <button
                type="button"
                onClick={() => setOpenVizId(vizId)}
                className="w-full flex items-center justify-between px-3 py-2 border-b text-left"
                style={{ borderColor: 'var(--c-glass-border)' }}
                title="Open in Visualize tab"
            >
                <span className="text-xs font-semibold truncate" style={{ color: 'var(--c-ink)' }}>
                    {spec?.title || 'Chart'}
                </span>
                <span
                    className="flex items-center gap-1 text-[10px] flex-shrink-0"
                    style={{ color: 'var(--c-signal-text)' }}
                >
                    <ExternalLink className="w-3 h-3" /> Edit
                </span>
            </button>
            <div className="p-2" style={{ height: 300 }}>
                {!nearViewport || loading ? (
                    <div
                        className="h-full flex items-center justify-center gap-2 text-xs"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <RefreshCw className="w-3.5 h-3.5 animate-spin" /> Loading chart…
                    </div>
                ) : error || !spec || !data ? (
                    <div
                        className="h-full flex items-center justify-center text-xs"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {error ?? 'Chart unavailable'}
                    </div>
                ) : (
                    // The card header already names the chart — don't repeat it in-canvas.
                    <EChartsRenderer
                        spec={spec}
                        data={data}
                        showTitle={false}
                        className="w-full h-full"
                    />
                )}
            </div>
        </div>
    );
}

/** Renderer for a ```viz fence: `{"viz_id": "…"}` or an array of them. */
export default function VizEmbed({ payload }: { payload: unknown }) {
    const items = Array.isArray(payload) ? payload : [payload];
    const ids = items
        .map((it) => (it && typeof it === 'object' ? (it as any).viz_id : null))
        .filter((id): id is string => typeof id === 'string' && id.length > 0);
    if (ids.length === 0) return null;
    return (
        <>
            {ids.map((id) => (
                <VizCard key={id} vizId={id} />
            ))}
        </>
    );
}
