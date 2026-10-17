import { describe, expect, it } from 'vitest';
import { defaultPosition, toEdges, toNodes } from './OntologyCanvas';
import { parseValueSynonyms, removeDefinition, renameEntityReferences } from './Inspector';
import { describeProposed } from './OntologyCard';
import { entityCardStyle } from './EntityNode';
import { formatCell } from './MetricPreview';
import {
    allNames,
    countCertified,
    emptySpec,
    errorsFor,
    toName,
    uniqueName,
    type DatasetInfo,
    type OntologySpec,
} from './types';

const spec = (): OntologySpec => ({
    schemaVersion: 1,
    entities: [
        {
            name: 'orders',
            dataset: 'f1',
            datasetLabel: 'orders.csv',
            synonyms: [],
            certified: true,
            primaryKey: 'order_id',
        },
        { name: 'customers', dataset: 'f2', synonyms: [], certified: false },
    ],
    relationships: [
        {
            name: 'order_customer',
            fromEntity: 'orders',
            fromField: 'customer_id',
            toEntity: 'customers',
            toField: 'id',
            cardinality: 'many_to_one',
            joinType: 'left',
            certified: false,
        },
    ],
    dimensions: [
        {
            name: 'region',
            entity: 'customers',
            field: 'region',
            synonyms: [],
            valueSynonyms: {},
            certified: false,
        },
    ],
    metrics: [
        {
            name: 'revenue',
            entity: 'orders',
            expr: 'order_total',
            aggregate: 'sum',
            filterExpr: "status != 'cancelled'",
            synonyms: [],
            certified: true,
        },
    ],
});

const datasets: DatasetInfo[] = [
    {
        id: 'f1',
        filename: 'orders.csv',
        fields: [
            { name: 'order_id', type: 'quantitative', role: 'dimension' },
            { name: 'customer_id', type: 'quantitative', role: 'dimension' },
        ],
    },
    {
        id: 'f2',
        filename: 'customers.csv',
        fields: [
            { name: 'id', type: 'quantitative', role: 'dimension' },
            { name: 'region', type: 'nominal', role: 'dimension' },
        ],
    },
];

// ---------------------------------------------------------------------- names

describe('name helpers', () => {
    it('coerces arbitrary text to lower_snake_case', () => {
        expect(toName('Q3 Sales (final)')).toBe('q3_sales_final');
        expect(toName('  Order Total  ')).toBe('order_total');
    });

    it('strips leading non-letters so the backend rule holds', () => {
        expect(toName('3rd party')).toBe('rd_party');
    });

    it('suffixes until the name is free', () => {
        const s = spec();
        expect(uniqueName(s, 'revenue')).toBe('revenue_2');
        expect(uniqueName(s, 'profit')).toBe('profit');
    });

    it('treats all four kinds as one namespace', () => {
        // 'region' is a dimension, so an entity may not reuse it.
        expect(uniqueName(spec(), 'region')).toBe('region_2');
        expect(allNames(spec())).toHaveLength(5);
    });
});

describe('countCertified', () => {
    it('counts across every kind', () => {
        expect(countCertified(spec())).toEqual({ certified: 2, total: 5 });
    });

    it('reports zero of zero for an empty model', () => {
        expect(countCertified(emptySpec())).toEqual({ certified: 0, total: 0 });
    });
});

describe('errorsFor', () => {
    const errors = [
        { path: 'metrics[0].expr', code: 'unknown_field', message: 'nope' },
        { path: 'metrics[1].expr', code: 'unknown_field', message: 'other' },
        { path: 'entities[0].dataset', code: 'unknown_dataset', message: 'gone' },
    ];

    it('matches only the requested definition', () => {
        expect(errorsFor(errors, 'metric', 0)).toHaveLength(1);
        expect(errorsFor(errors, 'metric', 0)[0].message).toBe('nope');
    });

    it('maps entity to the entities path', () => {
        expect(errorsFor(errors, 'entity', 0)[0].code).toBe('unknown_dataset');
    });
});

// --------------------------------------------------------------------- canvas

