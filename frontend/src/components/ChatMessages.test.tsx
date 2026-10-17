import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore } from '../store/useStore';

const embedLifecycle = vi.hoisted(() => ({ mounts: 0, unmounts: 0 }));

vi.mock('./visualize/VizCard', async () => {
    const React = await import('react');
    return {
        default: function MockVizEmbed() {
            React.useEffect(() => {
                embedLifecycle.mounts += 1;
                return () => {
                    embedLifecycle.unmounts += 1;
                };
            }, []);
            return React.createElement('div', null, 'Embedded chart');
        },
    };
});

import ChatMessages from './ChatMessages';

describe('ChatMessages persisted visualization lifecycle', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;
    let scrollIntoView: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        embedLifecycle.mounts = 0;
        embedLifecycle.unmounts = 0;
        scrollIntoView = vi.fn();
        Object.defineProperty(Element.prototype, 'scrollIntoView', {
            configurable: true,
            value: scrollIntoView,
        });
        useStore.setState({
            messages: [
                {
                    role: 'assistant',
                    content: '```viz\n{"viz_id":"viz-1"}\n```',
                    timestamp: new Date('2026-01-01T00:00:00Z'),
                },
            ],
            resolvedActionPromptIds: [],
            thinkingAgent: null,
            streamingAgent: null,
            liveTraceParts: [],
            streamingContent: null,
            streamingThinkingContent: null,
            isCompacting: false,
        });
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
        useStore.getState().resetSession();
        vi.clearAllMocks();
    });

    it('keeps an existing embed mounted while a new prompt streams', async () => {
        await act(async () => {
            root.render(<ChatMessages isTyping={false} />);
            await Promise.resolve();
        });
        expect(embedLifecycle.mounts).toBe(1);
        expect(embedLifecycle.unmounts).toBe(0);

        await act(async () => {
            useStore.getState().setStreamingContent('Starting a new response');
            root.render(<ChatMessages isTyping />);
            await Promise.resolve();
        });

        expect(embedLifecycle.mounts).toBe(1);
        expect(embedLifecycle.unmounts).toBe(0);
        expect(scrollIntoView).not.toHaveBeenCalled();
    });

    it('renders inline and fenced code without invalid block nesting', async () => {
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        useStore.setState({
            messages: [
                {
                    role: 'assistant',
                    content: 'Use `customer_id` here.\n\n```sql\nSELECT 1\n```',
                    timestamp: new Date('2026-01-01T00:00:00Z'),
                },
            ],
        });

        await act(async () => {
            root.render(<ChatMessages isTyping={false} />);
            await Promise.resolve();
        });

        expect(container.querySelector('p > code')?.textContent).toBe('customer_id');
        expect(container.querySelector('pre > code')?.textContent).toContain('SELECT 1');
        expect(
            consoleError.mock.calls.some((args) => String(args[0]).includes('validateDOMNesting'))
        ).toBe(false);
    });
});
