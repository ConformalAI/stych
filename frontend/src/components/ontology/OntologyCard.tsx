/**
 * The proposal review card — the human gate on agent-authored semantics.
 *
 * Deliberately NOT a VizCard clone. A chart is its own evidence, so VizCard just
 * renders it. A definition is a claim about what a word means, so this card
 * shows the claim in full and demands a decision: Accept certifies exactly the
 * names the agent added, Reject restores the version it was built on, and Edit
 * hands the user to the Ontology tab with that definition selected.
 *
 * The proposal itself lives in the database, not in this message — the card
 * loads it by id. That is what makes editing real, survive a reload, and stay
 * auditable in the version history.
 */

import { useCallback, useEffect, useState } from 'react';
import {
    AlertTriangle,
    BadgeCheck,
    Check,
    Link2,
    Pencil,
    Sigma,
    Table2,
    Tag,
    X,
} from 'lucide-react';
import {
    acceptProposal,
    getOntology,
    listOntologyVersions,
    rejectProposal,
} from '../../services/ontology';
import { useStore } from '../../store/useStore';
import type { DefinitionKind, OntologySpec } from './types';

interface ProposalPayload {
    ontology_id?: string;
    ontologyId?: string;
    version?: number;
}

type Status = 'loading' | 'pending' | 'accepted' | 'rejected' | 'error' | 'superseded';

const KIND_ICON: Record<DefinitionKind, typeof Sigma> = {
    entity: Table2,
    relationship: Link2,
    dimension: Tag,
    metric: Sigma,
};

interface ProposedItem {
    kind: DefinitionKind;
    name: string;
    summary: string;
    description?: string | null;
}

/** Describe each proposed definition in the terms a reviewer needs to judge it. */
export function describeProposed(spec: OntologySpec, names: string[]): ProposedItem[] {
    const out: ProposedItem[] = [];
    for (const name of names) {
        const entity = spec.entities.find((e) => e.name === name);
        if (entity) {
            out.push({
                kind: 'entity',
                name,
                summary: `from ${entity.datasetLabel || entity.dataset}${
                    entity.primaryKey ? ` · key ${entity.primaryKey}` : ''
                }`,
                description: entity.description,
            });
            continue;
        }
        const rel = spec.relationships.find((r) => r.name === name);
        if (rel) {
            out.push({
                kind: 'relationship',
                name,
                summary: `${rel.fromEntity}.${rel.fromField} → ${rel.toEntity}.${rel.toField} (${rel.cardinality.replace(/_/g, ' ')})`,
                description: rel.description,
            });
            continue;
        }
        const dim = spec.dimensions.find((d) => d.name === name);
        if (dim) {
            out.push({
                kind: 'dimension',
                name,
                summary: `${dim.entity}.${dim.field}`,
                description: dim.description,
            });
            continue;
        }
        const metric = spec.metrics.find((m) => m.name === name);
        if (metric) {
            const body = metric.expr
                ? `${metric.aggregate}(${metric.expr})`
                : `${metric.aggregate}(*)`;
            out.push({
                kind: 'metric',
                name,
                summary: metric.filterExpr ? `${body} where ${metric.filterExpr}` : body,
                description: metric.description,
            });
        }
    }
    return out;
}

