/**
 * The Ontology tab.
 *
 * Three panes, matching the Visualize tab's idiom: definition list on the left,
 * the entity graph in the middle, the inspector on the right. The header strip
 * reports coverage — how much of the model a human has actually signed off on —
 * because that number is the difference between an ontology the agent can be
 * trusted with and one it merely has.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import {
    AlertTriangle,
    BadgeCheck,
    History,
    PanelLeftClose,
    PanelLeftOpen,
    PanelRightClose,
    PanelRightOpen,
    RotateCcw,
    RotateCw,
    Save,
} from 'lucide-react';
import { useStore } from '../../store/useStore';
import DefinitionList from './DefinitionList';
import Inspector from './Inspector';
import MetricPreview from './MetricPreview';
import OntologyCanvas from './OntologyCanvas';
import VersionHistory from './VersionHistory';
import { countCertified, type DefinitionKind, type OntologySpec } from './types';
import { useOntologySpec } from './useOntologySpec';

const LIST_W = 208;
const INSPECTOR_W = 272;
const SLIDE = 'transition-[width] duration-300 ease-out overflow-hidden';

export default function OntologyView() {
    // The ontology outlives any one session, so this view owns no session prop.
    // The session id is only context: it validates the spec against whatever
    // data is loaded, and it is what a metric preview executes against.
    const sessionId = useStore((s) => s.sessionId);
    const {
        ontologyId,
        spec,
        updateSpec,
        version,
        datasets,
        missingDatasets,
        errors,
        selection,
        setSelection,
        loading,
        saving,
        dirty,
        loadError,
        saveError,
        setSaveError,
        conflict,
        save,
        certify,
        restore,
        reload,
        undo,
        redo,
        canUndo,
        canRedo,
    } = useOntologySpec(sessionId);

    const [showList, setShowList] = useState(true);
    const [showInspector, setShowInspector] = useState(true);
    const [showHistory, setShowHistory] = useState(false);

    // Uploading another dataset must refresh the entity picker; otherwise the
    // user cannot build the second entity a relationship needs without reloading.
    const datasetCount = useStore((s) => s.uploadedFiles.length);
    const lastOntologyEvent = useStore((s) => s.lastOntologyEvent);
    const openOntologyName = useStore((s) => s.openOntologyName);
    const setOpenOntologyName = useStore((s) => s.setOpenOntologyName);

    // The agent proposed something while this tab was open — pull it in, unless
    // the user has unsaved edits we would clobber.
    useEffect(() => {
        // Any session of this user's can propose into their one ontology, so
        // this deliberately does not filter on the event's session id.
        if (!lastOntologyEvent) return;
        if (dirty) return;
        void reload();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [lastOntologyEvent?.at]);

    useEffect(() => {
        if (dirty) return;
        void reload();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [datasetCount]);

    // Deep link from the chat proposal card's "Edit" button.
    useEffect(() => {
        if (!openOntologyName) return;
        const kind = findKind(spec, openOntologyName);
        if (kind) {
            setSelection({ kind, name: openOntologyName });
            setOpenOntologyName(null);
        }
    }, [openOntologyName, spec, setSelection, setOpenOntologyName]);

    const coverage = useMemo(() => countCertified(spec), [spec]);
    const draftNames = useMemo(
        () =>
            [...spec.entities, ...spec.relationships, ...spec.dimensions, ...spec.metrics]
                .filter((i) => !i.certified)
                .map((i) => i.name),
        [spec]
    );

    const certifyAll = useCallback(() => {
        if (dirty) return;
        void certify(draftNames, true);
    }, [dirty, draftNames, certify]);

    if (loading) {
        return (
            <div
                className="h-full flex items-center justify-center text-xs"
                style={{ color: 'var(--c-mist-fg)' }}
            >
                Loading ontology…
            </div>
        );
    }

    if (loadError) {
        return (
            <div className="h-full flex flex-col items-center justify-center gap-2">
                <p className="text-xs" style={{ color: 'var(--c-danger)' }}>
                    {loadError}
                </p>
                <button
                    type="button"
                    onClick={() => void reload()}
                    className="btn-secondary text-xs"
                >
                    Try again
                </button>
            </div>
        );
    }

    const selectedMetric = selection?.kind === 'metric' ? selection.name : null;

    return (
        <div className="h-full flex flex-col" style={{ background: 'var(--c-paper)' }}>
            {/* Toolbar */}
            <div
                className="flex items-center gap-2 px-3 py-2 border-b flex-shrink-0"
                style={{ borderColor: 'var(--c-glass-border)' }}
            >
                <button
                    type="button"
                    onClick={() => setShowList((v) => !v)}
                    title={showList ? 'Hide definitions' : 'Show definitions'}
                    className="p-1 rounded transition-colors hover:bg-[var(--c-glass)]"
                    style={{ color: 'var(--c-mist-fg)' }}
                >
                    {showList ? (
                        <PanelLeftClose className="w-3.5 h-3.5" />
                    ) : (
                        <PanelLeftOpen className="w-3.5 h-3.5" />
                    )}
                </button>

                <div className="flex items-center gap-1.5 min-w-0">
                    <span className="text-xs font-medium" style={{ color: 'var(--c-ink)' }}>
                        Ontology
                    </span>
                    <span className="text-[10px] font-mono" style={{ color: 'var(--c-mist-fg)' }}>
                        v{version}
                    </span>
                </div>

                {/* Coverage */}
                <div className="flex items-center gap-1.5 ml-2">
                    <BadgeCheck
                        className="w-3.5 h-3.5"
                        style={{
                            color:
                                coverage.total > 0 && coverage.certified === coverage.total
                                    ? 'var(--c-signal-text)'
                                    : 'var(--c-mist-fg)',
                        }}
                    />
                    <span className="text-[11px]" style={{ color: 'var(--c-mist-fg)' }}>
                        {coverage.certified} of {coverage.total} certified
                    </span>
                    {draftNames.length > 0 && !dirty && (
                        <button
                            type="button"
                            onClick={certifyAll}
                            className="text-[10px] px-1.5 py-0.5 rounded transition-colors"
                            style={{ background: 'var(--c-glass)', color: 'var(--c-ink-2)' }}
                        >
                            Certify all
                        </button>
                    )}
                </div>

                {errors.length > 0 && (
                    <span
                        className="flex items-center gap-1 text-[11px] ml-2"
                        style={{ color: 'var(--c-danger)' }}
                    >
                        <AlertTriangle className="w-3.5 h-3.5" />
                        {errors.length} issue{errors.length === 1 ? '' : 's'}
                    </span>
                )}

                <div className="ml-auto flex items-center gap-1">
                    <button
                        type="button"
                        onClick={undo}
                        disabled={!canUndo}
                        title="Undo"
                        className="p-1 rounded transition-colors hover:bg-[var(--c-glass)] disabled:opacity-30"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <RotateCcw className="w-3.5 h-3.5" />
                    </button>
                    <button
                        type="button"
                        onClick={redo}
                        disabled={!canRedo}
                        title="Redo"
                        className="p-1 rounded transition-colors hover:bg-[var(--c-glass)] disabled:opacity-30"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <RotateCw className="w-3.5 h-3.5" />
                    </button>
                    <button
                        type="button"
                        onClick={() => setShowHistory(true)}
                        title="Version history"
                        className="p-1 rounded transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        <History className="w-3.5 h-3.5" />
                    </button>
                    <button
                        type="button"
                        onClick={() => void save()}
                        disabled={!dirty || saving}
                        className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md transition-colors disabled:opacity-40"
                        style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
                    >
                        <Save className="w-3 h-3" />
                        {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
                    </button>
                    <button
                        type="button"
                        onClick={() => setShowInspector((v) => !v)}
                        title={showInspector ? 'Hide inspector' : 'Show inspector'}
                        className="p-1 rounded transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-mist-fg)' }}
                    >
                        {showInspector ? (
                            <PanelRightClose className="w-3.5 h-3.5" />
                        ) : (
                            <PanelRightOpen className="w-3.5 h-3.5" />
                        )}
                    </button>
                </div>
            </div>

            {(saveError || missingDatasets.length > 0) && (
                <div
                    className="px-3 py-1.5 text-[11px] flex items-center gap-2 border-b"
                    style={{
                        borderColor: 'var(--c-glass-border)',
                        background: 'color-mix(in srgb, var(--c-warning) 10%, transparent)',
                        color: 'var(--c-warning)',
                    }}
                >
                    <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
                    <span className="flex-1">
                        {saveError ??
                            `${missingDatasets.length} dataset(s) referenced by this ontology are not loaded in this session.`}
                    </span>
                    {conflict && (
                        <button
                            type="button"
                            onClick={() => {
                                setSaveError(null);
                                void reload();
                            }}
                            className="underline"
                        >
                            Reload
                        </button>
                    )}
                </div>
            )}

            {/* Panes */}
            <div className="flex-1 flex min-h-0">
                <div
                    className={`${SLIDE} border-r flex-shrink-0 max-lg:absolute max-lg:z-10 max-lg:h-full`}
                    style={{
                        width: showList ? LIST_W : 0,
                        borderColor: 'var(--c-glass-border)',
                        background: 'var(--c-paper)',
                    }}
                >
                    <div style={{ width: LIST_W }} className="h-full">
                        <DefinitionList
                            spec={spec}
                            datasets={datasets}
                            errors={errors}
                            selection={selection}
                            onSelect={setSelection}
                            updateSpec={updateSpec}
                        />
                    </div>
                </div>

                <div className="flex-1 min-w-0 flex flex-col">
                    <div className="flex-1 min-h-0">
                        {spec.entities.length === 0 ? (
                            <EmptyState hasDatasets={datasets.length > 0} />
                        ) : (
                            <ReactFlowProvider>
                                <OntologyCanvas
                                    spec={spec}
                                    datasets={datasets}
                                    errors={errors}
                                    selection={selection}
                                    onSelect={setSelection}
                                    updateSpec={updateSpec}
                                />
                            </ReactFlowProvider>
                        )}
                    </div>
                    {selectedMetric &&
                        (sessionId ? (
                            <MetricPreview
                                sessionId={sessionId}
                                spec={spec}
                                metricName={selectedMetric}
                            />
                        ) : (
                            /* A definition is editable without data; running it
                               is not. Say so rather than rendering an empty
                               preview that looks like a zero. */
                            <div
                                className="px-4 py-3 text-xs border-t"
                                style={{
                                    color: 'var(--c-mist-fg)',
                                    borderColor: 'var(--c-glass-border)',
                                }}
                            >
                                Open a session with data loaded to preview this metric.
                            </div>
                        ))}
                </div>

                <div
                    className={`${SLIDE} border-l flex-shrink-0 max-lg:absolute max-lg:right-0 max-lg:z-10 max-lg:h-full`}
                    style={{
                        width: showInspector ? INSPECTOR_W : 0,
                        borderColor: 'var(--c-glass-border)',
                        background: 'var(--c-paper)',
                    }}
                >
                    <div style={{ width: INSPECTOR_W }} className="h-full overflow-y-auto">
                        <Inspector
                            spec={spec}
                            datasets={datasets}
                            errors={errors}
                            selection={selection}
                            updateSpec={updateSpec}
                            onSelect={setSelection}
                        />
                    </div>
                </div>
            </div>

            {showHistory && ontologyId && (
                <VersionHistory
                    ontologyId={ontologyId}
                    currentVersion={version}
                    onRestore={(n) => {
                        void restore(n);
                        setShowHistory(false);
                    }}
                    onClose={() => setShowHistory(false)}
                />
            )}
        </div>
    );
}

function EmptyState({ hasDatasets }: { hasDatasets: boolean }) {
    return (
        <div className="h-full flex flex-col items-center justify-center gap-2 px-6 text-center">
            <p className="text-sm font-medium" style={{ color: 'var(--c-ink)' }}>
                No entities yet
            </p>
            <p className="text-xs max-w-sm leading-relaxed" style={{ color: 'var(--c-mist-fg)' }}>
                {hasDatasets
                    ? 'Add an entity from a dataset in the left panel, then drag a column from one entity onto another to define how they join. Or just ask the agent in chat — it can draft one for you to review.'
                    : 'Upload a dataset first — entities are built from the data in this session.'}
            </p>
        </div>
    );
}

function findKind(spec: OntologySpec, name: string): DefinitionKind | null {
    if (spec.entities.some((e) => e.name === name)) return 'entity';
    if (spec.relationships.some((r) => r.name === name)) return 'relationship';
    if (spec.dimensions.some((d) => d.name === name)) return 'dimension';
    if (spec.metrics.some((m) => m.name === name)) return 'metric';
    return null;
}
