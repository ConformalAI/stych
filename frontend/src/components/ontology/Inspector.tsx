/**
 * Inspector — edits whatever is selected on the canvas or in the lists.
 *
 * Each form writes a whole replacement definition through `onChange`, so the
 * spec-level updater stays the only mutation path and undo works uniformly.
 */

import { Trash2 } from 'lucide-react';
import {
    Field,
    SectionTitle,
    Select,
    SynonymInput,
    TextArea,
    TextInput,
    Toggle,
} from './InspectorFields';
import {
    errorsFor,
    type Aggregate,
    type Cardinality,
    type DatasetInfo,
    type DimensionDef,
    type EntityDef,
    type JoinType,
    type MetricDef,
    type OntologyErrorItem,
    type OntologySpec,
    type RelationshipDef,
    type Selection,
    type TimeUnit,
} from './types';

const CARDINALITIES: readonly { value: Cardinality; label: string }[] = [
    { value: 'many_to_one', label: 'many → one' },
    { value: 'one_to_many', label: 'one → many' },
    { value: 'one_to_one', label: 'one → one' },
    { value: 'many_to_many', label: 'many → many' },
];

const JOIN_TYPES: readonly { value: JoinType; label: string }[] = [
    { value: 'left', label: 'left (keep unmatched rows)' },
    { value: 'inner', label: 'inner (drop unmatched rows)' },
];

const AGGREGATES: readonly { value: Aggregate; label: string }[] = [
    { value: 'sum', label: 'sum' },
    { value: 'mean', label: 'mean (average)' },
    { value: 'count', label: 'count (rows)' },
    { value: 'distinct', label: 'distinct (unique values)' },
    { value: 'min', label: 'min' },
    { value: 'max', label: 'max' },
];

const TIME_UNITS: readonly { value: TimeUnit; label: string }[] = [
    { value: 'year', label: 'year' },
    { value: 'quarter', label: 'quarter' },
    { value: 'month', label: 'month' },
    { value: 'week', label: 'week' },
    { value: 'day', label: 'day' },
    { value: 'hour', label: 'hour' },
];

interface Props {
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errors: OntologyErrorItem[];
    selection: Selection;
    updateSpec: (next: (prev: OntologySpec) => OntologySpec) => void;
    onSelect: (selection: Selection) => void;
}