function OntologyCard({ ontologyId, version }: { ontologyId: string; version: number }) {
    const [status, setStatus] = useState<Status>('loading');
    const [items, setItems] = useState<ProposedItem[]>([]);
    const [error, setError] = useState<string | null>(null);
    const sessionId = useStore((s) => s.sessionId);
    // A later proposal supersedes this card, but `load` depends only on values
    // that never change for a given card — so without this the card would keep
    // offering Accept/Reject for a definition the user has already revised.
    const lastOntologyEvent = useStore((s) => s.lastOntologyEvent);
    const setActiveView = useStore((s) => s.setActiveView);
    const setOpenOntologyName = useStore((s) => s.setOpenOntologyName);

    const load = useCallback(async () => {
        if (!sessionId) return;
        try {
            const model = await getOntology(sessionId);
            const { names, superseded } = await readProposal(ontologyId, version);
            const described = describeProposed(model.spec, names);
            setItems(described);
            if (described.length === 0) {
                // The definitions are gone — someone already rejected this.
                setStatus('superseded');
            } else if (superseded) {
                // A later proposal revised these. Deciding on this card would act
                // on values the user has already moved past.
                setStatus('superseded');
            } else {
                const allCertified = described.every((item) =>
                    isCertified(model.spec, item.kind, item.name)
                );
                setStatus(allCertified ? 'accepted' : 'pending');
            }
        } catch {
            setError('Could not load this proposal.');
            setStatus('error');
        }
    }, [sessionId, ontologyId, version]);

    useEffect(() => {
        void load();
    }, [load]);

    useEffect(() => {
        if (!lastOntologyEvent || lastOntologyEvent.ontologyId !== ontologyId) return;
        // Only a LATER version can supersede this one; re-reading on our own
        // event would be a wasted round trip.
        if ((lastOntologyEvent.version ?? 0) <= version) return;
        void load();
    }, [lastOntologyEvent, ontologyId, version, load]);

    const onAccept = async () => {
        try {
            await acceptProposal(ontologyId, version);
            setStatus('accepted');
        } catch {
            setError('Could not accept this proposal.');
            setStatus('error');
        }
    };

    const onReject = async () => {
        try {
            await rejectProposal(ontologyId, version);
            setStatus('rejected');
        } catch {
            setError('Could not reject this proposal.');
            setStatus('error');
        }
    };

    const onEdit = (name?: string) => {
        if (name) setOpenOntologyName(name);
        setActiveView('ontology');
    };

    if (status === 'loading') {
        return (
            <div
                className="my-3 rounded-xl border px-3 py-4 text-xs"
                style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
            >
                Loading proposal…
            </div>
        );
    }

    const settled = status === 'accepted' || status === 'rejected' || status === 'superseded';

    return (
        <div
            className="my-3 rounded-xl border overflow-hidden"
            style={{
                borderColor: settled
                    ? 'var(--c-glass-border)'
                    : 'color-mix(in srgb, var(--c-signal) 35%, transparent)',
                background: 'var(--c-slate)',
            }}
        >
            <div
                className="flex items-center gap-2 px-3 py-2 border-b"
                style={{
                    borderColor: 'var(--c-glass-border)',
                    background: settled ? 'var(--c-glass)' : 'var(--c-signal-dim)',
                }}
            >
                <span
                    className="text-xs font-medium"
                    style={{ color: settled ? 'var(--c-ink-2)' : 'var(--c-signal-text)' }}
                >
                    {status === 'accepted'
                        ? 'Definitions certified'
                        : status === 'rejected'
                          ? 'Proposal rejected'
                          : status === 'superseded'
                            ? 'Superseded by a newer proposal below'
                            : 'Proposed definitions — review before use'}
                </span>
                {!settled && (
                    <span
                        className="text-[9px] px-1 py-px rounded uppercase tracking-wide font-mono ml-auto"
                        style={{ background: 'var(--c-mist)', color: 'var(--c-mist-fg)' }}
                    >
                        draft
                    </span>
                )}
                {status === 'accepted' && (
                    <BadgeCheck
                        className="w-3.5 h-3.5 ml-auto"
                        style={{ color: 'var(--c-signal-text)' }}
                    />
                )}
            </div>

            {status !== 'rejected' && (
                <div className="divide-y" style={{ borderColor: 'var(--c-glass-border)' }}>
                    {items.map((item) => {
                        const Icon = KIND_ICON[item.kind];
                        return (
                            <div key={item.name} className="flex items-start gap-2 px-3 py-2">
                                <Icon
                                    className="w-3.5 h-3.5 mt-0.5 flex-shrink-0"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                />
                                <div className="min-w-0 flex-1">
                                    <div className="flex items-baseline gap-1.5">
                                        <span
                                            className="text-xs font-medium font-mono"
                                            style={{ color: 'var(--c-ink)' }}
                                        >
                                            {item.name}
                                        </span>
                                        <span
                                            className="text-[10px]"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {item.kind}
                                        </span>
                                    </div>
                                    <p
                                        className="text-[11px] font-mono truncate"
                                        style={{ color: 'var(--c-ink-2)' }}
                                        title={item.summary}
                                    >
                                        {item.summary}
                                    </p>
                                    {item.description && (
                                        <p
                                            className="text-[10px] mt-0.5"
                                            style={{ color: 'var(--c-mist-fg)' }}
                                        >
                                            {item.description}
                                        </p>
                                    )}
                                </div>
                                <button
                                    type="button"
                                    onClick={() => onEdit(item.name)}
                                    title="Edit in the Ontology tab"
                                    className="p-1 rounded transition-colors hover:bg-[var(--c-glass)] flex-shrink-0"
                                    style={{ color: 'var(--c-mist-fg)' }}
                                >
                                    <Pencil className="w-3 h-3" />
                                </button>
                            </div>
                        );
                    })}
                </div>
            )}

            {error && (
                <p
                    className="px-3 py-2 text-[11px] flex items-center gap-1"
                    style={{ color: 'var(--c-danger)' }}
                >
                    <AlertTriangle className="w-3.5 h-3.5" />
                    {error}
                </p>
            )}

            {status === 'pending' && (
                <div
                    className="flex items-center gap-2 px-3 py-2 border-t"
                    style={{ borderColor: 'var(--c-glass-border)' }}
                >
                    <button
                        type="button"
                        onClick={onAccept}
                        className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md transition-colors"
                        style={{ background: 'var(--c-signal-dim)', color: 'var(--c-signal-text)' }}
                    >
                        <Check className="w-3 h-3" />
                        Accept
                    </button>
                    <button
                        type="button"
                        onClick={onReject}
                        className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-danger)' }}
                    >
                        <X className="w-3 h-3" />
                        Reject
                    </button>
                    <button
                        type="button"
                        onClick={() => onEdit(items[0]?.name)}
                        className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded-md ml-auto transition-colors hover:bg-[var(--c-glass)]"
                        style={{ color: 'var(--c-ink-2)' }}
                    >
                        <Pencil className="w-3 h-3" />
                        Edit in Ontology tab
                    </button>
                </div>
            )}

            {settled && (
                <div
                    className="px-3 py-2 border-t text-[11px] flex items-center"
                    style={{ borderColor: 'var(--c-glass-border)', color: 'var(--c-mist-fg)' }}
                >
                    <span>
                        {status === 'accepted'
                            ? 'These definitions are now certified and used for every answer.'
                            : status === 'superseded'
                              ? 'Review the newest card at the bottom of the chat instead.'
                              : 'The ontology was rolled back to its previous version.'}
                    </span>
                    <button
                        type="button"
                        onClick={() => onEdit(items[0]?.name)}
                        className="ml-auto underline"
                        style={{ color: 'var(--c-ink-2)' }}
                    >
                        Open Ontology tab
                    </button>
                </div>
            )}
        </div>
    );
}

