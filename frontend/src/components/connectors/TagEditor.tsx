import { useState } from 'react';
import { X } from 'lucide-react';

interface TagEditorProps {
    tags: string[];
    /** Every tag already in use, offered as suggestions. */
    suggestions?: string[];
    onChange: (next: string[]) => void;
    disabled?: boolean;
}

/**
 * Chip-style tag input. Normalization mirrors the server's `normalize_tags`
 * (lowercase, trimmed, deduped) so the UI shows what will actually be stored.
 */
export default function TagEditor({
    tags,
    suggestions = [],
    onChange,
    disabled = false,
}: TagEditorProps) {
    const [draft, setDraft] = useState('');

    const commit = (raw: string) => {
        const cleaned = raw.trim().toLowerCase().slice(0, 32);
        if (!cleaned || tags.includes(cleaned) || tags.length >= 20) {
            setDraft('');
            return;
        }
        onChange([...tags, cleaned]);
        setDraft('');
    };

    const unused = suggestions.filter((s) => !tags.includes(s)).slice(0, 6);

    return (
        <div>
            <div
                className="flex flex-wrap items-center gap-1.5 rounded-xl px-2.5 py-2 transition-colors"
                style={{
                    background: 'var(--c-glass)',
                    border: '1px solid var(--c-glass-border)',
                    opacity: disabled ? 0.6 : 1,
                }}
            >
                {tags.map((tag) => (
                    <span
                        key={tag}
                        className="inline-flex items-center gap-1 text-[11px] font-medium rounded px-1.5 py-0.5 border"
                        style={{
                            color: 'var(--c-ink)',
                            background: 'var(--c-slate-2)',
                            borderColor: 'var(--c-glass-border)',
                        }}
                    >
                        {tag}
                        <button
                            type="button"
                            disabled={disabled}
                            aria-label={`Remove tag ${tag}`}
                            onClick={() => onChange(tags.filter((t) => t !== tag))}
                            className="transition-colors hover:opacity-70"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            <X className="w-3 h-3" />
                        </button>
                    </span>
                ))}
                <input
                    value={draft}
                    disabled={disabled || tags.length >= 20}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ',') {
                            e.preventDefault();
                            commit(draft);
                        } else if (e.key === 'Backspace' && !draft && tags.length) {
                            onChange(tags.slice(0, -1));
                        }
                    }}
                    onBlur={() => commit(draft)}
                    placeholder={tags.length ? 'Add tag…' : 'e.g. prod, finance'}
                    className="flex-1 min-w-[100px] bg-transparent outline-none text-xs"
                    style={{ color: 'var(--c-ink)' }}
                />
            </div>

            {unused.length > 0 && (
                <div className="flex flex-wrap items-center gap-1 mt-2">
                    <span className="text-[10px]" style={{ color: 'var(--c-mist-fg)' }}>
                        Existing:
                    </span>
                    {unused.map((tag) => (
                        <button
                            key={tag}
                            type="button"
                            disabled={disabled}
                            onClick={() => commit(tag)}
                            className="text-[10px] rounded px-1.5 py-0.5 border transition-colors hover:bg-[var(--c-glass)]"
                            style={{
                                color: 'var(--c-mist-fg)',
                                borderColor: 'var(--c-glass-border)',
                            }}
                        >
                            + {tag}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
