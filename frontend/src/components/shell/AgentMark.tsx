interface AgentMarkProps {
    className?: string;
}

/**
 * The Agent workspace mark: an orchestrator core with three sub-agents on a
 * partial orbit. It reads as delegation rather than "chat", which is what
 * actually distinguishes this surface — and unlike a generic bot glyph it stays
 * legible down to 16px because the core carries the weight.
 *
 * Drawn on a 24×24 grid, inherits `currentColor` so active/inactive rail states
 * tint it for free.
 */
export default function AgentMark({ className }: AgentMarkProps) {
    return (
        <svg
            viewBox="0 0 24 24"
            className={className}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
        >
            {/* Orbit — broken so the satellites read as sitting on it */}
            <path d="M18.5 7.2a8 8 0 1 1-6.5-3.2" opacity="0.45" />
            {/* Spokes from core to each satellite */}
            <path d="M12 12 19 5" opacity="0.55" />
            <path d="M12 12 5.6 16.4" opacity="0.55" />
            <path d="M12 12 16.8 18.2" opacity="0.55" />
            {/* Orchestrator core */}
            <circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none" />
            {/* Sub-agents */}
            <circle cx="19" cy="5" r="1.9" fill="currentColor" stroke="none" />
            <circle cx="5.6" cy="16.4" r="1.5" fill="currentColor" stroke="none" opacity="0.8" />
            <circle cx="16.8" cy="18.2" r="1.5" fill="currentColor" stroke="none" opacity="0.8" />
        </svg>
    );
}
