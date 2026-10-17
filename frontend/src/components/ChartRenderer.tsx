/**
 * LEGACY renderer — kept only for ```chart / ```charts fences in historical
 * chat messages (baked-data Recharts specs from the pre-VizSpec GraphAgent).
 * All new visualizations flow through components/visualize/ (VizSpec +
 * ECharts, live data). Do not add features here.
 */
import { useState, useRef } from 'react';
import {
    BarChart,
    Bar,
    LineChart,
    Line,
    PieChart,
    Pie,
    AreaChart,
    Area,
    XAxis,
    YAxis,
    CartesianGrid,
    Tooltip,
    Legend,
    ResponsiveContainer,
    Cell,
    ReferenceLine,
    ScatterChart,
    Scatter,
} from 'recharts';
import {
    Maximize2,
    X,
    Download,
    BarChart2,
    TrendingUp,
    Activity,
    Minus,
    Table2,
} from 'lucide-react';
import { useStore } from '../store/useStore';

interface ChartSeries {
    key: string;
    label: string;
    color?: string;
}

interface ChartDataPoint {
    name: string;
    value?: number;
    [key: string]: any;
}

export interface ChartSpec {
    type: 'bar' | 'line' | 'pie' | 'area' | 'scatter';
    title?: string;
    xLabel?: string;
    yLabel?: string;
    data: ChartDataPoint[];
    series?: ChartSeries[];
}

interface ChartRendererProps {
    spec: ChartSpec;
}

const PALETTE = [
    '#0fb981',
    '#1fd68f',
    '#06b6d4',
    '#3b82f6',
    '#f59e0b',
    '#e8a13a',
    '#ef4444',
    '#ec4899',
];

type ActiveType = 'bar' | 'line' | 'area' | 'scatter';
const TYPE_ICONS: Record<ActiveType, JSX.Element> = {
    bar: <BarChart2 className="w-3.5 h-3.5" />,
    line: <Activity className="w-3.5 h-3.5" />,
    area: <TrendingUp className="w-3.5 h-3.5" />,
    scatter: <Activity className="w-3.5 h-3.5" />,
};