describe('toNodes', () => {
    it('builds one node per entity with its dataset columns', () => {
        const nodes = toNodes(spec(), datasets, [], null);
        expect(nodes.map((n) => n.id)).toEqual(['orders', 'customers']);
        expect(nodes[0].data.dataset?.fields).toHaveLength(2);
    });

    it('uses stored positions and falls back to a layout otherwise', () => {
        const s = spec();
        s.entities[0].x = 400;
        s.entities[0].y = 120;
        const nodes = toNodes(s, datasets, [], null);
        expect(nodes[0].position).toEqual({ x: 400, y: 120 });
        expect(nodes[1].position).toEqual(defaultPosition(1));
    });

    it('marks the columns a relationship joins on', () => {
        const nodes = toNodes(spec(), datasets, [], null);
        expect([...nodes[0].data.joinFields]).toEqual(['customer_id']);
        expect([...nodes[1].data.joinFields]).toEqual(['id']);
    });

    it('flags entities that have validation errors', () => {
        const errors = [{ path: 'entities[1].dataset', code: 'unknown_dataset', message: 'gone' }];
        const nodes = toNodes(spec(), datasets, errors, null);
        expect(nodes[0].data.hasError).toBe(false);
        expect(nodes[1].data.hasError).toBe(true);
    });

    it('counts the metrics and dimensions defined on each entity', () => {
        const nodes = toNodes(spec(), datasets, [], null);
        expect(nodes[0].data.metricCount).toBe(1);
        expect(nodes[1].data.dimensionCount).toBe(1);
    });

    it('reflects the current selection', () => {
        const nodes = toNodes(spec(), datasets, [], { kind: 'entity', name: 'customers' });
        expect(nodes[1].selected).toBe(true);
    });
});

describe('toEdges', () => {
    it('maps a relationship onto its two column handles', () => {
        const [edge] = toEdges(spec(), null);
        expect(edge).toMatchObject({
            id: 'order_customer',
            source: 'orders',
            sourceHandle: 'customer_id',
            target: 'customers',
            targetHandle: 'id',
        });
    });

    it('animates uncertified relationships so drafts read as provisional', () => {
        const s = spec();
        expect(toEdges(s, null)[0].animated).toBe(true);
        s.relationships[0].certified = true;
        expect(toEdges(s, null)[0].animated).toBe(false);
    });
});

// ------------------------------------------------------------------ inspector

describe('parseValueSynonyms', () => {
    it('parses one mapping per line', () => {
        expect(parseValueSynonyms('CA = California, Cali\nNY = New York')).toEqual({
            CA: ['California', 'Cali'],
            NY: ['New York'],
        });
    });

    it('ignores blank and malformed lines', () => {
        expect(parseValueSynonyms('\nCA = California\njunk\n = orphan')).toEqual({
            CA: ['California'],
        });
    });

    it('keeps equals signs that appear in the value', () => {
        expect(parseValueSynonyms('op = a=b')).toEqual({ op: ['a=b'] });
    });
});

describe('removeDefinition', () => {
    it('drops everything defined on a removed entity', () => {
        const next = removeDefinition(spec(), { kind: 'entity', name: 'orders' });
        expect(next.entities.map((e) => e.name)).toEqual(['customers']);
        // revenue was on orders, and the relationship pointed at it.
        expect(next.metrics).toHaveLength(0);
        expect(next.relationships).toHaveLength(0);
        // The customers dimension is untouched.
        expect(next.dimensions).toHaveLength(1);
    });

    it('removes a relationship without touching its entities', () => {
        const next = removeDefinition(spec(), { kind: 'relationship', name: 'order_customer' });
        expect(next.relationships).toHaveLength(0);
        expect(next.entities).toHaveLength(2);
    });

    it('removes a single metric', () => {
        const next = removeDefinition(spec(), { kind: 'metric', name: 'revenue' });
        expect(next.metrics).toHaveLength(0);
        expect(next.entities).toHaveLength(2);
    });
});

// ----------------------------------------------------------------------- card

