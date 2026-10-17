import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { FieldCatalog, FieldInfo, SavedViz, SpecErrorItem, VizData, VizSpec } from './types';
import { isMarkEncodingCompatible } from './markCapabilities';

export interface VizConflict {
    kind: 'remote-update' | 'save-conflict';
    message: string;
    serverVersion: number | null;
}

export interface UseVizSpec {
    catalog: FieldCatalog | null;
    catalogError: string | null;
    calcErrors: SpecErrorItem[];
    saved: SavedViz[];
    activeSource: string;
    activeVizId: string | null;
    activeVersion: number | null;
    spec: VizSpec;
    renderedSpec: VizSpec | null;
    title: string;
    setTitle: (title: string) => void;
    data: VizData | null;
    loading: boolean;
    saving: boolean;
    dirty: boolean;
    conflict: VizConflict | null;
    specErrors: SpecErrorItem[];
    queryError: string | null;
    updateSpec: (updater: (prev: VizSpec) => VizSpec) => void;
    undo: () => void;
    redo: () => void;
    canUndo: boolean;
    canRedo: boolean;
    newViz: () => Promise<void>;
    selectSource: (source: string) => Promise<void>;
    selectViz: (id: string) => Promise<void>;
    save: () => Promise<void>;
    saveAsCopy: () => Promise<void>;
    reloadRemote: () => Promise<void>;
    keepDraft: () => void;
    discardDraft: () => Promise<void>;
    remove: () => Promise<void>;
    refresh: () => void;
    refreshSaved: () => void;
}

export interface PersistenceContext {
    sessionId: string;
    specRef: MutableRefObject<VizSpec>;
    titleRef: MutableRefObject<string>;
    activeIdRef: MutableRefObject<string | null>;
    versionRef: MutableRefObject<number | null>;
    savedRef: MutableRefObject<SavedViz[]>;
    setSaving: Dispatch<SetStateAction<boolean>>;
    setSpecErrors: Dispatch<SetStateAction<SpecErrorItem[]>>;
    setQueryError: Dispatch<SetStateAction<string | null>>;
    setConflict: Dispatch<SetStateAction<VizConflict | null>>;
    setRenderedSpec: Dispatch<SetStateAction<VizSpec | null>>;
    setData: Dispatch<SetStateAction<VizData | null>>;
    setSaved: Dispatch<SetStateAction<SavedViz[]>>;
    setActive: (id: string | null, version: number | null) => void;
    setDraft: (spec: VizSpec, dirty: boolean) => void;
    setDirtyState: (dirty: boolean) => void;
    refreshSaved: () => void;
    newViz: (source?: string) => void;
    loadVisualization: (id: string, discardDraft: boolean) => Promise<void>;
}

export interface LifecycleContext {
    sessionId: string;
    lastVizEvent: {
        sessionId: string;
        vizId?: string | null;
        action?: string;
        source?: 'agent' | 'user';
        version?: number | null;
    } | null;
    openVizId: string | null;
    setOpenVizId: (id: string | null) => void;
    spec: VizSpec;
    title: string;
    dirty: boolean;
    activeVizId: string | null;
    activeVersion: number | null;
    specRef: MutableRefObject<VizSpec>;
    activeIdRef: MutableRefObject<string | null>;
    versionRef: MutableRefObject<number | null>;
    dirtyRef: MutableRefObject<boolean>;
    draftSessionRef: MutableRefObject<string>;
    savedRef: MutableRefObject<SavedViz[]>;
    sessionGeneration: MutableRefObject<number>;
    cancelPending: () => void;
    resetStacks: () => void;
    scheduleQuery: (spec: VizSpec) => void;
    setActive: (id: string | null, version: number | null) => void;
    setDirtyState: (dirty: boolean) => void;
    setDraft: (spec: VizSpec, dirty: boolean) => void;
    loadVisualization: (id: string, discardDraft: boolean) => Promise<void>;
    selectViz: (id: string) => Promise<void>;
    uploadedFileIds: string;
    setCatalog: Dispatch<SetStateAction<FieldCatalog | null>>;
    setCatalogError: Dispatch<SetStateAction<string | null>>;
    setCalcFields: Dispatch<SetStateAction<FieldInfo[]>>;
    setCalcErrors: Dispatch<SetStateAction<SpecErrorItem[]>>;
    setSaved: Dispatch<SetStateAction<SavedViz[]>>;
    setRenderedSpec: Dispatch<SetStateAction<VizSpec | null>>;
    setData: Dispatch<SetStateAction<VizData | null>>;
    setSpecErrors: Dispatch<SetStateAction<SpecErrorItem[]>>;
    setQueryError: Dispatch<SetStateAction<string | null>>;
    setConflict: Dispatch<SetStateAction<VizConflict | null>>;
}

