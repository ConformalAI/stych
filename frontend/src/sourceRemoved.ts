import { clearVizDraft } from './components/visualize/vizDraftStorage';
import { useStore } from './store/useStore';

export const SOURCE_REMOVED_MESSAGE = 'This dataset was removed from the chat.';

export function isSourceRemovedError(error: unknown): boolean {
    const status = (error as { response?: { status?: number } })?.response?.status;
    if (status === 410) return true;
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
    return (
        typeof detail === 'object' &&
        detail !== null &&
        'code' in detail &&
        (detail as { code?: unknown }).code === 'source_removed'
    );
}

export function sourceRemovedDisplay(error: unknown): string {
    const status = (error as { response?: { status?: number } })?.response?.status;
    if (status === 410 || status === 404 || isSourceRemovedError(error)) {
        return SOURCE_REMOVED_MESSAGE;
    }
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data
        ?.detail;
    if (typeof detail === 'string') return detail;
    if (
        detail &&
        typeof detail === 'object' &&
        'message' in detail &&
        typeof (detail as { message?: unknown }).message === 'string'
    ) {
        return (detail as { message: string }).message;
    }
    return SOURCE_REMOVED_MESSAGE;
}

export function forgetRetiredSource(fileId: string): void {
    const state = useStore.getState();
    const { sessionId, openVizId } = state;
    state.removeUploadedFile(fileId);
    state.setOpenVizId(null);
    state.setLastFilterEvent(null);
    state.setOpenFilterId(null);
    if (sessionId) {
        clearVizDraft(sessionId, openVizId);
        clearVizDraft(sessionId, null);
    }
}
