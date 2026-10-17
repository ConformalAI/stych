"""
Tool executors for the FilterAgent.

Observations are compact JSON strings (the ReAct loop passes them back to the
model verbatim). Everything is read-only against user data: ``inspect_data``
and ``run_analyzer`` run in a scratch copy of the dataset, so agent code can
never touch the real workspace; only save/update write — and only filter specs
to the app database, never data.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from pydantic import ValidationError

from agents.filter.tools import run_analyzer_step_dict
from database import filter_store
from database.connection import SessionLocal
from filters import annotate_pandas
from filters.service import resolve_row_identity
from filters.spec import ROW_KEY_COLUMN, parse_spec, validation_errors
from helpers.helper import to_json_safe
from helpers.sse import SseEventType
from workspace.manager import dataset_stem, resolve_active_dataset, seed_workspace_data
from workspace.runner import head_tail_preview
from workspace.sources import list_sources

logger = logging.getLogger(__name__)

_MAX_OBSERVATION = 4000
_SCRATCH_DIR = "filter_scratch"


def _dumps(payload: Any) -> str:
    text = json.dumps(payload, default=str)
    if len(text) > _MAX_OBSERVATION:
        text = text[:_MAX_OBSERVATION] + '… (truncated)"'
    return text


class FilterToolset:
    """Stateful per-run executor: one instance per FilterAgent turn."""

    def __init__(
        self,
        state,
        session_id: str,
        *,
        workspace_manager,
        workspace_runner,
        progress_emitter=None,
    ) -> None:
        self.state = state
        self.session_id = session_id
        self.workspace_manager = workspace_manager
        self.workspace_runner = workspace_runner
        self.progress_emitter = progress_emitter
        self.user_id = str((state.metadata or {}).get("user_id") or "")
        # Filters created/updated this turn — surfaced in the agent's metadata.
        self.saved: List[Dict[str, Any]] = []
        self._scratch: Optional[Path] = None
        self._toolkit = None

    @property
    def toolkit(self):
        """The bound Toolkit (schemas + validated dispatch) for this turn."""
        if self._toolkit is None:
            from agents.filter.tools import build_filter_toolkit  # noqa: PLC0415 — cycle

            self._toolkit = build_filter_toolkit(self)
        return self._toolkit

    # ------------------------------------------------------------- helpers

    def _df(self) -> pd.DataFrame:
        ctx = self.state.data_context
        df = ctx.working_dataframe if ctx is not None else None
        if df is None:
            raise RuntimeError("No dataset is loaded in this session.")
        return df

    def _remote_adapter(self):
        """Active live-source adapter, or None for an uploaded/local dataset."""
        ctx = self.state.data_context
        if ctx is None:
            return None
        remote = bool(callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source())
        if not remote and not ctx.is_sql_datasource():
            return None

        from agents.sql_agent import working_copies as wc  # noqa: PLC0415
        from datasources import adapters as table_adapters  # noqa: PLC0415

        return table_adapters.resolve_adapter(ctx, wc.active_source_variant(ctx.metadata or {}))

    def resolve_source(self) -> Tuple[str, Dict[str, Optional[str]], str]:
        """(source_kind, apply params, dataset_ref) for the active dataset.

        File sessions bind to the workspace parquet (seeding it if needed) so the
        saved filter's dataset_ref matches the preview tab's source id.
        """
        ctx = self.state.data_context
        if ctx is not None and ctx.is_sql_datasource():
            from agents.sql_agent import working_copies as wc  # noqa: PLC0415

            meta = ctx.metadata or {}
            variant = wc.active_source_variant(meta)
            ref = f"{ctx.file_id}::{variant}"
            return "sql", {"source_id": ref}, ref

        root = self.workspace_manager.get_or_create(self.session_id, user_id=self.user_id)
        resolved = resolve_active_dataset(root, self.state)
        if resolved is None or not (resolved[0].is_file() or resolved[1].is_file()):
            seed_workspace_data(root, self._df(), getattr(ctx, "filename", None))
            resolved = resolve_active_dataset(root, self.state)
        parquet_path, csv_path, _stem = resolved
        name = parquet_path.name if parquet_path.is_file() else csv_path.name
        return "file", {"path": name}, name

    def sources_block(self) -> str:
        """Multi-dataset catalog for the system prompt; empty when there's
        nothing to disambiguate (SQL session, or a single dataset)."""
        ctx = self.state.data_context
        if ctx is not None and ctx.is_sql_datasource():
            return ""
        root = self.workspace_manager.get_or_create(self.session_id, user_id=self.user_id)
        sources = list_sources(self.state, root)
        if len(sources) <= 1:
            return ""
        lines = ["Workspace datasets (pass one's id to select_dataset to switch):"]
        for s in sources:
            dims = ""
            if s["rows"] is not None and s["columns"] is not None:
                dims = f", {s['rows']}×{s['columns']}"
            elif s["columns"] is not None:
                dims = f", {s['columns']} cols"
            flags = " [active]" if s["active"] else ""
            cols = ", ".join(s["column_names"]) if s["column_names"] else ""
            lines.append(f"- `{s['id']}`{flags}{dims}: {cols}")
        return "\n".join(lines)

    def _scratch_workspace(self) -> Path:
        """A confined scratch dir holding ``filter_input.parquet`` (data +
        __row_key__). Sandbox runs are rooted here, never in the real workspace."""
        if self._scratch is not None and (self._scratch / annotate_pandas.INPUT_PARQUET).is_file():
            return self._scratch
        root = self.workspace_manager.get_or_create(self.session_id, user_id=self.user_id)
        scratch = Path(root) / _SCRATCH_DIR
        scratch.mkdir(parents=True, exist_ok=True)
        df = self._df()
        row_keys, _label, _warn = resolve_row_identity(df, parse_spec({"name": "probe"}).identity)
        frame = df.copy()
        frame[ROW_KEY_COLUMN] = row_keys.to_numpy()
        frame.to_parquet(scratch / annotate_pandas.INPUT_PARQUET, index=False)
        self._scratch = scratch
        return scratch

    def cleanup_scratch(self) -> None:
        """Remove the scratch dir once the turn ends — it never held anything
        the saved filter (or anything outside this instance) depends on."""
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None

    # ------------------------------------------------------------- dispatch

    async def call(self, name: str, args: Any) -> str:
        """Validated dispatch through the Toolkit (kept for tests/direct use)."""
        return await self.toolkit.execute(name, args)

    # ------------------------------------------------------------- inspect

    def _tool_select_dataset(self, args: Dict[str, Any]) -> str:
        """Switch which dataset this turn targets. Both ``_df()`` and
        ``resolve_source()`` key off ``state.data_context``/``active_dataset``,
        so routing every selection through ``set_active_dataset`` (the same
        call the orchestrator's ``_activate_named_source`` uses) keeps them
        in agreement — the tool exploring the schema and the tool persisting
        the filter can no longer target different files."""
        target_id = str(args.get("id") or "").strip()
        if not target_id:
            return _dumps({"error": "Missing id."})
        root = self.workspace_manager.get_or_create(self.session_id, user_id=self.user_id)
        sources = list_sources(self.state, root)
        match = next((s for s in sources if s["id"] == target_id), None)
        if match is None:
            return _dumps(
                {
                    "error": f"Unknown dataset id {target_id!r}.",
                    "available": [s["id"] for s in sources],
                }
            )
        if match["kind"] == "sql":
            return _dumps({"error": "select_dataset does not support SQL sources."})

        path_arg = target_id
        if match["kind"] == "dataset":
            name = target_id.split(":", 1)[1]
            dc = next((c for c in self.state.get_all_data_contexts() if c.filename == name), None)
            if dc is None:
                return _dumps({"error": f"Dataset {name!r} not found."})
            stem = dataset_stem(dc.filename)
            if not (root / f"{stem}.parquet").is_file():
                seed_workspace_data(root, dc.working_dataframe, dc.filename)
            path_arg = f"{stem}.parquet"

        from tools.workspace_toolset import WorkspaceToolset  # noqa: PLC0415 — avoid cycle

        result = WorkspaceToolset().set_active_dataset(root, path_arg, state=self.state)
        if str(result).startswith("ERROR"):
            return _dumps({"error": result})
        self._scratch = None
        df = self._df()
        return _dumps(
            {"selected": target_id, "rows": int(len(df)), "columns": [str(c) for c in df.columns]}
        )

    def _tool_get_schema(self, _args: Dict[str, Any]) -> str:
        adapter = self._remote_adapter()
        if adapter is not None:
            try:
                with adapter.bound_session():
                    all_schema = adapter.schema()
                    schema = all_schema[:60]
                    count = adapter.count()
                    names = [column.name for column in schema]
                    sample = adapter.fetch_sample(columns=names, limit=50)
                    stats = adapter.stats() if adapter.supports_stats else None
            except Exception as exc:  # adapter errors are returned to the agent, not raised
                return _dumps({"error": str(exc)})

            cols = []
            for column in schema:
                series = sample[column.name] if column.name in sample.columns else pd.Series()
                exact = (stats or {}).get(column.name) or {}
                cols.append(
                    {
                        "name": column.name,
                        "dtype": column.db_type,
                        "nulls": exact.get("nulls"),
                        "unique": exact.get("unique"),
                        "examples": [to_json_safe(v) for v in series.dropna().head(3)],
                    }
                )
            return _dumps(
                {
                    "rows": count.value,
                    "rows_approx": count.approx,
                    "columns": cols,
                    "truncated": len(all_schema) > 60,
                    "freshness": "live",
                    "sample_rows": len(sample),
                }
            )

        df = self._df()
        cols = []
        for col in list(df.columns)[:60]:
            series = df[col]
            try:
                unique = int(series.nunique())
            except TypeError:
                unique = None
            examples = [to_json_safe(v) for v in series.dropna().head(3)]
            cols.append(
                {
                    "name": str(col),
                    "dtype": str(series.dtype),
                    "nulls": int(series.isna().sum()),
                    "unique": unique,
                    "examples": examples,
                }
            )
        return _dumps({"rows": len(df), "columns": cols, "truncated": len(df.columns) > 60})

    def _tool_sample_rows(self, args: Dict[str, Any]) -> str:
        adapter = self._remote_adapter()
        if adapter is not None:
            n = min(int(args.get("n") or 5), 50)
            columns = args.get("columns")
            try:
                with adapter.bound_session():
                    names = [column.name for column in adapter.schema()]
                    if columns:
                        known = [column for column in columns if column in names]
                        if not known:
                            return _dumps({"error": f"None of {columns} exist."})
                    else:
                        known = names
                    frame = adapter.fetch_sample(columns=known, limit=n)
            except Exception as exc:
                return _dumps({"error": str(exc)})
            rows = [
                {str(k): to_json_safe(v) for k, v in rec.items()}
                for rec in frame.to_dict(orient="records")
            ]
            return _dumps({"rows": rows, "freshness": "live", "scope": "bounded"})

        df = self._df()
        n = min(int(args.get("n") or 5), 50)
        columns = args.get("columns")
        if columns:
            known = [c for c in columns if c in df.columns]
            if not known:
                return _dumps({"error": f"None of {columns} exist."})
            df = df[known]
        rows = [
            {str(k): to_json_safe(v) for k, v in rec.items()}
            for rec in df.head(n).to_dict(orient="records")
        ]
        return _dumps({"rows": rows})

    def _tool_column_values(self, args: Dict[str, Any]) -> str:
        adapter = self._remote_adapter()
        if adapter is not None:
            column = args.get("column")
            top_k = min(int(args.get("top_k") or 15), 25)
            try:
                with adapter.bound_session():
                    names = [item.name for item in adapter.schema()]
                    if column not in names:
                        return _dumps({"error": f"Column {column!r} not found."})
                    total = adapter.count()
                    frame = adapter.fetch_sample(columns=[column], limit=10_000)
            except Exception as exc:
                return _dumps({"error": str(exc)})
            series = frame[column]
            counts = series.value_counts(dropna=True).head(top_k)
            full = not total.approx and len(frame) >= total.value
            return _dumps(
                {
                    "values": [
                        {"value": to_json_safe(value), "count": int(count)}
                        for value, count in counts.items()
                    ],
                    "nulls": int(series.isna().sum()),
                    "distinct": int(series.nunique()),
                    "scope": "full" if full else "bounded",
                    "rows_sampled": len(frame),
                    "total_rows": total.value,
                    "freshness": "live",
                }
            )

        df = self._df()
        column = args.get("column")
        if column not in df.columns:
            return _dumps({"error": f"Column {column!r} not found."})
        top_k = min(int(args.get("top_k") or 15), 25)
        counts = df[column].value_counts(dropna=True).head(top_k)
        return _dumps(
            {
                "values": [{"value": to_json_safe(v), "count": int(c)} for v, c in counts.items()],
                "nulls": int(df[column].isna().sum()),
                "distinct": int(df[column].nunique()),
            }
        )

    def _tool_inspect_data(self, args: Dict[str, Any]) -> str:
        if self._remote_adapter() is not None:
            return _dumps(
                {
                    "error": "inspect_data is unavailable for live database sources; use "
                    "get_schema, sample_rows, column_values, and preview_filter."
                }
            )
        code = args.get("code") or ""
        if not code.strip():
            return _dumps({"error": "Empty code."})
        scratch = self._scratch_workspace()
        result = self.workspace_runner.run(scratch, code)
        if not result.get("success"):
            detail = (result.get("error") or result.get("stderr") or "sandbox failure").strip()
            return _dumps({"error": head_tail_preview(detail, head=750, tail=750)})
        stdout = (result.get("stdout") or "").strip() or "(no output — use print())"
        return _dumps({"stdout": stdout[: _MAX_OBSERVATION - 100]})

    # ------------------------------------------------------------- analyze

    def _tool_run_analyzer(self, args: Dict[str, Any]) -> str:
        if self._remote_adapter() is not None:
            return _dumps(
                {
                    "error": "run_analyzer is unavailable for live database sources; author "
                    "the FilterSpec and validate it with preview_filter."
                }
            )
        try:
            spec = parse_spec({"name": "probe", "annotate": [run_analyzer_step_dict(args)]})
        except ValidationError as exc:
            return _dumps({"validation_errors": validation_errors(exc)})
        df = self._df()
        row_keys, identity, _warn = resolve_row_identity(df, spec.identity)
        scratch = self._scratch_workspace()
        ann = annotate_pandas.run_annotate_steps(
            df, spec, row_keys=row_keys, workspace=scratch, runner=self.workspace_runner
        )
        outputs: Dict[str, Any] = {}
        annotated_mask = ann.frame.notna().any(axis=1)
        for col in ann.frame.columns:
            series = ann.frame[col].dropna()
            stat: Dict[str, Any] = {"non_null": int(len(series))}
            if len(series) and pd.api.types.is_numeric_dtype(series):
                stat.update(
                    min=to_json_safe(series.min()),
                    max=to_json_safe(series.max()),
                    mean=to_json_safe(round(float(series.mean()), 4)),
                    distinct=int(series.nunique()),
                )
            outputs[str(col)] = stat
        sample = ann.frame[annotated_mask].head(5).reset_index(names=ROW_KEY_COLUMN)
        return _dumps(
            {
                "statuses": ann.statuses,
                "identity": identity,
                "rows_total": len(df),
                "rows_annotated": int(annotated_mask.sum()),
                "outputs": outputs,
                "sample": [
                    {str(k): to_json_safe(v) for k, v in rec.items()}
                    for rec in sample.to_dict(orient="records")
                ],
                "degraded": ann.degraded,
            }
        )

    def _tool_preview_filter(self, args: Dict[str, Any]) -> str:
        from fastapi import HTTPException  # noqa: PLC0415

        from filters.routes import apply_spec  # noqa: PLC0415 — avoid cycle at import

        try:
            spec = parse_spec(args.get("spec") or {})
        except ValidationError as exc:
            return _dumps({"validation_errors": validation_errors(exc)})
        source, params, _ref = self.resolve_source()
        try:
            page = apply_spec(
                self.state,
                self.session_id,
                self.user_id,
                spec,
                source=source,
                path=params.get("path"),
                file_id=params.get("file_id"),
                source_id=params.get("source_id"),
                page=1,
                page_size=10,
                matches_only=True,
            )
        except HTTPException as exc:
            return _dumps({"error": exc.detail})
        names = [c["name"] for c in page["columns"]]
        sample = [{names[j]: v for j, v in enumerate(row) if j < 8} for row in page["rows"][:10]]
        result: Dict[str, Any] = {"summary": page["summary"], "matching_sample": sample}
        orphans = page["summary"].get("group_orphan_rows")
        if spec.present.group_by and orphans:
            result["group_sanity_warning"] = (
                f"{orphans} row(s) outside this filter's matches carry a non-null "
                f"{spec.present.group_by!r} value. A group_by column must be null/NaN for rows "
                "that are not in a group — a sentinel like -1 or 0 will scramble group_by banding "
                "(pairs won't render adjacent even though matches_only counts look correct). Fix "
                "the annotate step so non-members are null, then preview_filter again."
            )
        return _dumps(result)

    # ------------------------------------------------------------- persist

    def _require_user(self) -> Optional[str]:
        if not self.user_id:
            return "Cannot persist filters: session has no owning user."
        return None

    async def _emit_filter_event(
        self, filter_id: str, name: str, action: str, dataset_ref: str
    ) -> None:
        if self.progress_emitter is None:
            return
        await self.progress_emitter.emit(
            "filter",
            f"{'Saved' if action == 'created' else 'Updated'} filter “{name}”",
            event_type=SseEventType.FILTER.value,
            filter_id=filter_id,
            action=action,
            session_id=self.session_id,
            dataset_ref=dataset_ref,
        )

    async def _save_filter(self, args: Dict[str, Any]) -> str:
        denied = self._require_user()
        if denied:
            return _dumps({"error": denied})
        try:
            spec = parse_spec(args.get("spec") or {})
        except ValidationError as exc:
            return _dumps({"validation_errors": validation_errors(exc)})
        _source, _params, dataset_ref = self.resolve_source()
        db = SessionLocal()
        try:
            row = filter_store.create_filter(
                db,
                session_id=self.session_id,
                app_user_id=self.user_id,
                spec=spec.model_dump(mode="json"),
                dataset_ref=dataset_ref,
                name=args.get("name"),
                author="agent",
            )
            db.commit()
            result = {"filter_id": row.id, "name": row.name, "version": row.version}
        finally:
            db.close()
        self.saved.append({**result, "action": "created"})
        await self._emit_filter_event(
            result["filter_id"], result["name"], "created", row.dataset_ref
        )
        return _dumps({"saved": result})

    async def _update_filter(self, args: Dict[str, Any]) -> str:
        denied = self._require_user()
        if denied:
            return _dumps({"error": denied})
        try:
            spec = parse_spec(args.get("spec") or {})
        except ValidationError as exc:
            return _dumps({"validation_errors": validation_errors(exc)})
        db = SessionLocal()
        try:
            try:
                row = filter_store.update_filter(
                    db,
                    str(args.get("filter_id") or ""),
                    self.user_id,
                    spec=spec.model_dump(mode="json"),
                    name=args.get("name"),
                    author="agent",
                )
            except PermissionError:
                return _dumps({"error": "That filter belongs to a different user."})
            if row is None:
                return _dumps({"error": "Filter not found — use list_filters."})
            db.commit()
            result = {"filter_id": row.id, "name": row.name, "version": row.version}
        finally:
            db.close()
        self.saved.append({**result, "action": "updated"})
        await self._emit_filter_event(
            result["filter_id"], result["name"], "updated", row.dataset_ref
        )
        return _dumps({"saved": result})

    def _tool_list_filters(self, _args: Dict[str, Any]) -> str:
        if not self.user_id:
            return _dumps({"filters": []})
        db = SessionLocal()
        try:
            rows = filter_store.list_filters(db, self.session_id, self.user_id)
            return _dumps(
                {
                    "filters": [
                        {
                            "id": r.id,
                            "name": r.name,
                            "dataset_ref": r.dataset_ref,
                            "version": r.version,
                        }
                        for r in rows
                    ]
                }
            )
        finally:
            db.close()
