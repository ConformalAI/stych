import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { emptySpec, type SavedViz, type VizData, type VizSpec } from './types';

const services = vi.hoisted(() => ({
    getVizCatalog: vi.fn(),
    listVisualizations: vi.fn(),
    getVisualizationData: vi.fn(),
    getVisualization: vi.fn(),
    queryVizSpec: vi.fn(),
    validateCalculations: vi.fn(),
    createVisualization: vi.fn(),
    updateVisualization: vi.fn(),
    deleteVisualization: vi.fn(),
}));
const store = vi.hoisted(() => {
    const state = {
        lastVizEvent: null as Record<string, unknown> | null,
        openVizId: null as string | null,
        uploadedFiles: [] as Array<{ file_id: string }>,
    };
    const setOpenVizId = vi.fn((id: string | null) => {
        state.openVizId = id;
    });
    return {
        state,
        setOpenVizId,
        setLastVizEvent: vi.fn(),
        notifySessionActivity: vi.fn(),
    };
});

vi.mock('../../services/viz', () => services);
vi.mock('../../store/useStore', () => ({
    useStore: Object.assign(
        (selector: (state: Record<string, unknown>) => unknown) =>
            selector({ ...store.state, setOpenVizId: store.setOpenVizId }),
        {
            getState: () => ({
                ...store.state,
                setOpenVizId: store.setOpenVizId,
                setLastVizEvent: store.setLastVizEvent,
                notifySessionActivity: store.notifySessionActivity,
            }),
        }
    ),
}));

import { useVizSpec, type UseVizSpec } from './useVizSpec';

interface Deferred<T> {
    promise: Promise<T>;
    resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
        resolve = done;
    });
    return { promise, resolve };
}

function dataFor(value: number): VizData {
    return {
        columns: [{ name: 'x', field: 'value', kind: 'dimension', type: 'quantitative' }],
        rows: [[value]],
        meta: { row_count: 1, truncated: false, engine: 'pandas' },
    };
}

function savedViz(id: string): SavedViz {
    return {
        id,
        session_id: 'session-1',
        dataset_ref: null,
        title: id,
        spec: emptySpec(),
        version: 1,
        created_at: null,
        updated_at: null,
    };
}

let latest: UseVizSpec | null = null;

function current(): UseVizSpec {
    if (!latest) throw new Error('Hook has not rendered');
    return latest;
}

function Harness({
    sessionId = 'session-1',
    children,
}: {
    sessionId?: string;
    children?: ReactNode;
}) {
    latest = useVizSpec(sessionId);
    return children ?? null;
}

