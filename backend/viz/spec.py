"""
VizSpec — the declarative visualization specification (schema v1).

Vega-Lite-inspired: aggregation, binning, and time-bucketing live INSIDE
encoding channels, so compilers derive the GROUP BY from the encodings.
``transform`` holds only pre-aggregation operations: calculated fields
(``{"op": "calculate"}``, expression language in ``viz.expr``) and filters.
Calculated columns are projected before filters run, so filters and channels
use them exactly like real columns.

Wire format uses Vega-Lite-style key names where they exist (``timeUnit``,
``maxbins``, calculate's ``as``); Python attributes are snake_case with
aliases. Always serialize with ``spec.to_json_dict()`` so the frontend sees
the alias names.

Deliberately in-schema but deferred (validators reject them for now):
row/column faceting. Not in schema yet: layered marks, dashboards.
"""

from __future__ import annotations

from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1

FieldType = Literal["quantitative", "nominal", "ordinal", "temporal"]
Aggregate = Literal["sum", "mean", "min", "max", "count", "distinct"]
TimeUnit = Literal["year", "quarter", "month", "week", "day", "hour"]
MarkType = Literal["bar", "line", "area", "scatter", "pie", "histogram", "heatmap"]
TexturePattern = Literal["diagonal", "horizontal", "vertical", "dots", "grid", "crosshatch"]
ColorMode = Literal["single", "categorical", "continuous"]
ColorChannel = Literal["x", "y", "color"]
SortDir = Literal["asc", "desc"]
Predicate = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "in",
    "notin",
    "contains",
    "isnull",
    "notnull",
]

# Channels whose definition carries an aggregate act as measures; the rest are
# the grouping dimensions. Tooltip channels never influence grouping on their
# own — they are extra projections.
POSITIONAL_CHANNELS = ("x", "y")
GROUPABLE_CHANNELS = ("x", "y", "color", "size")


class _SpecModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class BinDef(_SpecModel):
    """Equal-width binning of a quantitative field."""

    maxbins: int = Field(default=10, ge=2, le=100)


class TopKDef(_SpecModel):
    """Keep the top-n groups of a dimension (ranked by the first measure)."""

    n: int = Field(default=20, ge=1, le=500)
    others: bool = False


class SortByEncoding(_SpecModel):
    """Sort a channel BY another channel's value (Vega-Lite sort-by-encoding).

    A bare ``"asc"``/``"desc"`` on a dimension orders it by its own values
    (alphabetical); this form orders it by a *different* channel — e.g. a bar
    chart's categorical x sorted by its y measure: ``{"encoding":"y","order":"desc"}``.
    Both compilers already ORDER BY the referenced channel's output column, so no
    engine change is needed.
    """

    encoding: Literal["x", "y", "color", "size"]
    order: SortDir = "desc"


class ChannelDef(_SpecModel):
    """A field → visual channel mapping."""

    # A certified metric from the session's ontology, e.g. "revenue". When set,
    # the ontology supplies the expression, aggregate and any metric-scoped
    # filter, and `field`/`aggregate` are derived — so a chart of "revenue"
    # computes it the same way a chat answer does. Charts without one behave
    # exactly as before; the ontology is optional everywhere.
    metric: Optional[str] = None
    field: Optional[str] = None  # None only with aggregate == "count"
    type: FieldType = "nominal"
    aggregate: Optional[Aggregate] = None
    time_unit: Optional[TimeUnit] = Field(default=None, alias="timeUnit")
    bin: Optional[BinDef] = None
    sort: Optional[Union[SortDir, SortByEncoding]] = None
    top_k: Optional[TopKDef] = Field(default=None, alias="topK")
    title: Optional[str] = None
    format: Optional[str] = None

    @property
    def is_measure(self) -> bool:
        return self.aggregate is not None


class FilterTransform(_SpecModel):
    op: Literal["filter"] = "filter"
    field: str
    predicate: Predicate
    value: Any = None


class FilterAnyTransform(_SpecModel):
    """An OR of filter-AND clauses, used for exact multi-mark selections."""

    op: Literal["filter_any"] = "filter_any"
    clauses: List[List[FilterTransform]] = Field(min_length=1)


class CalculateTransform(_SpecModel):
    """A derived column: ``expr`` (viz.expr language) projected as field ``as``."""

    op: Literal["calculate"] = "calculate"
    expr: str
    as_: str = Field(alias="as")


Transform = Annotated[
    Union[FilterTransform, FilterAnyTransform, CalculateTransform], Field(discriminator="op")
]


class MarkDef(_SpecModel):
    type: MarkType = "bar"
    stacked: bool = False
    smooth: bool = False
    point: bool = False


