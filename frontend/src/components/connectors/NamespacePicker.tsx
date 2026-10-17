import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Loader2 } from 'lucide-react';
import { listRemoteNamespaces } from '../../services/api';

interface NamespacePickerProps {
    connectionId: string;
    label: string;
    value: string;
    onChange: (value: string) => void;
    onSubmit: () => void;
}

export default function NamespacePicker({
    connectionId,
    label,
    value,
    onChange,
    onSubmit,
}: NamespacePickerProps) {
    const rootRef = useRef<HTMLDivElement>(null);
    const [choices, setChoices] = useState<string[] | null>(null);
    const [loading, setLoading] = useState(false);
    const [open, setOpen] = useState(false);
    const [warning, setWarning] = useState<string | null>(null);
    const plural = `${label.toLowerCase()}s`;

    useEffect(() => {
        setChoices(null);
        setWarning(null);
        setOpen(false);
    }, [connectionId]);

    useEffect(() => {
        const close = (event: MouseEvent) => {
            if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', close);
        return () => document.removeEventListener('mousedown', close);
    }, []);

    const showChoices = async () => {
        if (open) {
            setOpen(false);
            return;
        }
        setOpen(true);
        if (choices !== null || loading) return;
        setLoading(true);
        try {
            const result = await listRemoteNamespaces(connectionId);
            setChoices(result.namespaces ?? []);
            setWarning(result.complete ? null : result.warning || 'Discovery was incomplete.');
            if (!value.trim() && result.default_namespace) onChange(result.default_namespace);
        } catch {
            setChoices([]);
            setWarning(`Could not discover ${plural}. You can still enter one manually.`);
        } finally {
            setLoading(false);
        }
    };

    return (
        <div ref={rootRef} className="relative flex-1 min-w-0">
            <div className="flex rounded-lg" style={{ border: '1px solid var(--c-glass-border)' }}>
                <input
                    value={value}
                    onChange={(event) => {
                        onChange(event.target.value);
                        setOpen(false);
                    }}
                    onKeyDown={(event) => event.key === 'Enter' && onSubmit()}
                    placeholder={label}
                    aria-label={label}
                    role="combobox"
                    aria-autocomplete="list"
                    aria-expanded={open}
                    className="min-w-0 flex-1 rounded-l-lg px-2.5 py-1.5 text-xs outline-none"
                    style={{ background: 'var(--c-glass)', color: 'var(--c-ink)' }}
                />
                <button
                    type="button"
                    onClick={showChoices}
                    aria-label={`Show available ${plural}`}
                    aria-expanded={open}
                    title={`Show available ${plural}`}
                    className="px-2 rounded-r-lg transition-colors hover:bg-[var(--c-glass)]"
                    style={{ background: 'var(--c-glass)', color: 'var(--c-mist-fg)' }}
                >
                    {loading ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                        <ChevronDown
                            className={`w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
                        />
                    )}
                </button>
            </div>

            {open && (
                <div
                    role="listbox"
                    aria-label={`Available ${plural}`}
                    className="absolute z-20 left-0 right-0 mt-1 max-h-48 overflow-y-auto rounded-lg border p-1 shadow-xl"
                    style={{
                        background: 'var(--c-slate)',
                        borderColor: 'var(--c-glass-border)',
                    }}
                >
                    {loading ? (
                        <p className="px-2 py-2 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                            Discovering {plural}…
                        </p>
                    ) : choices && choices.length > 0 ? (
                        choices.map((choice) => (
                            <button
                                key={choice}
                                type="button"
                                role="option"
                                aria-selected={choice === value}
                                onClick={() => {
                                    onChange(choice);
                                    setOpen(false);
                                }}
                                className="block w-full rounded-md px-2 py-1.5 text-left text-xs font-mono hover:bg-[var(--c-glass)]"
                                style={{ color: 'var(--c-ink-2)' }}
                            >
                                {choice}
                            </button>
                        ))
                    ) : (
                        <p className="px-2 py-2 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                            No choices were returned. Enter a {label.toLowerCase()} manually.
                        </p>
                    )}
                    {warning && (
                        <p
                            className="mt-1 border-t px-2 py-2 text-[10px]"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                color: 'var(--c-mist-fg)',
                            }}
                        >
                            {warning}
                        </p>
                    )}
                </div>
            )}
        </div>
    );
}
