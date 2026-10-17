import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph2D, {
    type ForceGraphMethods,
    type NodeObject,
    type LinkObject,
} from 'react-force-graph-2d';
import {
    ArrowDownUp,
    Brain,
    ChevronDown,
    ChevronRight,
    Clock,
    Database,
    FileJson,
    FileSpreadsheet,
    FileText,
    Layers,
    Link2,
    Loader2,
    MessageSquare,
    RefreshCw,
    Search,
    Sparkles,
    Table,
    Tag,
    Trash2,
    X,
    Zap,
} from 'lucide-react';
import {
    deleteMemoryEntry,
    getMemoryGraph,
    listMemoryEntries,
    type MemoryEntry,
    type MemoryGraphEdge,
    type MemoryGraphNode,
} from '../services/api';
import { getUsageSessionSummaries } from '../services/auth';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type MemoryManagerTab = 'overview' | 'browse';
type MemoryTypeFilter = 'all' | 'experience' | 'world' | 'observation';

interface MemoryManagerProps {
    /** Fired when the total count changes so the parent status card can refresh. */
    onBankChanged?: () => void;
    /** Whether the full Hindsight stack is available. */
    hindsightEnabled: boolean;
}

// ---------------------------------------------------------------------------
// Knowledge Graph (react-force-graph-2d)
// ---------------------------------------------------------------------------

// Solid canvas node colors keyed on fact_type.
const NODE_FILL: Record<string, string> = {
    experience: '#34d399',
    world: '#63b3ed',
    observation: '#fbbf24',
};

// Extra fields only — react-force-graph merges these with NodeObject/LinkObject
// (which supply id/x/y/vx/vy/fx/fy and source/target).
interface GNode {
    id: string;
    label: string;
    text: string;
    fact_type: string;
}
interface GLink {
    link_type: string;
    weight: number;
}

/**
 * Obsidian-style knowledge graph.
 *
 * Rendered with react-force-graph-2d (canvas + d3-force). d3-force supplies proper
 * alpha cooling and per-node velocity decay, so the simulation settles instead of
 * oscillating; the library provides wheel-zoom, drag-to-pan, and node dragging that
 * only re-heats locally. Dragging a node pins it (fx/fy) so the rest of the graph
 * stays put — grabbing a node no longer re-explodes the whole layout.
 */
