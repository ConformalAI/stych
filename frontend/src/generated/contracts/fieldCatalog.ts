/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`. */

export type Fingerprint = string;
export type BuiltAt = string;
export type Engine = string;
export type RowCount = number;
export type Name = string;
export type Type = string;
export type Role = string;
export type DefaultAggregate = string | null;
export type Cardinality = number | null;
export type NullPct = number | null;
export type Samples = unknown[];
export type IsId = boolean;
export type DbType = string | null;
export type FormatHint = string | null;
export type IsCalculated = boolean;
export type Fields = FieldInfo[];
export type Source = string | null;
export type Scope = string | null;
export type Freshness = string | null;
export type RowLimit = number | null;
export type Truncated = boolean;
export type SchemaComplete = boolean;

export interface FieldCatalog {
  fingerprint: Fingerprint;
  built_at: BuiltAt;
  engine: Engine;
  row_count: RowCount;
  fields: Fields;
  source?: Source;
  scope?: Scope;
  freshness?: Freshness;
  row_limit?: RowLimit;
  truncated?: Truncated;
  schema_complete?: SchemaComplete;
}
export interface FieldInfo {
  name: Name;
  type: Type;
  role: Role;
  default_aggregate?: DefaultAggregate;
  cardinality?: Cardinality;
  null_pct?: NullPct;
  samples?: Samples;
  is_id?: IsId;
  db_type?: DbType;
  format_hint?: FormatHint;
  is_calculated?: IsCalculated;
}
