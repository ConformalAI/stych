/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`.
 *  Source of truth: backend/viz/spec.py Literal types.
 */

export type FieldType = 'quantitative' | 'nominal' | 'ordinal' | 'temporal';
export type Aggregate = 'sum' | 'mean' | 'min' | 'max' | 'count' | 'distinct';
export type TimeUnit = 'year' | 'quarter' | 'month' | 'week' | 'day' | 'hour';
export type MarkType = 'bar' | 'line' | 'area' | 'scatter' | 'pie' | 'histogram' | 'heatmap';
export type TexturePattern = 'diagonal' | 'horizontal' | 'vertical' | 'dots' | 'grid' | 'crosshatch';
export type ColorMode = 'single' | 'categorical' | 'continuous';
export type ColorChannel = 'x' | 'y' | 'color';
export type SortDir = 'asc' | 'desc';
export type Predicate = 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'between' | 'in' | 'notin' | 'contains' | 'isnull' | 'notnull';