describe('describeProposed', () => {
    it('summarises each kind in reviewable terms', () => {
        const items = describeProposed(spec(), ['orders', 'order_customer', 'region', 'revenue']);
        expect(items.map((i) => i.kind)).toEqual(['entity', 'relationship', 'dimension', 'metric']);
        expect(items[0].summary).toContain('orders.csv');
        expect(items[1].summary).toBe('orders.customer_id → customers.id (many to one)');
        expect(items[2].summary).toBe('customers.region');
    });

    it('shows a metric filter as part of its definition', () => {
        const [metric] = describeProposed(spec(), ['revenue']);
        expect(metric.summary).toBe("sum(order_total) where status != 'cancelled'");
    });

    it('renders a bare count without an expression', () => {
        const s = spec();
        s.metrics.push({
            name: 'order_count',
            entity: 'orders',
            aggregate: 'count',
            synonyms: [],
            certified: false,
        });
        const [metric] = describeProposed(s, ['order_count']);
        expect(metric.summary).toBe('count(*)');
    });

    it('skips names that no longer exist', () => {
        expect(describeProposed(spec(), ['ghost'])).toEqual([]);
    });
});

describe('formatCell', () => {
    it('renders nulls as a dash', () => {
        expect(formatCell(null)).toBe('—');
        expect(formatCell(undefined)).toBe('—');
    });

    it('formats integers and decimals differently', () => {
        expect(formatCell(1234)).toBe('1,234');
        expect(formatCell(1234.5678)).toBe('1,234.57');
    });

    it('passes strings through', () => {
        expect(formatCell('EU')).toBe('EU');
    });
});

// -------------------------------------------------------------- entity rename

describe('renameEntityReferences', () => {
    it('follows the rename through everything defined on the entity', () => {
        const next = renameEntityReferences(spec(), 'orders', 'sales');
        expect(next.metrics[0].entity).toBe('sales');
        expect(next.relationships[0].fromEntity).toBe('sales');
        // The other side of the join is untouched.
        expect(next.relationships[0].toEntity).toBe('customers');
    });

    it('rewrites whichever side of a relationship matches', () => {
        const next = renameEntityReferences(spec(), 'customers', 'accounts');
        expect(next.relationships[0].toEntity).toBe('accounts');
        expect(next.relationships[0].fromEntity).toBe('orders');
        expect(next.dimensions[0].entity).toBe('accounts');
    });

    it('leaves an unrelated rename alone', () => {
        const before = spec();
        expect(renameEntityReferences(before, 'nothing', 'x')).toEqual(before);
    });

    it('does not orphan definitions — every entity reference still resolves', () => {
        const next = renameEntityReferences(spec(), 'orders', 'sales');
        const names = new Set(next.entities.map((e) => e.name).concat('sales'));
        for (const m of next.metrics) expect(names.has(m.entity)).toBe(true);
        for (const d of next.dimensions) expect(names.has(d.entity)).toBe(true);
    });
});

// ------------------------------------------------------- drag affordance

describe('entityCardStyle', () => {
    it('lifts and goes translucent while dragging', () => {
        const s = entityCardStyle({ dragging: true });
        expect(s.opacity).toBe(0.82);
        expect(s.cursor).toBe('grabbing');
        expect(s.transform).toContain('scale(1.03)');
        expect(s.boxShadow).toContain('24px');
    });

    it('is fully opaque and grabbable at rest', () => {
        const s = entityCardStyle({});
        expect(s.opacity).toBe(1);
        expect(s.cursor).toBe('grab');
        expect(s.transform).toBeUndefined();
    });

    it('dragging chrome wins over selection', () => {
        // Otherwise a card dragged straight after being clicked would keep the
        // selection ring and never look picked up.
        const s = entityCardStyle({ selected: true, dragging: true });
        expect(s.opacity).toBe(0.82);
        expect(s.boxShadow).not.toContain('var(--c-signal-dim)');
    });

    it('shows the error border only when not selected or dragging', () => {
        expect(entityCardStyle({ hasError: true }).borderColor).toBe('var(--c-danger)');
        expect(entityCardStyle({ hasError: true, selected: true }).borderColor).toBe(
            'var(--c-signal)'
        );
    });
});