function isCertified(spec: OntologySpec, kind: DefinitionKind, name: string): boolean {
    const list =
        kind === 'entity'
            ? spec.entities
            : kind === 'relationship'
              ? spec.relationships
              : kind === 'dimension'
                ? spec.dimensions
                : spec.metrics;
    return list.find((i) => i.name === name)?.certified ?? false;
}

/** This card's proposed names, plus whether a later proposal has replaced it. */
async function readProposal(
    ontologyId: string,
    version: number
): Promise<{ names: string[]; superseded: boolean }> {
    const versions = await listOntologyVersions(ontologyId);
    return {
        names: versions.find((v) => v.version === version)?.proposed ?? [],
        superseded: versions.some((v) => v.version > version && v.proposed.length > 0),
    };
}

/** Fence entry point: ```ontology {"ontology_id": "…", "version": 3} ``` */
export default function OntologyEmbed({
    payload,
}: {
    payload: ProposalPayload | ProposalPayload[];
}) {
    const items = Array.isArray(payload) ? payload : [payload];
    return (
        <>
            {items.map((item, i) => {
                const id = item.ontology_id ?? item.ontologyId;
                if (!id || !item.version) return null;
                return (
                    <OntologyCard
                        key={`${id}-${item.version}-${i}`}
                        ontologyId={id}
                        version={item.version}
                    />
                );
            })}
        </>
    );
}
