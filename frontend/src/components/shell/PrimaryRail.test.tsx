import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PrimaryRail from './PrimaryRail';
import { activeNavId } from './navItems';

describe('activeNavId', () => {
    it('lights the tab that owns the current route', () => {
        expect(activeNavId('/chat')).toBe('agent');
        expect(activeNavId('/connectors')).toBe('connectors');
    });

    it('keeps the parent tab lit on nested routes', () => {
        expect(activeNavId('/connectors/abc123')).toBe('connectors');
    });

    it('returns null off-nav', () => {
        expect(activeNavId('/settings')).toBeNull();
    });
});

describe('PrimaryRail', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    const render = async (expanded: boolean, onToggle = vi.fn()) => {
        await act(async () =>
            root.render(
                <MemoryRouter initialEntries={['/chat']}>
                    <PrimaryRail expanded={expanded} onToggle={onToggle} />
                </MemoryRouter>
            )
        );
        return onToggle;
    };

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('hides labels when collapsed and stays narrow', async () => {
        await render(false);

        const nav = container.querySelector('nav') as HTMLElement;
        expect(nav.style.width).toBe('56px');
        expect(container.textContent).not.toContain('Connectors');
        // Links remain reachable even without visible labels.
        expect(container.querySelectorAll('a')).toHaveLength(2);
    });

    it('shows labels and section headings when expanded', async () => {
        await render(true);

        const nav = container.querySelector('nav') as HTMLElement;
        expect(nav.style.width).toBe('208px');
        expect(container.textContent).toContain('Agent');
        expect(container.textContent).toContain('Connectors');
        expect(container.textContent).toContain('Manage');
    });

    it('marks the active route and links to each destination', async () => {
        await render(true);

        const links = Array.from(container.querySelectorAll('a'));
        expect(links.map((a) => a.getAttribute('href'))).toEqual(['/chat', '/connectors']);
        expect(links[0].getAttribute('aria-current')).toBe('page');
        expect(links[1].getAttribute('aria-current')).toBeNull();
    });

    it('calls onToggle from the collapse control', async () => {
        const onToggle = await render(true);

        const button = container.querySelector('button[aria-expanded]') as HTMLButtonElement;
        await act(async () => {
            button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        });

        expect(onToggle).toHaveBeenCalledTimes(1);
    });
});