class Encoding(_SpecModel):
    x: Optional[ChannelDef] = None
    y: Optional[ChannelDef] = None
    color: Optional[ChannelDef] = None
    size: Optional[ChannelDef] = None
    tooltip: List[ChannelDef] = Field(default_factory=list)
    # Faceting — accepted by the schema so specs stay forward-compatible, but
    # rejected by validate_spec until implemented.
    row: Optional[ChannelDef] = None
    column: Optional[ChannelDef] = None

    def channel_items(self) -> List[tuple]:
        """(channel_name, ChannelDef) for every set groupable channel, stable order."""
        out = []
        for name in GROUPABLE_CHANNELS:
            ch = getattr(self, name)
            if ch is not None:
                out.append((name, ch))
        return out

    def dimensions(self) -> List[tuple]:
        return [(n, c) for n, c in self.channel_items() if not c.is_measure]

    def measures(self) -> List[tuple]:
        return [(n, c) for n, c in self.channel_items() if c.is_measure]


class DataRef(_SpecModel):
    # Stable DataContext.file_id. ``active`` remains a legacy/input convenience;
    # the executor rewrites it to the resolved file id before returning or
    # persisting a canonical spec.
    source: str = "active"


class TextureConfig(_SpecModel):
    enabled: bool = False
    pattern: TexturePattern = "diagonal"


def _validate_hex_color(color: str) -> str:
    if (
        len(color) != 7
        or color[0] != "#"
        or any(character not in "0123456789abcdefABCDEF" for character in color[1:])
    ):
        raise ValueError("colors must use #RRGGBB hex format")
    return color.upper()


class ColorBinding(_SpecModel):
    channel: ColorChannel
    field: Optional[str] = None


class ColorAssignment(_SpecModel):
    value: Union[str, int, float, bool, None]
    color: str

    @field_validator("color")
    @classmethod
    def validate_color(cls, color: str) -> str:
        return _validate_hex_color(color)


class ColorConfig(_SpecModel):
    mode: ColorMode
    value: Optional[str] = None
    palette: Optional[List[str]] = None
    binding: Optional[ColorBinding] = None
    assignments: List[ColorAssignment] = Field(default_factory=list, max_length=500)

    @field_validator("value")
    @classmethod
    def validate_value(cls, color: Optional[str]) -> Optional[str]:
        return _validate_hex_color(color) if color is not None else None

    @field_validator("palette")
    @classmethod
    def validate_palette(cls, palette: Optional[List[str]]) -> Optional[List[str]]:
        if palette is None:
            return None
        if not 1 <= len(palette) <= 12:
            raise ValueError("color.palette must contain between 1 and 12 colors")
        return [_validate_hex_color(color) for color in palette]

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "ColorConfig":
        if self.mode == "single" and self.value is None:
            raise ValueError("single color mode requires value")
        if self.mode == "categorical" and self.binding is None:
            raise ValueError("categorical color mode requires binding")
        if self.mode == "categorical" and self.binding is not None and self.binding.field is None:
            raise ValueError("categorical color binding requires a field")
        if self.mode == "continuous":
            if self.binding is None:
                raise ValueError("continuous color mode requires binding")
            if self.binding.channel != "color":
                raise ValueError("continuous color mode must bind to the color channel")
            if self.palette is None or len(self.palette) < 2:
                raise ValueError("continuous color mode requires at least two palette colors")
        return self


class VizConfig(_SpecModel):
    color: Optional[ColorConfig] = None
    # Legacy input retained for persisted v1 specs. New editor writes use
    # ``color`` so single, categorical, and continuous semantics are explicit.
    palette: Optional[List[str]] = None
    texture: TextureConfig = Field(default_factory=TextureConfig)
    legend: bool = True
    grid: bool = True
    labels: bool = False
    label_font_size: Optional[int] = Field(default=None, alias="labelFontSize", ge=8, le=32)
    label_color: Optional[str] = Field(default=None, alias="labelColor")
    number_format: Optional[str] = Field(default=None, alias="numberFormat")

    @field_validator("palette")
    @classmethod
    def validate_palette(cls, palette: Optional[List[str]]) -> Optional[List[str]]:
        if palette is None:
            return None
        if not 1 <= len(palette) <= 12:
            raise ValueError("palette must contain between 1 and 12 colors")
        return [_validate_hex_color(color) for color in palette]

    @field_validator("label_color")
    @classmethod
    def validate_label_color(cls, color: Optional[str]) -> Optional[str]:
        return _validate_hex_color(color) if color is not None else None


class VizSpec(_SpecModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    title: Optional[str] = None
    data: DataRef = Field(default_factory=DataRef)
    transform: List[Transform] = Field(default_factory=list)
    mark: MarkDef = Field(default_factory=MarkDef)
    encoding: Encoding = Field(default_factory=Encoding)
    config: VizConfig = Field(default_factory=VizConfig)

    def filters(self) -> List[Union[FilterTransform, FilterAnyTransform]]:
        return [t for t in self.transform if isinstance(t, (FilterTransform, FilterAnyTransform))]

    def calculations(self) -> List[CalculateTransform]:
        return [t for t in self.transform if isinstance(t, CalculateTransform)]

    def to_json_dict(self) -> dict:
        """Canonical wire form (alias keys, no Nones dropped — explicit spec)."""
        return self.model_dump(by_alias=True, mode="json")


class SpecError(BaseModel):
    """One structured validation problem; ``path`` is a dotted spec location."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.path}: {self.message}"
