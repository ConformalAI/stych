import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Paperclip, Sparkles, ChevronDown, Lock, Check, Square } from 'lucide-react';
import type { AvailableModel } from '../services/api';
import IntegrationSelector from './IntegrationSelector';
import SlashCommandMenu, { SLASH_COMMANDS, SlashCommand } from './SlashCommandMenu';
import { getChatDraft, setChatDraft } from '../services/storage';
import type { TaskType } from '../generated/contracts/taskTypes';

const TASK_OPTIONS: Array<{ value: TaskType; label: string }> = [
    { value: 'auto', label: 'Auto' },
    { value: 'analysis', label: 'Analysis' },
    { value: 'cleaning', label: 'Cleaning' },
    { value: 'qa', label: 'Q&A' },
];

/** Strip all provider prefixes, keep just the model slug e.g. "claude-sonnet-4-5" */
function shortModelName(id: string): string {
    const parts = id.split('/');
    return parts[parts.length - 1];
}

/**
 * For LiteLLM model IDs like "openrouter/anthropic/claude-sonnet-4-5"
 * extract a human-readable sub-group, e.g. "anthropic (via openrouter)".
 * Falls back to the raw provider field for non-LiteLLM entries.
 */
function modelGroup(id: string, provider: string): string {
    if (provider !== 'litellm') return provider;
    const parts = id.split('/');
    if (parts.length >= 3) return parts[1];
    if (parts.length === 2) return parts[0];
    return 'litellm';
}

interface ChatInputProps {
    sessionId: string | null;
    onSendMessage: (message: string, taskType?: TaskType) => void;
    onUploadClick?: () => void;
    onStop?: () => void;
    disabled?: boolean;
    models?: AvailableModel[];
    sessionModel?: string | null;
    onModelChange?: (id: string | null) => void;
    sessionHasMessages?: boolean;
    centered?: boolean;
}

