/**
 * API client for the ontology subsystem (backend/ontology/routes.py).
 */

import { api } from './api';
import type {
    OntologyModel,
    OntologyQueryDimension,
    OntologyQueryResult,
    OntologySpec,
    OntologyVersion,
} from '../components/ontology/types';

/**
 * The user's ontology. `sessionId` is optional validation context — it decides
 * which datasets the spec is checked against — not part of its identity.
 */
export const getOntology = async (sessionId?: string | null): Promise<OntologyModel> => {
    const response = await api.get('/api/ontology', {
        params: sessionId ? { session_id: sessionId } : undefined,
    });
    return response.data;
};

export interface SaveOntologyResult {
    id: string;
    spec: OntologySpec;
    version: number;
    errors: OntologyModel['errors'];
}

export const saveOntology = async (
    sessionId: string | null | undefined,
    spec: OntologySpec,
    expectedVersion?: number,
    summary?: string
): Promise<SaveOntologyResult> => {
    const response = await api.put(
        '/api/ontology',
        { spec, expected_version: expectedVersion, summary },
        { params: sessionId ? { session_id: sessionId } : undefined }
    );
    return response.data;
};

export const certifyDefinitions = async (
    names: string[],
    certified = true
): Promise<SaveOntologyResult & { changed: number }> => {
    const response = await api.post('/api/ontology/certify', { names, certified });
    return response.data;
};

export const queryOntology = async (
    sessionId: string,
    metrics: string[],
    dimensions: OntologyQueryDimension[] = []
): Promise<OntologyQueryResult> => {
    const response = await api.post(`/api/ontology/sessions/${sessionId}/query`, {
        metrics,
        dimensions,
    });
    return response.data;
};

export const listOntologyVersions = async (ontologyId: string): Promise<OntologyVersion[]> => {
    const response = await api.get(`/api/ontology/${ontologyId}/versions`);
    return response.data?.versions ?? [];
};

export const restoreOntologyVersion = async (
    ontologyId: string,
    versionN: number
): Promise<{ id: string; spec: OntologySpec; version: number }> => {
    const response = await api.post(`/api/ontology/${ontologyId}/restore/${versionN}`);
    return response.data;
};

export const acceptProposal = async (
    ontologyId: string,
    versionN: number
): Promise<{ id: string; spec: OntologySpec; version: number; accepted: string[] }> => {
    const response = await api.post(`/api/ontology/${ontologyId}/proposals/${versionN}/accept`);
    return response.data;
};

export const rejectProposal = async (
    ontologyId: string,
    versionN: number
): Promise<{ id: string; spec: OntologySpec; version: number }> => {
    const response = await api.post(`/api/ontology/${ontologyId}/proposals/${versionN}/reject`);
    return response.data;
};