export default function ChartRenderer({ spec }: ChartRendererProps) {
    const chatTheme = useStore((s) => s.chatTheme);
    const isDark = chatTheme === 'dark';

    const isPie = spec.type === 'pie';
    const isScatter = spec.type === 'scatter';
    const [activeType, setActiveType] = useState<ActiveType>(
        isPie ? 'bar' : (spec.type as ActiveType)
    );
    const [fullscreen, setFullscreen] = useState(false);
    const [showAvg, setShowAvg] = useState(false);
    const [showTable, setShowTable] = useState(false);
    const chartRef = useRef<HTMLDivElement>(null);

    const { title, xLabel, yLabel, data } = spec;
    const series: ChartSeries[] = spec.series ?? [
        { key: 'value', label: yLabel || 'Value', color: PALETTE[0] },
    ];

    // All columns: name + series keys that actually exist in data
    const tableColumns = [
        { key: 'name', label: xLabel || 'Name' },
        ...series.map((s) => ({ key: s.key, label: s.label, color: s.color })),
    ];

    const avg = (() => {
        const vals = data
            .map((d) => d[series[0].key] ?? d.value ?? 0)
            .filter((v) => typeof v === 'number');
        return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : 0;
    })();

    const theme = {
        grid: isDark ? 'rgba(255,255,255,0.07)' : '#e2e8f0',
        text: isDark ? '#94a3b8' : '#64748b',
        tooltipBg: isDark ? '#0f2744' : '#ffffff',
        tooltipBorder: isDark ? 'rgba(255,255,255,0.1)' : '#e2e8f0',
        title: isDark ? '#e2e8f0' : '#1e293b',
        tableBorder: isDark ? 'rgba(255,255,255,0.06)' : '#f1f5f9',
        tableHead: isDark ? 'rgba(255,255,255,0.04)' : '#f8fafc',
        tableHeadText: isDark ? '#64748b' : '#94a3b8',
        tableRowHover: isDark ? 'rgba(255,255,255,0.03)' : '#f8fafc',
        cellText: isDark ? '#cbd5e1' : '#334155',
    };

    // ── Downloads ────────────────────────────────────────────────────────────
    const downloadSVG = () => {
        const svg = chartRef.current?.querySelector('svg');
        if (!svg) return;
        const clone = svg.cloneNode(true) as SVGElement;
        clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
        const blob = new Blob([new XMLSerializer().serializeToString(clone)], {
            type: 'image/svg+xml',
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${title || 'chart'}.svg`;
        a.click();
        URL.revokeObjectURL(url);
    };

    const downloadCSV = () => {
        const headers = tableColumns.map((c) => c.label);
        const rows = data.map((d) =>
            tableColumns
                .map((c) => {
                    const v = d[c.key];
                    // quote if contains comma or newline
                    if (typeof v === 'string' && (v.includes(',') || v.includes('\n')))
                        return `"${v}"`;
                    return v ?? '';
                })
                .join(',')
        );
        const csv = [headers.join(','), ...rows].join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${title || 'chart-data'}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    };

    const handleDownload = () => (showTable ? downloadCSV() : downloadSVG());

    // ── Table view ───────────────────────────────────────────────────────────
    const renderTable = (maxH: string) => (
        <div className="overflow-auto" style={{ maxHeight: maxH }}>
            <table className="w-full text-xs border-collapse">
                <thead>
                    <tr
                        style={{
                            background: theme.tableHead,
                            borderBottom: `1px solid ${theme.tableBorder}`,
                        }}
                    >
                        <th
                            className="text-left px-3 py-2 font-semibold uppercase tracking-wider select-none"
                            style={{ color: theme.tableHeadText }}
                        >
                            #
                        </th>
                        {tableColumns.map((col) => (
                            <th
                                key={col.key}
                                className="text-left px-3 py-2 font-semibold uppercase tracking-wider select-none"
                                style={{ color: (col as any).color ?? theme.tableHeadText }}
                            >
                                {col.label}
                            </th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {data.map((row, i) => (
                        <tr
                            key={i}
                            style={{ borderBottom: `1px solid ${theme.tableBorder}` }}
                            className="transition-colors"
                            onMouseEnter={(e) =>
                                (e.currentTarget.style.background = theme.tableRowHover)
                            }
                            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                        >
                            <td
                                className="px-3 py-2 tabular-nums"
                                style={{ color: theme.tableHeadText }}
                            >
                                {i + 1}
                            </td>
                            {tableColumns.map((col) => {
                                const v = row[col.key];
                                const isNum = typeof v === 'number';
                                return (
                                    <td
                                        key={col.key}
                                        className={`px-3 py-2 tabular-nums ${isNum ? 'text-right font-medium' : ''}`}
                                        style={{
                                            color: isNum
                                                ? ((col as any).color ?? theme.cellText)
                                                : theme.cellText,
                                        }}
                                    >
                                        {isNum ? v.toLocaleString() : (v ?? '—')}
                                    </td>
                                );
                            })}
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );

    // ── Chart view ───────────────────────────────────────────────────────────
    const CustomTooltip = ({ active, payload, label }: any) => {
        if (!active || !payload?.length) return null;
        return (
            <div
                style={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}` }}
                className="rounded-xl px-3 py-2 shadow-xl text-sm"
            >
                {label && (
                    <p className="font-semibold mb-1.5 text-xs" style={{ color: theme.text }}>
                        {label}
                    </p>
                )}
                {payload.map((p: any) => (
                    <div key={p.dataKey} className="flex items-center gap-2">
                        <span
                            className="w-2 h-2 rounded-full flex-shrink-0"
                            style={{ background: p.color }}
                        />
                        <span style={{ color: theme.text }} className="text-xs">
                            {p.name}:
                        </span>
                        <span className="font-bold text-xs" style={{ color: p.color }}>
                            {typeof p.value === 'number' ? p.value.toLocaleString() : p.value}
                        </span>
                    </div>
                ))}
            </div>
        );
    };

    const commonAxisProps = {
        tick: { fill: theme.text, fontSize: 11 },
        axisLine: { stroke: theme.grid },
        tickLine: { stroke: theme.grid },
    };

    const renderChart = (height: number) => {
        // Rotate x-axis labels when there are many data points to avoid overlap
        const manyPoints = data.length > 8;
        const xAxisAngle = manyPoints ? -40 : 0;
        const xAxisHeight = manyPoints ? 60 : xLabel ? 48 : 24;
        const xTickInterval = data.length > 24 ? Math.ceil(data.length / 12) - 1 : 0;
        const margin = { top: 16, right: 20, left: 8, bottom: manyPoints ? 60 : xLabel ? 40 : 16 };

        // Smart Y-axis number formatter
        const formatY = (v: number) => {
            if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
            if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
            return v % 1 === 0 ? String(v) : v.toFixed(1);
        };

        if (isPie) {
            return (
                <ResponsiveContainer width="100%" height={height}>
                    <PieChart>
                        <Pie
                            data={data}
                            dataKey={
                                series[0].key === 'value' && data[0]?.value !== undefined
                                    ? 'value'
                                    : series[0].key
                            }
                            nameKey="name"
                            cx="50%"
                            cy="50%"
                            outerRadius={height * 0.32}
                            innerRadius={height * 0.14}
                            paddingAngle={2}
                            label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
                            labelLine={{ stroke: theme.text }}
                        >
                            {data.map((_, i) => (
                                <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                            ))}
                        </Pie>
                        <Tooltip content={<CustomTooltip />} />
                        <Legend wrapperStyle={{ color: theme.text, fontSize: 12 }} />
                    </PieChart>
                </ResponsiveContainer>
            );
        }
        if (isScatter || activeType === 'scatter') {
            return (
                <ResponsiveContainer width="100%" height={height}>
                    <ScatterChart margin={margin}>
                        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} vertical={false} />
                        <XAxis
                            dataKey="name"
                            type="number"
                            {...commonAxisProps}
                            label={
                                xLabel
                                    ? {
                                          value: xLabel,
                                          position: 'insideBottom',
                                          offset: -8,
                                          fill: theme.text,
                                          fontSize: 11,
                                      }
                                    : undefined
                            }
                        />
                        <YAxis
                            dataKey={series[0]?.key ?? 'value'}
                            type="number"
                            {...commonAxisProps}
                            label={
                                yLabel
                                    ? {
                                          value: yLabel,
                                          angle: -90,
                                          position: 'insideLeft',
                                          fill: theme.text,
                                          fontSize: 11,
                                          dx: -4,
                                      }
                                    : undefined
                            }
                            width={yLabel ? 60 : 48}
                        />
                        <Tooltip
                            content={<CustomTooltip />}
                            cursor={{
                                fill: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)',
                            }}
                        />
                        <Legend wrapperStyle={{ color: theme.text, fontSize: 12 }} />
                        <Scatter
                            data={data.map((d) => ({
                                ...d,
                                value: d[series[0]?.key ?? 'value'] ?? d.value,
                            }))}
                            dataKey="value"
                            name={series[0]?.label ?? yLabel ?? 'Value'}
                            fill={series[0]?.color ?? PALETTE[0]}
                        />
                    </ScatterChart>
                </ResponsiveContainer>
            );
        }

        const sharedChildren = (
            <>
                <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} vertical={false} />
                <XAxis
                    dataKey="name"
                    {...commonAxisProps}
                    angle={xAxisAngle}
                    textAnchor={manyPoints ? 'end' : 'middle'}
                    interval={xTickInterval}
                    height={xAxisHeight}
                    label={
                        !manyPoints && xLabel
                            ? {
                                  value: xLabel,
                                  position: 'insideBottom',
                                  offset: -8,
                                  fill: theme.text,
                                  fontSize: 11,
                              }
                            : undefined
                    }
                />
                <YAxis
                    {...commonAxisProps}
                    tickFormatter={formatY}
                    label={
                        yLabel
                            ? {
                                  value: yLabel,
                                  angle: -90,
                                  position: 'insideLeft',
                                  fill: theme.text,
                                  fontSize: 11,
                                  dx: -4,
                              }
                            : undefined
                    }
                    width={yLabel ? 60 : 48}
                />
                <Tooltip
                    content={<CustomTooltip />}
                    cursor={{ fill: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' }}
                />
                <Legend wrapperStyle={{ color: theme.text, fontSize: 12 }} />
                {showAvg && (
                    <ReferenceLine
                        y={avg}
                        stroke="#f59e0b"
                        strokeDasharray="5 3"
                        label={{
                            value: `avg ${avg.toFixed(1)}`,
                            fill: '#f59e0b',
                            fontSize: 10,
                            position: 'right',
                        }}
                    />
                )}
            </>
        );

        if (activeType === 'bar') {
            return (
                <ResponsiveContainer width="100%" height={height}>
                    <BarChart data={data} margin={margin} barCategoryGap="30%">
                        {sharedChildren}
                        {series.map((s, i) => (
                            <Bar
                                key={s.key}
                                dataKey={s.key}
                                name={s.label}
                                fill={s.color ?? PALETTE[i % PALETTE.length]}
                                radius={[4, 4, 0, 0]}
                                isAnimationActive
                            />
                        ))}
                    </BarChart>
                </ResponsiveContainer>
            );
        }

        if (activeType === 'line') {
            return (
                <ResponsiveContainer width="100%" height={height}>
                    <LineChart data={data} margin={margin}>
                        {sharedChildren}
                        {series.map((s, i) => (
                            <Line
                                key={s.key}
                                type="monotone"
                                dataKey={s.key}
                                name={s.label}
                                stroke={s.color ?? PALETTE[i % PALETTE.length]}
                                strokeWidth={2.5}
                                dot={
                                    data.length > 30
                                        ? false
                                        : { r: 3, fill: s.color ?? PALETTE[i % PALETTE.length] }
                                }
                                activeDot={{ r: 5 }}
                                isAnimationActive
                            />
                        ))}
                    </LineChart>
                </ResponsiveContainer>
            );
        }

        return (
            <ResponsiveContainer width="100%" height={height}>
                <AreaChart data={data} margin={margin}>
                    {sharedChildren}
                    {series.map((s, i) => {
                        const color = s.color ?? PALETTE[i % PALETTE.length];
                        return (
                            <Area
                                key={s.key}
                                type="monotone"
                                dataKey={s.key}
                                name={s.label}
                                stroke={color}
                                fill={color}
                                fillOpacity={isDark ? 0.15 : 0.12}
                                strokeWidth={2.5}
                                dot={false}
                                activeDot={{ r: 5 }}
                                isAnimationActive
                            />
                        );
                    })}
                </AreaChart>
            </ResponsiveContainer>
        );
    };

    // ── Shared action bar ────────────────────────────────────────────────────
    const btnBase = (active: boolean, activeClass: string, size = 'w-6 h-6') =>
        `${size} rounded-md flex items-center justify-center transition-all ${
            active
                ? activeClass
                : isDark
                  ? 'text-white/30 hover:text-blue-300 hover:bg-white/5'
                  : 'text-slate-400 hover:text-blue-600 hover:bg-blue-50'
        }`;

    const ActionBar = ({ btnSize = 'w-6 h-6' }: { btnSize?: string }) => (
        <div className="flex items-center gap-1.5 flex-shrink-0 ml-2">
            {/* Chart type switcher */}
            {!isPie && !isScatter && !showTable && (
                <div className="flex items-center gap-1 mr-0.5">
                    {(['bar', 'line', 'area'] as ActiveType[]).map((t) => (
                        <button
                            key={t}
                            onClick={() => setActiveType(t)}
                            title={t.charAt(0).toUpperCase() + t.slice(1) + ' chart'}
                            className={btnBase(activeType === t, 'text-white shadow-sm', btnSize)}
                        >
                            {TYPE_ICONS[t]}
                        </button>
                    ))}
                </div>
            )}

            {/* Average line toggle */}
            {!isPie && !isScatter && !showTable && (
                <button
                    onClick={() => setShowAvg((v) => !v)}
                    title={showAvg ? 'Hide average line' : 'Show average line'}
                    className={btnBase(showAvg, 'bg-amber-500/20 text-amber-400', btnSize)
                        .replace('hover:text-blue-300', 'hover:text-amber-300')
                        .replace('hover:text-blue-600', 'hover:text-amber-500')}
                >
                    <Minus className="w-3.5 h-3.5" />
                </button>
            )}

            {/* Data table toggle */}
            <button
                onClick={() => setShowTable((v) => !v)}
                title={showTable ? 'Show chart' : 'View data table'}
                className={btnBase(showTable, 'bg-emerald-500/20 text-emerald-400', btnSize)}
            >
                <Table2 className="w-3.5 h-3.5" />
            </button>

            {/* Download — SVG or CSV depending on view */}
            <button
                onClick={handleDownload}
                title={showTable ? 'Download CSV' : 'Download SVG'}
                className={btnBase(false, '', btnSize)}
            >
                <Download className="w-3.5 h-3.5" />
            </button>

            {/* Fullscreen */}
            <button
                onClick={() => setFullscreen(true)}
                title="Expand"
                className={btnBase(false, '', btnSize)}
            >
                <Maximize2 className="w-3.5 h-3.5" />
            </button>
        </div>
    );

    // ── Card layout ──────────────────────────────────────────────────────────
    const cardClass = `my-4 rounded-2xl border overflow-hidden transition-colors ${
        isDark ? 'bg-white/[0.04] border-white/[0.08]' : 'bg-white border-gray-100 shadow-sm'
    }`;

    const borderB = `border-b ${isDark ? 'border-white/[0.06]' : 'border-gray-100'}`;

    return (
        <>
            <div className={cardClass}>
                {/* Header */}
                <div className={`flex items-center justify-between px-4 pt-4 pb-2 ${borderB}`}>
                    <div className="flex items-center gap-2 min-w-0">
                        {title && (
                            <h3
                                className="text-sm font-semibold truncate"
                                style={{ color: theme.title }}
                            >
                                {title}
                            </h3>
                        )}
                        {showTable && (
                            <span
                                className="text-xs px-2 py-0.5 rounded-full border"
                                style={{
                                    background: 'var(--c-signal-dim)',
                                    borderColor:
                                        'color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                    color: 'var(--c-signal-text)',
                                }}
                            >
                                {data.length} rows
                            </span>
                        )}
                    </div>
                    <ActionBar />
                </div>

                {/* Body */}
                {showTable ? (
                    <div className="pb-1">{renderTable('320px')}</div>
                ) : (
                    <div ref={chartRef} className="px-2 pb-4 pt-3">
                        {data.length === 0 ? (
                            <div
                                className={`py-12 text-center text-sm ${isDark ? 'text-white/30' : 'text-slate-400'}`}
                            >
                                No data available
                            </div>
                        ) : (
                            renderChart(320)
                        )}
                    </div>
                )}
            </div>

            {/* Fullscreen modal */}
            {fullscreen && (
                <div
                    className="fixed inset-0 z-50 flex items-center justify-center p-6"
                    style={{ background: 'rgba(0,0,0,0.75)', backdropFilter: 'blur(8px)' }}
                    onClick={(e) => {
                        if (e.target === e.currentTarget) setFullscreen(false);
                    }}
                >
                    <div
                        className={`w-full max-w-5xl rounded-2xl border relative ${
                            isDark ? 'bg-[#0f1e35] border-white/10' : 'bg-white border-gray-200'
                        }`}
                    >
                        {/* Fullscreen header */}
                        <div className={`flex items-center justify-between px-5 py-3 ${borderB}`}>
                            <div className="flex items-center gap-2 min-w-0">
                                <h3
                                    className="text-sm font-semibold"
                                    style={{ color: theme.title }}
                                >
                                    {title || 'Chart'}
                                </h3>
                                {showTable && (
                                    <span
                                        className="text-xs px-2 py-0.5 rounded-full border"
                                        style={{
                                            background: 'var(--c-signal-dim)',
                                            borderColor:
                                                'color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                            color: 'var(--c-signal-text)',
                                        }}
                                    >
                                        {data.length} rows
                                    </span>
                                )}
                            </div>
                            <div className="flex items-center gap-1.5">
                                <ActionBar btnSize="w-7 h-7" />
                                <button
                                    onClick={() => setFullscreen(false)}
                                    className={`w-7 h-7 rounded-md flex items-center justify-center transition-all ${
                                        isDark
                                            ? 'text-white/30 hover:text-white hover:bg-white/10'
                                            : 'text-slate-400 hover:text-slate-700 hover:bg-gray-100'
                                    }`}
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            </div>
                        </div>

                        {/* Fullscreen body */}
                        {showTable ? (
                            <div className="pb-2">{renderTable('560px')}</div>
                        ) : (
                            <div className="px-4 pb-6 pt-4">{renderChart(520)}</div>
                        )}
                    </div>
                </div>
            )}
        </>
    );
}
