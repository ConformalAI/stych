"""
Semantic validation and normalization of a VizSpec against a FieldCatalog.

``normalize_spec`` fills deterministic gaps (histogram bins, implicit count
measures, channel types corrected from the catalog) so both the UI and the
agent can be tolerant on input while compilers always see a canonical spec.

``validate_spec`` returns structured ``SpecError``s (never raises) — the agent
loop feeds them back as tool observations for self-correction and the API
returns them as 422 details.
"""

from __future__ import annotations

import difflib
from typing import List, Optional

from .catalog import FieldCatalog, FieldInfo
from .spec import (
    ChannelDef,
    Encoding,
    FilterAnyTransform,
    FilterTransform,
    SortByEncoding,
    SpecError,
    VizSpec,
)

_NUMERIC_PREDICATES = {"gt", "gte", "lt", "lte", "between"}
_LIST_PREDICATES = {"in", "notin"}
_NO_VALUE_PREDICATES = {"isnull", "notnull"}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_spec(spec: VizSpec, catalog: FieldCatalog) -> VizSpec:
    """Return a canonical copy of ``spec``; never raises on bad specs."""
    spec = spec.model_copy(deep=True)
    enc = spec.encoding

    # Channel types follow the catalog: the catalog is authoritative for what a
    # field IS; the channel only chooses how it's used (aggregate/bin/timeUnit).
    for _, ch in enc.channel_items():
        _sync_channel_type(ch, catalog)
    for ch in enc.tooltip:
        _sync_channel_type(ch, catalog)

    # Histogram: x binned quantitative, y an implicit count.
    if spec.mark.type == "histogram":
        if enc.x is not None and enc.x.bin is None:
            from .spec import BinDef  # noqa: PLC0415

            enc.x.bin = BinDef()
        if enc.y is None:
            enc.y = ChannelDef(field=None, type="quantitative", aggregate="count")

    # Dimension-only aggregate charts get an implicit count on their value
    # channel. Horizontal bars put that value on x; all other supported marks
    # use y.
    if enc.measures() == [] and enc.dimensions():
        if spec.mark.type == "bar" and enc.x is None and enc.y is not None:
            enc.x = ChannelDef(field=None, type="quantitative", aggregate="count")
        elif enc.y is None:
            enc.y = ChannelDef(field=None, type="quantitative", aggregate="count")

    # count never carries a field; distinct/other aggregates require one.
    for _, ch in enc.measures():
        if ch.aggregate == "count":
            ch.field = None
    return spec