export function adoptRowTitle(spec: VizSpec, rowTitle: string | null | undefined): VizSpec {
    return spec.title || !rowTitle ? spec : { ...spec, title: rowTitle };
}

export function nextNewChartTitle(saved: SavedViz[], currentTitle = ''): string {
    let highest = saved.length;
    for (const title of [...saved.map((row) => row.title ?? ''), currentTitle]) {
        const match = /^New Chart (\d+)$/.exec(title);
        if (match) highest = Math.max(highest, Number(match[1]));
    }
    return `New Chart ${highest + 1}`;
}

export function filterSavedVisualizations(saved: SavedViz[], source: string): SavedViz[] {
    if (!source || source === 'active') return saved;
    return saved.filter((row) => {
        const rowSource = row.dataset_ref ?? row.spec.data?.source ?? 'active';
        return rowSource === source;
    });
}

export function intermediateFieldForSpec(
    spec: VizSpec,
    catalog: FieldCatalog | null,
    renderedSpec: VizSpec | null
): FieldInfo | null {
    if (renderedSpec) return null;
    const encodedFields = [
        spec.encoding.x,
        spec.encoding.y,
        spec.encoding.color,
        spec.encoding.size,
    ].filter((channel) => Boolean(channel?.field));
    if (encodedFields.length !== 1) return null;
    return catalog?.fields.find((field) => field.name === encodedFields[0]?.field) ?? null;
}

export function hasSameQueryShape(left: VizSpec, right: VizSpec): boolean {
    const queryShape = (spec: VizSpec) => ({
        data: spec.data,
        transform: spec.transform,
        markType: spec.mark.type,
        encoding: spec.encoding,
    });
    return JSON.stringify(queryShape(left)) === JSON.stringify(queryShape(right));
}

export function applyPresentation(rendered: VizSpec, draft: VizSpec): VizSpec {
    return {
        ...rendered,
        title: draft.title,
        mark: { ...draft.mark },
        config: draft.config ? { ...draft.config } : undefined,
    };
}

export function isQueryable(spec: VizSpec): boolean {
    if (isMarkEncodingCompatible(spec)) return true;

    const { x, y, color, size } = spec.encoding;
    const dimension = (channel: typeof x) => Boolean(channel?.field && !channel.aggregate);
    const optionalDimension = (channel: typeof x) => !channel || dimension(channel);

    // The backend canonicalizes a dimension-only aggregate chart by adding an
    // implicit count. This is a queryable editing state even though the visible
    // shelves are not yet a complete chart arrangement.
    switch (spec.mark.type) {
        case 'bar':
            return (
                ((dimension(x) && !y) || (dimension(y) && !x)) && optionalDimension(color) && !size
            );
        case 'line':
        case 'area':
            return dimension(x) && !y && optionalDimension(color) && !size;
        case 'pie':
            return dimension(x) !== dimension(color) && !y && !size;
        default:
            return false;
    }
}

export function errorDetail(error: unknown, fallback: string): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
    if (typeof detail === 'string') return detail;
    if (
        detail &&
        typeof detail === 'object' &&
        'message' in detail &&
        typeof detail.message === 'string'
    ) {
        return detail.message;
    }
    return fallback;
}

export function isConflictResponse(error: unknown): boolean {
    return (error as { response?: { status?: number } })?.response?.status === 409;
}
