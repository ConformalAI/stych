import { AlertTriangle } from 'lucide-react';
import type { MarkType, VizSpec } from './types';

interface Props {
    markType: MarkType;
    recommendation: VizSpec | null;
    onApply: (spec: VizSpec) => void;
}

export default function ChartCompatibilityNotice({ markType, recommendation, onApply }: Props) {
    return (
        <div
            role="alert"
            className="flex flex-wrap items-start gap-2 rounded-lg border px-3 py-2 text-xs"
            style={{
                background: 'color-mix(in srgb, var(--c-danger) 8%, transparent)',
                borderColor: 'color-mix(in srgb, var(--c-danger) 30%, transparent)',
                color: 'var(--c-danger)',
            }}
        >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
            <span className="min-w-[180px] flex-1">
                A {markType} chart can&apos;t use these fields in their current positions. Your
                selections were left unchanged.
            </span>
            {recommendation && (
                <button
                    type="button"
                    onClick={() => onApply(recommendation)}
                    className="rounded-md border px-2 py-1 font-semibold"
                    style={{
                        borderColor: 'color-mix(in srgb, var(--c-danger) 35%, transparent)',
                    }}
                >
                    Use recommended arrangement
                </button>
            )}
            {!recommendation && (
                <span className="w-full pl-5">Choose different fields or another chart type.</span>
            )}
        </div>
    );
}