def _sync_channel_type(ch: ChannelDef, catalog: FieldCatalog) -> None:
    if ch.field is None:
        return
    info = catalog.get(ch.field)
    if info is None:
        return
    # Respect an explicit ordinal choice on nominal fields; else trust catalog.
    if not (ch.type == "ordinal" and info.type == "nominal"):
        ch.type = info.type  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_spec(spec: VizSpec, catalog: FieldCatalog) -> List[SpecError]:
    errors: List[SpecError] = []
    enc = spec.encoding

    if enc.row is not None or enc.column is not None:
        errors.append(
            SpecError(
                path="encoding.row" if enc.row is not None else "encoding.column",
                code="unsupported_channel",
                message="Row/column faceting is not implemented yet.",
            )
        )

    # -- field references ---------------------------------------------------
    for name, ch in enc.channel_items():
        errors.extend(_check_channel(f"encoding.{name}", ch, catalog))
    for i, ch in enumerate(enc.tooltip):
        errors.extend(_check_channel(f"encoding.tooltip[{i}]", ch, catalog))
    for i, t in enumerate(spec.transform):
        if isinstance(t, FilterTransform):
            errors.extend(_check_filter(i, t, catalog))
        elif isinstance(t, FilterAnyTransform):
            for clause_i, clause in enumerate(t.clauses):
                if not clause:
                    errors.append(
                        SpecError(
                            path=f"transform[{i}].clauses[{clause_i}]",
                            code="empty_filter_clause",
                            message="Each filter_any clause needs at least one filter.",
                        )
                    )
                for filter_i, item in enumerate(clause):
                    errors.extend(
                        _check_filter(
                            f"transform[{i}].clauses[{clause_i}][{filter_i}]", item, catalog
                        )
                    )

    # -- shape rules ----------------------------------------------------------
    dims, measures = enc.dimensions(), enc.measures()
    if not enc.x and not enc.y:
        errors.append(
            SpecError(
                path="encoding",
                code="missing_encoding",
                message="At least one of encoding.x or encoding.y is required.",
            )
        )
        return errors

    errors.extend(_check_mark_rules(spec, dims, measures))
    errors.extend(_check_color_config(spec))

    # Tooltip fields are projected for raw charts. On aggregate charts a raw
    # tooltip would either silently change grouping or need its own aggregate;
    # neither semantic is currently represented by the renderer.
    if enc.tooltip and measures:
        errors.append(
            SpecError(
                path="encoding.tooltip",
                code="unsupported_aggregated_tooltip",
                message="Tooltip fields are not supported on aggregated charts yet.",
            )
        )

    # topK only meaningful on plain categorical dimensions.
    for name, ch in enc.channel_items():
        if ch.top_k is None:
            continue
        if ch.is_measure:
            errors.append(
                SpecError(
                    path=f"encoding.{name}.topK",
                    code="topk_on_measure",
                    message="topK applies to dimension channels only.",
                )
            )
        elif (
            ch.bin is not None or ch.time_unit is not None or ch.type not in ("nominal", "ordinal")
        ):
            errors.append(
                SpecError(
                    path=f"encoding.{name}.topK",
                    code="topk_on_continuous",
                    message="topK applies only to plain categorical dimensions.",
                )
            )
        elif not measures:
            # topK ranks groups by the first measure; with no measure there is no
            # aggregation to rank (raw specs like scatter), so it is meaningless —
            # and both compilers assume a measure exists (SQL would crash on it).
            errors.append(
                SpecError(
                    path=f"encoding.{name}.topK",
                    code="topk_needs_measure",
                    message=(
                        "topK ranks groups by a measure — add an aggregated measure "
                        "channel (e.g. y with an aggregate) or remove topK."
                    ),
                )
            )

    # sort-by-encoding must point at a channel that is actually set, else it
    # would silently no-op (the referenced output column never exists).
    for name, ch in enc.channel_items():
        if isinstance(ch.sort, SortByEncoding) and getattr(enc, ch.sort.encoding) is None:
            errors.append(
                SpecError(
                    path=f"encoding.{name}.sort",
                    code="sort_target_missing",
                    message=(
                        f"sort references channel '{ch.sort.encoding}', which is not set. "
                        "Point sort at an encoded channel (e.g. the measure)."
                    ),
                )
            )

    if spec.mark.stacked and spec.mark.type not in ("bar", "area"):
        errors.append(
            SpecError(
                path="mark.stacked",
                code="invalid_mark_option",
                message="stacked is only supported for bar and area marks.",
            )
        )
    if spec.mark.smooth and spec.mark.type not in ("line", "area"):
        errors.append(
            SpecError(
                path="mark.smooth",
                code="invalid_mark_option",
                message="smooth is only supported for line and area marks.",
            )
        )
    if spec.mark.point and spec.mark.type not in ("line", "area"):
        errors.append(
            SpecError(
                path="mark.point",
                code="invalid_mark_option",
                message="point is only supported for line and area marks.",
            )
        )
    return errors


def _check_color_config(spec: VizSpec) -> List[SpecError]:
    color = spec.config.color
    if color is None or color.mode == "single":
        return []

    binding = color.binding
    if binding is None:  # Schema validation normally catches this.
        return []
    channel = getattr(spec.encoding, binding.channel)
    if channel is None:
        return [
            SpecError(
                path="config.color.binding.channel",
                code="color_binding_missing_channel",
                message=f"Color binding references unset channel '{binding.channel}'.",
            )
        ]
    if channel.field != binding.field:
        return [
            SpecError(
                path="config.color.binding.field",
                code="color_binding_field_mismatch",
                message=(
                    f"Color binding expects field '{binding.field}', but "
                    f"encoding.{binding.channel} uses '{channel.field}'."
                ),
            )
        ]
    if color.mode == "categorical" and channel.is_measure:
        return [
            SpecError(
                path="config.color.binding",
                code="categorical_color_needs_dimension",
                message="Categorical color assignments must bind to a grouping dimension.",
            )
        ]
    if color.mode == "continuous" and not channel.is_measure:
        return [
            SpecError(
                path="config.color.binding",
                code="continuous_color_needs_measure",
                message="Continuous color must bind to an aggregated measure.",
            )
        ]
    return []


def _suggest(name: str, catalog: FieldCatalog) -> str:
    close = difflib.get_close_matches(name, catalog.field_names(), n=2, cutoff=0.6)
    return f" Did you mean: {', '.join(close)}?" if close else ""


