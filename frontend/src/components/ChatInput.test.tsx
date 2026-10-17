import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./IntegrationSelector', () => ({
    default: () => null,
}));

import ChatInput from './ChatInput';

describe('ChatInput session drafts', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        localStorage.clear();
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        localStorage.clear();
    });

    it('restores the draft belonging to each session', async () => {
        localStorage.setItem('stych_chat_draft:session-1', 'First session draft');
        localStorage.setItem('stych_chat_draft:session-2', 'Second session draft');

        await act(async () => {
            root.render(<ChatInput sessionId="session-1" onSendMessage={vi.fn()} />);
        });
        expect(container.querySelector('textarea')?.value).toBe('First session draft');

        await act(async () => {
            root.render(<ChatInput sessionId="session-2" onSendMessage={vi.fn()} />);
        });
        expect(container.querySelector('textarea')?.value).toBe('Second session draft');
    });

    it('writes changes immediately and removes the draft after submission', async () => {
        const onSendMessage = vi.fn();
        await act(async () => {
            root.render(<ChatInput sessionId="session-1" onSendMessage={onSendMessage} />);
        });

        const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
        await act(async () => {
            const valueSetter = Object.getOwnPropertyDescriptor(
                HTMLTextAreaElement.prototype,
                'value'
            )?.set;
            valueSetter?.call(textarea, 'Analyze the outliers');
            textarea.dispatchEvent(new Event('input', { bubbles: true }));
        });
        expect(localStorage.getItem('stych_chat_draft:session-1')).toBe('Analyze the outliers');

        await act(async () => {
            container
                .querySelector('form')
                ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        });
        expect(onSendMessage).toHaveBeenCalledWith('Analyze the outliers', 'auto');
        expect(localStorage.getItem('stych_chat_draft:session-1')).toBeNull();
    });
});