function KnowledgeGraph({
    nodes: rawNodes,
    edges: rawEdges,
    onNodeClick,
}: {
    nodes: MemoryGraphNode[];
    edges: MemoryGraphEdge[];
    onNodeClick?: (node: { id: string; label: string }) => void;
}) {
    const containerRef = useRef<HTMLDivElement>(null);
    const fgRef = useRef<ForceGraphMethods<GNode, GLink>>();
    const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
    const [hoverId, setHoverId] = useState<string | null>(null);
    const didFitRef = useRef(false);

    // Fresh node/link objects each time the source data changes. react-force-graph
    // mutates these in place (x/y/vx/vy), so we must not reuse the API objects.
    const graphData = useMemo(
        () => ({
            nodes: rawNodes.map((n) => ({
                id: n.id,
                label: n.label,
                text: n.text,
                fact_type: n.fact_type,
            })) as NodeObject<GNode>[],
            links: rawEdges.map((e) => ({
                source: e.source,
                target: e.target,
                link_type: e.link_type,
                weight: e.weight,
            })) as LinkObject<GNode, GLink>[],
        }),
        [rawNodes, rawEdges]
    );

    // Adjacency (by id) for hover highlighting of neighbors.
    const neighbors = useMemo(() => {
        const map = new Map<string, Set<string>>();
        const link = (a: string, b: string) => {
            const s = map.get(a);
            if (s) s.add(b);
            else map.set(a, new Set([b]));
        };
        for (const e of rawEdges) {
            link(e.source, e.target);
            link(e.target, e.source);
        }
        return map;
    }, [rawEdges]);

    // Track container size so the canvas fills its card responsively.
    useEffect(() => {
        const el = containerRef.current;
        if (!el) return;
        const ro = new ResizeObserver(() => {
            setSize({ w: el.clientWidth, h: el.clientHeight });
        });
        ro.observe(el);
        setSize({ w: el.clientWidth, h: el.clientHeight });
        return () => ro.disconnect();
    }, []);

    // The <ForceGraph2D> is gated on `ready` (a measured width) — until then
    // fgRef.current is undefined. Depend on `ready` too so the force tuning is
    // (re)applied the moment the graph actually mounts, not just when data changes.
    const ready = size.w > 0;

    // Tune the d3 forces for open, Obsidian-like spacing, then re-energize the
    // simulation so it re-settles with them. Without the explicit reheat, the
    // library has already run its warmup ticks with the DEFAULT charge (-30) and
    // the graph stays clustered — `d3Force('charge')` is only a getter, so
    // mutating its strength does not restart the cooldown on its own.
    useEffect(() => {
        const fg = fgRef.current;
        if (!fg || !ready) return;
        // ~1.5x more open spacing than the initial tuning: stronger repulsion +
        // longer rest length so nodes breathe like an Obsidian graph.
        fg.d3Force('charge')?.strength(-240).distanceMax(900);
        fg.d3Force('link')?.distance(72);
        fg.d3ReheatSimulation();
        didFitRef.current = false;
    }, [graphData, ready]);

    const isHighlighted = (id: string) =>
        hoverId != null && (id === hoverId || !!neighbors.get(hoverId)?.has(id));

    const linkHot = (l: LinkObject<GNode, GLink>) => {
        if (hoverId == null) return false;
        const s = typeof l.source === 'object' ? (l.source as GNode).id : (l.source as string);
        const t = typeof l.target === 'object' ? (l.target as GNode).id : (l.target as string);
        return s === hoverId || t === hoverId;
    };

    if (rawNodes.length === 0) {
        return (
            <div
                className="flex flex-col items-center justify-center text-center gap-3 rounded-xl border px-6"
                style={{
                    height: 'clamp(320px, 46vh, 560px)',
                    borderColor: 'var(--c-glass-border)',
                    color: 'var(--c-ink-2)',
                }}
            >
                <div
                    className="w-14 h-14 rounded-2xl grid place-items-center"
                    style={{
                        background: 'var(--c-signal-dim)',
                        color: 'var(--c-signal-text)',
                    }}
                >
                    <Brain className="w-7 h-7" />
                </div>
                <div className="space-y-1 max-w-xs">
                    <p
                        className="text-sm font-semibold"
                        style={{
                            color: 'var(--c-ink)',
                            fontFamily: "'Space Grotesk', sans-serif",
                        }}
                    >
                        Your knowledge graph is empty
                    </p>
                    <p className="text-xs leading-relaxed">
                        Start chatting with your data — Stych extracts facts as you go and weaves
                        them into a graph you can explore here.
                    </p>
                </div>
            </div>
        );
    }

    return (
        <div
            ref={containerRef}
            className="w-full rounded-xl overflow-hidden"
            style={{
                height: 'clamp(360px, 52vh, 620px)',
                background: 'var(--c-glass)',
                cursor: 'grab',
            }}
        >
            {ready && (
                <ForceGraph2D<GNode, GLink>
                    ref={fgRef}
                    width={size.w}
                    height={size.h}
                    graphData={graphData}
                    backgroundColor="rgba(0,0,0,0)"
                    // Physics: cooling + velocity decay so it settles and stays settled.
                    cooldownTicks={220}
                    warmupTicks={40}
                    d3VelocityDecay={0.4}
                    d3AlphaDecay={0.03}
                    onEngineStop={() => {
                        if (!didFitRef.current) {
                            didFitRef.current = true;
                            fgRef.current?.zoomToFit(500, 40);
                        }
                    }}
                    // Nodes
                    nodeRelSize={4}
                    nodeVal={(n) => (isHighlighted(n.id) ? 2.4 : 1.4)}
                    nodeColor={(n) => NODE_FILL[n.fact_type] || NODE_FILL.experience}
                    nodeLabel={(n) =>
                        `<div style="max-width:240px;padding:2px 2px;font:12px system-ui;color:#e2e8f0">${
                            n.text || n.label
                        }</div>`
                    }
                    nodeCanvasObjectMode={() => 'after'}
                    nodeCanvasObject={(n, ctx, scale) => {
                        // Label only the node under the cursor (not its lit-up neighbors)
                        // and everything once the user has zoomed in.
                        const isHover = n.id === hoverId;
                        if (!isHover && scale < 1.6) return;
                        const label = n.label.length > 22 ? n.label.slice(0, 22) + '…' : n.label;
                        const fontSize = Math.min(14, 11 / scale + 3);
                        ctx.font = `${isHover ? 600 : 400} ${fontSize}px 'Space Grotesk', system-ui, sans-serif`;
                        ctx.textAlign = 'center';
                        ctx.textBaseline = 'top';
                        ctx.fillStyle = isHover
                            ? 'rgba(226,232,240,0.98)'
                            : 'rgba(203,213,225,0.7)';
                        ctx.fillText(label, n.x ?? 0, (n.y ?? 0) + 6);
                    }}
                    onNodeHover={(n) => setHoverId(n ? n.id : null)}
                    onNodeClick={(n) => onNodeClick?.({ id: n.id, label: n.label })}
                    onNodeDragEnd={(n) => {
                        // Release the node (don't pin) so the cooling simulation eases it
                        // back into place — the Obsidian "falls back into place" feel.
                        n.fx = undefined;
                        n.fy = undefined;
                    }}
                    // Links: faint by default, brighten a node's own edges on hover.
                    linkColor={(l) => {
                        const hot = linkHot(l);
                        if (!hot) return 'rgba(148,163,184,0.10)';
                        return l.link_type === 'causal'
                            ? 'rgba(251,191,36,0.55)'
                            : l.link_type === 'entity'
                              ? 'rgba(99,179,237,0.55)'
                              : 'rgba(148,163,184,0.5)';
                    }}
                    linkWidth={(l) =>
                        linkHot(l) ? Math.max(1, l.weight * 1.4) : Math.max(0.3, l.weight * 0.5)
                    }
                    minZoom={0.4}
                    maxZoom={8}
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Type badge
// ---------------------------------------------------------------------------

const TYPE_STYLES: Record<
    string,
    { label: string; bg: string; dot: string; icon: React.FC<{ className?: string }> }
> = {
    experience: {
        label: 'Experience',
        bg: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
        dot: 'bg-emerald-400',
        icon: Zap,
    },
    world: {
        label: 'World Fact',
        bg: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
        dot: 'bg-blue-400',
        icon: Brain,
    },
    observation: {
        label: 'Observation',
        bg: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
        dot: 'bg-amber-400',
        icon: Sparkles,
    },
};

function TypeBadge({ type }: { type: string }) {
    const style = TYPE_STYLES[type] || TYPE_STYLES.experience;
    return (
        <span
            className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border ${style.bg}`}
        >
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${style.dot}`} />
            {style.label}
        </span>
    );
}

// ---------------------------------------------------------------------------
// Overview tab — knowledge graph + summary stats
// ---------------------------------------------------------------------------

function OverviewTab({
    hindsightEnabled,
    onNodeClick,
}: {
    hindsightEnabled: boolean;
    onNodeClick?: (node: { id: string; label: string }) => void;
}) {
    const [graphNodes, setGraphNodes] = useState<MemoryGraphNode[]>([]);
    const [graphEdges, setGraphEdges] = useState<MemoryGraphEdge[]>([]);
    const [loading, setLoading] = useState(true);

    const loadGraph = useCallback(async () => {
        if (!hindsightEnabled) {
            setLoading(false);
            return;
        }
        setLoading(true);
        try {
            const res = await getMemoryGraph(120);
            setGraphNodes(res.nodes || []);
            setGraphEdges(res.edges || []);
        } catch {
            setGraphNodes([]);
            setGraphEdges([]);
        } finally {
            setLoading(false);
        }
    }, [hindsightEnabled]);

    useEffect(() => {
        void loadGraph();
    }, [loadGraph]);

    return (
        <div className="space-y-5">
            {/* Knowledge graph card */}
            <div
                className="rounded-xl border p-4"
                style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
            >
                <div className="flex items-center justify-between mb-3">
                    <p className="text-sm font-medium" style={{ color: 'var(--c-ink)' }}>
                        Knowledge Graph
                        {graphNodes.length > 0 && (
                            <span
                                className="ml-2 text-xs font-normal"
                                style={{ color: 'var(--c-ink-2)' }}
                            >
                                {graphNodes.length} nodes · {graphEdges.length} edges
                            </span>
                        )}
                    </p>
                    <button
                        type="button"
                        onClick={() => void loadGraph()}
                        disabled={loading}
                        className="inline-flex items-center gap-1 text-xs px-2.5 py-1 rounded-lg border transition-colors disabled:opacity-40"
                        style={{
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink-2)',
                        }}
                    >
                        <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
                        Refresh
                    </button>
                </div>

                {loading ? (
                    <div
                        className="flex items-center justify-center"
                        style={{ height: 'clamp(360px, 52vh, 620px)' }}
                    >
                        <Loader2
                            className="w-5 h-5 animate-spin"
                            style={{ color: 'var(--c-signal)' }}
                        />
                    </div>
                ) : (
                    <KnowledgeGraph
                        nodes={graphNodes}
                        edges={graphEdges}
                        onNodeClick={onNodeClick}
                    />
                )}

                {/* Legend */}
                <div className="flex items-center gap-4 mt-3 justify-center flex-wrap">
                    {[
                        { color: 'bg-emerald-400', label: 'Experience' },
                        { color: 'bg-blue-400', label: 'World Fact' },
                        { color: 'bg-amber-400', label: 'Observation' },
                    ].map(({ color, label }) => (
                        <div key={label} className="flex items-center gap-1.5">
                            <span className={`w-2.5 h-2.5 rounded-full ${color} opacity-80`} />
                            <span className="text-xs" style={{ color: 'var(--c-ink-2)' }}>
                                {label}
                            </span>
                        </div>
                    ))}
                    <span className="text-xs" style={{ color: 'var(--c-ink-2)' }}>
                        · Hover to read · Drag nodes to rearrange · Scroll to zoom · Drag background
                        to pan
                    </span>
                </div>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Memory tags: datasets (dataset:<file>) and sessions (session:<id>)
// ---------------------------------------------------------------------------

type Connector = 'csv' | 'excel' | 'json' | 'parquet' | 'postgres' | 'file';

// Visual identity per data-source connector. The memory tag only carries the
// dataset filename, so the connector is inferred from its extension (no
// extension ⇒ a database table).
const CONNECTOR_META: Record<
    Connector,
    {
        label: string;
        color: string;
        Icon: React.FC<{ className?: string; style?: React.CSSProperties }>;
    }
> = {
    csv: { label: 'CSV', color: '#34d399', Icon: Table },
    excel: { label: 'Excel', color: '#22c55e', Icon: FileSpreadsheet },
    json: { label: 'JSON', color: '#f59e0b', Icon: FileJson },
    parquet: { label: 'Parquet', color: '#a78bfa', Icon: FileText },
    postgres: { label: 'Postgres', color: '#38bdf8', Icon: Database },
    file: { label: 'File', color: '#94a3b8', Icon: FileText },
};

function connectorOf(name: string): Connector {
    const n = name.toLowerCase();
    if (n.endsWith('.csv')) return 'csv';
    if (n.endsWith('.xlsx') || n.endsWith('.xls')) return 'excel';
    if (n.endsWith('.json')) return 'json';
    if (n.endsWith('.parquet') || n.endsWith('.pq')) return 'parquet';
    if (!/\.[a-z0-9]{1,6}$/.test(n)) return 'postgres';
    return 'file';
}

interface ParsedTags {
    datasets: { raw: string; name: string; connector: Connector }[];
    sessions: { raw: string; id: string }[];
    others: string[];
}

function parseTags(tags: string[]): ParsedTags {
    const out: ParsedTags = { datasets: [], sessions: [], others: [] };
    for (const t of tags) {
        if (t.startsWith('dataset:')) {
            const name = t.slice(8);
            out.datasets.push({ raw: t, name, connector: connectorOf(name) });
        } else if (t.startsWith('session:')) {
            out.sessions.push({ raw: t, id: t.slice(8) });
        } else {
            out.others.push(t);
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// Browse & Manage tab
// ---------------------------------------------------------------------------

const LOAD_CAP = 500;
type SortMode = 'newest' | 'oldest';
type GroupMode = 'none' | 'type' | 'date';
type Timeframe = 'all' | '7d' | '30d';

function BrowseTab({
    hindsightEnabled,
    onBankChanged,
    focusEntry,
    onFocusHandled,
}: {
    hindsightEnabled: boolean;
    onBankChanged?: () => void;
    focusEntry?: { id: string; label: string } | null;
    onFocusHandled?: () => void;
}) {
    const [entries, setEntries] = useState<MemoryEntry[]>([]);
    const [total, setTotal] = useState(0);
    const [graphNodes, setGraphNodes] = useState<MemoryGraphNode[]>([]);
    const [graphEdges, setGraphEdges] = useState<MemoryGraphEdge[]>([]);
    const [loading, setLoading] = useState(false);
    const [search, setSearch] = useState('');
    const [typeFilter, setTypeFilter] = useState<MemoryTypeFilter>('all');
    const [timeframe, setTimeframe] = useState<Timeframe>('all');
    const [activeTags, setActiveTags] = useState<Set<string>>(new Set());
    const [sort, setSort] = useState<SortMode>('newest');
    const [groupBy, setGroupBy] = useState<GroupMode>('none');
    const [expandedId, setExpandedId] = useState<string | null>(null);
    const [showAllConns, setShowAllConns] = useState(false);
    const [sessionTitles, setSessionTitles] = useState<Map<string, string>>(new Map());
    const [openGroups, setOpenGroups] = useState({
        datasets: false,
        sessions: false,
        tags: false,
    });
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [deleting, setDeleting] = useState<Set<string>>(new Set());
    const [bulkDeleting, setBulkDeleting] = useState(false);
    const [confirmBulk, setConfirmBulk] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const pendingScroll = useRef<string | null>(null);

    // The bank is small (personal memory), so load everything once and do all
    // filtering / grouping / connection lookups client-side for instant response.
    const load = useCallback(async () => {
        if (!hindsightEnabled) return;
        setLoading(true);
        setError(null);
        try {
            const [res, graph, sessions] = await Promise.all([
                listMemoryEntries({ limit: LOAD_CAP, offset: 0 }),
                getMemoryGraph(LOAD_CAP).catch(() => ({
                    nodes: [] as MemoryGraphNode[],
                    edges: [] as MemoryGraphEdge[],
                    total_units: 0,
                })),
                getUsageSessionSummaries(365, 100).catch(() => ({
                    window_days: 365,
                    sessions: [],
                })),
            ]);
            setEntries(res.items);
            setTotal(res.total);
            setGraphNodes(graph.nodes || []);
            setGraphEdges(graph.edges || []);
            const titles = new Map<string, string>();
            for (const s of sessions.sessions || []) {
                if (s.title) titles.set(s.session_id, s.title);
            }
            setSessionTitles(titles);
        } catch {
            setError('Failed to load memories.');
        } finally {
            setLoading(false);
        }
    }, [hindsightEnabled]);

    useEffect(() => {
        void load();
    }, [load]);

    // Jump-from-graph: clear filters so the focused memory is visible, then expand it.
    useEffect(() => {
        if (!focusEntry) return;
        setSearch('');
        setTypeFilter('all');
        setTimeframe('all');
        setActiveTags(new Set());
        setGroupBy('none');
        setExpandedId(focusEntry.id);
        pendingScroll.current = focusEntry.id;
        onFocusHandled?.();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [focusEntry]);

    // Lookups for connections + labels.
    const entryById = useMemo(() => {
        const m = new Map<string, MemoryEntry>();
        for (const e of entries) m.set(e.id, e);
        return m;
    }, [entries]);
    const nodeById = useMemo(() => {
        const m = new Map<string, MemoryGraphNode>();
        for (const n of graphNodes) m.set(n.id, n);
        return m;
    }, [graphNodes]);
    const adjacency = useMemo(() => {
        const m = new Map<string, Set<string>>();
        const link = (a: string, b: string) => {
            const s = m.get(a);
            if (s) s.add(b);
            else m.set(a, new Set([b]));
        };
        for (const e of graphEdges) {
            link(e.source, e.target);
            link(e.target, e.source);
        }
        return m;
    }, [graphEdges]);

    // Tags split into datasets / sessions / other, each sorted by frequency, for
    // the collapsible filter groups below the search bar.
    const tagGroups = useMemo(() => {
        const ds = new Map<string, number>();
        const ss = new Map<string, number>();
        const other = new Map<string, number>();
        for (const e of entries)
            for (const t of e.tags) {
                const bucket = t.startsWith('dataset:')
                    ? ds
                    : t.startsWith('session:')
                      ? ss
                      : other;
                bucket.set(t, (bucket.get(t) ?? 0) + 1);
            }
        const sorted = (m: Map<string, number>) => [...m.entries()].sort((a, b) => b[1] - a[1]);
        return { datasets: sorted(ds), sessions: sorted(ss), others: sorted(other) };
    }, [entries]);

    const sessionLabel = (id: string) => sessionTitles.get(id) || `Session ${id.slice(0, 8)}`;

    const toggleTag = (raw: string) =>
        setActiveTags((prev) => {
            const n = new Set(prev);
            if (n.has(raw)) n.delete(raw);
            else n.add(raw);
            return n;
        });

    const typeCounts = useMemo(() => {
        const c: Record<string, number> = { experience: 0, world: 0, observation: 0 };
        for (const e of entries) if (e.type in c) c[e.type] += 1;
        return c;
    }, [entries]);

    // Client-side filter + sort.
    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        const now = Date.now();
        const cutoff =
            timeframe === '7d'
                ? now - 7 * 86_400_000
                : timeframe === '30d'
                  ? now - 30 * 86_400_000
                  : 0;
        const out = entries.filter((e) => {
            if (typeFilter !== 'all' && e.type !== typeFilter) return false;
            if (activeTags.size > 0 && !e.tags.some((t) => activeTags.has(t))) return false;
            if (cutoff) {
                const t = e.timestamp ? new Date(e.timestamp).getTime() : NaN;
                if (!Number.isFinite(t) || t < cutoff) return false;
            }
            if (q) {
                const hay = (e.content + ' ' + e.tags.join(' ')).toLowerCase();
                if (!hay.includes(q)) return false;
            }
            return true;
        });
        out.sort((a, b) => {
            const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
            const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
            return sort === 'newest' ? tb - ta : ta - tb;
        });
        return out;
    }, [entries, search, typeFilter, activeTags, timeframe, sort]);

    // Grouping for display.
    const groups = useMemo(() => {
        if (groupBy === 'none') return [{ key: 'all', label: '', items: filtered }];
        if (groupBy === 'type') {
            const order = ['experience', 'world', 'observation'];
            const by = new Map<string, MemoryEntry[]>();
            for (const e of filtered) {
                const arr = by.get(e.type);
                if (arr) arr.push(e);
                else by.set(e.type, [e]);
            }
            return order
                .filter((t) => by.has(t))
                .map((t) => ({
                    key: t,
                    label: TYPE_STYLES[t]?.label ?? t,
                    items: by.get(t) ?? [],
                }));
        }
        const buckets: { key: string; label: string; items: MemoryEntry[] }[] = [
            { key: 'today', label: 'Today', items: [] },
            { key: 'yesterday', label: 'Yesterday', items: [] },
            { key: 'week', label: 'Previous 7 days', items: [] },
            { key: 'month', label: 'Previous 30 days', items: [] },
            { key: 'older', label: 'Older', items: [] },
            { key: 'undated', label: 'Undated', items: [] },
        ];
        const now = new Date();
        const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
        for (const e of filtered) {
            const t = e.timestamp ? new Date(e.timestamp).getTime() : NaN;
            if (!Number.isFinite(t)) buckets[5].items.push(e);
            else if (t >= startOfToday) buckets[0].items.push(e);
            else if (t >= startOfToday - 86_400_000) buckets[1].items.push(e);
            else if (t >= startOfToday - 7 * 86_400_000) buckets[2].items.push(e);
            else if (t >= startOfToday - 30 * 86_400_000) buckets[3].items.push(e);
            else buckets[4].items.push(e);
        }
        return buckets.filter((b) => b.items.length > 0);
    }, [filtered, groupBy]);

    const connectionsFor = (id: string) => {
        const ids = adjacency.get(id);
        if (!ids) return [] as { id: string; label: string; type: string }[];
        return [...ids].map((cid) => {
            const en = entryById.get(cid);
            const nd = nodeById.get(cid);
            return {
                id: cid,
                label: (en?.content || nd?.label || nd?.text || 'Untitled').slice(0, 60),
                type: en?.type || nd?.fact_type || 'experience',
            };
        });
    };

    const focusConnection = (id: string) => {
        setShowAllConns(false);
        setExpandedId(id);
        pendingScroll.current = id;
    };

    const toggleSelect = (id: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(id)) {
                next.delete(id);
            } else {
                next.add(id);
            }
            return next;
        });
    };

    const toggleSelectAll = () => {
        const keys = filtered.map((e) => e.document_id || e.id);
        if (keys.length > 0 && keys.every((k) => selected.has(k))) {
            setSelected(new Set());
        } else {
            setSelected(new Set(keys));
        }
    };

    const handleDeleteSingle = async (entry: MemoryEntry) => {
        const key = entry.document_id || entry.id;
        if (!key) return;
        setDeleting((p) => new Set(p).add(key));
        setError(null);
        try {
            await deleteMemoryEntry(key);
            setEntries((prev) => prev.filter((e) => (e.document_id || e.id) !== key));
            setTotal((t) => Math.max(0, t - 1));
            setSelected((prev) => {
                const n = new Set(prev);
                n.delete(key);
                return n;
            });
            onBankChanged?.();
        } catch {
            setError('Failed to delete entry. Please try again.');
        } finally {
            setDeleting((p) => {
                const n = new Set(p);
                n.delete(key);
                return n;
            });
        }
    };

    const handleBulkDelete = async () => {
        setBulkDeleting(true);
        setConfirmBulk(false);
        setError(null);
        const ids = Array.from(selected);
        let failed = 0;
        for (const id of ids) {
            try {
                await deleteMemoryEntry(id);
                setEntries((prev) => prev.filter((e) => (e.document_id || e.id) !== id));
                setTotal((t) => Math.max(0, t - 1));
            } catch {
                failed++;
            }
        }
        setSelected(new Set());
        setBulkDeleting(false);
        onBankChanged?.();
        if (failed > 0) setError(`${failed} entr${failed > 1 ? 'ies' : 'y'} could not be deleted.`);
    };

    const fmtTime = (ts: string | null) => {
        if (!ts) return null;
        try {
            return new Intl.DateTimeFormat(undefined, {
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
            }).format(new Date(ts));
        } catch {
            return ts;
        }
    };

    // Scroll a focused / connection-jumped memory into view once it renders.
    useEffect(() => {
        const id = pendingScroll.current;
        if (!id || loading) return;
        const el = document.getElementById(`mem-entry-${id}`);
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            pendingScroll.current = null;
        }
    });

    return (
        <div className="space-y-4">
            {/* Toolbar: search + sort + grouping + timeframe + refresh */}
            <div className="flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[180px]">
                    <Search
                        className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    />
                    <input
                        type="text"
                        placeholder="Search memories…"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        className="w-full pl-9 pr-3 py-2 rounded-lg border text-sm outline-none focus:ring-1 transition-colors"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink)',
                        }}
                    />
                    {search && (
                        <button
                            type="button"
                            onClick={() => setSearch('')}
                            className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded"
                            style={{ color: 'var(--c-ink-2)' }}
                        >
                            <X className="w-3.5 h-3.5" />
                        </button>
                    )}
                </div>

                <div className="relative">
                    <ArrowDownUp
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    />
                    <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as SortMode)}
                        className="pl-7 pr-6 py-2 rounded-lg border text-sm appearance-none outline-none cursor-pointer"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink)',
                        }}
                    >
                        <option value="newest">Newest</option>
                        <option value="oldest">Oldest</option>
                    </select>
                </div>

                <div className="relative">
                    <Layers
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    />
                    <select
                        value={groupBy}
                        onChange={(e) => setGroupBy(e.target.value as GroupMode)}
                        className="pl-7 pr-6 py-2 rounded-lg border text-sm appearance-none outline-none cursor-pointer"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink)',
                        }}
                    >
                        <option value="none">No grouping</option>
                        <option value="type">Group by type</option>
                        <option value="date">Group by date</option>
                    </select>
                </div>

                <div className="relative">
                    <Clock
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 pointer-events-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    />
                    <select
                        value={timeframe}
                        onChange={(e) => setTimeframe(e.target.value as Timeframe)}
                        className="pl-7 pr-6 py-2 rounded-lg border text-sm appearance-none outline-none cursor-pointer"
                        style={{
                            background: 'var(--c-glass)',
                            borderColor: 'var(--c-glass-border)',
                            color: 'var(--c-ink)',
                        }}
                    >
                        <option value="all">All time</option>
                        <option value="7d">Last 7 days</option>
                        <option value="30d">Last 30 days</option>
                    </select>
                </div>

                <button
                    type="button"
                    onClick={() => void load()}
                    disabled={loading}
                    className="inline-flex items-center justify-center px-2.5 py-2 rounded-lg border transition-colors disabled:opacity-40"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
                    title="Refresh"
                >
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
                </button>
            </div>

            {/* Type filter chips with counts */}
            <div className="flex flex-wrap items-center gap-2">
                {(
                    [
                        { id: 'all', label: 'All', count: entries.length },
                        { id: 'experience', label: 'Experience', count: typeCounts.experience },
                        { id: 'world', label: 'World Facts', count: typeCounts.world },
                        {
                            id: 'observation',
                            label: 'Observations',
                            count: typeCounts.observation,
                        },
                    ] as { id: MemoryTypeFilter; label: string; count: number }[]
                ).map(({ id, label, count }) => {
                    const active = typeFilter === id;
                    const accent =
                        id === 'experience'
                            ? '#34d399'
                            : id === 'world'
                              ? '#63b3ed'
                              : id === 'observation'
                                ? '#fbbf24'
                                : null;
                    return (
                        <button
                            key={id}
                            type="button"
                            onClick={() => setTypeFilter(id)}
                            className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full border transition-all"
                            style={{
                                borderColor: active
                                    ? (accent ?? 'var(--c-signal)')
                                    : 'var(--c-glass-border)',
                                background: active
                                    ? accent
                                        ? `${accent}22`
                                        : 'var(--c-signal-dim)'
                                    : 'var(--c-glass)',
                                color: active ? 'var(--c-ink)' : 'var(--c-ink-2)',
                            }}
                        >
                            {accent && (
                                <span
                                    className="w-1.5 h-1.5 rounded-full"
                                    style={{ background: accent }}
                                />
                            )}
                            {label}
                            <span style={{ color: 'var(--c-ink-2)' }}>{count}</span>
                        </button>
                    );
                })}
            </div>

            {/* Collapsible filter groups: Datasets · Sessions · Tags */}
            {(tagGroups.datasets.length > 0 ||
                tagGroups.sessions.length > 0 ||
                tagGroups.others.length > 0) && (
                <div className="space-y-1.5">
                    {(
                        [
                            {
                                key: 'datasets' as const,
                                title: 'Datasets',
                                Icon: Database,
                                items: tagGroups.datasets,
                            },
                            {
                                key: 'sessions' as const,
                                title: 'Sessions',
                                Icon: MessageSquare,
                                items: tagGroups.sessions,
                            },
                            {
                                key: 'tags' as const,
                                title: 'Tags',
                                Icon: Tag,
                                items: tagGroups.others,
                            },
                        ] as const
                    )
                        .filter((g) => g.items.length > 0)
                        .map(({ key, title, Icon, items }) => {
                            const open = openGroups[key];
                            const activeCount = items.filter(([raw]) => activeTags.has(raw)).length;
                            return (
                                <div
                                    key={key}
                                    className="rounded-lg border"
                                    style={{
                                        borderColor: 'var(--c-glass-border)',
                                        background: 'var(--c-glass)',
                                    }}
                                >
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setOpenGroups((p) => ({ ...p, [key]: !p[key] }))
                                        }
                                        className="w-full flex items-center gap-2 px-3 py-1.5 text-xs"
                                        style={{ color: 'var(--c-ink-2)' }}
                                    >
                                        <ChevronDown
                                            className="w-3.5 h-3.5 transition-transform"
                                            style={{ transform: open ? 'none' : 'rotate(-90deg)' }}
                                        />
                                        <Icon className="w-3.5 h-3.5" />
                                        <span
                                            className="font-medium"
                                            style={{ color: 'var(--c-ink)' }}
                                        >
                                            {title}
                                        </span>
                                        <span className="opacity-60">{items.length}</span>
                                        {activeCount > 0 && (
                                            <span
                                                className="ml-auto text-[10px] px-1.5 py-0.5 rounded-full"
                                                style={{
                                                    background: 'var(--c-signal-dim)',
                                                    color: 'var(--c-signal-text)',
                                                }}
                                            >
                                                {activeCount} active
                                            </span>
                                        )}
                                    </button>
                                    {open && (
                                        <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2.5 pt-0.5">
                                            {items.map(([raw, count]) => {
                                                const active = activeTags.has(raw);
                                                const isDataset = raw.startsWith('dataset:');
                                                const isSession = raw.startsWith('session:');
                                                const meta = isDataset
                                                    ? CONNECTOR_META[connectorOf(raw.slice(8))]
                                                    : null;
                                                const ChipIcon =
                                                    meta?.Icon ?? (isSession ? MessageSquare : Tag);
                                                const accent =
                                                    meta?.color ??
                                                    (isSession ? '#a78bfa' : undefined);
                                                const label = isDataset
                                                    ? raw.slice(8)
                                                    : isSession
                                                      ? sessionLabel(raw.slice(8))
                                                      : raw;
                                                return (
                                                    <button
                                                        key={raw}
                                                        type="button"
                                                        onClick={() => toggleTag(raw)}
                                                        className="inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-md border transition-all max-w-[220px]"
                                                        style={{
                                                            borderColor: active
                                                                ? (accent ?? 'var(--c-signal)')
                                                                : 'var(--c-glass-border)',
                                                            background: active
                                                                ? accent
                                                                    ? `${accent}22`
                                                                    : 'var(--c-signal-dim)'
                                                                : 'transparent',
                                                            color: active
                                                                ? 'var(--c-ink)'
                                                                : 'var(--c-ink-2)',
                                                        }}
                                                        title={label}
                                                    >
                                                        <ChipIcon
                                                            className="w-3 h-3 shrink-0"
                                                            style={
                                                                accent
                                                                    ? { color: accent }
                                                                    : undefined
                                                            }
                                                        />
                                                        <span className="truncate">{label}</span>
                                                        <span className="opacity-60 shrink-0">
                                                            {count}
                                                        </span>
                                                    </button>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    {activeTags.size > 0 && (
                        <button
                            type="button"
                            onClick={() => setActiveTags(new Set())}
                            className="text-xs px-1"
                            style={{ color: 'var(--c-signal-text)' }}
                        >
                            Clear all filters ({activeTags.size})
                        </button>
                    )}
                </div>
            )}

            {/* Bulk action bar */}
            {selected.size > 0 && (
                <div
                    className="flex items-center justify-between gap-3 rounded-xl border px-4 py-2.5"
                    style={{
                        borderColor: 'rgba(239,68,68,0.3)',
                        background: 'rgba(239,68,68,0.06)',
                    }}
                >
                    <p className="text-sm" style={{ color: 'var(--c-ink)' }}>
                        <span className="font-semibold text-red-400">{selected.size}</span> entr
                        {selected.size > 1 ? 'ies' : 'y'} selected
                    </p>
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setSelected(new Set())}
                            className="text-xs px-3 py-1 rounded-lg border transition-colors"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                color: 'var(--c-ink-2)',
                            }}
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={() => setConfirmBulk(true)}
                            disabled={bulkDeleting}
                            className="btn-danger inline-flex items-center gap-1.5 text-xs px-3 py-1 disabled:opacity-50"
                        >
                            {bulkDeleting ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                                <Trash2 className="w-3.5 h-3.5" />
                            )}
                            Delete selected
                        </button>
                    </div>
                </div>
            )}

            {/* Bulk delete confirm */}
            {confirmBulk && (
                <div
                    className="rounded-xl border px-4 py-3 flex items-center justify-between gap-4"
                    style={{
                        borderColor: 'rgba(239,68,68,0.35)',
                        background: 'rgba(239,68,68,0.08)',
                    }}
                >
                    <p className="text-sm text-red-400">
                        Permanently delete {selected.size} entr{selected.size > 1 ? 'ies' : 'y'}?
                        This cannot be undone.
                    </p>
                    <div className="flex gap-2 shrink-0">
                        <button
                            type="button"
                            onClick={() => setConfirmBulk(false)}
                            className="text-xs px-3 py-1 rounded-lg border"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                color: 'var(--c-ink-2)',
                            }}
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={() => void handleBulkDelete()}
                            className="btn-danger text-xs px-3 py-1"
                        >
                            Confirm Delete
                        </button>
                    </div>
                </div>
            )}

            {error && (
                <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-400">
                    {error}
                </div>
            )}

            {/* Result header */}
            {!loading && filtered.length > 0 && (
                <div className="flex items-center justify-between">
                    <label
                        className="flex items-center gap-2 text-xs cursor-pointer select-none"
                        style={{ color: 'var(--c-ink-2)' }}
                    >
                        <input
                            type="checkbox"
                            className="rounded"
                            checked={filtered.every((e) => selected.has(e.document_id || e.id))}
                            onChange={toggleSelectAll}
                        />
                        Select all ({filtered.length})
                    </label>
                    <p className="text-xs" style={{ color: 'var(--c-ink-2)' }}>
                        Showing {filtered.length.toLocaleString()} of {total.toLocaleString()}
                        {total > LOAD_CAP && ` · first ${LOAD_CAP} loaded`}
                    </p>
                </div>
            )}

            {/* Entries (grouped, expandable, with connections) */}
            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <Loader2
                        className="w-5 h-5 animate-spin"
                        style={{ color: 'var(--c-signal)' }}
                    />
                </div>
            ) : filtered.length === 0 ? (
                <div
                    className="rounded-xl border p-8 text-center"
                    style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
                >
                    <Brain
                        className="w-8 h-8 mx-auto mb-3 opacity-30"
                        style={{ color: 'var(--c-ink)' }}
                    />
                    <p className="text-sm" style={{ color: 'var(--c-ink-2)' }}>
                        {entries.length === 0
                            ? 'No memory entries yet.'
                            : 'No memories match your filters.'}
                    </p>
                </div>
            ) : (
                <div className="space-y-5">
                    {groups.map((group) => (
                        <div key={group.key} className="space-y-2">
                            {group.label && (
                                <div className="flex items-center gap-2 px-1">
                                    <p
                                        className="text-xs font-semibold uppercase tracking-wide"
                                        style={{ color: 'var(--c-ink-2)' }}
                                    >
                                        {group.label}
                                    </p>
                                    <span
                                        className="text-xs"
                                        style={{ color: 'var(--c-ink-2)', opacity: 0.6 }}
                                    >
                                        {group.items.length}
                                    </span>
                                    <div
                                        className="flex-1 h-px"
                                        style={{ background: 'var(--c-glass-border)' }}
                                    />
                                </div>
                            )}
                            {group.items.map((entry) => {
                                const key = entry.document_id || entry.id;
                                const isSelected = selected.has(key);
                                const isDel = deleting.has(key);
                                const isExpanded = expandedId === entry.id;
                                const connCount = adjacency.get(entry.id)?.size ?? 0;
                                const conns = isExpanded ? connectionsFor(entry.id) : [];
                                const pt = parseTags(entry.tags);
                                return (
                                    <div
                                        key={entry.id}
                                        id={`mem-entry-${entry.id}`}
                                        className="rounded-xl border transition-all group"
                                        style={{
                                            borderColor: isSelected
                                                ? 'rgba(239,68,68,0.4)'
                                                : isExpanded
                                                  ? 'var(--c-signal)'
                                                  : 'var(--c-glass-border)',
                                            background: isSelected
                                                ? 'rgba(239,68,68,0.05)'
                                                : 'var(--c-glass)',
                                        }}
                                    >
                                        <div className="flex items-start gap-3 px-4 py-3">
                                            <div
                                                className="mt-0.5 shrink-0"
                                                onClick={(e) => e.stopPropagation()}
                                            >
                                                <input
                                                    type="checkbox"
                                                    className="rounded cursor-pointer"
                                                    checked={isSelected}
                                                    onChange={() => toggleSelect(key)}
                                                />
                                            </div>

                                            <button
                                                type="button"
                                                onClick={() => {
                                                    setShowAllConns(false);
                                                    setExpandedId(isExpanded ? null : entry.id);
                                                }}
                                                className="flex-1 min-w-0 text-left"
                                            >
                                                <p
                                                    className={`text-sm leading-relaxed ${isExpanded ? '' : 'line-clamp-3'}`}
                                                    style={{ color: 'var(--c-ink)' }}
                                                >
                                                    {entry.content}
                                                </p>
                                                <div className="flex flex-wrap items-center gap-2 mt-2">
                                                    <TypeBadge type={entry.type} />
                                                    {entry.timestamp && (
                                                        <span
                                                            className="inline-flex items-center gap-1 text-xs"
                                                            style={{ color: 'var(--c-ink-2)' }}
                                                        >
                                                            <Clock className="w-3 h-3" />
                                                            {fmtTime(entry.timestamp)}
                                                        </span>
                                                    )}
                                                    {connCount > 0 && (
                                                        <span
                                                            className="inline-flex items-center gap-1 text-xs"
                                                            style={{ color: 'var(--c-ink-2)' }}
                                                            title={`${connCount} connected memories`}
                                                        >
                                                            <Link2 className="w-3 h-3" />
                                                            {connCount}
                                                        </span>
                                                    )}
                                                    {pt.datasets.map((d) => {
                                                        const meta = CONNECTOR_META[d.connector];
                                                        return (
                                                            <span
                                                                key={d.raw}
                                                                className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-md border max-w-[200px]"
                                                                style={{
                                                                    borderColor: `${meta.color}55`,
                                                                    background: `${meta.color}14`,
                                                                    color: 'var(--c-ink)',
                                                                }}
                                                                title={`${meta.label} · ${d.name}`}
                                                            >
                                                                <meta.Icon
                                                                    className="w-3 h-3 shrink-0"
                                                                    style={{ color: meta.color }}
                                                                />
                                                                <span className="truncate">
                                                                    {d.name}
                                                                </span>
                                                            </span>
                                                        );
                                                    })}
                                                    {pt.sessions.map((s) => (
                                                        <span
                                                            key={s.raw}
                                                            className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-md border max-w-[200px]"
                                                            style={{
                                                                borderColor: '#a78bfa55',
                                                                background: '#a78bfa14',
                                                                color: 'var(--c-ink)',
                                                            }}
                                                            title={`Session · ${sessionLabel(s.id)}`}
                                                        >
                                                            <MessageSquare
                                                                className="w-3 h-3 shrink-0"
                                                                style={{ color: '#a78bfa' }}
                                                            />
                                                            <span className="truncate">
                                                                {sessionLabel(s.id)}
                                                            </span>
                                                        </span>
                                                    ))}
                                                    {pt.others.slice(0, 3).map((tag) => (
                                                        <span
                                                            key={tag}
                                                            className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-md border"
                                                            style={{
                                                                borderColor:
                                                                    'var(--c-glass-border)',
                                                                color: 'var(--c-ink-2)',
                                                            }}
                                                        >
                                                            <Tag className="w-2.5 h-2.5" />
                                                            {tag}
                                                        </span>
                                                    ))}
                                                    {pt.others.length > 3 && (
                                                        <span
                                                            className="text-xs"
                                                            style={{ color: 'var(--c-ink-2)' }}
                                                        >
                                                            +{pt.others.length - 3}
                                                        </span>
                                                    )}
                                                </div>
                                            </button>

                                            <div className="flex items-center gap-1 shrink-0">
                                                <ChevronRight
                                                    className="w-4 h-4 transition-transform"
                                                    style={{
                                                        color: 'var(--c-ink-2)',
                                                        transform: isExpanded
                                                            ? 'rotate(90deg)'
                                                            : 'none',
                                                    }}
                                                />
                                                <button
                                                    type="button"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        void handleDeleteSingle(entry);
                                                    }}
                                                    disabled={isDel}
                                                    className="p-1.5 rounded-lg border opacity-0 group-hover:opacity-100 transition-all disabled:opacity-50"
                                                    style={{
                                                        borderColor: 'rgba(239,68,68,0.25)',
                                                        color: 'rgb(239,68,68)',
                                                        background: 'rgba(239,68,68,0.05)',
                                                    }}
                                                    title="Remove this memory"
                                                >
                                                    {isDel ? (
                                                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                    ) : (
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    )}
                                                </button>
                                            </div>
                                        </div>

                                        {isExpanded && (
                                            <div
                                                className="px-4 pb-3 ml-7 border-t space-y-3"
                                                style={{ borderColor: 'var(--c-glass-border)' }}
                                            >
                                                <div className="pt-3">
                                                    <p
                                                        className="text-xs font-semibold mb-1.5 inline-flex items-center gap-1"
                                                        style={{ color: 'var(--c-ink-2)' }}
                                                    >
                                                        <Link2 className="w-3 h-3" />
                                                        Connected memories
                                                        {conns.length > 0
                                                            ? ` (${conns.length})`
                                                            : ''}
                                                    </p>
                                                    {conns.length === 0 ? (
                                                        <p
                                                            className="text-xs"
                                                            style={{
                                                                color: 'var(--c-ink-2)',
                                                                opacity: 0.7,
                                                            }}
                                                        >
                                                            No connections.
                                                        </p>
                                                    ) : (
                                                        <div className="flex flex-wrap gap-1.5">
                                                            {conns
                                                                .slice(
                                                                    0,
                                                                    showAllConns ? conns.length : 12
                                                                )
                                                                .map((c) => {
                                                                    const accent =
                                                                        c.type === 'world'
                                                                            ? '#63b3ed'
                                                                            : c.type ===
                                                                                'observation'
                                                                              ? '#fbbf24'
                                                                              : '#34d399';
                                                                    return (
                                                                        <button
                                                                            key={c.id}
                                                                            type="button"
                                                                            onClick={() =>
                                                                                focusConnection(
                                                                                    c.id
                                                                                )
                                                                            }
                                                                            className="inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg border text-left max-w-[240px] transition-colors hover:border-[var(--c-signal)]"
                                                                            style={{
                                                                                borderColor:
                                                                                    'var(--c-glass-border)',
                                                                                background:
                                                                                    'var(--c-glass)',
                                                                                color: 'var(--c-ink)',
                                                                            }}
                                                                            title={c.label}
                                                                        >
                                                                            <span
                                                                                className="w-1.5 h-1.5 rounded-full shrink-0"
                                                                                style={{
                                                                                    background:
                                                                                        accent,
                                                                                }}
                                                                            />
                                                                            <span className="truncate">
                                                                                {c.label}
                                                                            </span>
                                                                        </button>
                                                                    );
                                                                })}
                                                            {conns.length > 12 && (
                                                                <button
                                                                    type="button"
                                                                    onClick={() =>
                                                                        setShowAllConns((v) => !v)
                                                                    }
                                                                    className="text-xs self-center px-2 py-1 rounded-lg border transition-colors"
                                                                    style={{
                                                                        borderColor:
                                                                            'var(--c-glass-border)',
                                                                        background:
                                                                            'var(--c-signal-dim)',
                                                                        color: 'var(--c-signal-text)',
                                                                    }}
                                                                >
                                                                    {showAllConns
                                                                        ? 'Show less'
                                                                        : `Show all ${conns.length}`}
                                                                </button>
                                                            )}
                                                        </div>
                                                    )}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main exported component
// ---------------------------------------------------------------------------

export default function MemoryManager({ onBankChanged, hindsightEnabled }: MemoryManagerProps) {
    const [activeTab, setActiveTab] = useState<MemoryManagerTab>('overview');
    const [focusEntry, setFocusEntry] = useState<{ id: string; label: string } | null>(null);

    // Clicking a graph node jumps to the Browse tab focused on that memory.
    const handleNodeClick = (node: { id: string; label: string }) => {
        setFocusEntry(node);
        setActiveTab('browse');
    };

    const tabs: Array<{
        id: MemoryManagerTab;
        label: string;
        icon: React.FC<{ className?: string }>;
    }> = [
        { id: 'overview', label: 'Overview', icon: Brain },
        { id: 'browse', label: 'Browse & Manage', icon: Search },
    ];

    if (!hindsightEnabled) {
        return (
            <div
                className="rounded-xl border px-4 py-6 text-center"
                style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
            >
                <Brain
                    className="w-7 h-7 mx-auto mb-2 opacity-30"
                    style={{ color: 'var(--c-ink)' }}
                />
                <p className="text-sm" style={{ color: 'var(--c-ink-2)' }}>
                    Long-term memory is not enabled on this server.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-4">
            {/* Sub-tabs */}
            <div
                className="inline-flex p-0.5 rounded-xl border gap-0.5"
                style={{ borderColor: 'var(--c-glass-border)', background: 'var(--c-glass)' }}
            >
                {tabs.map(({ id, label, icon: Icon }) => (
                    <button
                        key={id}
                        type="button"
                        onClick={() => setActiveTab(id)}
                        className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-sm font-medium transition-all"
                        style={
                            activeTab === id
                                ? {
                                      background: 'var(--c-signal-dim)',
                                      color: 'var(--c-signal-text)',
                                  }
                                : { color: 'var(--c-ink-2)', background: 'transparent' }
                        }
                    >
                        <Icon className="w-3.5 h-3.5" />
                        {label}
                    </button>
                ))}
            </div>

            {activeTab === 'overview' ? (
                <OverviewTab hindsightEnabled={hindsightEnabled} onNodeClick={handleNodeClick} />
            ) : (
                <BrowseTab
                    hindsightEnabled={hindsightEnabled}
                    onBankChanged={onBankChanged}
                    focusEntry={focusEntry}
                    onFocusHandled={() => setFocusEntry(null)}
                />
            )}
        </div>
    );
}