def _check_channel(path: str, ch: ChannelDef, catalog: FieldCatalog) -> List[SpecError]:
    errors: List[SpecError] = []
    info: Optional[FieldInfo] = None

    if ch.field is None:
        if ch.aggregate != "count":
            errors.append(
                SpecError(
                    path=f"{path}.field",
                    code="missing_field",
                    message="Channel needs a field (only aggregate='count' may omit it).",
                )
            )
            return errors
    else:
        info = catalog.get(ch.field)
        if info is None:
            errors.append(
                SpecError(
                    path=f"{path}.field",
                    code="unknown_field",
                    message=f"Field '{ch.field}' does not exist.{_suggest(ch.field, catalog)}",
                )
            )
            return errors

    if ch.aggregate in ("sum", "mean", "min", "max") and info is not None:
        if info.type != "quantitative":
            errors.append(
                SpecError(
                    path=f"{path}.aggregate",
                    code="aggregate_type_mismatch",
                    message=(
                        f"Aggregate '{ch.aggregate}' requires a numeric field, but "
                        f"'{info.name}' is {info.type}. No edit to the chart can fix "
                        f"this — the column itself has to become numeric."
                    ),
                )
            )
    if ch.time_unit is not None and info is not None and info.type != "temporal":
        errors.append(
            SpecError(
                path=f"{path}.timeUnit",
                code="timeunit_type_mismatch",
                message=f"timeUnit requires a temporal field; '{info.name}' is {info.type}.",
            )
        )
    if ch.bin is not None and info is not None and info.type != "quantitative":
        errors.append(
            SpecError(
                path=f"{path}.bin",
                code="bin_type_mismatch",
                message=f"bin requires a numeric field; '{info.name}' is {info.type}.",
            )
        )
    if ch.bin is not None and ch.time_unit is not None:
        errors.append(
            SpecError(
                path=f"{path}.bin",
                code="bin_and_timeunit",
                message="A channel cannot set both bin and timeUnit.",
            )
        )
    if ch.bin is not None and ch.aggregate is not None:
        errors.append(
            SpecError(
                path=f"{path}.bin",
                code="bin_on_measure",
                message="bin makes a channel a dimension; remove its aggregate.",
            )
        )
    return errors


def _check_filter(i: int | str, t, catalog: FieldCatalog) -> List[SpecError]:
    errors: List[SpecError] = []
    path = i if isinstance(i, str) else f"transform[{i}]"
    info = catalog.get(t.field)
    if info is None:
        errors.append(
            SpecError(
                path=f"{path}.field",
                code="unknown_field",
                message=f"Field '{t.field}' does not exist.{_suggest(t.field, catalog)}",
            )
        )
        return errors
    if t.predicate in _NO_VALUE_PREDICATES:
        return errors
    if t.value is None:
        errors.append(
            SpecError(
                path=f"{path}.value",
                code="missing_value",
                message=f"Predicate '{t.predicate}' requires a value.",
            )
        )
        return errors
    if t.predicate in _LIST_PREDICATES and not isinstance(t.value, list):
        errors.append(
            SpecError(
                path=f"{path}.value",
                code="value_not_list",
                message=f"Predicate '{t.predicate}' requires a list value.",
            )
        )
    if t.predicate == "between" and (not isinstance(t.value, list) or len(t.value) != 2):
        errors.append(
            SpecError(
                path=f"{path}.value",
                code="bad_between",
                message="Predicate 'between' requires a [low, high] pair.",
            )
        )
    if t.predicate == "contains" and info.type != "nominal":
        errors.append(
            SpecError(
                path=f"{path}.predicate",
                code="contains_on_non_text",
                message=f"'contains' only applies to text fields; '{info.name}' is {info.type}.",
            )
        )
    if t.predicate in _NUMERIC_PREDICATES and info.type == "nominal":
        errors.append(
            SpecError(
                path=f"{path}.predicate",
                code="range_on_text",
                message=f"'{t.predicate}' does not apply to text field '{info.name}'.",
            )
        )
    return errors


