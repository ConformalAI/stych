import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Plug, Check, Settings } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { getIntegrations, type IntegrationStatus } from '../services/api';
import { useStore } from '../store/useStore';

export default function IntegrationSelector({ openDown = false }: { openDown?: boolean }) {
    const enabledIntegrations = useStore((s) => s.enabledIntegrations);
    const setEnabledIntegrations = useStore((s) => s.setEnabledIntegrations);
    const navigate = useNavigate();
    const [isOpen, setIsOpen] = useState(false);
    const [integrations, setIntegrations] = useState<IntegrationStatus[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [fetchError, setFetchError] = useState(false);
    const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
    const buttonRef = useRef<HTMLButtonElement>(null);
    const dropdownRef = useRef<HTMLDivElement>(null);

    const fetchIntegrations = useCallback(() => {
        let alive = true;
        setFetchError(false);
        void getIntegrations()
            .then((list) => {
                if (alive) {
                    setIntegrations(list.filter((i) => i.connected));
                    setIsLoading(false);
                }
            })
            .catch(() => {
                if (alive) {
                    setIsLoading(false);
                    setFetchError(true);
                }
            });
        return () => {
            alive = false;
        };
    }, []);

    useEffect(fetchIntegrations, [fetchIntegrations]);

    const openMenu = useCallback(() => {
        if (buttonRef.current) {
            const rect = buttonRef.current.getBoundingClientRect();
            if (openDown) {
                setDropdownStyle({ top: rect.bottom + 6, left: rect.left });
            } else {
                setDropdownStyle({
                    bottom: window.innerHeight - rect.top + 6,
                    right: window.innerWidth - rect.right,
                });
            }
        }
        // Refresh the integration list every time the dropdown is opened so
        // changes made in Settings are reflected without a full page reload.
        fetchIntegrations();
        setIsOpen((p) => !p);
    }, [openDown, fetchIntegrations]);

    useEffect(() => {
        if (!isOpen) return;
        const handler = (e: MouseEvent) => {
            if (
                dropdownRef.current &&
                !dropdownRef.current.contains(e.target as Node) &&
                buttonRef.current &&
                !buttonRef.current.contains(e.target as Node)
            ) {
                setIsOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [isOpen]);

    const connectedIntegrations = integrations.filter((i) => i.connected);
    const hasConnected = connectedIntegrations.length > 0;

    const selectedSet = new Set(
        enabledIntegrations || connectedIntegrations.map((i) => i.server_id)
    );
    const allSelected = enabledIntegrations === null;
    const selectedCount = allSelected ? connectedIntegrations.length : selectedSet.size;

    const toggleIntegration = (serverId: string) => {
        if (allSelected) {
            const all = connectedIntegrations.map((i) => i.server_id);
            const next = all.filter((id) => id !== serverId);
            setEnabledIntegrations(next);
        } else {
            const current = enabledIntegrations || [];
            if (selectedSet.has(serverId)) {
                const next = current.filter((id) => id !== serverId);
                setEnabledIntegrations(next.length === connectedIntegrations.length ? null : next);
            } else {
                const next = [...current, serverId];
                setEnabledIntegrations(next.length === connectedIntegrations.length ? null : next);
            }
        }
    };

    const label = isLoading
        ? 'Integrations'
        : !hasConnected
          ? 'Integrations'
          : selectedCount === connectedIntegrations.length
            ? 'All integrations'
            : selectedCount === 0
              ? 'No integrations'
              : selectedCount === 1
                ? '1 integration'
                : `${selectedCount} integrations`;

    return (
        <div className="relative">
            <button
                ref={buttonRef}
                type="button"
                onClick={openMenu}
                className="flex items-center gap-1.5 px-2 py-1 rounded-md border transition-colors text-xs hover:bg-white/5"
                style={{
                    background: 'transparent',
                    borderColor: 'var(--c-glass-border)',
                    color: 'var(--c-mist-fg)',
                }}
            >
                <Plug className="w-3 h-3" />
                <span>{label}</span>
            </button>

            {isOpen &&
                createPortal(
                    <div
                        ref={dropdownRef}
                        style={{
                            position: 'fixed',
                            ...dropdownStyle,
                            background: 'var(--c-slate)',
                            border: '1px solid var(--c-glass-border)',
                            boxShadow: '0 20px 60px rgba(0,0,0,0.4), 0 4px 16px rgba(0,0,0,0.2)',
                        }}
                        className="w-64 rounded-xl z-[9999] p-1.5 max-h-80 overflow-y-auto"
                    >
                        <div
                            className="px-2 py-1.5 text-[10px] uppercase tracking-widest font-semibold"
                            style={{ color: 'var(--c-mist-fg)' }}
                        >
                            Active Integrations
                        </div>
                        {fetchError && (
                            <div className="px-2 py-2 text-xs" style={{ color: 'var(--c-danger)' }}>
                                Failed to load integrations.{' '}
                                <button
                                    type="button"
                                    className="underline"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        fetchIntegrations();
                                    }}
                                >
                                    Retry
                                </button>
                            </div>
                        )}
                        {connectedIntegrations.map((integration) => {
                            const active = selectedSet.has(integration.server_id);
                            return (
                                <button
                                    key={integration.server_id}
                                    type="button"
                                    onClick={() => toggleIntegration(integration.server_id)}
                                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg transition-colors text-left"
                                    style={{ color: 'var(--c-ink)' }}
                                    onMouseEnter={(e) =>
                                        ((e.currentTarget as HTMLElement).style.background =
                                            'var(--c-glass)')
                                    }
                                    onMouseLeave={(e) =>
                                        ((e.currentTarget as HTMLElement).style.background =
                                            'transparent')
                                    }
                                >
                                    <div
                                        className="flex-shrink-0 w-4 h-4 rounded border flex items-center justify-center transition-colors"
                                        style={
                                            active
                                                ? {
                                                      background: 'var(--c-signal)',
                                                      borderColor: 'var(--c-signal)',
                                                  }
                                                : { borderColor: 'var(--c-glass-border)' }
                                        }
                                    >
                                        {active && <Check className="w-3 h-3 text-white" />}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                        <div
                                            className="text-xs font-medium"
                                            style={{ color: 'var(--c-ink)' }}
                                        >
                                            {integration.name}
                                        </div>
                                        <div
                                            className="text-[10px] truncate"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {integration.description}
                                        </div>
                                    </div>
                                </button>
                            );
                        })}
                        {!hasConnected && (
                            <div className="px-2 py-4 text-center">
                                <p className="text-xs mb-3" style={{ color: 'var(--c-ink-2)' }}>
                                    No integrations connected yet.
                                </p>
                                <button
                                    type="button"
                                    onClick={() => {
                                        setIsOpen(false);
                                        navigate('/settings');
                                    }}
                                    className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border transition-colors"
                                    style={{
                                        background: 'var(--c-signal-dim)',
                                        borderColor:
                                            'color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                        color: 'var(--c-signal-text)',
                                    }}
                                >
                                    <Settings className="w-3 h-3" />
                                    Connect in Settings
                                </button>
                            </div>
                        )}
                    </div>,
                    document.body
                )}
        </div>
    );
}
