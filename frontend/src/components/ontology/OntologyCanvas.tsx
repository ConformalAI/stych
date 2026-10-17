/**
 * The ontology graph canvas.
 *
 * Spec → nodes/edges is a pure derivation (`toNodes`/`toEdges`, unit-tested);
 * everything the user does on the canvas writes back through `updateSpec`, so
 * the spec stays the single source of truth exactly as VizSpec does for charts.
 *
 * Entity positions live in the spec (`x`/`y`) rather than in local state, so a
 * layout the user arranges survives a reload and is versioned with everything
 * else.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Background,
    Controls,
    ReactFlow,
    applyNodeChanges,
    type Connection,
    type Edge,
    type Node,
    type NodeChange,
    type Node as FlowNode,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useStore } from '../../store/useStore';
import EntityNode, { type EntityNodeData } from './EntityNode';
import {
    uniqueName,
    type DatasetInfo,
    type OntologyErrorItem,
    type OntologySpec,
    type Selection,
} from './types';

const nodeTypes = { entity: EntityNode };

/** Deterministic fallback layout for entities that have never been placed. */
export const defaultPosition = (index: number): { x: number; y: number } => ({
    x: 60 + (index % 3) * 300,
    y: 60 + Math.floor(index / 3) * 280,
});

export function toNodes(
    spec: OntologySpec,
    datasets: DatasetInfo[],
    errors: OntologyErrorItem[],
    selection: Selection
): Node<EntityNodeData>[] {
    const byId = new Map(datasets.map((d) => [d.id, d]));
    const joinFieldsByEntity = new Map<string, Set<string>>();
    for (const rel of spec.relationships) {
        if (!joinFieldsByEntity.has(rel.fromEntity))
            joinFieldsByEntity.set(rel.fromEntity, new Set());
        if (!joinFieldsByEntity.has(rel.toEntity)) joinFieldsByEntity.set(rel.toEntity, new Set());
        joinFieldsByEntity.get(rel.fromEntity)!.add(rel.fromField);
        joinFieldsByEntity.get(rel.toEntity)!.add(rel.toField);
    }

    return spec.entities.map((entity, index) => ({
        id: entity.name,
        type: 'entity',
        position:
            entity.x != null && entity.y != null
                ? { x: entity.x, y: entity.y }
                : defaultPosition(index),
        selected: selection?.kind === 'entity' && selection.name === entity.name,
        data: {
            entity,
            dataset: byId.get(entity.dataset),
            hasError: errors.some((e) => e.path.startsWith(`entities[${index}]`)),
            metricCount: spec.metrics.filter((m) => m.entity === entity.name).length,
            dimensionCount: spec.dimensions.filter((d) => d.entity === entity.name).length,
            joinFields: joinFieldsByEntity.get(entity.name) ?? new Set<string>(),
        },
    }));
}

export function toEdges(spec: OntologySpec, selection: Selection): Edge[] {
    return spec.relationships.map((rel) => {
        const selected = selection?.kind === 'relationship' && selection.name === rel.name;
        return {
            id: rel.name,
            source: rel.fromEntity,
            sourceHandle: rel.fromField,
            target: rel.toEntity,
            targetHandle: rel.toField,
            label: rel.cardinality.replace(/_/g, ' '),
            animated: !rel.certified,
            selected,
            style: {
                stroke: selected ? 'var(--c-signal)' : 'var(--c-mist-fg)',
                strokeWidth: selected ? 2.5 : 1.5,
            },
            labelStyle: { fill: 'var(--c-mist-fg)', fontSize: 9 },
            labelBgStyle: { fill: 'var(--c-paper)' },
        };
    });
}

interface Props {
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errors: OntologyErrorItem[];
    selection: Selection;
    onSelect: (selection: Selection) => void;
    updateSpec: (next: (prev: OntologySpec) => OntologySpec) => void;
}