export default function Inspector({
    spec,
    datasets,
    errors,
    selection,
    updateSpec,
    onSelect,
}: Props) {
    if (!selection) {
        return (
            <div className="p-4 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                <SectionTitle>Nothing selected</SectionTitle>
                <p className="leading-relaxed">
                    Pick an entity, relationship, metric, or dimension to edit it. Drag a column
                    from one entity card onto another to create a relationship.
                </p>
            </div>
        );
    }

    const index = indexOf(spec, selection);
    if (index < 0) {
        return (
            <div className="p-4 text-xs" style={{ color: 'var(--c-mist-fg)' }}>
                That definition no longer exists.
            </div>
        );
    }
    const itemErrors = errorsFor(errors, selection.kind, index);
    const errorFor = (suffix: string) =>
        itemErrors.find((e) => e.path.endsWith(`.${suffix}`))?.message;

    const remove = () => {
        updateSpec((prev) => removeDefinition(prev, selection));
        onSelect(null);
    };

    /** Apply an edit, following the selection through a rename. */
    const commit = (
        key: 'entities' | 'relationships' | 'dimensions' | 'metrics',
        next: { name: string }
    ) => {
        const previousName = selection.name;
        const renamed = next.name !== previousName;
        updateSpec((prev) => {
            const list = replaceAt(prev[key] as { name: string }[], index, next);
            const updated = { ...prev, [key]: list } as unknown as OntologySpec;
            return renamed && selection.kind === 'entity'
                ? renameEntityReferences(updated, previousName, next.name)
                : updated;
        });
        if (renamed) onSelect({ kind: selection.kind, name: next.name });
    };

    return (
        <div className="p-3">
            <div className="flex items-center justify-between mb-3">
                <SectionTitle>{selection.kind}</SectionTitle>
                <button
                    type="button"
                    onClick={remove}
                    title={`Delete ${selection.name}`}
                    className="p-1 rounded transition-colors hover:bg-[var(--c-glass)]"
                    style={{ color: 'var(--c-danger)' }}
                >
                    <Trash2 className="w-3.5 h-3.5" />
                </button>
            </div>

            {itemErrors.length > 0 && (
                <div
                    className="rounded-md border px-2 py-1.5 mb-3 text-[10px] leading-relaxed"
                    style={{
                        borderColor: 'color-mix(in srgb, var(--c-danger) 40%, transparent)',
                        background: 'color-mix(in srgb, var(--c-danger) 10%, transparent)',
                        color: 'var(--c-danger)',
                    }}
                >
                    {itemErrors.map((e) => (
                        <div key={e.path + e.code}>{e.message}</div>
                    ))}
                </div>
            )}

            {selection.kind === 'entity' && (
                <EntityForm
                    entity={spec.entities[index]}
                    dataset={datasets.find((d) => d.id === spec.entities[index].dataset)}
                    datasets={datasets}
                    errorFor={errorFor}
                    onChange={(next) => commit('entities', next)}
                />
            )}
            {selection.kind === 'relationship' && (
                <RelationshipForm
                    relationship={spec.relationships[index]}
                    spec={spec}
                    datasets={datasets}
                    errorFor={errorFor}
                    onChange={(next) => commit('relationships', next)}
                />
            )}
            {selection.kind === 'dimension' && (
                <DimensionForm
                    dimension={spec.dimensions[index]}
                    spec={spec}
                    datasets={datasets}
                    errorFor={errorFor}
                    onChange={(next) => commit('dimensions', next)}
                />
            )}
            {selection.kind === 'metric' && (
                <MetricForm
                    metric={spec.metrics[index]}
                    spec={spec}
                    datasets={datasets}
                    errorFor={errorFor}
                    onChange={(next) => commit('metrics', next)}
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Forms
// ---------------------------------------------------------------------------

type ErrorFor = (suffix: string) => string | undefined;

function CertifyToggle({
    certified,
    kind,
    onChange,
}: {
    certified: boolean;
    kind: string;
    onChange: (v: boolean) => void;
}) {
    return (
        <Toggle
            checked={certified}
            onChange={onChange}
            label={certified ? 'Certified' : 'Draft'}
            description={
                certified
                    ? `Answers built on this ${kind} are presented as established.`
                    : `Answers built on this ${kind} are flagged as unverified.`
            }
        />
    );
}

function EntityForm({
    entity,
    dataset,
    datasets,
    errorFor,
    onChange,
}: {
    entity: EntityDef;
    dataset?: DatasetInfo;
    datasets: DatasetInfo[];
    errorFor: ErrorFor;
    onChange: (next: EntityDef) => void;
}) {
    const columns = dataset?.fields.map((f) => ({ value: f.name, label: f.name })) ?? [];
    return (
        <>
            <Field label="Name" hint="lower_snake_case; used by the agent">
                <TextInput
                    value={entity.name}
                    mono
                    onChange={(name) => onChange({ ...entity, name })}
                />
            </Field>
            <Field label="Display label">
                <TextInput
                    value={entity.label ?? ''}
                    placeholder={entity.name}
                    onChange={(label) => onChange({ ...entity, label })}
                />
            </Field>
            <Field label="Dataset" error={errorFor('dataset')}>
                <Select
                    value={entity.dataset}
                    options={datasets.map((d) => ({ value: d.id, label: d.filename }))}
                    onChange={(datasetId) =>
                        onChange({
                            ...entity,
                            dataset: datasetId,
                            datasetLabel: datasets.find((d) => d.id === datasetId)?.filename,
                            // Columns come from the dataset; a swap invalidates them.
                            primaryKey: null,
                            titleField: null,
                        })
                    }
                />
            </Field>
            <Field
                label="Primary key"
                hint="The column that identifies one row"
                error={errorFor('primaryKey')}
            >
                <Select
                    value={entity.primaryKey ?? ''}
                    placeholder="— none —"
                    options={columns}
                    onChange={(primaryKey) => onChange({ ...entity, primaryKey })}
                />
            </Field>
            <Field
                label="Title field"
                hint="The column that best names one row"
                error={errorFor('titleField')}
            >
                <Select
                    value={entity.titleField ?? ''}
                    placeholder="— none —"
                    options={columns}
                    onChange={(titleField) => onChange({ ...entity, titleField })}
                />
            </Field>
            <Field label="Description">
                <TextArea
                    value={entity.description ?? ''}
                    placeholder="What one row represents"
                    onChange={(description) => onChange({ ...entity, description })}
                />
            </Field>
            <Field label="Synonyms" hint="Other words users say for this">
                <SynonymInput
                    value={entity.synonyms}
                    onChange={(synonyms) => onChange({ ...entity, synonyms })}
                />
            </Field>
            <CertifyToggle
                certified={entity.certified}
                kind="entity"
                onChange={(certified) => onChange({ ...entity, certified })}
            />
        </>
    );
}

function columnsOf(spec: OntologySpec, datasets: DatasetInfo[], entityName: string) {
    const entity = spec.entities.find((e) => e.name === entityName);
    const dataset = datasets.find((d) => d.id === entity?.dataset);
    return dataset?.fields.map((f) => ({ value: f.name, label: f.name })) ?? [];
}

function RelationshipForm({
    relationship,
    spec,
    datasets,
    errorFor,
    onChange,
}: {
    relationship: RelationshipDef;
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errorFor: ErrorFor;
    onChange: (next: RelationshipDef) => void;
}) {
    const entities = spec.entities.map((e) => ({ value: e.name, label: e.label || e.name }));
    return (
        <>
            <Field label="Name">
                <TextInput
                    value={relationship.name}
                    mono
                    onChange={(name) => onChange({ ...relationship, name })}
                />
            </Field>
            <Field label="From entity" error={errorFor('fromEntity')}>
                <Select
                    value={relationship.fromEntity}
                    options={entities}
                    onChange={(fromEntity) =>
                        onChange({ ...relationship, fromEntity, fromField: '' })
                    }
                />
            </Field>
            <Field label="From column" error={errorFor('fromField')}>
                <Select
                    value={relationship.fromField}
                    placeholder="— pick a column —"
                    options={columnsOf(spec, datasets, relationship.fromEntity)}
                    onChange={(fromField) => onChange({ ...relationship, fromField })}
                />
            </Field>
            <Field label="To entity" error={errorFor('toEntity')}>
                <Select
                    value={relationship.toEntity}
                    options={entities}
                    onChange={(toEntity) => onChange({ ...relationship, toEntity, toField: '' })}
                />
            </Field>
            <Field label="To column" error={errorFor('toField')}>
                <Select
                    value={relationship.toField}
                    placeholder="— pick a column —"
                    options={columnsOf(spec, datasets, relationship.toEntity)}
                    onChange={(toField) => onChange({ ...relationship, toField })}
                />
            </Field>
            <Field label="Cardinality" hint="How many 'from' rows per 'to' row">
                <Select
                    value={relationship.cardinality}
                    options={CARDINALITIES}
                    onChange={(cardinality) => onChange({ ...relationship, cardinality })}
                />
            </Field>
            <Field label="Join type">
                <Select
                    value={relationship.joinType}
                    options={JOIN_TYPES}
                    onChange={(joinType) => onChange({ ...relationship, joinType })}
                />
            </Field>
            <CertifyToggle
                certified={relationship.certified}
                kind="relationship"
                onChange={(certified) => onChange({ ...relationship, certified })}
            />
        </>
    );
}

function DimensionForm({
    dimension,
    spec,
    datasets,
    errorFor,
    onChange,
}: {
    dimension: DimensionDef;
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errorFor: ErrorFor;
    onChange: (next: DimensionDef) => void;
}) {
    return (
        <>
            <Field label="Name" hint="What the agent will call this">
                <TextInput
                    value={dimension.name}
                    mono
                    onChange={(name) => onChange({ ...dimension, name })}
                />
            </Field>
            <Field label="Entity" error={errorFor('entity')}>
                <Select
                    value={dimension.entity}
                    options={spec.entities.map((e) => ({
                        value: e.name,
                        label: e.label || e.name,
                    }))}
                    onChange={(entity) => onChange({ ...dimension, entity, field: '' })}
                />
            </Field>
            <Field label="Column" error={errorFor('field')}>
                <Select
                    value={dimension.field}
                    placeholder="— pick a column —"
                    options={columnsOf(spec, datasets, dimension.entity)}
                    onChange={(field) => onChange({ ...dimension, field })}
                />
            </Field>
            <Field label="Description">
                <TextArea
                    value={dimension.description ?? ''}
                    onChange={(description) => onChange({ ...dimension, description })}
                />
            </Field>
            <Field label="Synonyms">
                <SynonymInput
                    value={dimension.synonyms}
                    onChange={(synonyms) => onChange({ ...dimension, synonyms })}
                />
            </Field>
            <Field
                label="Value synonyms"
                hint="One per line, as CA = California — lets the agent match spoken values to stored codes"
            >
                <TextArea
                    rows={3}
                    value={Object.entries(dimension.valueSynonyms)
                        .map(([k, v]) => `${k} = ${v.join(', ')}`)
                        .join('\n')}
                    placeholder="CA = California"
                    onChange={(raw) =>
                        onChange({ ...dimension, valueSynonyms: parseValueSynonyms(raw) })
                    }
                />
            </Field>
            <CertifyToggle
                certified={dimension.certified}
                kind="dimension"
                onChange={(certified) => onChange({ ...dimension, certified })}
            />
        </>
    );
}

function MetricForm({
    metric,
    spec,
    datasets,
    errorFor,
    onChange,
}: {
    metric: MetricDef;
    spec: OntologySpec;
    datasets: DatasetInfo[];
    errorFor: ErrorFor;
    onChange: (next: MetricDef) => void;
}) {
    const temporal = columnsOf(spec, datasets, metric.entity).filter((c) => {
        const entity = spec.entities.find((e) => e.name === metric.entity);
        const dataset = datasets.find((d) => d.id === entity?.dataset);
        return dataset?.fields.find((f) => f.name === c.value)?.type === 'temporal';
    });

    return (
        <>
            <Field label="Name" hint="What the agent will call this">
                <TextInput
                    value={metric.name}
                    mono
                    onChange={(name) => onChange({ ...metric, name })}
                />
            </Field>
            <Field label="Entity" error={errorFor('entity')}>
                <Select
                    value={metric.entity}
                    options={spec.entities.map((e) => ({
                        value: e.name,
                        label: e.label || e.name,
                    }))}
                    onChange={(entity) => onChange({ ...metric, entity })}
                />
            </Field>
            <Field label="Aggregate">
                <Select
                    value={metric.aggregate}
                    options={AGGREGATES}
                    onChange={(aggregate) => onChange({ ...metric, aggregate })}
                />
            </Field>
            <Field
                label="Expression"
                hint={
                    metric.aggregate === 'count'
                        ? 'Not needed for count — it counts rows.'
                        : 'A row-level expression, e.g. order_total - refund_amount'
                }
                error={errorFor('expr')}
            >
                <TextInput
                    value={metric.expr ?? ''}
                    mono
                    disabled={metric.aggregate === 'count'}
                    placeholder="order_total - refund_amount"
                    onChange={(expr) => onChange({ ...metric, expr })}
                />
            </Field>
            <Field
                label="Filter"
                hint="Scoped to this metric alone, so it stays correct alongside metrics with other rules"
                error={errorFor('filterExpr')}
            >
                <TextInput
                    value={metric.filterExpr ?? ''}
                    mono
                    placeholder="status != 'cancelled'"
                    onChange={(filterExpr) => onChange({ ...metric, filterExpr })}
                />
            </Field>
            <Field
                label="Default time column"
                hint="Used for '… over time' with no stated grain"
                error={errorFor('defaultTimeField')}
            >
                <Select
                    value={metric.defaultTimeField ?? ''}
                    placeholder="— none —"
                    options={temporal}
                    onChange={(defaultTimeField) => onChange({ ...metric, defaultTimeField })}
                />
            </Field>
            <Field label="Default grain">
                <Select
                    value={metric.defaultTimeUnit ?? ''}
                    placeholder="— none —"
                    options={TIME_UNITS}
                    onChange={(defaultTimeUnit) => onChange({ ...metric, defaultTimeUnit })}
                />
            </Field>
            <Field
                label="Description"
                hint="Say what is included and excluded — the agent repeats this"
            >
                <TextArea
                    value={metric.description ?? ''}
                    placeholder="Net of refunds, excluding cancelled orders"
                    onChange={(description) => onChange({ ...metric, description })}
                />
            </Field>
            <Field label="Synonyms">
                <SynonymInput
                    value={metric.synonyms}
                    onChange={(synonyms) => onChange({ ...metric, synonyms })}
                />
            </Field>
            <CertifyToggle
                certified={metric.certified}
                kind="metric"
                onChange={(certified) => onChange({ ...metric, certified })}
            />
        </>
    );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function parseValueSynonyms(raw: string): Record<string, string[]> {
    const out: Record<string, string[]> = {};
    for (const line of raw.split('\n')) {
        const [key, ...rest] = line.split('=');
        const value = rest.join('=').trim();
        if (!key?.trim() || !value) continue;
        out[key.trim()] = value
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean);
    }
    return out;
}

export function indexOf(spec: OntologySpec, selection: Selection): number {
    if (!selection) return -1;
    const list = listFor(spec, selection.kind);
    return list.findIndex((i) => i.name === selection.name);
}

function listFor(spec: OntologySpec, kind: NonNullable<Selection>['kind']): { name: string }[] {
    if (kind === 'entity') return spec.entities;
    if (kind === 'relationship') return spec.relationships;
    if (kind === 'dimension') return spec.dimensions;
    return spec.metrics;
}

function replaceAt<T>(list: T[], index: number, next: T): T[] {
    return list.map((item, i) => (i === index ? next : item));
}

export function renameEntityReferences(spec: OntologySpec, from: string, to: string): OntologySpec {
    return {
        ...spec,
        relationships: spec.relationships.map((r) => ({
            ...r,
            fromEntity: r.fromEntity === from ? to : r.fromEntity,
            toEntity: r.toEntity === from ? to : r.toEntity,
        })),
        dimensions: spec.dimensions.map((d) => (d.entity === from ? { ...d, entity: to } : d)),
        metrics: spec.metrics.map((m) => (m.entity === from ? { ...m, entity: to } : m)),
    };
}

export function removeDefinition(
    spec: OntologySpec,
    selection: NonNullable<Selection>
): OntologySpec {
    const { kind, name } = selection;
    if (kind === 'entity') {
        // Removing an entity orphans everything defined on it; drop those too
        // rather than leaving a spec that cannot validate.
        return {
            ...spec,
            entities: spec.entities.filter((e) => e.name !== name),
            relationships: spec.relationships.filter(
                (r) => r.fromEntity !== name && r.toEntity !== name
            ),
            dimensions: spec.dimensions.filter((d) => d.entity !== name),
            metrics: spec.metrics.filter((m) => m.entity !== name),
        };
    }
    if (kind === 'relationship') {
        return { ...spec, relationships: spec.relationships.filter((r) => r.name !== name) };
    }
    if (kind === 'dimension') {
        return { ...spec, dimensions: spec.dimensions.filter((d) => d.name !== name) };
    }
    return { ...spec, metrics: spec.metrics.filter((m) => m.name !== name) };
}
