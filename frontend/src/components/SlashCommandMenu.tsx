/**
 * Slash-command palette for the chat input.
 *
 * Purely presentational: ChatInput owns the open state, filtering, and
 * keyboard navigation; this renders the fixed-position list (fixed so it
 * escapes the input card's overflow-hidden clipping, same trick as the
 * model dropdown).
 */

import React from 'react';
import { Network, SlidersHorizontal } from 'lucide-react';
import type { TaskType } from '../generated/contracts/taskTypes';

export interface SlashCommand {
    name: TaskType;
    hint: string;
    usage: string;
}

export const SLASH_COMMANDS: SlashCommand[] = [
    {
        name: 'filter',
        hint: 'Create or edit a saved highlight filter for the Data tab',
        usage: '/filter <what to highlight>',
    },
    {
        name: 'ontology',
        hint: 'Define or extend the semantic model — entities, joins, metrics',
        usage: '/ontology <what to define>',
    },
];

const COMMAND_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
    filter: SlidersHorizontal,
    ontology: Network,
};

interface SlashCommandMenuProps {
    commands: SlashCommand[];
    activeIndex: number;
    style: React.CSSProperties;
    onSelect: (command: SlashCommand) => void;
    onHover: (index: number) => void;
}

export default function SlashCommandMenu({
    commands,
    activeIndex,
    style,
    onSelect,
    onHover,
}: SlashCommandMenuProps) {
    if (commands.length === 0) return null;
    return (
        <div
            className="z-[9999] rounded-xl overflow-hidden py-1"
            style={{
                position: 'fixed',
                ...style,
                background: 'var(--c-slate)',
                border: '1px solid var(--c-glass-border)',
                boxShadow: '0 20px 60px rgba(0,0,0,0.45)',
            }}
            role="listbox"
            aria-label="Slash commands"
        >
            {commands.map((cmd, i) => {
                const Icon = COMMAND_ICONS[cmd.name] ?? SlidersHorizontal;
                const active = i === activeIndex;
                return (
                    <button
                        key={cmd.name}
                        type="button"
                        role="option"
                        aria-selected={active}
                        // Fires before the textarea blurs (mousedown < blur < click).
                        onMouseDown={(e) => {
                            e.preventDefault();
                            onSelect(cmd);
                        }}
                        onMouseEnter={() => onHover(i)}
                        className="w-full flex items-start gap-2.5 px-3 py-2 text-left transition-colors"
                        style={{
                            background: active ? 'var(--c-signal-dim)' : 'transparent',
                        }}
                    >
                        <Icon className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                        <span className="min-w-0">
                            <span
                                className="block text-xs font-semibold"
                                style={{
                                    color: active ? 'var(--c-signal-text)' : 'var(--c-ink)',
                                }}
                            >
                                /{cmd.name}
                                <span
                                    className="ml-2 font-normal"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    {cmd.usage}
                                </span>
                            </span>
                            <span
                                className="block text-[11px] truncate"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                {cmd.hint}
                            </span>
                        </span>
                    </button>
                );
            })}
        </div>
    );
}
