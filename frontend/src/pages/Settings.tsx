import { type ComponentType, type ReactNode, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Activity,
    RefreshCw,
    Save,
    UserCircle2,
    CreditCard,
    Shield,
    Plug,
    ArrowUpDown,
    Download,
    ChevronLeft,
    ChevronRight,
    X,
    Cpu,
    Check,
    Brain,
    Trash2,
    AlertTriangle,
    Link as LinkIcon,
    Unlink,
    ChevronDown,
    ChevronUp,
} from 'lucide-react';
import Header from '../components/Header';
import { useAuthStore } from '../store/useAuthStore';
import { useStore } from '../store/useStore';
import {
    updateCurrentUser,
    uploadAvatar,
    changePassword,
    getUsageSummary,
    type UsageSummary,
    getUsageEvents,
    exportUsageEventsCsv,
    getUsageEvent,
    type UsageEvent,
    type UsageEventSortBy,
    type SortOrder,
} from '../services/auth';
import {
    getAvailableModels,
    setUserModel,
    checkRouterHealth,
    getMemoryStatus,
    deleteMemoryBank,
    updateMemoryPreference,
    getIntegrations,
    saveIntegrationCredentials,
    deleteIntegrationCredentials,
    startGoogleOAuth,
    type AvailableModel,
    type RouterHealthResponse,
    type MemoryStatus,
    type IntegrationStatus,
} from '../services/api';
import { parseIntegrationOAuthMessage } from '../services/authHeaders';
import MemoryManager from '../components/MemoryManager';
import SettingsShell from '../components/settings/SettingsShell';

function shortModelName(id: string): string {
    const parts = id.split('/');
    return parts[parts.length - 1];
}

type SettingsTab = 'account' | 'billing' | 'security' | 'integrations' | 'models' | 'memory';

const TAB_ITEMS: Array<{
    id: SettingsTab;
    label: string;
    icon: ComponentType<{ className?: string }>;
    implemented: boolean;
}> = [
    { id: 'account', label: 'Account', icon: UserCircle2, implemented: true },
    { id: 'billing', label: 'Billing', icon: CreditCard, implemented: true },
    { id: 'security', label: 'Security', icon: Shield, implemented: true },
    { id: 'models', label: 'Models', icon: Cpu, implemented: true },
    { id: 'memory', label: 'Memory', icon: Brain, implemented: true },
    { id: 'integrations', label: 'Integrations', icon: Plug, implemented: true },
];

const SECTION_META: Record<SettingsTab, { title: string; description: string }> = {
    account: {
        title: 'Account Profile',
        description: 'Edit your public profile details used across the app.',
    },
    billing: {
        title: 'Billing',
        description: 'Credit and token consumption from tracked LiteLLM usage.',
    },
    security: {
        title: 'Security',
        description: 'Manage your account password and login security.',
    },
    models: {
        title: 'Default Model',
        description: 'Pick the model every new session starts with — override per-session in chat.',
    },
    memory: {
        title: 'Long-term Memory',
        description: 'Stych extracts facts from your chats and recalls them in future sessions.',
    },
    integrations: {
        title: 'Integrations',
        description: 'Connect Stych to external services the AI can push results to.',
    },
};

