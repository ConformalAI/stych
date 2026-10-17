/**
 * TypeScript mirror of the OntologySpec.
 *
 * SOURCE OF TRUTH: backend/ontology/spec.py — keep these in sync by hand, the
 * same way components/visualize/types.ts mirrors backend/viz/spec.py. Wire
 * format uses the backend's alias names (camelCase), not the Python attribute
 * names.
 */

export type Cardinality = 'one_to_one' | 'many_to_one' | 'one_to_many' | 'many_to_many';
export type JoinType = 'inner' | 'left';
export type Aggregate = 'sum' | 'mean' | 'min' | 'max' | 'count' | 'distinct';
export type TimeUnit = 'year' | 'quarter' | 'month' | 'week' | 'day' | 'hour';
export type DefinitionKind = 'entity' | 'relationship' | 'dimension' | 'metric';

export interface Provenance {
    author: 'user' | 'agent';
    intent?: string | null;
    model?: string | null;
    createdAt?: string | null;
}

export interface EntityDef {
    name: string;
    dataset: string;
    datasetLabel?: string | null;
    label?: string | null;
    description?: string | null;
    synonyms: string[];
    primaryKey?: string | null;
    titleField?: string | null;
    /** Canvas position, persisted so the graph layout survives a reload. */
    x?: number | null;
    y?: number | null;
    certified: boolean;
    provenance?: Provenance;
}

export interface RelationshipDef {
    name: string;
    fromEntity: string;
    fromField: string;
    toEntity: string;
    toField: string;
    cardinality: Cardinality;
    joinType: JoinType;
    label?: string | null;
    description?: string | null;
    certified: boolean;
    provenance?: Provenance;
}

export interface DimensionDef {
    name: string;
    entity: string;
    field: string;
    label?: string | null;
    description?: string | null;
    synonyms: string[];
    valueSynonyms: Record<string, string[]>;
    certified: boolean;
    provenance?: Provenance;
}

export interface MetricDef {
    name: string;
    entity: string;
    label?: string | null;
    description?: string | null;
    expr?: string | null;
    aggregate: Aggregate;
    filterExpr?: string | null;
    defaultTimeField?: string | null;
    defaultTimeUnit?: TimeUnit | null;
    format?: string | null;
    synonyms: string[];
    certified: boolean;
    provenance?: Provenance;
}

export interface OntologySpec {
    schemaVersion: 1;
    title?: string | null;
    entities: EntityDef[];
    relationships: RelationshipDef[];
    dimensions: DimensionDef[];
    metrics: MetricDef[];
}

export interface OntologyErrorItem {
    path: string;
    code: string;
    message: string;
}

/** One dataset in the session, with the columns an entity can be built from. */
export interface DatasetInfo {
    id: string;
    filename: string;
    row_count?: number | null;
    fields: {
        name: string;
        type: string;
        role: string;
        cardinality?: number | null;
        null_pct?: number | null;
        samples?: unknown[];
    }[];
}

export interface OntologyModel {
    id: string;
    session_id: string;
    title?: string | null;
    spec: OntologySpec;
    version: number;
    errors: OntologyErrorItem[];
    datasets: DatasetInfo[];
    missing_datasets: string[];
    updated_at?: string | null;
}

export interface OntologyVersion {
    version: number;
    spec: OntologySpec;
    author: 'user' | 'agent';
    summary?: string | null;
    proposed: string[];
    created_at?: string | null;
}

export interface OntologyQueryDimension {
    name: string;
    time_unit?: TimeUnit;
    top_k?: number;
}

export interface OntologyQueryResult {
    data: {
        columns: { name: string; field?: string | null; kind: string; type: string }[];
        rows: unknown[][];
        meta: Record<string, unknown>;
    };
    uncertified: string[];
    spans_entities: boolean;
    /** Relationships on the join path that duplicate base rows, inflating sums. */
    fan_out: string[];
    base_entity: string;
}

/** Anything selectable in the editor — drives the inspector panel. */
export type Selection = { kind: DefinitionKind; name: string } | null;

export const emptySpec = (): OntologySpec => ({
    schemaVersion: 1,
    entities: [],
    relationships: [],
    dimensions: [],
    metrics: [],
});

export const emptyEntity = (name: string, dataset: string): EntityDef => ({
    name,
    dataset,
    synonyms: [],
    certified: false,
});

export const emptyMetric = (name: string, entity: string): MetricDef => ({
    name,
    entity,
    aggregate: 'count',
    synonyms: [],
    certified: false,
});

export const emptyDimension = (name: string, entity: string, field: string): DimensionDef => ({
    name,
    entity,
    field,
    synonyms: [],
    valueSynonyms: {},
    certified: false,
});

/** Every definition in the spec, flattened — used for name-uniqueness checks. */
export const allNames = (spec: OntologySpec): string[] => [
    ...spec.entities.map((e) => e.name),
    ...spec.relationships.map((r) => r.name),
    ...spec.dimensions.map((d) => d.name),
    ...spec.metrics.map((m) => m.name),
];

/** Backend names are lower_snake_case; mirror that rule so the UI fails fast. */
export const toName = (raw: string): string =>
    raw
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_]+/g, '_')
        .replace(/^[^a-z]+/, '')
        .replace(/_+/g, '_')
        .replace(/_$/, '')
        .slice(0, 60);

/** `base`, `base_2`, `base_3`… — the first name not already taken. */
export const uniqueName = (spec: OntologySpec, base: string): string => {
    const taken = new Set(allNames(spec));
    const seed = toName(base) || 'item';
    if (!taken.has(seed)) return seed;
    let n = 2;
    while (taken.has(`${seed}_${n}`)) n += 1;
    return `${seed}_${n}`;
};

/** Errors that belong to one definition, matched on its spec path. */
export const errorsFor = (
    errors: OntologyErrorItem[],
    kind: DefinitionKind,
    index: number
): OntologyErrorItem[] => {
    const prefix = `${kind === 'entity' ? 'entities' : `${kind}s`}[${index}]`;
    return errors.filter((e) => e.path.startsWith(prefix));
};

export const countCertified = (spec: OntologySpec): { certified: number; total: number } => {
    const items = [...spec.entities, ...spec.relationships, ...spec.dimensions, ...spec.metrics];
    return { certified: items.filter((i) => i.certified).length, total: items.length };
};
