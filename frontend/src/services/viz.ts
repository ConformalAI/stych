/**
 * API client for the visualization subsystem (backend/viz/routes.py).
 */

import { api } from './api';
import type {
    AskMode,
    AskResponse,
    FieldCatalog,
    FieldInfo,
    SavedViz,
    SelectionPayload,
    SpecErrorItem,
    Transform,
    VizData,
    VizSpec,
    VizVersion,
} from '../components/visualize/types';

export const getVizCatalog = async (sessionId: string, source?: string): Promise<FieldCatalog> => {
    const response = await api.get(`/api/viz/sessions/${sessionId}/catalog`, {
        params: source && source !== 'active' ? { source } : undefined,
    });
    return response.data;
};

export interface VizQueryResult {
    spec: VizSpec;
    data: VizData;
}

export const queryVizSpec = async (sessionId: string, spec: VizSpec): Promise<VizQueryResult> => {
    const response = await api.post(`/api/viz/sessions/${sessionId}/query`, { spec });
    return response.data;
};

export const listVisualizations = async (sessionId: string): Promise<SavedViz[]> => {
    const response = await api.get(`/api/viz/sessions/${sessionId}/visualizations`);
    return response.data?.visualizations ?? [];
};

export const createVisualization = async (
    sessionId: string,
    spec: VizSpec,
    title?: string | null
): Promise<{ visualization: SavedViz; data: VizData }> => {
    const response = await api.post(`/api/viz/sessions/${sessionId}/visualizations`, {
        spec,
        title: title ?? null,
    });
    return response.data;
};

export const updateVisualization = async (
    vizId: string,
    spec: VizSpec,
    title?: string | null,
    expectedVersion?: number | null
): Promise<{ visualization: SavedViz; data: VizData }> => {
    const response = await api.put(`/api/viz/visualizations/${vizId}`, {
        spec,
        title: title ?? null,
        expected_version: expectedVersion ?? null,
    });
    return response.data;
};

export const deleteVisualization = async (vizId: string): Promise<void> => {
    await api.delete(`/api/viz/visualizations/${vizId}`);
};

export const getVisualizationData = async (vizId: string): Promise<VizQueryResult> => {
    const response = await api.get(`/api/viz/visualizations/${vizId}/data`);
    return response.data;
};

export const getVisualization = async (vizId: string): Promise<SavedViz> => {
    const response = await api.get(`/api/viz/visualizations/${vizId}`);
    return response.data?.visualization;
};

/** Parse + type-check calculated fields without executing (instant editor feedback). */
export const validateCalculations = async (
    sessionId: string,
    transform: Transform[],
    source?: string
): Promise<{ fields: FieldInfo[]; errors: SpecErrorItem[] }> => {
    const response = await api.post(`/api/viz/sessions/${sessionId}/calculations/validate`, {
        transform,
        source: source ?? 'active',
    });
    return response.data;
};

/** "Ask this chart": analyze a selection; may return an agent-created drill-down viz. */
export const askVizSelection = async (
    sessionId: string,
    body: {
        spec: VizSpec;
        selection: SelectionPayload;
        mode: AskMode;
        question?: string | null;
        viz_id?: string | null;
    }
): Promise<AskResponse> => {
    const response = await api.post(`/api/viz/sessions/${sessionId}/ask`, body);
    return response.data;
};

export const listVizVersions = async (vizId: string): Promise<VizVersion[]> => {
    const response = await api.get(`/api/viz/visualizations/${vizId}/versions`);
    return response.data?.versions ?? [];
};

export const restoreVizVersion = async (
    vizId: string,
    versionN: number,
    expectedVersion: number
): Promise<SavedViz> => {
    const response = await api.post(`/api/viz/visualizations/${vizId}/restore/${versionN}`, null, {
        params: { expected_version: expectedVersion },
    });
    return response.data?.visualization;
};