export default function OntologyCanvas({
    spec,
    datasets,
    errors,
    selection,
    onSelect,
    updateSpec,
}: Props) {
    const chatTheme = useStore((s) => s.chatTheme);
    const derived = useMemo(
        () => toNodes(spec, datasets, errors, selection),
        [spec, datasets, errors, selection]
    );
    const edges = useMemo(() => toEdges(spec, selection), [spec, selection]);

    // React Flow is controlled here, so EVERY change must be applied back or the
    // node simply does not move while you drag it. Live movement therefore runs
    // on local state; only the final position is written to the spec, which is
    // what keeps one drag as one undo step instead of hundreds.
    const [nodes, setNodes] = useState<FlowNode<EntityNodeData>[]>(derived);
    useEffect(() => setNodes(derived), [derived]);

    const onNodesChange = useCallback(
        (changes: NodeChange<FlowNode<EntityNodeData>>[]) =>
            setNodes((current) => applyNodeChanges(changes, current)),
        []
    );

    const onNodeDragStop = useCallback(
        (_: unknown, node: FlowNode) => {
            updateSpec((prev) => ({
                ...prev,
                entities: prev.entities.map((entity) =>
                    entity.name === node.id
                        ? {
                              ...entity,
                              x: Math.round(node.position.x),
                              y: Math.round(node.position.y),
                          }
                        : entity
                ),
            }));
        },
        [updateSpec]
    );

    const onConnect = useCallback(
        (connection: Connection) => {
            const { source, target, sourceHandle, targetHandle } = connection;
            if (!source || !target || !sourceHandle || !targetHandle) return;
            if (source === target) return; // the backend rejects self-joins

            updateSpec((prev) => {
                const duplicate = prev.relationships.some(
                    (r) =>
                        r.fromEntity === source &&
                        r.fromField === sourceHandle &&
                        r.toEntity === target &&
                        r.toField === targetHandle
                );
                if (duplicate) return prev;

                const name = uniqueName(prev, `${source}_${target}`);
                const next = {
                    ...prev,
                    relationships: [
                        ...prev.relationships,
                        {
                            name,
                            fromEntity: source,
                            fromField: sourceHandle,
                            toEntity: target,
                            toField: targetHandle,
                            // The common case by far: many fact rows point at one
                            // dimension row. The inspector can change it.
                            cardinality: 'many_to_one' as const,
                            joinType: 'left' as const,
                            certified: false,
                        },
                    ],
                };
                onSelect({ kind: 'relationship', name });
                return next;
            });
        },
        [updateSpec, onSelect]
    );

    return (
        <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onNodeDragStop={onNodeDragStop}
            onConnect={onConnect}
            onNodeClick={(_, node) => onSelect({ kind: 'entity', name: node.id })}
            onEdgeClick={(_, edge) => onSelect({ kind: 'relationship', name: edge.id })}
            onPaneClick={() => onSelect(null)}
            fitView
            fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
            minZoom={0.2}
            maxZoom={1.75}
            proOptions={{ hideAttribution: true }}
            colorMode={chatTheme}
            style={
                {
                    background: 'var(--c-paper)',
                    // React Flow's own dark palette is close but not ours; its
                    // themeable custom properties let the controls match the app
                    // exactly instead of approximately.
                    '--xy-controls-button-background-color': 'var(--c-slate)',
                    '--xy-controls-button-background-color-hover': 'var(--c-glass)',
                    '--xy-controls-button-color': 'var(--c-ink)',
                    '--xy-controls-button-color-hover': 'var(--c-signal-text)',
                    '--xy-controls-button-border-color': 'var(--c-glass-border)',
                } as React.CSSProperties
            }
        >
            <Background color="var(--c-mist)" gap={18} size={1} />
            <Controls
                showInteractive={false}
                style={{ border: '1px solid var(--c-glass-border)', borderRadius: 8 }}
            />
        </ReactFlow>
    );
}