export default function Settings() {
    const navigate = useNavigate();
    const { user, setUser } = useAuthStore();
    const setSelectedModel = useStore((s) => s.setSelectedModel);
    const setStoreModels = useStore((s) => s.setAvailableModels);
    const [activeTab, setActiveTab] = useState<SettingsTab>('account');
    const [handle, setHandle] = useState(user?.handle ?? '');
    const [name, setName] = useState(user?.name ?? '');
    const [profilePicture, setProfilePicture] = useState(user?.profile_picture ?? '');
    const [saving, setSaving] = useState(false);
    const [notice, setNotice] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [avatarUploading, setAvatarUploading] = useState(false);
    const avatarInputRef = useRef<HTMLInputElement>(null);
    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [passwordSaving, setPasswordSaving] = useState(false);
    const [passwordNotice, setPasswordNotice] = useState<string | null>(null);
    const [passwordError, setPasswordError] = useState<string | null>(null);

    const switchTab = (tab: SettingsTab) => {
        setActiveTab(tab);
        // Clear stale feedback from the previous tab so users never see
        // a success/error message that belongs to a different section.
        setNotice(null);
        setError(null);
        setPasswordNotice(null);
        setPasswordError(null);
        setMemoryError(null);
        setMemoryNotice(null);
        setDeleteConfirm(false);
        // Fetch memory status when switching to memory tab
        if (tab === 'memory' && memoryStatus === null) {
            void fetchMemoryStatus();
        }
        // Fetch integrations when switching to integrations tab
        if (tab === 'integrations' && integrations.length === 0) {
            void fetchIntegrations();
        }
    };
    const [usageSummary, setUsageSummary] = useState<UsageSummary | null>(null);
    const [billingLoading, setBillingLoading] = useState(false);
    const [billingError, setBillingError] = useState<string | null>(null);
    const [events, setEvents] = useState<UsageEvent[]>([]);
    const [eventsPage, setEventsPage] = useState(1);
    const [eventsPageSize] = useState(10);
    const [eventsTotal, setEventsTotal] = useState(0);
    const [eventsSortBy, setEventsSortBy] = useState<UsageEventSortBy>('created_at');
    const [eventsSortOrder, setEventsSortOrder] = useState<SortOrder>('desc');
    const [eventsLoading, setEventsLoading] = useState(false);
    const [eventDetail, setEventDetail] = useState<UsageEvent | null>(null);
    const [eventDetailLoading, setEventDetailLoading] = useState(false);

    // Memory tab state
    const [memoryStatus, setMemoryStatus] = useState<MemoryStatus | null>(null);
    const [memoryLoading, setMemoryLoading] = useState(false);
    const [memoryError, setMemoryError] = useState<string | null>(null);
    const [memoryNotice, setMemoryNotice] = useState<string | null>(null);
    const [memoryPrefSaving, setMemoryPrefSaving] = useState(false);
    const [deleteConfirm, setDeleteConfirm] = useState(false);
    const [deleting, setDeleting] = useState(false);

    // Models tab state
    const [modelsLoading, setModelsLoading] = useState(false);
    const [modelsError, setModelsError] = useState<string | null>(null);
    const [availableModels, setAvailableModels] = useState<AvailableModel[]>([]);
    const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
    const [modelSaving, setModelSaving] = useState(false);
    const [modelNotice, setModelNotice] = useState<string | null>(null);
    const [routerHealth, setRouterHealth] = useState<RouterHealthResponse | null>(null);
    const [healthChecking, setHealthChecking] = useState(false);
    const [modelsRefreshing, setModelsRefreshing] = useState(false);

    // Integrations tab state
    const [integrations, setIntegrations] = useState<IntegrationStatus[]>([]);
    const [integrationsLoading, setIntegrationsLoading] = useState(false);
    const [integrationsError, setIntegrationsError] = useState<string | null>(null);
    // Per-server form state: server_id → credential values being edited
    const [integrationForms, setIntegrationForms] = useState<
        Record<string, Record<string, string>>
    >({});
    // Which server's form is currently open
    const [openIntegrationForm, setOpenIntegrationForm] = useState<string | null>(null);
    const [integrationSaving, setIntegrationSaving] = useState<Record<string, boolean>>({});
    const [integrationNotices, setIntegrationNotices] = useState<Record<string, string>>({});
    // Per-server OAuth loading state (keyed by server_id)
    const [googleOAuthLoading, setGoogleOAuthLoading] = useState<Record<string, boolean>>({});

    const fetchIntegrations = async () => {
        setIntegrationsLoading(true);
        setIntegrationsError(null);
        try {
            const data = await getIntegrations();
            setIntegrations(data);
        } catch {
            setIntegrationsError('Failed to load integrations.');
        } finally {
            setIntegrationsLoading(false);
        }
    };

    const handleConnectIntegration = async (serverId: string) => {
        const creds = integrationForms[serverId] ?? {};
        setIntegrationSaving((prev) => ({ ...prev, [serverId]: true }));
        try {
            await saveIntegrationCredentials(serverId, creds);
            setIntegrationNotices((prev) => ({ ...prev, [serverId]: 'Connected successfully.' }));
            setOpenIntegrationForm(null);
            await fetchIntegrations();
        } catch (err: any) {
            setIntegrationNotices((prev) => ({
                ...prev,
                [serverId]:
                    err?.response?.data?.detail || err?.message || 'Failed to save credentials.',
            }));
        } finally {
            setIntegrationSaving((prev) => ({ ...prev, [serverId]: false }));
        }
    };

    const handleDisconnectIntegration = async (serverId: string) => {
        setIntegrationSaving((prev) => ({ ...prev, [serverId]: true }));
        try {
            await deleteIntegrationCredentials(serverId);
            setIntegrationNotices((prev) => ({ ...prev, [serverId]: 'Disconnected.' }));
            await fetchIntegrations();
        } catch (err: any) {
            setIntegrationNotices((prev) => ({
                ...prev,
                [serverId]: err?.response?.data?.detail || err?.message || 'Failed to disconnect.',
            }));
        } finally {
            setIntegrationSaving((prev) => ({ ...prev, [serverId]: false }));
        }
    };

    // Server IDs that use Google OAuth popup flow instead of manual credential form
    const GOOGLE_OAUTH_SERVER_IDS = new Set([
        'google_sheets',
        'google_drive',
        'google_docs',
        'google_slides',
    ]);

    const handleGoogleOAuth = async (serverId: string) => {
        setGoogleOAuthLoading((prev) => ({ ...prev, [serverId]: true }));
        setIntegrationNotices((prev) => ({ ...prev, [serverId]: '' }));
        try {
            const authUrl = await startGoogleOAuth(serverId);
            const popup = window.open(authUrl, `${serverId}_oauth`, 'width=520,height=640');
            if (!popup) {
                setIntegrationNotices((prev) => ({
                    ...prev,
                    [serverId]: 'Popup blocked — allow popups for this page and try again.',
                }));
                setGoogleOAuthLoading((prev) => ({ ...prev, [serverId]: false }));
                return;
            }

            let settled = false;
            const finish = (notice: string | null, refresh: boolean) => {
                if (settled) return;
                settled = true;
                window.removeEventListener('message', onMessage);
                clearInterval(poll);
                try {
                    popup.close();
                } catch {
                    /* ignore */
                }
                if (refresh) void fetchIntegrations();
                if (notice !== null) {
                    setIntegrationNotices((prev) => ({ ...prev, [serverId]: notice }));
                }
                setGoogleOAuthLoading((prev) => ({ ...prev, [serverId]: false }));
            };

            const onMessage = (e: MessageEvent) => {
                if (e.origin !== window.location.origin) return;
                const parsed = parseIntegrationOAuthMessage(e.data, serverId);
                if (parsed) {
                    if (parsed.status === 'success') {
                        finish('Connected successfully.', true);
                    } else {
                        finish(parsed.message || 'Authorization failed.', false);
                    }
                    return;
                }
                // Backward compatibility with older callback pages
                if (e.data === `${serverId}_connected`) {
                    finish('Connected successfully.', true);
                }
            };

            window.addEventListener('message', onMessage);
            const poll = setInterval(() => {
                if (popup.closed) {
                    finish(null, true);
                }
            }, 600);
        } catch (err: any) {
            setIntegrationNotices((prev) => ({
                ...prev,
                [serverId]: err?.response?.data?.detail || err?.message || 'Failed to start OAuth.',
            }));
            setGoogleOAuthLoading((prev) => ({ ...prev, [serverId]: false }));
        }
    };

    const onSaveAccount = async () => {
        if (!user) return;
        setSaving(true);
        setError(null);
        setNotice(null);
        try {
            const updated = await updateCurrentUser({
                handle,
                name,
                profile_picture: profilePicture,
            });
            setUser(updated);
            setNotice('Account profile updated.');
        } catch (err: any) {
            setError(
                err?.response?.data?.detail ||
                    err?.response?.data?.error ||
                    err?.message ||
                    'Failed to update profile.'
            );
        } finally {
            setSaving(false);
        }
    };

    const onAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        setAvatarUploading(true);
        setError(null);
        setNotice(null);
        try {
            const updated = await uploadAvatar(file);
            setUser(updated);
            setProfilePicture(updated.profile_picture ?? '');
            setNotice('Profile image updated.');
        } catch (err: any) {
            setError(
                err?.response?.data?.detail ||
                    err?.response?.data?.error ||
                    err?.message ||
                    'Failed to upload image.'
            );
        } finally {
            setAvatarUploading(false);
            if (avatarInputRef.current) avatarInputRef.current.value = '';
        }
    };

    const onChangePassword = async () => {
        if (!user?.can_change_password) return;
        setPasswordError(null);
        setPasswordNotice(null);

        if (newPassword.length < 8) {
            setPasswordError('New password must be at least 8 characters.');
            return;
        }
        if (newPassword !== confirmPassword) {
            setPasswordError('New password and confirmation do not match.');
            return;
        }
        if (currentPassword === newPassword) {
            setPasswordError('New password must be different from current password.');
            return;
        }

        setPasswordSaving(true);
        try {
            await changePassword(currentPassword, newPassword);
            setCurrentPassword('');
            setNewPassword('');
            setConfirmPassword('');
            setPasswordNotice('Password updated successfully.');
        } catch (err: any) {
            setPasswordError(
                err?.response?.data?.detail ||
                    err?.response?.data?.error ||
                    err?.message ||
                    'Failed to update password.'
            );
        } finally {
            setPasswordSaving(false);
        }
    };

    const fetchMemoryStatus = async () => {
        setMemoryLoading(true);
        setMemoryError(null);
        try {
            const status = await getMemoryStatus();
            setMemoryStatus(status);
        } catch {
            setMemoryError('Could not load memory status.');
        } finally {
            setMemoryLoading(false);
        }
    };

    const onToggleMemoryPreference = async (enabled: boolean) => {
        setMemoryPrefSaving(true);
        setMemoryError(null);
        setMemoryNotice(null);
        try {
            const result = await updateMemoryPreference(enabled);
            setMemoryStatus((prev) => (prev ? { ...prev, user_enabled: result.enabled } : prev));
            setMemoryNotice(
                result.enabled
                    ? 'Long-term memory enabled. Future conversations may be saved and recalled.'
                    : 'Long-term memory disabled. New conversations will not be saved.'
            );
        } catch (err: any) {
            setMemoryError(
                err?.response?.data?.detail || err?.message || 'Failed to update memory preference.'
            );
        } finally {
            setMemoryPrefSaving(false);
        }
    };

    const onDeleteMemory = async () => {
        setDeleting(true);
        setMemoryError(null);
        try {
            await deleteMemoryBank();
            setMemoryStatus((prev) =>
                prev
                    ? {
                          ...prev,
                          bank_exists: false,
                          document_count: 0,
                          fact_count: 0,
                          pending_operations: 0,
                      }
                    : prev
            );
            setMemoryNotice('All memory deleted. Stych will start fresh in future sessions.');
            setDeleteConfirm(false);
        } catch (err: any) {
            setMemoryError(
                err?.response?.data?.detail || err?.message || 'Failed to delete memory.'
            );
        } finally {
            setDeleting(false);
        }
    };

    const toggleSort = (key: UsageEventSortBy) => {
        setEventsPage(1);
        if (eventsSortBy === key) {
            setEventsSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'));
        } else {
            setEventsSortBy(key);
            setEventsSortOrder('desc');
        }
    };

    const totalPages = Math.max(1, Math.ceil(eventsTotal / eventsPageSize));

    const onExportCsv = async () => {
        try {
            await exportUsageEventsCsv({
                days: 30,
                sort_by: eventsSortBy,
                sort_order: eventsSortOrder,
            });
        } catch (err: any) {
            setBillingError(err?.response?.data?.detail || err?.message || 'CSV export failed.');
        }
    };

    const openEventDetail = async (eventId: string) => {
        setEventDetailLoading(true);
        try {
            const detail = await getUsageEvent(eventId);
            setEventDetail(detail);
        } catch (err: any) {
            setBillingError(
                err?.response?.data?.detail || err?.message || 'Failed to load event details.'
            );
        } finally {
            setEventDetailLoading(false);
        }
    };

    useEffect(() => {
        if (activeTab !== 'billing') return;
        let mounted = true;

        const load = async () => {
            setBillingLoading(true);
            setBillingError(null);
            try {
                const summary = await getUsageSummary(30);
                if (mounted) setUsageSummary(summary);
            } catch (err: any) {
                if (mounted) {
                    setBillingError(
                        err?.response?.data?.detail ||
                            err?.message ||
                            'Failed to load billing usage.'
                    );
                }
            } finally {
                if (mounted) setBillingLoading(false);
            }
        };
        load();
        return () => {
            mounted = false;
        };
    }, [activeTab]);

    useEffect(() => {
        if (activeTab !== 'billing') return;
        let mounted = true;

        const loadEvents = async () => {
            setEventsLoading(true);
            try {
                const res = await getUsageEvents({
                    days: 30,
                    page: eventsPage,
                    page_size: eventsPageSize,
                    sort_by: eventsSortBy,
                    sort_order: eventsSortOrder,
                });
                if (!mounted) return;
                setEvents(res.items || []);
                setEventsTotal(res.total_count || 0);
            } catch (err: any) {
                if (mounted) {
                    setBillingError(
                        err?.response?.data?.detail ||
                            err?.message ||
                            'Failed to load billing events.'
                    );
                }
            } finally {
                if (mounted) setEventsLoading(false);
            }
        };
        loadEvents();
        return () => {
            mounted = false;
        };
    }, [activeTab, eventsPage, eventsPageSize, eventsSortBy, eventsSortOrder]);

    const fetchModels = async (opts?: { silent?: boolean }) => {
        if (!opts?.silent) setModelsLoading(true);
        else setModelsRefreshing(true);
        setModelsError(null);
        try {
            const data = await getAvailableModels();
            setAvailableModels(data.models);
            setStoreModels(data.models); // keep ChatInput dropdown in sync
            setSelectedModelId(data.preferred_model);
        } catch (err: any) {
            setModelsError(err?.response?.data?.detail || err?.message || 'Failed to load models.');
        } finally {
            setModelsLoading(false);
            setModelsRefreshing(false);
        }
    };

    useEffect(() => {
        if (activeTab !== 'models') return;
        fetchModels();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTab]);

    const onSelectModel = async (modelId: string | null) => {
        setModelSaving(true);
        setModelNotice(null);
        setModelsError(null);
        try {
            const res = await setUserModel(modelId);
            setSelectedModelId(res.preferred_model);
            setSelectedModel(res.preferred_model);
            setModelNotice(
                res.preferred_model ? `Default model saved.` : 'Reverted to system default.'
            );
        } catch (err: any) {
            setModelsError(err?.response?.data?.detail || err?.message || 'Failed to save model.');
        } finally {
            setModelSaving(false);
        }
    };

    const onCheckHealth = async () => {
        setHealthChecking(true);
        try {
            const res = await checkRouterHealth();
            setRouterHealth(res);
        } catch {
            setRouterHealth({ healthy: false, latency_ms: 0 });
        } finally {
            setHealthChecking(false);
        }
    };

    const meta = SECTION_META[activeTab];
    const headerActions: ReactNode =
        activeTab === 'models' ? (
            <>
                <button
                    type="button"
                    disabled={modelsRefreshing || modelsLoading}
                    onClick={() => fetchModels({ silent: true })}
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs px-3 py-1.5 disabled:opacity-50"
                    title="Re-fetch model list from LiteLLM"
                >
                    <RefreshCw
                        className={`w-3.5 h-3.5 ${modelsRefreshing ? 'animate-spin' : ''}`}
                    />
                    {modelsRefreshing ? 'Refreshing…' : 'Refresh models'}
                </button>
                <button
                    type="button"
                    disabled={healthChecking}
                    onClick={onCheckHealth}
                    className="btn-secondary inline-flex items-center gap-1.5 text-xs px-3 py-1.5 disabled:opacity-50"
                >
                    <Activity className="w-3.5 h-3.5" />
                    {healthChecking ? 'Checking…' : 'Check health'}
                </button>
            </>
        ) : activeTab === 'memory' ? (
            <button
                type="button"
                disabled={memoryLoading}
                onClick={fetchMemoryStatus}
                className="btn-secondary inline-flex items-center gap-1.5 text-xs px-3 py-1.5 disabled:opacity-50"
            >
                <RefreshCw className={`w-3.5 h-3.5 ${memoryLoading ? 'animate-spin' : ''}`} />
                {memoryLoading ? 'Loading…' : 'Refresh'}
            </button>
        ) : null;

    return (
        <div
            className="h-screen flex flex-col transition-colors duration-300"
            style={{ background: 'var(--c-paper)' }}
        >
            <Header isConnected={true} />

            <SettingsShell
                tabs={TAB_ITEMS}
                activeTab={activeTab}
                onSelect={(id) => switchTab(id as SettingsTab)}
                onBack={() => navigate('/chat')}
                title={meta.title}
                description={meta.description}
                headerActions={headerActions}
            >
                {activeTab === 'account' && (
                    <div className="space-y-4">
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div className="space-y-1.5">
                                <label
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Email
                                </label>
                                <input
                                    type="text"
                                    value={user?.email ?? ''}
                                    readOnly
                                    className="input w-full cursor-not-allowed opacity-60"
                                />
                            </div>
                            <div className="space-y-1.5">
                                <label
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Handle
                                </label>
                                <input
                                    type="text"
                                    value={handle}
                                    onChange={(e) => setHandle(e.target.value)}
                                    placeholder="username"
                                    className="input w-full"
                                />
                                <p className="text-[11px]" style={{ color: 'var(--c-mist-fg)' }}>
                                    3-50 chars. Letters, numbers, underscore, hyphen.
                                </p>
                            </div>
                            <div className="space-y-1.5">
                                <label
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Name
                                </label>
                                <input
                                    type="text"
                                    value={name}
                                    onChange={(e) => setName(e.target.value)}
                                    placeholder="Your display name"
                                    className="input w-full"
                                />
                            </div>
                        </div>

                        <div className="space-y-2">
                            <label className="text-xs uppercase tracking-wide text-white/45">
                                Profile Image
                            </label>

                            {/* Avatar preview */}
                            {profilePicture && (
                                <div className="flex items-center gap-3">
                                    <img
                                        src={profilePicture}
                                        alt="Avatar preview"
                                        className="w-14 h-14 rounded-full object-cover border border-white/20 bg-white/10"
                                        onError={(e) =>
                                            ((e.target as HTMLImageElement).style.display = 'none')
                                        }
                                    />
                                </div>
                            )}

                            {/* Upload button */}
                            <div className="flex items-center gap-2">
                                <input
                                    ref={avatarInputRef}
                                    type="file"
                                    accept="image/*"
                                    className="hidden"
                                    onChange={onAvatarChange}
                                />
                                <button
                                    type="button"
                                    onClick={() => avatarInputRef.current?.click()}
                                    disabled={avatarUploading}
                                    className="btn-secondary inline-flex items-center gap-2 disabled:opacity-50 text-sm rounded-lg px-3 py-2"
                                >
                                    {avatarUploading ? 'Uploading…' : 'Upload image'}
                                </button>
                                <span className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                                    or paste a URL below
                                </span>
                            </div>

                            {/* Manual URL fallback */}
                            <input
                                type="url"
                                value={profilePicture}
                                onChange={(e) => setProfilePicture(e.target.value)}
                                placeholder="https://…"
                                className="input w-full"
                            />
                            <p className="text-[11px]" style={{ color: 'var(--c-mist-fg)' }}>
                                JPEG / PNG / WebP · max 5 MB · resized to 256 × 256
                            </p>
                        </div>

                        {error && (
                            <div className="text-sm rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-red-200">
                                {error}
                            </div>
                        )}
                        {notice && (
                            <div className="text-sm rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-emerald-200">
                                {notice}
                            </div>
                        )}

                        <div className="pt-2">
                            <button
                                type="button"
                                onClick={onSaveAccount}
                                disabled={saving}
                                className="btn-primary inline-flex items-center gap-2 disabled:opacity-60"
                            >
                                <Save className="w-4 h-4" />
                                {saving ? 'Saving...' : 'Save changes'}
                            </button>
                        </div>
                    </div>
                )}

                {activeTab === 'billing' && (
                    <div className="space-y-4">
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                            <div
                                className="rounded-xl border p-3 sm:p-4"
                                style={{
                                    borderColor: 'var(--c-glass-border)',
                                    background: 'var(--c-glass)',
                                }}
                            >
                                <p
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    Plan
                                </p>
                                <p
                                    className="text-base sm:text-xl font-semibold mt-1"
                                    style={{ color: 'var(--c-ink)' }}
                                >
                                    Pro Trial
                                </p>
                                <p className="text-[11px] sm:text-xs text-amber-300 mt-2">
                                    WIP: Stripe integration
                                </p>
                            </div>
                            <div
                                className="rounded-xl border p-3 sm:p-4"
                                style={{
                                    borderColor: 'var(--c-glass-border)',
                                    background: 'var(--c-glass)',
                                }}
                            >
                                <p
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    30d Credits
                                </p>
                                <p
                                    className="text-base sm:text-xl font-semibold mt-1"
                                    style={{ color: 'var(--c-ink)' }}
                                >
                                    {billingLoading
                                        ? '...'
                                        : (usageSummary?.credits_consumed ?? 0).toFixed(2)}
                                </p>
                                <div
                                    className="mt-3 h-2 rounded overflow-hidden"
                                    style={{ background: 'var(--c-glass-border)' }}
                                >
                                    <div
                                        className="h-full"
                                        style={{
                                            background: 'var(--c-signal)',
                                            width: `${Math.min(
                                                ((usageSummary?.credits_consumed ?? 0) / 500) * 100,
                                                100
                                            )}%`,
                                        }}
                                    />
                                </div>
                            </div>
                            <div
                                className="rounded-xl border p-3 sm:p-4"
                                style={{
                                    borderColor: 'var(--c-glass-border)',
                                    background: 'var(--c-glass)',
                                }}
                            >
                                <p
                                    className="text-xs uppercase tracking-wide"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    30d Tokens
                                </p>
                                <p
                                    className="text-base sm:text-xl font-semibold mt-1"
                                    style={{ color: 'var(--c-ink)' }}
                                >
                                    {billingLoading
                                        ? '...'
                                        : (usageSummary?.total_tokens ?? 0).toLocaleString()}
                                </p>
                                <p
                                    className="text-[11px] sm:text-xs mt-2"
                                    style={{ color: 'var(--c-ink-2)' }}
                                >
                                    Est. ${(usageSummary?.estimated_cost_usd ?? 0).toFixed(4)} USD
                                </p>
                            </div>
                        </div>
                        {billingError && (
                            <div className="text-sm rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-red-200">
                                {billingError}
                            </div>
                        )}
                        <div
                            className="rounded-xl border p-4"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                background: 'var(--c-glass)',
                            }}
                        >
                            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-3">
                                <p
                                    className="text-sm font-semibold"
                                    style={{ color: 'var(--c-ink)' }}
                                >
                                    Request consumption (30d)
                                </p>
                                <button
                                    type="button"
                                    onClick={onExportCsv}
                                    className="btn-secondary inline-flex items-center gap-2 self-start text-xs px-3 py-1.5"
                                >
                                    <Download className="w-3.5 h-3.5" />
                                    Export CSV
                                </button>
                            </div>

                            {/* Desktop table */}
                            <div className="hidden md:block overflow-auto">
                                <table className="w-full text-sm">
                                    <thead>
                                        <tr
                                            className="border-b"
                                            style={{
                                                color: 'var(--c-mist-fg)',
                                                borderColor: 'var(--c-glass-border)',
                                            }}
                                        >
                                            <th className="text-left py-2 pr-3">
                                                <button
                                                    onClick={() => toggleSort('created_at')}
                                                    className="inline-flex items-center gap-1"
                                                >
                                                    Time <ArrowUpDown className="w-3 h-3" />
                                                </button>
                                            </th>
                                            <th className="text-left py-2 pr-3">Model / Agent</th>
                                            <th className="text-right py-2 pr-3">
                                                <button
                                                    onClick={() => toggleSort('total_tokens')}
                                                    className="inline-flex items-center gap-1"
                                                >
                                                    Tokens <ArrowUpDown className="w-3 h-3" />
                                                </button>
                                            </th>
                                            <th className="text-right py-2 pr-3">
                                                <button
                                                    onClick={() => toggleSort('credits_consumed')}
                                                    className="inline-flex items-center gap-1"
                                                >
                                                    Credits <ArrowUpDown className="w-3 h-3" />
                                                </button>
                                            </th>
                                            <th className="text-right py-2">
                                                <button
                                                    onClick={() => toggleSort('estimated_cost_usd')}
                                                    className="inline-flex items-center gap-1"
                                                >
                                                    USD <ArrowUpDown className="w-3 h-3" />
                                                </button>
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {events.map((row) => (
                                            <tr
                                                key={row.id}
                                                className="border-b hover:bg-white/5 cursor-pointer transition-colors"
                                                style={{
                                                    borderColor: 'var(--c-glass-border)',
                                                    color: 'var(--c-ink)',
                                                }}
                                                onClick={() => openEventDetail(row.id)}
                                            >
                                                <td className="py-2 pr-3 whitespace-nowrap text-xs">
                                                    {row.created_at
                                                        ? new Date(row.created_at).toLocaleString()
                                                        : '-'}
                                                </td>
                                                <td className="py-2 pr-3">
                                                    <div className="font-medium">{row.model}</div>
                                                    <div
                                                        className="text-[11px]"
                                                        style={{
                                                            color: 'var(--c-mist-fg)',
                                                        }}
                                                    >
                                                        {row.provider} ·{' '}
                                                        {row.agent_name || 'unknown'}
                                                    </div>
                                                </td>
                                                <td className="py-2 pr-3 text-right">
                                                    {row.total_tokens.toLocaleString()}
                                                </td>
                                                <td className="py-2 pr-3 text-right">
                                                    {row.credits_consumed.toFixed(3)}
                                                </td>
                                                <td className="py-2 text-right">
                                                    ${row.estimated_cost_usd.toFixed(5)}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>

                            {/* Mobile simplified cards */}
                            <div className="md:hidden space-y-2">
                                {events.map((row) => (
                                    <button
                                        key={row.id}
                                        type="button"
                                        onClick={() => openEventDetail(row.id)}
                                        className="w-full text-left rounded-lg border px-3 py-2 hover:bg-white/5 transition-colors"
                                        style={{
                                            borderColor: 'var(--c-glass-border)',
                                            background: 'var(--c-glass)',
                                        }}
                                    >
                                        <div className="flex items-center justify-between gap-2">
                                            <p
                                                className="text-sm font-medium truncate"
                                                style={{ color: 'var(--c-ink)' }}
                                            >
                                                {row.model}
                                            </p>
                                            <p
                                                className="text-xs"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            >
                                                {row.created_at
                                                    ? new Date(row.created_at).toLocaleDateString()
                                                    : '-'}
                                            </p>
                                        </div>
                                        <div className="mt-1 flex items-center gap-1.5 text-[11px]">
                                            <span
                                                className="px-1.5 py-0.5 rounded"
                                                style={{
                                                    background: 'var(--c-signal-dim)',
                                                    color: 'var(--c-signal-text)',
                                                    border: '1px solid color-mix(in srgb, var(--c-signal) 20%, transparent)',
                                                }}
                                            >
                                                {row.total_tokens.toLocaleString()} tok
                                            </span>
                                            <span className="px-1.5 py-0.5 rounded border border-emerald-400/30 bg-emerald-500/10 text-emerald-300">
                                                {row.credits_consumed.toFixed(3)} cr
                                            </span>
                                            <span
                                                className="px-1.5 py-0.5 rounded"
                                                style={{ color: 'var(--c-ink-2)' }}
                                            >
                                                ${row.estimated_cost_usd.toFixed(5)}
                                            </span>
                                        </div>
                                    </button>
                                ))}
                                {eventsLoading && (
                                    <div className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                                        Loading events...
                                    </div>
                                )}
                            </div>

                            <div className="mt-3 flex items-center justify-between">
                                <p className="text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                                    Page {eventsPage} / {totalPages} · {eventsTotal} events
                                </p>
                                <div className="flex items-center gap-1.5">
                                    <button
                                        type="button"
                                        onClick={() => setEventsPage((p) => Math.max(1, p - 1))}
                                        disabled={eventsPage <= 1}
                                        className="w-7 h-7 rounded inline-flex items-center justify-center disabled:opacity-40 hover:bg-white/5 transition-colors"
                                        style={{
                                            border: '1px solid var(--c-glass-border)',
                                            color: 'var(--c-ink-2)',
                                        }}
                                    >
                                        <ChevronLeft className="w-4 h-4" />
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() =>
                                            setEventsPage((p) => Math.min(totalPages, p + 1))
                                        }
                                        disabled={eventsPage >= totalPages}
                                        className="w-7 h-7 rounded inline-flex items-center justify-center disabled:opacity-40 hover:bg-white/5 transition-colors"
                                        style={{
                                            border: '1px solid var(--c-glass-border)',
                                            color: 'var(--c-ink-2)',
                                        }}
                                    >
                                        <ChevronRight className="w-4 h-4" />
                                    </button>
                                </div>
                            </div>
                        </div>
                        <div className="rounded-xl border border-amber-400/30 bg-amber-500/10 p-4">
                            <h3 className="text-amber-200 font-semibold">
                                Billing actions are WIP
                            </h3>
                            <p className="text-sm text-amber-100/80 mt-1">
                                Upgrade, invoices, payment methods, and seat management will be
                                enabled in the real billing integration.
                            </p>
                        </div>
                    </div>
                )}

                {activeTab === 'security' && (
                    <div className="space-y-4">
                        <div
                            className="rounded-xl border p-4 space-y-3"
                            style={{
                                borderColor: 'var(--c-glass-border)',
                                background: 'var(--c-glass)',
                            }}
                        >
                            <h3 className="text-sm font-semibold" style={{ color: 'var(--c-ink)' }}>
                                Change password
                            </h3>
                            {!user?.can_change_password ? (
                                <div className="text-sm rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-amber-300">
                                    Password change is only available for email/password accounts.
                                    Social login accounts (e.g. Google) do not have a password
                                    managed here.
                                </div>
                            ) : (
                                <>
                                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                                        <div className="space-y-1">
                                            <label
                                                className="text-xs uppercase tracking-wide"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            >
                                                Current password
                                            </label>
                                            <input
                                                type="password"
                                                value={currentPassword}
                                                onChange={(e) => setCurrentPassword(e.target.value)}
                                                placeholder="Current password"
                                                className="input w-full"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <label
                                                className="text-xs uppercase tracking-wide"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            >
                                                New password
                                            </label>
                                            <input
                                                type="password"
                                                value={newPassword}
                                                onChange={(e) => setNewPassword(e.target.value)}
                                                placeholder="New password"
                                                className="input w-full"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <label
                                                className="text-xs uppercase tracking-wide"
                                                style={{ color: 'var(--c-mist-fg)' }}
                                            >
                                                Confirm new password
                                            </label>
                                            <input
                                                type="password"
                                                value={confirmPassword}
                                                onChange={(e) => setConfirmPassword(e.target.value)}
                                                placeholder="Confirm new password"
                                                className="input w-full"
                                            />
                                        </div>
                                    </div>

                                    {passwordError && (
                                        <div className="text-sm rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-red-300">
                                            {passwordError}
                                        </div>
                                    )}
                                    {passwordNotice && (
                                        <div className="text-sm rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-emerald-300">
                                            {passwordNotice}
                                        </div>
                                    )}

                                    <button
                                        type="button"
                                        onClick={onChangePassword}
                                        disabled={passwordSaving}
                                        className="btn-primary inline-flex items-center gap-2 disabled:opacity-60"
                                    >
                                        {passwordSaving ? 'Updating...' : 'Change password'}
                                    </button>
                                </>
                            )}
                        </div>
                    </div>
                )}

                {activeTab === 'models' && (
                    <div className="space-y-5">
                        {modelNotice && (
                            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-300">
                                {modelNotice}
                            </div>
                        )}
                        {modelsError && (
                            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-300">
                                {modelsError}
                            </div>
                        )}

                        {/* LiteLLM health result */}
                        {routerHealth && (
                            <div
                                className={`rounded-lg border px-4 py-3 text-sm ${
                                    routerHealth.healthy
                                        ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
                                        : 'bg-red-500/10 border-red-500/30 text-red-300'
                                }`}
                            >
                                <div className="flex items-center gap-2">
                                    <span
                                        className={`w-2 h-2 rounded-full shrink-0 ${routerHealth.healthy ? 'bg-emerald-400' : 'bg-red-400'}`}
                                    />
                                    <span className="font-medium">
                                        LiteLLM — {routerHealth.healthy ? 'online' : 'offline'}
                                    </span>
                                    <span className="text-xs opacity-70">
                                        {routerHealth.latency_ms} ms
                                    </span>
                                </div>
                                {routerHealth.reply && (
                                    <p className="mt-1.5 text-xs opacity-70 italic">
                                        &quot;{routerHealth.reply}&quot;
                                    </p>
                                )}
                            </div>
                        )}

                        {modelsLoading ? (
                            <p className="text-sm" style={{ color: 'var(--c-ink-2)' }}>
                                Loading models…
                            </p>
                        ) : (
                            <>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                    {availableModels.map((model) => {
                                        const isSelected = selectedModelId === model.id;
                                        return (
                                            <button
                                                key={model.id}
                                                type="button"
                                                disabled={modelSaving}
                                                onClick={() =>
                                                    onSelectModel(isSelected ? null : model.id)
                                                }
                                                title={model.id}
                                                className="flex items-center gap-3 rounded-xl border p-3 text-left transition-all hover:bg-white/5 disabled:opacity-60"
                                                style={
                                                    isSelected
                                                        ? {
                                                              background: 'var(--c-signal-dim)',
                                                              borderColor:
                                                                  'color-mix(in srgb, var(--c-signal) 40%, transparent)',
                                                              boxShadow:
                                                                  '0 0 0 1px color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                                          }
                                                        : {
                                                              borderColor: 'var(--c-glass-border)',
                                                              background: 'var(--c-glass)',
                                                          }
                                                }
                                            >
                                                <div
                                                    className="w-4 h-4 rounded-full border shrink-0 flex items-center justify-center"
                                                    style={
                                                        isSelected
                                                            ? {
                                                                  background: 'var(--c-signal)',
                                                                  borderColor: 'var(--c-signal)',
                                                              }
                                                            : {
                                                                  borderColor:
                                                                      'var(--c-glass-border)',
                                                              }
                                                    }
                                                >
                                                    {isSelected && (
                                                        <Check className="w-2.5 h-2.5 text-white" />
                                                    )}
                                                </div>
                                                <p
                                                    className="text-sm font-medium truncate"
                                                    style={{ color: 'var(--c-ink)' }}
                                                >
                                                    {shortModelName(model.id)}
                                                </p>
                                            </button>
                                        );
                                    })}
                                </div>

                                {selectedModelId && (
                                    <button
                                        type="button"
                                        disabled={modelSaving}
                                        className="text-xs underline underline-offset-2 transition-colors disabled:opacity-60"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                        onClick={() => onSelectModel(null)}
                                    >
                                        Reset to system default
                                    </button>
                                )}
                            </>
                        )}
                    </div>
                )}

                {activeTab === 'memory' && (
                    <div className="space-y-5">
                        {memoryNotice && (
                            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-300">
                                {memoryNotice}
                            </div>
                        )}
                        {memoryError && (
                            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-300">
                                {memoryError}
                            </div>
                        )}

                        {/* Status card */}
                        {memoryLoading && !memoryStatus ? (
                            <p className="text-sm italic" style={{ color: 'var(--c-ink-2)' }}>
                                Loading memory status…
                            </p>
                        ) : memoryStatus ? (
                            <>
                                {/* Status + toggle strip */}
                                <div
                                    className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-xl border px-4 py-3.5"
                                    style={{
                                        borderColor: 'var(--c-glass-border)',
                                        background: 'var(--c-glass)',
                                    }}
                                >
                                    <span
                                        className={`w-2.5 h-2.5 rounded-full shrink-0 ${
                                            memoryStatus.enabled &&
                                            memoryStatus.user_enabled &&
                                            memoryStatus.hindsight_ok
                                                ? 'bg-emerald-400'
                                                : 'bg-amber-400'
                                        }`}
                                    />
                                    <div className="flex-1 min-w-0">
                                        <p
                                            className="text-sm font-medium"
                                            style={{ color: 'var(--c-ink)' }}
                                        >
                                            {!memoryStatus.enabled
                                                ? 'Unavailable on server'
                                                : !memoryStatus.user_enabled
                                                  ? 'Off for your account'
                                                  : !memoryStatus.hindsight_ok
                                                    ? 'Server unreachable'
                                                    : 'Active — saving & recalling memory'}
                                        </p>
                                        <p
                                            className="text-xs mt-0.5"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {!memoryStatus.enabled
                                                ? 'Ask your admin to enable HINDSIGHT_ENABLED on the backend.'
                                                : !memoryStatus.user_enabled
                                                  ? 'Your chats will not be saved or recalled until you turn this on.'
                                                  : !memoryStatus.hindsight_ok
                                                    ? 'The memory server is configured but not reachable right now.'
                                                    : 'Facts are extracted from your chats and recalled automatically.'}
                                        </p>
                                    </div>

                                    {/* Quiet delete-all trigger */}
                                    {memoryStatus.enabled &&
                                        memoryStatus.hindsight_ok &&
                                        memoryStatus.bank_exists &&
                                        !deleteConfirm && (
                                            <button
                                                type="button"
                                                onClick={() => setDeleteConfirm(true)}
                                                className="inline-flex items-center gap-1.5 text-xs font-medium rounded-lg px-2.5 py-1.5 transition-colors"
                                                style={{ color: 'var(--c-danger)' }}
                                                onMouseEnter={(e) =>
                                                    (e.currentTarget.style.background =
                                                        'color-mix(in srgb, var(--c-danger) 10%, transparent)')
                                                }
                                                onMouseLeave={(e) =>
                                                    (e.currentTarget.style.background =
                                                        'transparent')
                                                }
                                            >
                                                <Trash2 className="w-3.5 h-3.5" />
                                                Delete all
                                            </button>
                                        )}

                                    {/* Enable toggle */}
                                    <button
                                        type="button"
                                        role="switch"
                                        aria-checked={memoryStatus.user_enabled}
                                        disabled={!memoryStatus.enabled || memoryPrefSaving}
                                        onClick={() =>
                                            onToggleMemoryPreference(!memoryStatus.user_enabled)
                                        }
                                        className="relative inline-flex h-7 w-12 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                        style={
                                            memoryStatus.user_enabled
                                                ? {
                                                      borderColor: 'rgba(52,211,153,0.4)',
                                                      background: 'rgba(52,211,153,0.2)',
                                                  }
                                                : {
                                                      borderColor: 'var(--c-glass-border)',
                                                      background: 'var(--c-glass)',
                                                  }
                                        }
                                    >
                                        <span
                                            className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
                                                memoryStatus.user_enabled
                                                    ? 'translate-x-6'
                                                    : 'translate-x-1'
                                            }`}
                                        />
                                    </button>
                                </div>

                                {/* Inline delete confirm */}
                                {deleteConfirm && memoryStatus.bank_exists && (
                                    <div className="rounded-xl border border-red-500/40 bg-red-500/10 p-4 space-y-3">
                                        <div className="flex items-center gap-2 text-red-300">
                                            <AlertTriangle className="w-4 h-4 shrink-0" />
                                            <p className="text-sm font-semibold">
                                                Delete all memory?
                                            </p>
                                        </div>
                                        <p className="text-xs" style={{ color: 'var(--c-ink-2)' }}>
                                            Permanently erases{' '}
                                            <strong style={{ color: 'var(--c-ink)' }}>
                                                {memoryStatus.fact_count} facts
                                            </strong>{' '}
                                            across{' '}
                                            <strong style={{ color: 'var(--c-ink)' }}>
                                                {memoryStatus.document_count} turns
                                            </strong>
                                            . Stych will have no recall of past sessions. This
                                            cannot be undone.
                                        </p>
                                        <div className="flex items-center gap-2">
                                            <button
                                                type="button"
                                                disabled={deleting}
                                                onClick={onDeleteMemory}
                                                className="inline-flex items-center gap-2 bg-red-600 hover:bg-red-500 disabled:opacity-60 text-white text-sm font-medium rounded-lg px-4 py-2 transition-colors"
                                            >
                                                {deleting ? 'Deleting…' : 'Yes, delete everything'}
                                            </button>
                                            <button
                                                type="button"
                                                disabled={deleting}
                                                onClick={() => setDeleteConfirm(false)}
                                                className="btn-secondary inline-flex items-center gap-2 text-sm px-4 py-2 disabled:opacity-60"
                                            >
                                                Cancel
                                            </button>
                                        </div>
                                    </div>
                                )}

                                {/* Stats grid — only when memory is active for this user */}
                                {memoryStatus.enabled &&
                                    memoryStatus.user_enabled &&
                                    memoryStatus.hindsight_ok &&
                                    (memoryStatus.bank_exists ? (
                                        <div className="space-y-3">
                                            {/* Memory types, broken out to match the graph legend */}
                                            <div className="grid grid-cols-3 gap-3">
                                                {[
                                                    {
                                                        label: 'Experience',
                                                        value: memoryStatus.experience_count.toLocaleString(),
                                                        accent: '#34d399',
                                                    },
                                                    {
                                                        label: 'World Facts',
                                                        value: memoryStatus.world_count.toLocaleString(),
                                                        accent: '#63b3ed',
                                                    },
                                                    {
                                                        label: 'Observations',
                                                        value: memoryStatus.observation_count.toLocaleString(),
                                                        accent: '#fbbf24',
                                                    },
                                                ].map(({ label, value, accent }) => (
                                                    <div
                                                        key={label}
                                                        className="rounded-xl border p-4 text-center"
                                                        style={{
                                                            borderColor: 'var(--c-glass-border)',
                                                            background: 'var(--c-glass)',
                                                        }}
                                                    >
                                                        <p
                                                            className="text-2xl font-semibold"
                                                            style={{ color: accent }}
                                                        >
                                                            {value}
                                                        </p>
                                                        <p
                                                            className="text-xs mt-1 inline-flex items-center gap-1.5"
                                                            style={{ color: 'var(--c-mist-fg)' }}
                                                        >
                                                            <span
                                                                className="w-1.5 h-1.5 rounded-full"
                                                                style={{ background: accent }}
                                                            />
                                                            {label}
                                                        </p>
                                                    </div>
                                                ))}
                                            </div>
                                            {/* Retention + pending */}
                                            <div className="grid grid-cols-2 gap-3">
                                                {[
                                                    {
                                                        label: 'Turns indexed',
                                                        value: memoryStatus.document_count.toLocaleString(),
                                                        highlight: false,
                                                    },
                                                    {
                                                        label: 'Pending',
                                                        value: memoryStatus.pending_operations,
                                                        highlight: memoryStatus.pending_operations > 0,
                                                    },
                                                ].map(({ label, value, highlight }) => (
                                                    <div
                                                        key={label}
                                                        className="rounded-xl border p-4 text-center"
                                                        style={{
                                                            borderColor: 'var(--c-glass-border)',
                                                            background: 'var(--c-glass)',
                                                        }}
                                                    >
                                                        <p
                                                            className="text-2xl font-semibold"
                                                            style={{
                                                                color: highlight
                                                                    ? 'var(--c-warning)'
                                                                    : 'var(--c-ink)',
                                                            }}
                                                        >
                                                            {value}
                                                        </p>
                                                        <p
                                                            className="text-xs mt-1"
                                                            style={{ color: 'var(--c-mist-fg)' }}
                                                        >
                                                            {label}
                                                        </p>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    ) : (
                                        <p
                                            className="text-sm italic"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            No memory yet — start chatting and facts will be
                                            extracted automatically.
                                        </p>
                                    ))}
                            </>
                        ) : null}

                        {/* Memory Manager — knowledge graph + browse & delete */}
                        {memoryStatus && memoryStatus.enabled && (
                            <MemoryManager
                                hindsightEnabled={
                                    !!(
                                        memoryStatus.enabled &&
                                        memoryStatus.user_enabled &&
                                        memoryStatus.hindsight_ok
                                    )
                                }
                                onBankChanged={fetchMemoryStatus}
                            />
                        )}
                    </div>
                )}

                {activeTab === 'integrations' && (
                    <div className="space-y-4">
                        {integrationsError && (
                            <div className="rounded-xl border border-red-400/30 bg-red-500/10 p-3 text-sm text-red-300">
                                {integrationsError}
                            </div>
                        )}

                        {integrationsLoading ? (
                            <div
                                className="flex items-center gap-2 text-sm py-4"
                                style={{ color: 'var(--c-mist-fg)' }}
                            >
                                <RefreshCw className="w-4 h-4 animate-spin" />
                                Loading integrations…
                            </div>
                        ) : (
                            <div className="grid grid-cols-1 gap-3">
                                {integrations.map((integration) => {
                                    const isOpen = openIntegrationForm === integration.server_id;
                                    const formValues =
                                        integrationForms[integration.server_id] ?? {};
                                    const isSaving =
                                        integrationSaving[integration.server_id] ?? false;
                                    const notice = integrationNotices[integration.server_id];
                                    const needsForm = integration.required_credentials.length > 0;
                                    const isWip = ([] as string[]).includes(integration.server_id);

                                    return (
                                        <div
                                            key={integration.server_id}
                                            className="rounded-xl border overflow-hidden"
                                            style={{
                                                borderColor: 'var(--c-glass-border)',
                                                background: 'var(--c-glass)',
                                            }}
                                        >
                                            {/* Card header */}
                                            <div className="flex items-center gap-3 p-4">
                                                <div
                                                    className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 overflow-hidden"
                                                    style={{
                                                        background: 'var(--c-glass)',
                                                        border: '1px solid var(--c-glass-border)',
                                                    }}
                                                >
                                                    {integration.server_id === 'google_sheets' ? (
                                                        /* Google Sheets icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M14 0H4a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6z"
                                                                fill="#23A566"
                                                            />
                                                            <path
                                                                d="M14 0v6h6z"
                                                                fill="#1C8B55"
                                                                opacity=".8"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="10"
                                                                width="12"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="13"
                                                                width="12"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="16"
                                                                width="8"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id === 'google_drive' ? (
                                                        /* Google Drive icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M12 3L4.5 16.5h4L12 11.5l3.5 5H20L12 3z"
                                                                fill="#4285F4"
                                                            />
                                                            <path
                                                                d="M4.5 16.5h15L17 13H7L4.5 16.5z"
                                                                fill="#34A853"
                                                                opacity="0.85"
                                                            />
                                                            <path
                                                                d="M8.5 11.5L12 3l3.5 8.5h-7z"
                                                                fill="#FBBC04"
                                                                opacity="0.9"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id === 'google_docs' ? (
                                                        /* Google Docs icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M14 0H4a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6z"
                                                                fill="#4285F4"
                                                            />
                                                            <path
                                                                d="M14 0v6h6z"
                                                                fill="#3367D6"
                                                                opacity=".8"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="10"
                                                                width="12"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="13"
                                                                width="12"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                            <rect
                                                                x="6"
                                                                y="16"
                                                                width="8"
                                                                height="1.4"
                                                                rx=".7"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id ===
                                                      'google_slides' ? (
                                                        /* Google Slides icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M14 0H4a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6z"
                                                                fill="#F4B400"
                                                            />
                                                            <path
                                                                d="M14 0v6h6z"
                                                                fill="#DB9F00"
                                                                opacity=".8"
                                                            />
                                                            <rect
                                                                x="5"
                                                                y="9"
                                                                width="14"
                                                                height="9"
                                                                rx="1"
                                                                fill="#fff"
                                                                opacity=".9"
                                                            />
                                                            <rect
                                                                x="9"
                                                                y="19"
                                                                width="6"
                                                                height="1.2"
                                                                rx=".6"
                                                                fill="#fff"
                                                                opacity=".7"
                                                            />
                                                            <rect
                                                                x="10.5"
                                                                y="20.2"
                                                                width="3"
                                                                height="1"
                                                                rx=".5"
                                                                fill="#fff"
                                                                opacity=".5"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id === 'opendata' ? (
                                                        /* TryOpenData icon */
                                                        <svg
                                                            viewBox="0 0 32 32"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M 4 18 L 16 23 L 28 18 L 16 13 Z"
                                                                fill="#2563EB"
                                                                opacity="0.35"
                                                            />
                                                            <path
                                                                d="M 4 18 L 16 23 L 16 30 L 4 25 Z"
                                                                fill="#2563EB"
                                                            />
                                                            <path
                                                                d="M 16 23 L 28 18 L 28 25 L 16 30 Z"
                                                                fill="#1E3A5F"
                                                            />
                                                            <path
                                                                d="M 4 13 L 16 18 L 28 13 L 16 8 Z"
                                                                fill="#10B981"
                                                            />
                                                            <path
                                                                d="M 4 9 L 16 14 L 28 9 L 16 4 Z"
                                                                fill="#38BDF8"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id === 'notion' ? (
                                                        /* Notion icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <path
                                                                d="M4 3h11l5 5v13H4V3z"
                                                                fill="#fff"
                                                                opacity="0.9"
                                                            />
                                                            <path
                                                                d="M15 3l5 5h-5V3z"
                                                                fill="#e2e2e2"
                                                            />
                                                            <rect
                                                                x="7"
                                                                y="9"
                                                                width="10"
                                                                height="1.2"
                                                                rx=".6"
                                                                fill="#555"
                                                            />
                                                            <rect
                                                                x="7"
                                                                y="12"
                                                                width="10"
                                                                height="1.2"
                                                                rx=".6"
                                                                fill="#555"
                                                            />
                                                            <rect
                                                                x="7"
                                                                y="15"
                                                                width="7"
                                                                height="1.2"
                                                                rx=".6"
                                                                fill="#555"
                                                            />
                                                        </svg>
                                                    ) : integration.server_id ===
                                                      'yahoo_finance' ? (
                                                        /* Yahoo Finance icon */
                                                        <svg
                                                            viewBox="0 0 24 24"
                                                            className="w-5 h-5"
                                                            xmlns="http://www.w3.org/2000/svg"
                                                        >
                                                            <rect
                                                                width="24"
                                                                height="24"
                                                                rx="4"
                                                                fill="#6001D2"
                                                            />
                                                            <text
                                                                x="12"
                                                                y="17"
                                                                textAnchor="middle"
                                                                fontSize="13"
                                                                fontWeight="bold"
                                                                fill="#fff"
                                                                fontFamily="system-ui,sans-serif"
                                                            >
                                                                Y!
                                                            </text>
                                                        </svg>
                                                    ) : (
                                                        <Plug className="w-4 h-4 text-blue-400" />
                                                    )}
                                                </div>
                                                <div className="flex-1 min-w-0">
                                                    <div className="flex items-center gap-2">
                                                        <span
                                                            className="text-sm font-medium"
                                                            style={{
                                                                color: 'var(--c-ink)',
                                                            }}
                                                        >
                                                            {integration.name}
                                                        </span>
                                                        {isWip ? (
                                                            <span className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/30 text-amber-400">
                                                                Coming soon
                                                            </span>
                                                        ) : integration.connected ? (
                                                            <span className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 border border-emerald-500/30 text-emerald-400">
                                                                <Check className="w-2.5 h-2.5" />
                                                                Connected
                                                            </span>
                                                        ) : (
                                                            <span
                                                                className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full"
                                                                style={{
                                                                    background: 'var(--c-glass)',
                                                                    border: '1px solid var(--c-glass-border)',
                                                                    color: 'var(--c-mist-fg)',
                                                                }}
                                                            >
                                                                Not connected
                                                            </span>
                                                        )}
                                                    </div>
                                                    <p
                                                        className="text-xs mt-0.5 truncate"
                                                        style={{
                                                            color: 'var(--c-mist-fg)',
                                                        }}
                                                    >
                                                        {integration.description}
                                                    </p>
                                                </div>

                                                <div className="flex items-center gap-2 flex-shrink-0">
                                                    {isWip ? (
                                                        <span
                                                            className="px-3 py-1.5 rounded-lg text-xs font-medium cursor-not-allowed"
                                                            style={{
                                                                background: 'var(--c-glass)',
                                                                border: '1px solid var(--c-glass-border)',
                                                                color: 'var(--c-mist-fg)',
                                                                opacity: 0.5,
                                                            }}
                                                        >
                                                            Coming soon
                                                        </span>
                                                    ) : integration.connected ? (
                                                        <button
                                                            type="button"
                                                            onClick={() =>
                                                                handleDisconnectIntegration(
                                                                    integration.server_id
                                                                )
                                                            }
                                                            disabled={isSaving}
                                                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-red-400/20 bg-red-500/10 text-red-400 hover:bg-red-500/20 text-xs font-medium transition-colors disabled:opacity-50"
                                                        >
                                                            <Unlink className="w-3 h-3" />
                                                            Disconnect
                                                        </button>
                                                    ) : (
                                                        <button
                                                            type="button"
                                                            onClick={() => {
                                                                if (
                                                                    GOOGLE_OAUTH_SERVER_IDS.has(
                                                                        integration.server_id
                                                                    )
                                                                ) {
                                                                    void handleGoogleOAuth(
                                                                        integration.server_id
                                                                    );
                                                                } else if (!needsForm) {
                                                                    void handleConnectIntegration(
                                                                        integration.server_id
                                                                    );
                                                                } else {
                                                                    setOpenIntegrationForm(
                                                                        isOpen
                                                                            ? null
                                                                            : integration.server_id
                                                                    );
                                                                }
                                                            }}
                                                            disabled={
                                                                isSaving ||
                                                                !!googleOAuthLoading[
                                                                    integration.server_id
                                                                ]
                                                            }
                                                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
                                                            style={{
                                                                background: 'var(--c-signal-dim)',
                                                                color: 'var(--c-signal-text)',
                                                                border: '1px solid color-mix(in srgb, var(--c-signal) 25%, transparent)',
                                                            }}
                                                        >
                                                            {googleOAuthLoading[
                                                                integration.server_id
                                                            ] ? (
                                                                <RefreshCw className="w-3 h-3 animate-spin" />
                                                            ) : (
                                                                <LinkIcon className="w-3 h-3" />
                                                            )}
                                                            {GOOGLE_OAUTH_SERVER_IDS.has(
                                                                integration.server_id
                                                            )
                                                                ? googleOAuthLoading[
                                                                      integration.server_id
                                                                  ]
                                                                    ? 'Opening…'
                                                                    : 'Connect with Google'
                                                                : 'Connect'}
                                                            {needsForm &&
                                                                !GOOGLE_OAUTH_SERVER_IDS.has(
                                                                    integration.server_id
                                                                ) &&
                                                                (isOpen ? (
                                                                    <ChevronUp className="w-3 h-3 ml-0.5" />
                                                                ) : (
                                                                    <ChevronDown className="w-3 h-3 ml-0.5" />
                                                                ))}
                                                        </button>
                                                    )}
                                                </div>
                                            </div>

                                            {/* Inline credential form */}
                                            {isOpen && !integration.connected && (
                                                <div
                                                    className="border-t p-4 space-y-3"
                                                    style={{
                                                        borderColor: 'var(--c-glass-border)',
                                                        background: 'rgba(0,0,0,0.15)',
                                                    }}
                                                >
                                                    {GOOGLE_OAUTH_SERVER_IDS.has(
                                                        integration.server_id
                                                    ) && (
                                                        <div className="space-y-3">
                                                            {/* Primary: OAuth button */}
                                                            <div className="flex flex-col gap-2">
                                                                <button
                                                                    type="button"
                                                                    onClick={() =>
                                                                        void handleGoogleOAuth(
                                                                            integration.server_id
                                                                        )
                                                                    }
                                                                    disabled={
                                                                        !!googleOAuthLoading[
                                                                            integration.server_id
                                                                        ]
                                                                    }
                                                                    className="flex items-center justify-center gap-2.5 w-full rounded-lg disabled:opacity-60 text-sm font-medium py-2.5 transition-colors hover:bg-white/5"
                                                                    style={{
                                                                        background:
                                                                            'var(--c-glass)',
                                                                        border: '1px solid var(--c-glass-border)',
                                                                        color: 'var(--c-ink)',
                                                                    }}
                                                                >
                                                                    {googleOAuthLoading[
                                                                        integration.server_id
                                                                    ] ? (
                                                                        <RefreshCw className="w-4 h-4 animate-spin" />
                                                                    ) : (
                                                                        /* Google G logo */
                                                                        <svg
                                                                            className="w-4 h-4"
                                                                            viewBox="0 0 24 24"
                                                                            xmlns="http://www.w3.org/2000/svg"
                                                                        >
                                                                            <path
                                                                                d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
                                                                                fill="#4285F4"
                                                                            />
                                                                            <path
                                                                                d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                                                                                fill="#34A853"
                                                                            />
                                                                            <path
                                                                                d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
                                                                                fill="#FBBC05"
                                                                            />
                                                                            <path
                                                                                d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
                                                                                fill="#EA4335"
                                                                            />
                                                                        </svg>
                                                                    )}
                                                                    {googleOAuthLoading[
                                                                        integration.server_id
                                                                    ]
                                                                        ? 'Opening…'
                                                                        : 'Connect with Google'}
                                                                </button>
                                                                <p
                                                                    className="text-[10px] text-center"
                                                                    style={{
                                                                        color: 'var(--c-mist-fg)',
                                                                    }}
                                                                >
                                                                    A popup will open asking you to
                                                                    sign in and approve access to
                                                                    your Google account.
                                                                </p>
                                                            </div>
                                                        </div>
                                                    )}

                                                    {(integration.required_credentials.includes(
                                                        'api_key'
                                                    ) ||
                                                        integration.optional_credentials.includes(
                                                            'api_key'
                                                        )) && (
                                                        <div>
                                                            <label
                                                                className="block text-xs font-medium mb-1.5"
                                                                style={{
                                                                    color: 'var(--c-ink-2)',
                                                                }}
                                                            >
                                                                API Key{' '}
                                                                {integration.optional_credentials.includes(
                                                                    'api_key'
                                                                ) && (
                                                                    <span
                                                                        style={{
                                                                            color: 'var(--c-mist-fg)',
                                                                        }}
                                                                    >
                                                                        (optional)
                                                                    </span>
                                                                )}
                                                            </label>
                                                            <input
                                                                type="password"
                                                                placeholder="od_live_..."
                                                                value={formValues.api_key ?? ''}
                                                                onChange={(e) =>
                                                                    setIntegrationForms((prev) => ({
                                                                        ...prev,
                                                                        [integration.server_id]: {
                                                                            ...formValues,
                                                                            api_key: e.target.value,
                                                                        },
                                                                    }))
                                                                }
                                                                className="input w-full text-xs"
                                                            />
                                                            {integration.server_id ===
                                                                'opendata' && (
                                                                <p
                                                                    className="text-[10px] mt-1"
                                                                    style={{
                                                                        color: 'var(--c-mist-fg)',
                                                                    }}
                                                                >
                                                                    Get your API key at{' '}
                                                                    <a
                                                                        href="https://tryopendata.ai/settings"
                                                                        target="_blank"
                                                                        rel="noopener noreferrer"
                                                                        style={{
                                                                            color: 'var(--c-signal-text)',
                                                                        }}
                                                                        className="underline"
                                                                    >
                                                                        tryopendata.ai/settings
                                                                    </a>{' '}
                                                                    → API Keys. Keys start with{' '}
                                                                    <code
                                                                        style={{
                                                                            color: 'var(--c-ink-2)',
                                                                        }}
                                                                    >
                                                                        od_live_
                                                                    </code>
                                                                </p>
                                                            )}
                                                        </div>
                                                    )}

                                                    {notice && (
                                                        <p
                                                            className={`text-xs ${notice.toLowerCase().includes('fail') || notice.toLowerCase().includes('error') ? 'text-red-400' : 'text-emerald-400'}`}
                                                        >
                                                            {notice}
                                                        </p>
                                                    )}

                                                    <div className="flex items-center gap-2 pt-1">
                                                        <button
                                                            type="button"
                                                            onClick={() =>
                                                                void handleConnectIntegration(
                                                                    integration.server_id
                                                                )
                                                            }
                                                            disabled={isSaving}
                                                            className="btn-primary flex items-center gap-1.5 text-xs disabled:opacity-50"
                                                        >
                                                            {isSaving ? (
                                                                <RefreshCw className="w-3 h-3 animate-spin" />
                                                            ) : (
                                                                <Save className="w-3 h-3" />
                                                            )}
                                                            Save & Connect
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={() =>
                                                                setOpenIntegrationForm(null)
                                                            }
                                                            className="btn-secondary px-3 py-1.5 text-xs"
                                                        >
                                                            Cancel
                                                        </button>
                                                    </div>
                                                </div>
                                            )}

                                            {/* Notice for no-form integrations */}
                                            {notice && !isOpen && (
                                                <div className="px-4 pb-3">
                                                    <p
                                                        className={`text-xs ${notice.toLowerCase().includes('fail') || notice.toLowerCase().includes('error') ? 'text-red-400' : 'text-emerald-400'}`}
                                                    >
                                                        {notice}
                                                    </p>
                                                </div>
                                            )}
                                        </div>
                                    );
                                })}

                                {integrations.length === 0 && !integrationsLoading && (
                                    <p
                                        className="text-sm py-4 text-center"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        No integrations available.
                                    </p>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </SettingsShell>

            {eventDetail && (
                <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm p-4 flex items-center justify-center">
                    <div
                        className="w-full max-w-xl rounded-2xl border p-5"
                        style={{
                            background: 'var(--c-slate)',
                            borderColor: 'var(--c-glass-border)',
                            boxShadow: '0 24px 64px rgba(0,0,0,0.5)',
                        }}
                    >
                        <div className="flex items-center justify-between gap-3">
                            <h3
                                className="font-semibold"
                                style={{
                                    color: 'var(--c-ink)',
                                    fontFamily: "'Space Grotesk', sans-serif",
                                }}
                            >
                                Usage event details
                            </h3>
                            <button
                                type="button"
                                onClick={() => setEventDetail(null)}
                                className="w-8 h-8 rounded-full inline-flex items-center justify-center hover:bg-white/10 transition-colors"
                                style={{
                                    border: '1px solid var(--c-glass-border)',
                                    color: 'var(--c-ink-2)',
                                }}
                            >
                                <X className="w-4 h-4" />
                            </button>
                        </div>
                        {eventDetailLoading ? (
                            <p className="text-sm mt-4" style={{ color: 'var(--c-mist-fg)' }}>
                                Loading...
                            </p>
                        ) : (
                            <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                                {[
                                    { label: 'Model', value: eventDetail.model },
                                    {
                                        label: 'Agent / Provider',
                                        value: `${eventDetail.agent_name || 'unknown'} · ${eventDetail.provider}`,
                                    },
                                    {
                                        label: 'Prompt / Completion',
                                        value: `${eventDetail.prompt_tokens.toLocaleString()} / ${eventDetail.completion_tokens.toLocaleString()}`,
                                    },
                                    {
                                        label: 'Latency',
                                        value: eventDetail.latency_ms
                                            ? `${eventDetail.latency_ms.toFixed(1)} ms`
                                            : 'N/A',
                                    },
                                    {
                                        label: 'Tokens / Credits',
                                        value: `${eventDetail.total_tokens.toLocaleString()} / ${eventDetail.credits_consumed.toFixed(3)}`,
                                    },
                                    {
                                        label: 'Estimated Cost',
                                        value: `$${eventDetail.estimated_cost_usd.toFixed(5)}`,
                                    },
                                ].map(({ label, value }) => (
                                    <div
                                        key={label}
                                        className="rounded-lg border p-3"
                                        style={{
                                            borderColor: 'var(--c-glass-border)',
                                            background: 'var(--c-glass)',
                                        }}
                                    >
                                        <p
                                            className="text-xs uppercase"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {label}
                                        </p>
                                        <p className="mt-1" style={{ color: 'var(--c-ink)' }}>
                                            {value}
                                        </p>
                                    </div>
                                ))}
                                <div
                                    className="rounded-lg border p-3 sm:col-span-2"
                                    style={{
                                        borderColor: 'var(--c-glass-border)',
                                        background: 'var(--c-glass)',
                                    }}
                                >
                                    <p
                                        className="text-xs uppercase"
                                        style={{ color: 'var(--c-mist-fg)' }}
                                    >
                                        Session
                                    </p>
                                    <p className="mt-1 break-all" style={{ color: 'var(--c-ink)' }}>
                                        {eventDetail.session_id || 'N/A'}
                                    </p>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}
