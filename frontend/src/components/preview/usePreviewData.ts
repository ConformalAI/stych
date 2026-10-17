import { useEffect, useRef, useState } from 'react';
import { ColumnStats, PreviewDataProvider, PreviewPage } from './types';

const DEBOUNCE_MS = 300;

export interface UsePreviewData {
    data: PreviewPage | null;
    stats: Record<string, ColumnStats> | null;
    page: number;
    totalPages: number;
    loading: boolean;
    error: string | null;
    sortBy: string | undefined;
    sortDir: 'asc' | 'desc';
    search: string;
    filters: Record<string, string>;
    setPage: (page: number) => void;
    toggleSort: (column: string) => void;
    setSearch: (value: string) => void;
    setFilter: (column: string, value: string) => void;
    retry: () => void;
}

/**
 * Owns the preview query state (page/sort/filters/search) and fetching against
 * a PreviewDataProvider: debounced text inputs, stale-response protection, and
 * one-time stats retrieval per provider key.
 */
export function usePreviewData(provider: PreviewDataProvider, pageSize = 100): UsePreviewData {
    const [page, setPage] = useState(1);
    const [sortBy, setSortBy] = useState<string | undefined>(undefined);
    const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');
    const [search, setSearch] = useState('');
    const [filters, setFilters] = useState<Record<string, string>>({});
    // Debounced copies drive fetching; filters as JSON so equal values short-circuit.
    const [debSearch, setDebSearch] = useState('');
    const [debFiltersJson, setDebFiltersJson] = useState('{}');
    const [data, setData] = useState<PreviewPage | null>(null);
    const [stats, setStats] = useState<Record<string, ColumnStats> | null>(null);
    const [totalPages, setTotalPages] = useState(1);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [reloadTick, setReloadTick] = useState(0);

    const requestId = useRef(0);
    const statsLoadedKey = useRef<string | null>(null);

    // Reset during render on provider change so effects only ever run with
    // fresh state (avoids one stale request per source switch).
    const [prevKey, setPrevKey] = useState(provider.key);
    if (prevKey !== provider.key) {
        setPrevKey(provider.key);
        setPage(1);
        setSortBy(undefined);
        setSortDir('asc');
        setSearch('');
        setFilters({});
        setDebSearch('');
        setDebFiltersJson('{}');
        setData(null);
        setStats(null);
        setTotalPages(1);
        setError(null);
    }

    useEffect(() => {
        const timer = setTimeout(() => {
            setDebSearch(search);
            setDebFiltersJson(JSON.stringify(filters));
            setPage(1);
        }, DEBOUNCE_MS);
        return () => clearTimeout(timer);
    }, [search, filters]);

    useEffect(() => {
        const id = ++requestId.current;
        const controller = new AbortController();
        const includeStats = statsLoadedKey.current !== provider.key;
        setLoading(true);

        provider
            .fetchPage(
                {
                    offset: (page - 1) * pageSize,
                    limit: pageSize,
                    sortBy,
                    sortDir,
                    search: debSearch || undefined,
                    filters: JSON.parse(debFiltersJson),
                },
                { includeStats, signal: controller.signal }
            )
            .then((result) => {
                if (id !== requestId.current) return;
                setData(result);
                setError(null);
                const pages = Math.max(1, Math.ceil(result.filteredRows / pageSize));
                setTotalPages(pages);
                if (page > pages) setPage(pages);
                if (result.stats) {
                    setStats(result.stats);
                }
                statsLoadedKey.current = provider.key;
                setLoading(false);
            })
            .catch((err) => {
                if (id !== requestId.current || controller.signal.aborted) return;
                setError(
                    err?.response?.data?.detail || err?.message || 'Failed to load preview.'
                );
                setLoading(false);
            });

        return () => controller.abort();
    }, [provider, page, pageSize, sortBy, sortDir, debSearch, debFiltersJson, reloadTick]);

    const toggleSort = (column: string) => {
        setPage(1);
        if (sortBy !== column) {
            setSortBy(column);
            setSortDir('asc');
        } else if (sortDir === 'asc') {
            setSortDir('desc');
        } else {
            setSortBy(undefined);
            setSortDir('asc');
        }
    };

    const setFilter = (column: string, value: string) => {
        setFilters((prev) => {
            const next = { ...prev };
            if (value) {
                next[column] = value;
            } else {
                delete next[column];
            }
            return next;
        });
    };

    return {
        data,
        stats,
        page,
        totalPages,
        loading,
        error,
        sortBy,
        sortDir,
        search,
        filters,
        setPage: (p: number) => setPage(Math.max(1, Math.min(p, totalPages))),
        toggleSort,
        setSearch,
        setFilter,
        retry: () => setReloadTick((t) => t + 1),
    };
}