def _check_mark_rules(spec: VizSpec, dims: list, measures: list) -> List[SpecError]:
    errors: List[SpecError] = []
    enc: Encoding = spec.encoding
    mark = spec.mark.type

    def err(path: str, code: str, message: str) -> None:
        errors.append(SpecError(path=path, code=code, message=message))

    def require_dimension(name: str, message: str) -> bool:
        ch = getattr(enc, name)
        if ch is None or ch.is_measure:
            err(f"encoding.{name}", "invalid_channel_for_mark", message)
            return False
        return True

    def require_measure(name: str, message: str) -> bool:
        ch = getattr(enc, name)
        if ch is None or not ch.is_measure:
            err(f"encoding.{name}", "invalid_channel_for_mark", message)
            return False
        return True

    def reject(name: str, message: str) -> None:
        if getattr(enc, name) is not None:
            err(f"encoding.{name}", "invalid_channel_for_mark", message)

    def optional_dimension(name: str, chart: str) -> None:
        ch = getattr(enc, name)
        if ch is not None and ch.is_measure:
            err(
                f"encoding.{name}",
                "invalid_channel_for_mark",
                f"{chart} {name} must be a grouping field without an aggregate.",
            )

    if mark == "pie":
        x_dim = enc.x is not None and not enc.x.is_measure
        color_dim = enc.color is not None and not enc.color.is_measure
        if not x_dim and not color_dim:
            err(
                "encoding",
                "pie_needs_dimension",
                "Pie needs exactly one category, on x or color.",
            )
        elif x_dim and color_dim:
            err(
                "encoding",
                "pie_needs_one_dimension",
                "Pie accepts only one category, on x or color.",
            )
        if x_dim:
            optional_dimension("x", "Pie")
        if color_dim:
            optional_dimension("color", "Pie")
        require_measure("y", "Pie needs exactly one measure on y.")
        reject("size", "Pie does not support size.")
        if enc.color is not None and enc.color.is_measure:
            err("encoding.color", "invalid_channel_for_mark", "Pie color must be its category.")
    elif mark == "histogram":
        if enc.x is None or enc.x.is_measure or enc.x.bin is None:
            err("encoding.x", "histogram_needs_bin", "Histogram needs a binned numeric x.")
        y = enc.y
        if y is None or y.aggregate != "count":
            err("encoding.y", "histogram_y_count", "Histogram y must be a count.")
        optional_dimension("color", "Histogram")
        reject("size", "Histogram does not support size.")
    elif mark == "heatmap":
        x_dim = enc.x is not None and not enc.x.is_measure
        y_dim = enc.y is not None and not enc.y.is_measure
        if not (x_dim and y_dim):
            err("encoding", "heatmap_needs_two_dims", "Heatmap needs dimension x AND y.")
        if enc.color is None or not enc.color.is_measure:
            err(
                "encoding.color",
                "heatmap_needs_measure_color",
                "Heatmap needs a measure on color (e.g. count or sum of a numeric field).",
            )
        reject("size", "Heatmap does not support size.")
    elif mark == "scatter":

        def raw_numeric(ch: Optional[ChannelDef]) -> bool:
            return bool(
                ch is not None
                and not ch.is_measure
                and ch.field is not None
                and ch.type == "quantitative"
                and ch.bin is None
            )

        def aggregate_numeric(ch: Optional[ChannelDef]) -> bool:
            return bool(
                ch is not None
                and ch.is_measure
                and ch.field is not None
                and ch.type == "quantitative"
                and ch.bin is None
            )

        raw_axes = raw_numeric(enc.x) and raw_numeric(enc.y)
        aggregate_axes = aggregate_numeric(enc.x) and aggregate_numeric(enc.y)
        if not (raw_axes or aggregate_axes):
            err(
                "encoding",
                "scatter_numeric_axes",
                "Scatter axes must both be numeric and use the same mode: raw or aggregated.",
            )
        optional_dimension("color", "Scatter")
        size_valid = enc.size is None or (
            raw_numeric(enc.size) if raw_axes else aggregate_numeric(enc.size)
        )
        if not size_valid:
            err(
                "encoding.size",
                "invalid_channel_for_mark",
                "Scatter size must be numeric and use the same aggregation mode as its axes.",
            )
    elif mark == "bar":
        vertical = (
            enc.x is not None and not enc.x.is_measure and enc.y is not None and enc.y.is_measure
        )
        horizontal = (
            enc.y is not None and not enc.y.is_measure and enc.x is not None and enc.x.is_measure
        )
        if not (vertical or horizontal):
            code = (
                "two_measure_axes"
                if enc.x is not None and enc.y is not None and enc.x.is_measure and enc.y.is_measure
                else "bar_needs_dimension_measure"
            )
            err(
                "encoding",
                code,
                "Bar needs one dimension axis and one aggregated measure axis.",
            )
        optional_dimension("color", "Bar")
        reject("size", "Bar does not support size.")
    else:  # line | area
        require_dimension("x", f"{mark.title()} needs a dimension on x.")
        require_measure("y", f"{mark.title()} needs an aggregated measure on y.")
        optional_dimension("color", mark.title())
        reject("size", f"{mark.title()} does not support size.")
    return errors
