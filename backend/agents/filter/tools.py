"""
Tool definitions for the filter ReAct loop, built on the unified Toolkit.

Argument schemas are *generated* from the filters-subsystem Pydantic models
(``filters/spec.py``) — the model sees the real FilterSpec/AnnotateStep
contract instead of an opaque ``{"type": "object"}`` blob, and invalid calls
are rejected with structured ``validation_errors`` before any handler runs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from filters.spec import AnnotationOutput, FilterSpec
from react_engine.toolkit import Tool, Toolkit


class SelectDatasetArgs(BaseModel):
    id: str = Field(..., description="A dataset id from the Workspace datasets list.")


class SampleRowsArgs(BaseModel):
    n: int = Field(5, ge=1, le=50, description="Rows to return (max 50).")
    columns: Optional[List[str]] = Field(None, description="Optional column subset.")


class ColumnValuesArgs(BaseModel):
    column: str = Field(..., description="Column to profile.")
    top_k: int = Field(15, ge=1, le=25, description="Max values (default 15, max 25).")


class InspectDataArgs(BaseModel):
    code: str = Field(..., description="Short read-only Python script; print() findings.")


class RunAnalyzerArgs(BaseModel):
    """Flattened, non-union mirror of ``AnnotateStep``.

    A discriminated Union (``kind``-tagged ``oneOf``) as the sole top-level tool
    argument reliably gets serialized as an escaped JSON *string* instead of a
    nested object — reproduced directly against the configured model/provider:
    identical prompt, only the schema changed, and the union form alone flipped
    the model from emitting a proper dict to stringifying it. Plain fields with
    no ``oneOf`` avoid the failure entirely, so this tool takes ``kind`` plus
    every step kind's fields flattened side by side (irrelevant ones just stay
    unset) rather than one polymorphic ``step`` object.
    """

    kind: Literal["fuzzy_duplicates", "duplicate_values", "pandas", "sql"] = Field(
        ..., description="Which annotate step kind to test."
    )
    id: str = Field("probe", description="Step id, short lowercase [a-z0-9_].")
    outputs: List[AnnotationOutput] = Field(
        default_factory=list,
        description=(
            "Declared output columns. Required (min 1) for 'pandas'/'sql'; optional for "
            "'fuzzy_duplicates'/'duplicate_values' (sensible defaults are filled in)."
        ),
    )
    # fuzzy_duplicates
    on: Optional[str] = Field(None, description="[fuzzy_duplicates] Column to fuzzy-match on.")
    strategy: str = Field("token_sort", description="[fuzzy_duplicates] Match strategy.")
    threshold: float = Field(0.9, ge=0.0, le=1.0, description="[fuzzy_duplicates] Match threshold.")
    max_rows: int = Field(50_000, ge=100, le=200_000, description="[fuzzy_duplicates] Row cap.")
    # duplicate_values
    columns: Optional[List[str]] = Field(
        None, description="[duplicate_values] Columns to match on (exact dupes)."
    )
    min_count: int = Field(2, ge=2, description="[duplicate_values] Minimum group size.")
    # pandas
    code: Optional[str] = Field(
        None, description="[pandas] Python code — see the AnnotateStep pandas-step contract."
    )
    # sql
    sql: Optional[str] = Field(None, description="[sql] SELECT statement.")
    key_column: Optional[str] = Field(
        None, description="[sql] Row-key column returned by the SELECT."
    )


def run_analyzer_step_dict(args: Dict[str, Any]) -> Dict[str, Any]:
    """Reconstruct one AnnotateStep-shaped dict from flattened RunAnalyzerArgs fields."""
    kind = args.get("kind")
    step: Dict[str, Any] = {"id": args.get("id") or "probe", "kind": kind}
    if args.get("outputs"):
        step["outputs"] = args["outputs"]
    if kind == "fuzzy_duplicates":
        step["on"] = args.get("on")
        for key in ("strategy", "threshold", "max_rows"):
            if args.get(key) is not None:
                step[key] = args[key]
    elif kind == "duplicate_values":
        step["columns"] = args.get("columns")
        if args.get("min_count") is not None:
            step["min_count"] = args["min_count"]
    elif kind == "pandas":
        step["code"] = args.get("code")
    elif kind == "sql":
        step["sql"] = args.get("sql")
        step["key_column"] = args.get("key_column")
    return step


class PreviewFilterArgs(BaseModel):
    spec: FilterSpec = Field(..., description="The complete FilterSpec to dry-run.")


class SaveFilterArgs(BaseModel):
    spec: FilterSpec = Field(..., description="The complete FilterSpec to persist.")
    name: Optional[str] = Field(None, description="Display name (defaults to spec.name).")


class UpdateFilterArgs(BaseModel):
    filter_id: str = Field(..., description="Id of the filter to overwrite (see list_filters).")
    spec: FilterSpec = Field(..., description="The complete replacement FilterSpec.")
    name: Optional[str] = Field(None, description="Optional new display name.")


def build_filter_toolkit(toolset) -> Toolkit:
    """Bind the filter tools to one FilterToolset instance."""
    return Toolkit(
        [
            Tool(
                name="select_dataset",
                description=(
                    "Switch which uploaded dataset this filter targets. Call this FIRST, "
                    "before any other tool, when the environment block lists more than one "
                    "Workspace dataset and the request names or implies one that isn't "
                    "marked [active]. Do not guess between multiple candidates — ask the "
                    "user to clarify instead."
                ),
                handler=toolset._tool_select_dataset,
                input_model=SelectDatasetArgs,
            ),
            Tool(
                name="get_schema",
                description=(
                    "Column names, dtypes and example values for the active dataset, plus "
                    "null/unique counts when the source can provide them cheaply. Call this "
                    "before referencing any column."
                ),
                handler=toolset._tool_get_schema,
                parallel_safe=True,
            ),
            Tool(
                name="sample_rows",
                description="A few real rows so you can see actual value formats and casing.",
                handler=toolset._tool_sample_rows,
                input_model=SampleRowsArgs,
                parallel_safe=True,
            ),
            Tool(
                name="column_values",
                description="Top distinct values of one column with occurrence counts.",
                handler=toolset._tool_column_values,
                input_model=ColumnValuesArgs,
                parallel_safe=True,
            ),
            Tool(
                name="inspect_data",
                description=(
                    "Run a short READ-ONLY Python script against a scratch copy of the data "
                    "to answer ad-hoc questions the structured tools cannot (distributions, "
                    "cross-column null counts, testing a regex or a threshold). The script "
                    "reads 'filter_input.parquet' (the dataset plus a __row_key__ column) "
                    "and must print() its findings. It cannot touch the real workspace, the "
                    "network, or the database. Prefer get_schema/sample_rows/column_values "
                    "when they suffice — they are much faster. Uploaded files only; live "
                    "database sources must use the structured tools."
                ),
                handler=toolset._tool_inspect_data,
                input_model=InspectDataArgs,
            ),
            Tool(
                name="run_analyzer",
                description=(
                    "Execute ONE annotate step and return a validation summary: rows "
                    "annotated, coverage, per-output stats (score distribution, group "
                    "count/sizes) and a few sample rows. Use it to pick thresholds and to "
                    "verify agent-written pandas steps before putting them in a spec. "
                    "Uploaded files only; validate live database specs with preview_filter."
                ),
                handler=toolset._tool_run_analyzer,
                input_model=RunAnalyzerArgs,
            ),
            Tool(
                name="preview_filter",
                description=(
                    "Dry-run a complete FilterSpec: returns matched/total row counts, "
                    "per-rule counts, group count, warnings, and up to 10 matching sample "
                    "rows. ALWAYS run this before save_filter and sanity-check the counts "
                    "(0 matches or ~everything matching usually means a bad threshold)."
                ),
                handler=toolset._tool_preview_filter,
                input_model=PreviewFilterArgs,
            ),
            Tool(
                name="save_filter",
                description="Persist a new filter for this dataset and notify the UI.",
                handler=toolset._save_filter,
                input_model=SaveFilterArgs,
            ),
            Tool(
                name="update_filter",
                description="Overwrite an existing filter's spec (bumps its version).",
                handler=toolset._update_filter,
                input_model=UpdateFilterArgs,
            ),
            Tool(
                name="list_filters",
                description=(
                    "Saved filters for this session/dataset (id, name, version). Check "
                    "before creating: edits to an existing filter should update_filter, "
                    "not duplicate it."
                ),
                handler=toolset._tool_list_filters,
                parallel_safe=True,
            ),
        ]
    )
