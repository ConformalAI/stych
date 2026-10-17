import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ChartCompatibilityNotice from './ChartCompatibilityNotice';
import { emptySpec } from './types';

describe('ChartCompatibilityNotice', () => {
    let container: HTMLDivElement;
    let root: ReturnType<typeof createRoot>;

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
        root = createRoot(container);
    });

    afterEach(async () => {
        await act(async () => root.unmount());
        container.remove();
    });

    it('explains that the fields were preserved and applies the recommendation explicitly', async () => {
        const recommendation = emptySpec();
        const onApply = vi.fn();
        await act(async () =>
            root.render(
                <ChartCompatibilityNotice
                    markType="histogram"
                    recommendation={recommendation}
                    onApply={onApply}
                />
            )
        );

        expect(container.textContent).toContain('Your selections were left unchanged');
        const button = container.querySelector('button') as HTMLButtonElement;
        expect(button.textContent).toContain('Use recommended arrangement');

        await act(async () => button.click());
        expect(onApply).toHaveBeenCalledWith(recommendation);
    });

    it('does not offer an arrangement when the current fields are insufficient', async () => {
        await act(async () =>
            root.render(
                <ChartCompatibilityNotice
                    markType="heatmap"
                    recommendation={null}
                    onApply={vi.fn()}
                />
            )
        );

        expect(container.querySelector('button')).toBeNull();
        expect(container.textContent).toContain('Choose different fields or another chart type');
    });
});
