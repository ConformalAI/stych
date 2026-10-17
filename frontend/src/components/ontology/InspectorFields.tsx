/**
 * Small form primitives shared by every inspector form.
 *
 * Kept deliberately plain: they render through the app's CSS variables so the
 * ontology editor inherits the theme rather than restating it.
 */

import type { ReactNode } from 'react';

const inputStyle = {
    background: 'var(--c-glass)',
    borderColor: 'var(--c-glass-border)',
    color: 'var(--c-ink)',
} as const;

const inputClass =
    'w-full rounded-md border px-2 py-1.5 text-xs outline-none focus:border-[var(--c-signal)] transition-colors';

export function Field({
    label,
    hint,
    error,
    children,
}: {
    label: string;
    hint?: string;
    error?: string;
    children: ReactNode;
}) {
    return (
        <label className="block mb-3">
            <span
                className="block text-[10px] uppercase tracking-widest font-semibold mb-1"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                {label}
            </span>
            {children}
            {hint && !error && (
                <span className="block text-[10px] mt-1" style={{ color: 'var(--c-mist-fg)' }}>
                    {hint}
                </span>
            )}
            {error && (
                <span className="block text-[10px] mt-1" style={{ color: 'var(--c-danger)' }}>
                    {error}
                </span>
            )}
        </label>
    );
}

export function TextInput({
    value,
    onChange,
    placeholder,
    mono,
    disabled,
}: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
    mono?: boolean;
    disabled?: boolean;
}) {
    return (
        <input
            type="text"
            value={value}
            disabled={disabled}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
            className={`${inputClass} ${mono ? 'font-mono' : ''}`}
            style={inputStyle}
        />
    );
}

export function TextArea({
    value,
    onChange,
    placeholder,
    rows = 2,
}: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
    rows?: number;
}) {
    return (
        <textarea
            value={value}
            rows={rows}
            placeholder={placeholder}
            onChange={(e) => onChange(e.target.value)}
            className={`${inputClass} resize-y`}
            style={inputStyle}
        />
    );
}

export function Select<T extends string>({
    value,
    onChange,
    options,
    placeholder,
}: {
    value: T | '';
    onChange: (v: T) => void;
    options: readonly { value: T; label: string }[];
    placeholder?: string;
}) {
    return (
        <select
            value={value}
            onChange={(e) => onChange(e.target.value as T)}
            className={inputClass}
            style={inputStyle}
        >
            {placeholder && <option value="">{placeholder}</option>}
            {options.map((o) => (
                <option key={o.value} value={o.value}>
                    {o.label}
                </option>
            ))}
        </select>
    );
}

/** Comma-separated synonym editor — the backend dedupes case-insensitively. */
export function SynonymInput({
    value,
    onChange,
}: {
    value: string[];
    onChange: (v: string[]) => void;
}) {
    return (
        <TextInput
            value={value.join(', ')}
            placeholder="net sales, turnover"
            onChange={(raw) =>
                onChange(
                    raw
                        .split(',')
                        .map((s) => s.trim())
                        .filter(Boolean)
                )
            }
        />
    );
}

export function Toggle({
    checked,
    onChange,
    label,
    description,
}: {
    checked: boolean;
    onChange: (v: boolean) => void;
    label: string;
    description?: string;
}) {
    return (
        <button
            type="button"
            onClick={() => onChange(!checked)}
            className="w-full flex items-start gap-2 rounded-md border px-2 py-2 text-left transition-colors mb-3"
            style={{
                borderColor: checked
                    ? 'color-mix(in srgb, var(--c-signal) 40%, transparent)'
                    : 'var(--c-glass-border)',
                background: checked ? 'var(--c-signal-dim)' : 'var(--c-glass)',
            }}
        >
            <span
                className="mt-0.5 w-3.5 h-3.5 rounded flex-shrink-0 border flex items-center justify-center text-[9px]"
                style={{
                    borderColor: checked ? 'var(--c-signal)' : 'var(--c-mist-fg)',
                    background: checked ? 'var(--c-signal)' : 'transparent',
                    color: 'var(--c-paper)',
                }}
                aria-hidden
            >
                {checked ? '✓' : ''}
            </span>
            <span className="min-w-0">
                <span
                    className="block text-xs font-medium"
                    style={{ color: checked ? 'var(--c-signal-text)' : 'var(--c-ink)' }}
                >
                    {label}
                </span>
                {description && (
                    <span
                        className="block text-[10px] mt-0.5"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {description}
                    </span>
                )}
            </span>
        </button>
    );
}

export function SectionTitle({ children }: { children: ReactNode }) {
    return (
        <p
            className="text-[10px] uppercase tracking-widest font-semibold mb-2"
            style={{ color: 'var(--c-mist-fg)' }}
        >
            {children}
        </p>
    );
}
