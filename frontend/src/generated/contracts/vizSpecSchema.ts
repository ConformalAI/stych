/** GENERATED FILE — do not edit. Regenerate with `npm run codegen`. */

export type SchemaVersion = 1;
export type Title = string | null;
export type Source = string;
export type Op = "filter";
export type Field = string;
export type Predicate =
  | "eq"
  | "neq"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "between"
  | "in"
  | "notin"
  | "contains"
  | "isnull"
  | "notnull";
export type Op1 = "filter_any";
/**
 * @minItems 1
 */
export type Clauses = [FilterTransform[], ...FilterTransform[][]];
export type Op2 = "calculate";
export type Expr = string;
export type As = string;
export type Transform = (FilterTransform | FilterAnyTransform | CalculateTransform)[];
export type Type = "bar" | "line" | "area" | "scatter" | "pie" | "histogram" | "heatmap";
export type Stacked = boolean;
export type Smooth = boolean;
export type Point = boolean;
export type Metric = string | null;
export type Field1 = string | null;
export type Type1 = "quantitative" | "nominal" | "ordinal" | "temporal";
export type Aggregate = ("sum" | "mean" | "min" | "max" | "count" | "distinct") | null;
export type Timeunit = ("year" | "quarter" | "month" | "week" | "day" | "hour") | null;
export type Maxbins = number;
export type Sort = ("asc" | "desc") | SortByEncoding | null;
export type Encoding1 = "x" | "y" | "color" | "size";
export type Order = "asc" | "desc";
export type N = number;
export type Others = boolean;
export type Title1 = string | null;
export type Format = string | null;
export type Tooltip = ChannelDef[];
export type Mode = "single" | "categorical" | "continuous";
export type Value = string | null;
export type Palette = string[] | null;
export type Channel = "x" | "y" | "color";
export type Field2 = string | null;
export type Value1 = string | number | boolean | null;
export type Color = string;
/**
 * @maxItems 500
 */
export type Assignments = ColorAssignment[];
export type Palette1 = string[] | null;
export type Enabled = boolean;
export type Pattern = "diagonal" | "horizontal" | "vertical" | "dots" | "grid" | "crosshatch";
export type Legend = boolean;
export type Grid = boolean;
export type Labels = boolean;
export type Labelfontsize = number | null;
export type Labelcolor = string | null;
export type Numberformat = string | null;

export interface VizSpec {
  schema_version?: SchemaVersion;
  title?: Title;
  data?: DataRef;
  transform?: Transform;
  mark?: MarkDef;
  encoding?: Encoding;
  config?: VizConfig;
}
export interface DataRef {
  source?: Source;
}
export interface FilterTransform {
  op?: Op;
  field: Field;
  predicate: Predicate;
  value?: unknown;
}
/**
 * An OR of filter-AND clauses, used for exact multi-mark selections.
 */
export interface FilterAnyTransform {
  op?: Op1;
  clauses: Clauses;
}
/**
 * A derived column: ``expr`` (viz.expr language) projected as field ``as``.
 */
export interface CalculateTransform {
  op?: Op2;
  expr: Expr;
  as: As;
}
export interface MarkDef {
  type?: Type;
  stacked?: Stacked;
  smooth?: Smooth;
  point?: Point;
}
export interface Encoding {
  x?: ChannelDef | null;
  y?: ChannelDef | null;
  color?: ChannelDef | null;
  size?: ChannelDef | null;
  tooltip?: Tooltip;
  row?: ChannelDef | null;
  column?: ChannelDef | null;
}
/**
 * A field → visual channel mapping.
 */
export interface ChannelDef {
  metric?: Metric;
  field?: Field1;
  type?: Type1;
  aggregate?: Aggregate;
  timeUnit?: Timeunit;
  bin?: BinDef | null;
  sort?: Sort;
  topK?: TopKDef | null;
  title?: Title1;
  format?: Format;
}
/**
 * Equal-width binning of a quantitative field.
 */
export interface BinDef {
  maxbins?: Maxbins;
}
/**
 * Sort a channel BY another channel's value (Vega-Lite sort-by-encoding).
 *
 * A bare ``"asc"``/``"desc"`` on a dimension orders it by its own values
 * (alphabetical); this form orders it by a *different* channel — e.g. a bar
 * chart's categorical x sorted by its y measure: ``{"encoding":"y","order":"desc"}``.
 * Both compilers already ORDER BY the referenced channel's output column, so no
 * engine change is needed.
 */
export interface SortByEncoding {
  encoding: Encoding1;
  order?: Order;
}
/**
 * Keep the top-n groups of a dimension (ranked by the first measure).
 */
export interface TopKDef {
  n?: N;
  others?: Others;
}
export interface VizConfig {
  color?: ColorConfig | null;
  palette?: Palette1;
  texture?: TextureConfig;
  legend?: Legend;
  grid?: Grid;
  labels?: Labels;
  labelFontSize?: Labelfontsize;
  labelColor?: Labelcolor;
  numberFormat?: Numberformat;
}
export interface ColorConfig {
  mode: Mode;
  value?: Value;
  palette?: Palette;
  binding?: ColorBinding | null;
  assignments?: Assignments;
}
export interface ColorBinding {
  channel: Channel;
  field?: Field2;
}
export interface ColorAssignment {
  value: Value1;
  color: Color;
}
export interface TextureConfig {
  enabled?: Enabled;
  pattern?: Pattern;
}
