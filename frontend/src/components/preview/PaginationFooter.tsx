import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';

interface PaginationFooterProps {
    page: number;
    totalPages: number;
    pageSize: number;
    filteredRows: number;
    totalRows: number;
    truncated?: boolean;
    /** True when filteredRows/totalRows are estimates (exact count timed out). */
    approx?: boolean;
    onPageChange: (page: number) => void;
}

function PagerButton({
    onClick,
    disabled,
    label,
    children,
}: {
    onClick: () => void;
    disabled: boolean;
    label: string;
    children: React.ReactNode;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            disabled={disabled}
            aria-label={label}
            className="p-1.5 rounded-md border transition-colors disabled:opacity-30 disabled:cursor-default hover:enabled:bg-white/5"
            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-ink-2)' }}
        >
            {children}
        </button>
    );
}

export default function PaginationFooter({
    page,
    totalPages,
    pageSize,
    filteredRows,
    totalRows,
    truncated,
    approx,
    onPageChange,
}: PaginationFooterProps) {
    const [draft, setDraft] = useState(String(page));

    useEffect(() => {
        setDraft(String(page));
    }, [page]);

    const commitDraft = () => {
        const parsed = parseInt(draft, 10);
        if (Number.isNaN(parsed)) {
            setDraft(String(page));
            return;
        }
        const clamped = Math.max(1, Math.min(parsed, totalPages));
        setDraft(String(clamped));
        onPageChange(clamped);
    };

    const rangeStart = filteredRows === 0 ? 0 : (page - 1) * pageSize + 1;
    const rangeEnd = Math.min(page * pageSize, filteredRows);

    return (
        <div
            className="flex items-center justify-between gap-3 px-3 py-2 border-t text-xs flex-wrap"
            style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
        >
            <div className="flex items-center gap-2 min-w-0">
                <span
                    title={
                        approx ? 'Estimated count — the exact count timed out' : undefined
                    }
                >
                    {rangeStart.toLocaleString()}–{rangeEnd.toLocaleString()} of{' '}
                    {approx ? '~' : ''}
                    {filteredRows.toLocaleString()}
                    {filteredRows < totalRows &&
                        ` · filtered from ${approx ? '~' : ''}${totalRows.toLocaleString()}`}
                </span>
                {truncated && (
                    <span
                        className="px-1.5 py-0.5 rounded-full border text-[10px]"
                        style={{
                            color: 'var(--c-warning)',
                            borderColor: 'rgba(232,161,58,0.3)',
                            background: 'rgba(232,161,58,0.08)',
                        }}
                        title="This file is large; only the first rows were loaded for preview."
                    >
                        partial preview
                    </span>
                )}
            </div>

            <div className="flex items-center gap-1.5">
                <PagerButton
                    onClick={() => onPageChange(1)}
                    disabled={page <= 1}
                    label="First page"
                >
                    <ChevronsLeft className="w-3.5 h-3.5" />
                </PagerButton>
                <PagerButton
                    onClick={() => onPageChange(page - 1)}
                    disabled={page <= 1}
                    label="Previous page"
                >
                    <ChevronLeft className="w-3.5 h-3.5" />
                </PagerButton>

                <span className="flex items-center gap-1.5 px-1">
                    Page
                    <input
                        type="text"
                        inputMode="numeric"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value.replace(/[^0-9]/g, ''))}
                        onBlur={commitDraft}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') commitDraft();
                        }}
                        className="w-12 px-1.5 py-1 rounded-md border text-center font-mono focus:outline-none"
                        style={{
                            borderColor: 'var(--c-glass-border)',
                            background: 'var(--c-glass)',
                            color: 'var(--c-ink)',
                        }}
                        aria-label="Current page"
                    />
                    of {totalPages.toLocaleString()}
                </span>

                <PagerButton
                    onClick={() => onPageChange(page + 1)}
                    disabled={page >= totalPages}
                    label="Next page"
                >
                    <ChevronRight className="w-3.5 h-3.5" />
                </PagerButton>
                <PagerButton
                    onClick={() => onPageChange(totalPages)}
                    disabled={page >= totalPages}
                    label="Last page"
                >
                    <ChevronsRight className="w-3.5 h-3.5" />
                </PagerButton>
            </div>
        </div>
    );
}