describe('useVizSpec chart transitions', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(async () => {
        localStorage.clear();
        latest = null;
        store.state.lastVizEvent = null;
        store.state.openVizId = null;
        store.setOpenVizId.mockClear();
        store.setLastVizEvent.mockClear();
        store.notifySessionActivity.mockClear();
        services.getVizCatalog.mockResolvedValue({
            source: 'dataset-1',
            fingerprint: 'f',
            built_at: '',
            engine: 'pandas',
            row_count: 0,
            fields: [],
        });
        services.listVisualizations.mockResolvedValue([]);
        services.validateCalculations.mockResolvedValue({ fields: [], errors: [] });
        services.getVisualization.mockImplementation(async (id: string) => savedViz(id));
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
        await act(async () => root.render(<Harness />));
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        vi.clearAllMocks();
    });

    it('ignores an older chart load that finishes after the latest selection', async () => {
        const first = deferred<{ spec: VizSpec; data: VizData }>();
        const second = deferred<{ spec: VizSpec; data: VizData }>();
        services.getVisualizationData.mockImplementation((id: string) =>
            id === 'first' ? first.promise : second.promise
        );

        let firstLoad!: Promise<void>;
        let secondLoad!: Promise<void>;
        await act(async () => {
            firstLoad = current().selectViz('first');
            secondLoad = current().selectViz('second');
        });
        await act(async () => {
            second.resolve({ spec: { ...emptySpec(), title: 'second' }, data: dataFor(2) });
            await secondLoad;
        });
        await act(async () => {
            first.resolve({ spec: { ...emptySpec(), title: 'first' }, data: dataFor(1) });
            await firstLoad;
        });

        expect(latest?.activeVizId).toBe('second');
        expect(latest?.renderedSpec?.title).toBe('second');
        expect(latest?.data?.rows).toEqual([[2]]);
    });

    it('opens the explicitly requested chat chart instead of the latest chart', async () => {
        await act(async () => root.unmount());
        root = createRoot(container);
        const listed = deferred<SavedViz[]>();
        const requested = {
            ...savedViz('requested'),
            updated_at: '2026-01-01T00:00:00',
        };
        const latestChart = {
            ...savedViz('latest'),
            updated_at: '2026-02-01T00:00:00',
        };
        store.state.openVizId = requested.id;
        services.listVisualizations.mockReturnValue(listed.promise);
        services.getVisualizationData.mockImplementation(async (id: string) => ({
            spec: { ...emptySpec(), title: id },
            data: dataFor(id === requested.id ? 1 : 2),
        }));

        await act(async () => {
            root.render(<Harness />);
            await Promise.resolve();
        });
        await act(async () => {
            listed.resolve([latestChart, requested]);
            await listed.promise;
            await Promise.resolve();
        });

        expect(current().activeVizId).toBe(requested.id);
        expect(current().renderedSpec?.title).toBe(requested.id);
        expect(services.getVisualizationData).not.toHaveBeenCalledWith(latestChart.id);
        expect(store.setOpenVizId).toHaveBeenCalledWith(null);
    });

    it('defaults to the latest chart for the active dataset only', async () => {
        await act(async () => root.unmount());
        root = createRoot(container);
        const csvChart = {
            ...savedViz('csv-chart'),
            dataset_ref: 'csv-1',
            spec: { ...emptySpec(), data: { source: 'csv-1' } },
            updated_at: '2026-03-01T00:00:00',
        };
        const postgresChart = {
            ...savedViz('postgres-chart'),
            dataset_ref: 'postgres-1',
            spec: { ...emptySpec(), data: { source: 'postgres-1' } },
            updated_at: '2026-02-01T00:00:00',
        };
        services.getVizCatalog.mockResolvedValue({
            source: 'postgres-1',
            fingerprint: 'pg-f',
            built_at: '',
            engine: 'sql',
            row_count: 2,
            fields: [],
        });
        services.listVisualizations.mockResolvedValue([csvChart, postgresChart]);
        services.getVisualizationData.mockImplementation(async (id: string) => ({
            spec: id === postgresChart.id ? postgresChart.spec : csvChart.spec,
            data: dataFor(id === postgresChart.id ? 2 : 1),
        }));

        await act(async () => {
            root.render(<Harness />);
            await Promise.resolve();
            await Promise.resolve();
        });

        expect(services.getVisualizationData).toHaveBeenCalledWith(postgresChart.id);
        expect(services.getVisualizationData).not.toHaveBeenCalledWith(csvChart.id);
        expect(current().saved.map((chart) => chart.id)).toEqual([postgresChart.id]);
    });

    it('restores an unsaved draft after the workspace is remounted', async () => {
        await act(async () => {
            current().updateSpec((spec) => ({
                ...spec,
                config: { ...spec.config, grid: false },
            }));
        });
        await act(async () => root.unmount());
        root = createRoot(container);
        await act(async () => root.render(<Harness />));

        expect(latest?.dirty).toBe(true);
        expect(latest?.spec.config?.grid).toBe(false);
    });

    it('starts a new chart with a visible default name and empty shelves', async () => {
        await act(async () => current().newViz());

        expect(current().spec).toEqual({
            ...emptySpec(),
            title: 'New Chart 1',
            data: { source: 'dataset-1' },
        });
        expect(current().title).toBe('New Chart 1');
        expect(current().dirty).toBe(false);
        expect(current().renderedSpec).toBeNull();
        expect(localStorage.getItem('stych_viz_draft_v2:session-1:new')).toBeNull();
    });

    it('starts a blank chart and reloads fields when the data source changes', async () => {
        services.getVizCatalog.mockImplementation(async (_sessionId: string, source?: string) => ({
            source: source ?? 'dataset-1',
            fingerprint: `f-${source ?? 'dataset-1'}`,
            built_at: '',
            engine: 'pandas',
            row_count: 2,
            fields: [],
        }));

        await act(async () => current().selectSource('dataset-2'));

        expect(current().spec).toEqual({
            ...emptySpec(),
            title: 'New Chart 1',
            data: { source: 'dataset-2' },
        });
        expect(current().activeSource).toBe('dataset-2');
        expect(services.getVizCatalog).toHaveBeenCalledWith('session-1', 'dataset-2');
    });

    it('does not wipe an incomplete unsaved chart', async () => {
        await act(async () => {
            current().updateSpec((spec) => ({
                ...spec,
                data: { source: 'dataset-1' },
                transform: [{ op: 'filter', field: 'region', predicate: 'eq', value: 'East' }],
                encoding: {
                    ...spec.encoding,
                    x: { field: 'region', type: 'nominal' },
                },
            }));
        });

        expect(current().dirty).toBe(true);
        await act(async () => current().newViz());

        expect(current().spec.encoding.x?.field).toBe('region');
        expect(current().spec.transform).toHaveLength(1);
        expect(current().dirty).toBe(true);
        expect(current().renderedSpec).toBeNull();
        expect(current().queryError).toBe('Finish or discard this chart before creating another.');
    });

    it('does not discard an incomplete chart when switching data sources', async () => {
        await act(async () => {
            current().updateSpec((spec) => ({
                ...spec,
                data: { source: 'dataset-1' },
                encoding: {
                    ...spec.encoding,
                    x: { field: 'region', type: 'nominal' },
                },
            }));
        });

        await act(async () => current().selectSource('dataset-2'));

        expect(current().spec.data?.source).toBe('dataset-1');
        expect(current().spec.encoding.x?.field).toBe('region');
        expect(current().queryError).toBe(
            'Finish or discard this chart before switching data sources.'
        );
    });

    it('does not persist the previous editor state into a new session', async () => {
        await act(async () => {
            current().updateSpec((spec) => ({
                ...spec,
                data: { source: 'dataset-from-session-1' },
                transform: [{ op: 'filter', field: 'region', predicate: 'eq', value: 'East' }],
                encoding: {
                    ...spec.encoding,
                    x: { field: 'region', type: 'nominal' },
                },
            }));
        });

        await act(async () => root.render(<Harness sessionId="session-2" />));

        expect(current().spec).toEqual({ ...emptySpec(), title: 'New Chart 1' });
        expect(current().dirty).toBe(false);
        expect(localStorage.getItem('stych_viz_draft_v2:session-2:new')).toBeNull();
    });

    it('saves a complete unsaved chart before opening the next numbered chart', async () => {
        vi.useFakeTimers();
        try {
            const requested: VizSpec = {
                ...emptySpec(),
                title: 'New Chart 1',
                mark: { type: 'histogram' },
                encoding: {
                    x: {
                        field: 'quantity',
                        type: 'quantitative',
                        bin: { maxbins: 10 },
                    },
                },
            };
            const canonical = { ...requested, data: { source: 'dataset-1' } };
            const chartData: VizData = {
                columns: [
                    {
                        name: 'x',
                        field: 'quantity',
                        kind: 'dimension',
                        type: 'quantitative',
                    },
                ],
                rows: [[0]],
                meta: { row_count: 1, truncated: false, engine: 'pandas' },
            };
            services.queryVizSpec.mockResolvedValue({ spec: canonical, data: chartData });
            services.createVisualization.mockResolvedValue({
                visualization: {
                    ...savedViz('chart-1'),
                    dataset_ref: 'dataset-1',
                    title: 'New Chart 1',
                    spec: canonical,
                },
                data: chartData,
            });

            await act(async () => current().updateSpec(() => requested));
            await act(async () => vi.advanceTimersByTimeAsync(350));
            await act(async () => current().newViz());

            expect(services.createVisualization).toHaveBeenCalledWith(
                'session-1',
                expect.objectContaining({ title: 'New Chart 1' }),
                'New Chart 1'
            );
            expect(current().saved.map((chart) => chart.title)).toEqual(['New Chart 1']);
            expect(store.setLastVizEvent).toHaveBeenCalledWith(
                expect.objectContaining({
                    sessionId: 'session-1',
                    vizId: 'chart-1',
                    action: 'created',
                    source: 'user',
                    version: 1,
                })
            );
            expect(current().activeVizId).toBeNull();
            expect(current().spec).toEqual({
                ...emptySpec(),
                title: 'New Chart 2',
                data: { source: 'dataset-1' },
            });
        } finally {
            vi.useRealTimers();
        }
    });

    it('publishes an updated user event after saving an existing chart', async () => {
        const existingSpec: VizSpec = {
            ...emptySpec(),
            title: 'Existing chart',
            data: { source: 'dataset-1' },
            encoding: {
                x: { field: 'region', type: 'nominal' },
                y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
            },
        };
        const existing = {
            ...savedViz('existing-chart'),
            dataset_ref: 'dataset-1',
            title: 'Existing chart',
            spec: existingSpec,
        };
        const chartData: VizData = {
            columns: [
                { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
            ],
            rows: [['East', 10]],
            meta: { row_count: 1, truncated: false, engine: 'pandas' },
        };
        services.getVisualization.mockResolvedValue(existing);
        services.getVisualizationData.mockResolvedValue({ spec: existingSpec, data: chartData });
        services.updateVisualization.mockResolvedValue({
            visualization: {
                ...existing,
                version: 2,
                spec: {
                    ...existingSpec,
                    config: { ...existingSpec.config, grid: false },
                },
            },
            data: chartData,
        });

        await act(async () => current().selectViz(existing.id));
        await act(async () =>
            current().updateSpec((spec) => ({
                ...spec,
                config: { ...spec.config, grid: false },
            }))
        );
        await act(async () => current().save());

        expect(services.updateVisualization).toHaveBeenCalledWith(
            existing.id,
            expect.objectContaining({
                config: expect.objectContaining({ grid: false }),
            }),
            'Existing chart',
            1
        );
        expect(store.setLastVizEvent).toHaveBeenCalledWith(
            expect.objectContaining({
                vizId: existing.id,
                action: 'updated',
                source: 'user',
                version: 2,
            })
        );
    });

    it('queries a dimension-only bar and keeps its derived count out of the shelves', async () => {
        vi.useFakeTimers();
        try {
            const requested: VizSpec = {
                ...emptySpec(),
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                },
            };
            const canonical: VizSpec = {
                ...requested,
                data: { source: 'dataset-1' },
                encoding: {
                    ...requested.encoding,
                    y: { field: null, type: 'quantitative', aggregate: 'count' },
                },
            };
            services.queryVizSpec.mockResolvedValue({
                spec: canonical,
                data: {
                    columns: [
                        {
                            name: 'x',
                            field: 'region',
                            kind: 'dimension',
                            type: 'nominal',
                        },
                        {
                            name: 'y',
                            field: null,
                            kind: 'measure',
                            type: 'quantitative',
                        },
                    ],
                    rows: [['EU', 12]],
                    meta: { row_count: 1, truncated: false, engine: 'sql' },
                },
            });

            await act(async () => current().updateSpec(() => requested));
            await act(async () => vi.advanceTimersByTimeAsync(350));

            expect(latest?.spec.encoding.y ?? null).toBeNull();
            expect(latest?.spec.data?.source).toBe('dataset-1');
            expect(latest?.renderedSpec?.encoding.y).toMatchObject({
                aggregate: 'count',
                field: null,
            });
            expect(services.queryVizSpec).toHaveBeenCalledWith('session-1', requested);
        } finally {
            vi.useRealTimers();
        }
    });

    it('previews color changes without querying the data again', async () => {
        vi.useFakeTimers();
        try {
            const requested: VizSpec = {
                ...emptySpec(),
                title: 'Revenue',
                encoding: {
                    x: { field: 'region', type: 'nominal' },
                    y: { field: 'revenue', type: 'quantitative', aggregate: 'sum' },
                },
            };
            const canonical = { ...requested, data: { source: 'dataset-1' } };
            const chartData: VizData = {
                columns: [
                    { name: 'x', field: 'region', kind: 'dimension', type: 'nominal' },
                    { name: 'y', field: 'revenue', kind: 'measure', type: 'quantitative' },
                ],
                rows: [['East', 10]],
                meta: { row_count: 1, truncated: false, engine: 'pandas' },
            };
            services.queryVizSpec.mockResolvedValue({ spec: canonical, data: chartData });

            await act(async () => current().updateSpec(() => requested));
            await act(async () => vi.advanceTimersByTimeAsync(350));
            expect(services.queryVizSpec).toHaveBeenCalledTimes(1);

            await act(async () =>
                current().updateSpec((spec) => ({
                    ...spec,
                    config: {
                        ...spec.config,
                        color: {
                            mode: 'categorical',
                            binding: { channel: 'x', field: 'region' },
                            palette: ['#2563EB'],
                            assignments: [{ value: 'East', color: '#DC2626' }],
                        },
                        labelFontSize: 14,
                    },
                }))
            );
            await act(async () => vi.advanceTimersByTimeAsync(350));

            expect(services.queryVizSpec).toHaveBeenCalledTimes(1);
            expect(current().spec.config?.color?.assignments).toEqual([
                { value: 'East', color: '#DC2626' },
            ]);
            expect(current().renderedSpec?.config?.labelFontSize).toBe(14);
            expect(current().dirty).toBe(true);
        } finally {
            vi.useRealTimers();
        }
    });
});