export default function ChatInput({
    sessionId,
    onSendMessage,
    onUploadClick,
    onStop,
    disabled = false,
    models = [],
    sessionModel = null,
    onModelChange,
    sessionHasMessages = false,
    centered = false,
}: ChatInputProps) {
    const draftSessionId = sessionId;
    const [message, setMessage] = useState(() => getChatDraft(draftSessionId));
    const [taskType, setTaskType] = useState<TaskType>('auto');
    const [modelOpen, setModelOpen] = useState(false);
    const [inputFocused, setInputFocused] = useState(false);
    const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
    const [slashIndex, setSlashIndex] = useState(0);
    const [slashDismissed, setSlashDismissed] = useState(false);
    const [slashStyle, setSlashStyle] = useState<React.CSSProperties>({});
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const modelBtnRef = useRef<HTMLButtonElement>(null);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const canSend = message.trim().length > 0 && !disabled;
    const modelLabel = sessionModel ? shortModelName(sessionModel) : 'Default model';
    const isModelLocked = sessionHasMessages;

    useEffect(() => {
        setMessage(getChatDraft(draftSessionId));
    }, [draftSessionId]);

    // Slash palette: open while the first token is a partial command ("/", "/fil").
    const slashQuery = /^\/([a-zA-Z]*)$/.exec(message)?.[1]?.toLowerCase() ?? null;
    const slashCommands =
        slashQuery !== null && !slashDismissed
            ? SLASH_COMMANDS.filter((c) => c.name.startsWith(slashQuery))
            : [];
    const slashOpen = slashCommands.length > 0;

    // Auto-resize textarea
    useEffect(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 160) + 'px';
    }, [message]);

    // Open dropdown: when centered open downward; when at bottom open upward.
    const openDropdown = useCallback(() => {
        if (isModelLocked) return;
        if (!modelOpen && modelBtnRef.current) {
            const rect = modelBtnRef.current.getBoundingClientRect();
            if (centered) {
                setDropdownStyle({ top: rect.bottom + 8, right: window.innerWidth - rect.right });
            } else {
                setDropdownStyle({
                    bottom: window.innerHeight - rect.top + 8,
                    right: window.innerWidth - rect.right,
                });
            }
        }
        setModelOpen((p) => !p);
    }, [isModelLocked, modelOpen, centered]);

    // Close dropdown on outside click
    useEffect(() => {
        if (!modelOpen) return;
        const handler = (e: MouseEvent) => {
            if (
                dropdownRef.current &&
                !dropdownRef.current.contains(e.target as Node) &&
                modelBtnRef.current &&
                !modelBtnRef.current.contains(e.target as Node)
            ) {
                setModelOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [modelOpen]);

    // Anchor the slash palette to the textarea (fixed positioning escapes the
    // card's overflow-hidden, like the model dropdown).
    useEffect(() => {
        if (!slashOpen || !textareaRef.current) return;
        const rect = textareaRef.current.getBoundingClientRect();
        const width = Math.min(rect.width, 420);
        setSlashStyle(
            centered
                ? { top: rect.bottom + 12, left: rect.left, width }
                : { bottom: window.innerHeight - rect.top + 12, left: rect.left, width }
        );
    }, [slashOpen, centered]);

    useEffect(() => {
        setSlashIndex(0);
    }, [slashQuery]);

    const handleMessageChange = (value: string) => {
        setMessage(value);
        setChatDraft(draftSessionId, value);
        if (slashDismissed && !value.startsWith('/')) setSlashDismissed(false);
    };

    const completeSlashCommand = (cmd: SlashCommand) => {
        handleMessageChange(`/${cmd.name} `);
        textareaRef.current?.focus();
    };

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!message.trim() || disabled) return;
        // "/filter <request>" routes the remainder straight to the filter agent.
        const slash = /^\/(\w+)\s+([\s\S]+)$/.exec(message.trim());
        const slashCommand = slash
            ? SLASH_COMMANDS.find((command) => command.name === slash[1].toLowerCase())
            : undefined;
        if (slash && slashCommand) {
            onSendMessage(slash[2].trim(), slashCommand.name);
        } else if (/^\/[a-zA-Z]*$/.test(message.trim())) {
            return; // bare "/command" — nothing to send yet
        } else {
            onSendMessage(message.trim(), taskType);
        }
        handleMessageChange('');
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (slashOpen) {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                const delta = e.key === 'ArrowDown' ? 1 : -1;
                setSlashIndex((i) => (i + delta + slashCommands.length) % slashCommands.length);
                return;
            }
            if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                completeSlashCommand(slashCommands[slashIndex]);
                return;
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                setSlashDismissed(true);
                return;
            }
        }
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e);
        }
    };

    const SlashMenu = slashOpen ? (
        <SlashCommandMenu
            commands={slashCommands}
            activeIndex={slashIndex}
            style={slashStyle}
            onSelect={completeSlashCommand}
            onHover={setSlashIndex}
        />
    ) : null;

    // Shared model dropdown panel (rendered via fixed positioning so it escapes clipping)
    const ModelDropdown = modelOpen ? (
        <div
            ref={dropdownRef}
            style={{
                position: 'fixed',
                ...dropdownStyle,
                background: 'var(--c-slate)',
                border: '1px solid var(--c-glass-border)',
                boxShadow: '0 20px 60px rgba(0,0,0,0.45)',
            }}
            className="w-72 max-h-[60vh] overflow-y-auto rounded-xl z-[9999]"
        >
            <div
                className="px-3 py-2 border-b text-[10px] uppercase tracking-widest font-medium"
                style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
            >
                Session model
            </div>
            <button
                type="button"
                onClick={() => {
                    onModelChange?.(null);
                    setModelOpen(false);
                }}
                className="w-full flex items-center gap-3 px-3 py-2.5 text-left text-sm transition-colors hover:bg-white/5"
            >
                <span
                    className="w-4 h-4 rounded-full border flex items-center justify-center shrink-0"
                    style={
                        !sessionModel
                            ? { background: 'var(--c-signal)', borderColor: 'var(--c-signal)' }
                            : { borderColor: 'var(--c-glass-border)' }
                    }
                >
                    {!sessionModel && <Check className="w-2.5 h-2.5 text-white" />}
                </span>
                <div className="min-w-0">
                    <p className="font-medium" style={{ color: 'var(--c-ink)' }}>
                        Default model
                    </p>
                    <p className="text-[11px]" style={{ color: 'var(--c-mist-fg)' }}>
                        Uses your account setting
                    </p>
                </div>
            </button>
            <div className="border-t" style={{ borderColor: 'var(--c-glass-border)' }} />
            {Array.from(new Set(models.map((m) => modelGroup(m.id, m.provider)))).map((group) => (
                <div key={group}>
                    <p
                        className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-widest font-medium"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {group}
                    </p>
                    {models
                        .filter((m) => modelGroup(m.id, m.provider) === group)
                        .map((model) => {
                            const isSelected = sessionModel === model.id;
                            return (
                                <button
                                    key={model.id}
                                    type="button"
                                    onClick={() => {
                                        onModelChange?.(model.id);
                                        setModelOpen(false);
                                    }}
                                    className="w-full flex items-center gap-3 px-3 py-2 text-left text-sm transition-colors hover:bg-white/5"
                                >
                                    <span
                                        className="w-4 h-4 rounded-full border flex items-center justify-center shrink-0"
                                        style={
                                            isSelected
                                                ? {
                                                      background: 'var(--c-signal)',
                                                      borderColor: 'var(--c-signal)',
                                                  }
                                                : { borderColor: 'var(--c-glass-border)' }
                                        }
                                    >
                                        {isSelected && <Check className="w-2.5 h-2.5 text-white" />}
                                    </span>
                                    <div className="min-w-0">
                                        <p
                                            className="font-medium truncate"
                                            style={{ color: 'var(--c-ink)' }}
                                        >
                                            {shortModelName(model.id)}
                                        </p>
                                        {model.is_default && (
                                            <p
                                                className="text-[11px]"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            >
                                                Provider default
                                            </p>
                                        )}
                                    </div>
                                </button>
                            );
                        })}
                </div>
            ))}
        </div>
    ) : null;

    /* ─────────────────────────────────────────────────────────────────────────
       CENTERED  — beautiful floating card for the empty/hero state
       ───────────────────────────────────────────────────────────────────────── */
    if (centered) {
        return (
            <form onSubmit={handleSubmit} className="w-full">
                {/* Gradient-border card */}
                <div
                    className="p-[1.5px] rounded-3xl transition-all duration-300"
                    style={{
                        background: inputFocused
                            ? 'linear-gradient(135deg, var(--c-signal) 0%, color-mix(in srgb, var(--c-signal) 40%, var(--c-slate-2)) 100%)'
                            : 'var(--c-glass-border)',
                        boxShadow: inputFocused
                            ? '0 0 48px var(--c-signal-glow), 0 20px 60px rgba(0,0,0,0.2)'
                            : '0 8px 40px rgba(0,0,0,0.12)',
                    }}
                >
                    <div
                        className="rounded-[calc(1.5rem-1.5px)] overflow-hidden"
                        style={{
                            background: 'var(--c-slate)',
                            backdropFilter: 'blur(24px)',
                        }}
                    >
                        {/* Textarea */}
                        <textarea
                            ref={textareaRef}
                            value={message}
                            onChange={(e) => handleMessageChange(e.target.value)}
                            onKeyDown={handleKeyDown}
                            onFocus={() => setInputFocused(true)}
                            onBlur={() => setInputFocused(false)}
                            placeholder={disabled ? 'Agent is thinking…' : 'Ask about your data…'}
                            className="w-full resize-none bg-transparent px-5 pt-5 pb-3 text-[15px] leading-relaxed focus:outline-none min-h-[84px] max-h-[200px]"
                            style={{
                                color: 'var(--c-ink)',
                            }}
                            rows={2}
                        />

                        {/* Bottom action bar */}
                        <div
                            className="flex items-center justify-between gap-2 px-3 py-2.5 border-t"
                            style={{ borderColor: 'var(--c-glass-border)' }}
                        >
                            {/* Left: task pills + model + integrations */}
                            <div className="flex items-center gap-0.5 min-w-0 overflow-x-auto no-scrollbar">
                                {TASK_OPTIONS.map((opt) => (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => setTaskType(opt.value)}
                                        className="px-2.5 py-1 rounded-full text-xs font-medium transition-all duration-150 whitespace-nowrap flex-shrink-0"
                                        style={
                                            taskType === opt.value
                                                ? {
                                                      background: 'var(--c-signal-dim)',
                                                      color: 'var(--c-signal-text)',
                                                      border: '1px solid color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                                  }
                                                : { color: 'var(--c-mist-fg)' }
                                        }
                                    >
                                        {opt.label}
                                    </button>
                                ))}

                                {models.length > 0 && (
                                    <span
                                        className="w-px h-3.5 mx-1.5 flex-shrink-0"
                                        style={{ background: 'var(--c-glass-border)' }}
                                    />
                                )}

                                {/* Model picker */}
                                {models.length > 0 && (
                                    <button
                                        ref={modelBtnRef}
                                        type="button"
                                        disabled={isModelLocked}
                                        onClick={openDropdown}
                                        title={
                                            isModelLocked
                                                ? 'Model locked for this session.'
                                                : 'Select model'
                                        }
                                        className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-all flex-shrink-0 hover:bg-white/5"
                                        style={{
                                            color: isModelLocked
                                                ? 'var(--c-mist-fg)'
                                                : sessionModel
                                                  ? 'var(--c-signal-text)'
                                                  : 'var(--c-mist-fg)',
                                            cursor: isModelLocked ? 'not-allowed' : undefined,
                                        }}
                                    >
                                        {isModelLocked ? (
                                            <Lock className="w-3 h-3 shrink-0" />
                                        ) : (
                                            <span
                                                className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                                                style={{
                                                    background: sessionModel
                                                        ? 'var(--c-signal)'
                                                        : 'var(--c-mist-fg)',
                                                }}
                                            />
                                        )}
                                        <span className="max-w-[110px] truncate">{modelLabel}</span>
                                        {!isModelLocked && (
                                            <ChevronDown
                                                className={`w-3 h-3 flex-shrink-0 transition-transform ${modelOpen ? 'rotate-180' : ''}`}
                                            />
                                        )}
                                    </button>
                                )}

                                <IntegrationSelector openDown={true} />
                            </div>

                            {/* Right: attach + send */}
                            <div className="flex items-center gap-1.5 flex-shrink-0">
                                {onUploadClick && (
                                    <button
                                        type="button"
                                        onClick={onUploadClick}
                                        disabled={disabled}
                                        title="Add data"
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-colors disabled:opacity-40 hover:bg-white/5"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        <Paperclip className="w-4 h-4" />
                                    </button>
                                )}
                                {disabled && onStop ? (
                                    <button
                                        type="button"
                                        onClick={onStop}
                                        title="Stop generating"
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-all duration-150"
                                        style={{
                                            background: 'rgba(248,113,113,0.15)',
                                            border: '1px solid rgba(248,113,113,0.3)',
                                            color: '#fca5a5',
                                        }}
                                    >
                                        <Square className="w-3.5 h-3.5 fill-current" />
                                    </button>
                                ) : (
                                    <button
                                        type="submit"
                                        disabled={!canSend}
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-all duration-150"
                                        style={
                                            canSend
                                                ? {
                                                      background: 'var(--c-signal)',
                                                      color: '#fff',
                                                      boxShadow: '0 4px 16px var(--c-signal-glow)',
                                                  }
                                                : {
                                                      background: 'var(--c-glass)',
                                                      color: 'var(--c-mist-fg)',
                                                      cursor: 'not-allowed',
                                                  }
                                        }
                                    >
                                        <Send className="w-4 h-4" />
                                    </button>
                                )}
                            </div>
                        </div>
                    </div>
                </div>

                {/* Fixed model dropdown */}
                {ModelDropdown}
                {SlashMenu}

                <p className="text-[11px] mt-2.5 text-center" style={{ color: 'var(--c-mist-fg)' }}>
                    {disabled
                        ? 'Waiting for response…'
                        : 'Enter to send · Shift+Enter for new line'}
                </p>
            </form>
        );
    }

    /* ─────────────────────────────────────────────────────────────────────────
       BOTTOM  — unified floating card at the bottom of the chat view
       ───────────────────────────────────────────────────────────────────────── */
    return (
        <div
            className="px-3 sm:px-5 pb-4 pt-2 sticky bottom-0 z-20"
            style={{
                background: 'linear-gradient(to top, var(--c-paper) 55%, transparent)',
            }}
        >
            <form onSubmit={handleSubmit} className="max-w-3xl mx-auto">
                {/* Gradient-border unified card */}
                <div
                    className="p-[1.5px] rounded-3xl transition-all duration-300"
                    style={{
                        background: inputFocused
                            ? 'linear-gradient(135deg, var(--c-signal) 0%, color-mix(in srgb, var(--c-signal) 40%, var(--c-slate-2)) 100%)'
                            : 'var(--c-glass-border)',
                        boxShadow: inputFocused
                            ? '0 0 32px var(--c-signal-glow), 0 12px 40px rgba(0,0,0,0.2)'
                            : '0 4px 24px rgba(0,0,0,0.12)',
                    }}
                >
                    <div
                        className="rounded-[calc(1.5rem-1.5px)] overflow-hidden"
                        style={{
                            background: 'var(--c-slate)',
                            backdropFilter: 'blur(20px)',
                        }}
                    >
                        {/* Textarea */}
                        <textarea
                            ref={textareaRef}
                            value={message}
                            onChange={(e) => handleMessageChange(e.target.value)}
                            onKeyDown={handleKeyDown}
                            onFocus={() => setInputFocused(true)}
                            onBlur={() => setInputFocused(false)}
                            placeholder={disabled ? 'Agent is thinking…' : 'Ask about your data…'}
                            className="w-full resize-none bg-transparent px-4 pt-4 pb-3 text-sm leading-relaxed focus:outline-none min-h-[52px] max-h-[160px]"
                            style={{ color: 'var(--c-ink)' }}
                            rows={1}
                        />

                        {/* Bottom action bar */}
                        <div
                            className="flex items-center justify-between gap-2 px-3 py-2 border-t"
                            style={{ borderColor: 'var(--c-glass-border)' }}
                        >
                            {/* Left: task pills + model + integrations */}
                            <div className="flex items-center gap-0.5 min-w-0 overflow-x-auto no-scrollbar">
                                <Sparkles
                                    className="w-3 h-3 flex-shrink-0 mr-1"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                />
                                {TASK_OPTIONS.map((opt) => (
                                    <button
                                        key={opt.value}
                                        type="button"
                                        onClick={() => setTaskType(opt.value)}
                                        className="px-2.5 py-1 rounded-full text-xs font-medium transition-all duration-150 whitespace-nowrap flex-shrink-0"
                                        style={
                                            taskType === opt.value
                                                ? {
                                                      background: 'var(--c-signal-dim)',
                                                      color: 'var(--c-signal-text)',
                                                      border: '1px solid color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                                  }
                                                : { color: 'var(--c-mist-fg)' }
                                        }
                                    >
                                        {opt.label}
                                    </button>
                                ))}

                                {models.length > 0 && (
                                    <span
                                        className="w-px h-3 mx-1.5 flex-shrink-0"
                                        style={{ background: 'var(--c-glass-border)' }}
                                    />
                                )}

                                {/* Model picker */}
                                {models.length > 0 && (
                                    <button
                                        ref={modelBtnRef}
                                        type="button"
                                        disabled={isModelLocked}
                                        onClick={openDropdown}
                                        title={
                                            isModelLocked
                                                ? 'Model locked for this session.'
                                                : 'Select model'
                                        }
                                        className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-all flex-shrink-0 hover:bg-white/5"
                                        style={{
                                            color: isModelLocked
                                                ? 'var(--c-mist-fg)'
                                                : sessionModel
                                                  ? 'var(--c-signal-text)'
                                                  : 'var(--c-mist-fg)',
                                            cursor: isModelLocked ? 'not-allowed' : undefined,
                                        }}
                                    >
                                        {isModelLocked ? (
                                            <Lock className="w-3 h-3 shrink-0" />
                                        ) : (
                                            <span
                                                className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                                                style={{
                                                    background: sessionModel
                                                        ? 'var(--c-signal)'
                                                        : 'var(--c-mist-fg)',
                                                }}
                                            />
                                        )}
                                        <span className="max-w-[110px] truncate">{modelLabel}</span>
                                        {!isModelLocked && (
                                            <ChevronDown
                                                className={`w-3 h-3 flex-shrink-0 transition-transform ${modelOpen ? 'rotate-180' : ''}`}
                                            />
                                        )}
                                    </button>
                                )}

                                <IntegrationSelector openDown={false} />
                            </div>

                            {/* Right: attach + send */}
                            <div className="flex items-center gap-1.5 flex-shrink-0">
                                {onUploadClick && (
                                    <button
                                        type="button"
                                        onClick={onUploadClick}
                                        disabled={disabled}
                                        title="Add data"
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-colors disabled:opacity-40 hover:bg-white/5"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        <Paperclip className="w-4 h-4" />
                                    </button>
                                )}
                                {disabled && onStop ? (
                                    <button
                                        type="button"
                                        onClick={onStop}
                                        title="Stop generating"
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-all duration-150"
                                        style={{
                                            background: 'rgba(248,113,113,0.15)',
                                            border: '1px solid rgba(248,113,113,0.3)',
                                            color: '#fca5a5',
                                        }}
                                    >
                                        <Square className="w-3.5 h-3.5 fill-current" />
                                    </button>
                                ) : (
                                    <button
                                        type="submit"
                                        disabled={!canSend}
                                        className="w-8 h-8 rounded-xl flex items-center justify-center transition-all duration-150"
                                        style={
                                            canSend
                                                ? {
                                                      background: 'var(--c-signal)',
                                                      color: '#fff',
                                                      boxShadow: '0 4px 16px var(--c-signal-glow)',
                                                  }
                                                : {
                                                      background: 'var(--c-glass)',
                                                      color: 'var(--c-mist-fg)',
                                                      cursor: 'not-allowed',
                                                  }
                                        }
                                    >
                                        <Send className="w-4 h-4" />
                                    </button>
                                )}
                            </div>
                        </div>
                    </div>
                </div>

                {/* Fixed model dropdown */}
                {ModelDropdown}
                {SlashMenu}

                <p
                    className="text-[10px] sm:text-[11px] mt-1.5 text-center"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    {isModelLocked && sessionModel
                        ? `Model locked to ${shortModelName(sessionModel)} · `
                        : ''}
                    {disabled
                        ? 'Waiting for response… you can keep typing'
                        : 'Enter to send · Shift+Enter for new line'}
                </p>
            </form>
        </div>
    );
}
