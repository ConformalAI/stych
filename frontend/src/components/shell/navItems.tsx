import { Plug, type LucideIcon } from 'lucide-react';
import AgentMark from './AgentMark';

/**
 * A destination in the primary rail.
 *
 * `feature` marks the one item that carries the product's core loop — it is
 * rendered with more visual weight than the rest, so the agent workspace reads
 * as the way you use the platform rather than as a peer of the admin surfaces.
 */
export interface NavItem {
    id: string;
    label: string;
    /** Shown under the label in the expanded rail and as tooltip body when collapsed. */
    hint: string;
    icon: LucideIcon | ((props: { className?: string }) => JSX.Element);
    path: string;
    feature?: boolean;
}

export interface NavSection {
    id: string;
    /** Rendered as a small caps heading; hidden while the rail is collapsed. */
    label: string | null;
    items: NavItem[];
}

/**
 * The rail's contents. New surfaces are added here and nowhere else — the rail,
 * its tooltips, and the active-route matching all derive from this list.
 */
export const NAV_SECTIONS: NavSection[] = [
    {
        id: 'workspace',
        label: null,
        items: [
            {
                id: 'agent',
                label: 'Agent',
                hint: 'Chat with your data',
                icon: AgentMark,
                path: '/chat',
                feature: true,
            },
        ],
    },
    {
        id: 'manage',
        label: 'Manage',
        items: [
            {
                id: 'connectors',
                label: 'Connectors',
                hint: 'Databases and warehouses',
                icon: Plug,
                path: '/connectors',
            },
        ],
    },
];

export const ALL_NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((s) => s.items);

/** Longest-prefix match, so nested routes keep their parent tab lit. */
export function activeNavId(pathname: string): string | null {
    let best: NavItem | null = null;
    for (const item of ALL_NAV_ITEMS) {
        if (pathname === item.path || pathname.startsWith(`${item.path}/`)) {
            if (!best || item.path.length > best.path.length) best = item;
        }
    }
    return best?.id ?? null;
}
