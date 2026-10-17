import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DatasetSelector from './DatasetSelector';

describe('DatasetSelector', () => {
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

    it('shows every session dataset and switches sources', async () => {
        const onSelect = vi.fn();
        await act(async () =>
            root.render(
                <DatasetSelector
                    datasets={[
                        { file_id: 'f1', filename: 'sales.xlsx' },
                        { file_id: 'f2', filename: 'inventory.csv' },
                    ]}
                    activeSource="f1"
                    onSelect={onSelect}
                />
            )
        );

        const select = container.querySelector('select');
        expect(select).not.toBeNull();
        if (!select) throw new Error('Dataset selector was not rendered');
        expect(Array.from(select.options).map((option) => option.text)).toEqual([
            'sales.xlsx',
            'inventory.csv',
        ]);

        await act(async () => {
            select.value = 'f2';
            select.dispatchEvent(new Event('change', { bubbles: true }));
        });
        expect(onSelect).toHaveBeenCalledWith('f2');
    });
});
