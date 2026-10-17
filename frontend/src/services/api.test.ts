import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AxiosError, type InternalAxiosRequestConfig } from 'axios';

// Mock auth headers so importing api.ts doesn't pull in the SuperTokens SDK,
// and so the request interceptor has nothing to do during the test.
vi.mock('./authHeaders', () => ({
    getAuthHeaders: vi.fn(async () => ({})),
}));

import { api, deleteSession, uploadFile } from './api';

/**
 * Regression test for the CSV upload 422.
 *
 * The shared axios instance sets a default `Content-Type: application/json`.
 * In axios 1.x, FormData + a JSON content-type makes `transformRequest`
 * serialize the form to JSON (formDataToJSON) instead of sending multipart, so
 * the backend's /api/upload route saw no `file` field and returned 422.
 *
 * We capture the request config *after* axios has run `transformRequest` (by
 * swapping in a fake adapter) and assert the body is still real multipart
 * FormData — not a JSON string.
 */
describe('uploadFile', () => {
    let originalAdapter: typeof api.defaults.adapter;
    let captured: { data: unknown; headers: Record<string, unknown> } | undefined;

    beforeEach(() => {
        originalAdapter = api.defaults.adapter;
        captured = undefined;
        api.defaults.adapter = async (config) => {
            captured = {
                data: config.data,
                headers: config.headers as unknown as Record<string, unknown>,
            };
            return {
                data: {
                    file_id: 'f1',
                    session_id: 'sess-123',
                    filename: 'data.csv',
                    size_mb: 0,
                    rows: 1,
                    columns: 2,
                    column_names: ['a', 'b'],
                    summary: {},
                },
                status: 200,
                statusText: 'OK',
                headers: {},
                config,
            };
        };
    });

    afterEach(() => {
        api.defaults.adapter = originalAdapter;
    });

    it('sends the file as multipart FormData, not a JSON-serialized body', async () => {
        const file = new File(['a,b\n1,2\n'], 'data.csv', { type: 'text/csv' });

        await uploadFile(file, 'sess-123');

        // The regression: if Content-Type stayed application/json, axios would
        // have turned the FormData into a JSON string by this point.
        expect(typeof captured?.data).not.toBe('string');
        expect(captured?.data).toBeInstanceOf(FormData);

        const fd = captured?.data as FormData;
        expect(fd.get('file')).toBeInstanceOf(File);
        expect(fd.get('session_id')).toBe('sess-123');
    });

    it('does not force a JSON content-type on the upload request', async () => {
        const file = new File(['a,b\n1,2\n'], 'data.csv', { type: 'text/csv' });

        await uploadFile(file, 'sess-123');

        const headers = captured?.headers ?? {};
        const contentType = String(
            (headers['Content-Type'] ?? headers['content-type'] ?? '') as string
        );
        expect(contentType).not.toContain('application/json');
    });
});

describe('deleteSession', () => {
    let originalAdapter: typeof api.defaults.adapter;

    beforeEach(() => {
        originalAdapter = api.defaults.adapter;
    });

    afterEach(() => {
        api.defaults.adapter = originalAdapter;
    });

    it('treats a 404 as already deleted', async () => {
        api.defaults.adapter = async (config) => {
            throw new AxiosError(
                'Request failed with status code 404',
                AxiosError.ERR_BAD_REQUEST,
                config as InternalAxiosRequestConfig,
                undefined,
                {
                    status: 404,
                    statusText: 'Not Found',
                    data: { detail: 'Session not found' },
                    headers: {},
                    config,
                }
            );
        };

        const result = await deleteSession('ghost-id');
        expect(result).toEqual({
            message: 'Session deleted successfully',
            session_id: 'ghost-id',
        });
    });

    it('returns the server payload on success', async () => {
        api.defaults.adapter = async (config) => ({
            status: 200,
            statusText: 'OK',
            data: { message: 'Session deleted successfully', session_id: 'real' },
            headers: {},
            config,
        });

        const result = await deleteSession('real');
        expect(result.session_id).toBe('real');
    });

    it('rethrows non-404 failures', async () => {
        api.defaults.adapter = async (config) => {
            throw new AxiosError(
                'Request failed with status code 500',
                AxiosError.ERR_BAD_RESPONSE,
                config as InternalAxiosRequestConfig,
                undefined,
                {
                    status: 500,
                    statusText: 'Internal Server Error',
                    data: { detail: 'boom' },
                    headers: {},
                    config,
                }
            );
        };

        await expect(deleteSession('x')).rejects.toMatchObject({
            response: { status: 500 },
        });
    });
});
