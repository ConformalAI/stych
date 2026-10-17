"""Unit tests for the unified source catalog (workspace/sources.py)."""

from __future__ import annotations

import pandas as pd

from workspace.sources import list_sources, render_sources_block


class _Ctx:
    def __init__(self, df, filename, metadata):
        self.working_dataframe = df
        self.filename = filename
        self.metadata = metadata

    def is_sql_datasource(self):
        return bool(self.metadata.get("connection_url"))


class _State:
    def __init__(self, metadata, data_context=None, session_id="s1"):
        self.metadata = metadata
        self.data_context = data_context
        self.session_id = session_id


class _StateWithCtxs(_State):
    def __init__(self, metadata, contexts, session_id="s1"):
        super().__init__(
            metadata, data_context=contexts[0] if contexts else None, session_id=session_id
        )
        self._contexts = contexts

    def get_all_data_contexts(self):
        return list(self._contexts)


def _seed_files(root):
    df = pd.DataFrame({"a": range(5), "b": list("vwxyz")})
    # Active dataset as parquet + leftover csv of the same identity + the raw
    # upload name — parquet+csv must collapse to one source.
    df.to_parquet(root / "titanic.parquet", index=False)
    df.to_csv(root / "titanic.csv", index=False)
    df.to_csv(root / "titanic upload.csv", index=False)  # dataset_stem -> titanic_upload
    # A genuinely separate agent output.
    df.assign(c=1).to_csv(root / "titanic_clean.csv", index=False)
    (root / "report.txt").write_text("not a source")


def test_mirror_copies_collapse_and_prefer_parquet(tmp_path):
    _seed_files(tmp_path)
    state = _State({"active_dataset": "titanic"})
    sources = list_sources(state, tmp_path)

    files = {s["id"]: s for s in sources if s["kind"] == "file"}
    # titanic.parquet + titanic.csv collapse to one entry, keyed on the parquet.
    assert "titanic.parquet" in files
    assert "titanic.csv" not in files
    # Separate outputs remain their own sources; report.txt is not a source.
    assert "titanic_clean.csv" in files
    assert "titanic upload.csv" in files  # distinct identity (titanic_upload)
    assert not any(s["name"] == "report.txt" for s in sources)


def test_active_flag_and_parquet_rowcount(tmp_path):
    _seed_files(tmp_path)
    state = _State({"active_dataset": "titanic"})
    sources = list_sources(state, tmp_path)

    active = [s for s in sources if s["active"]]
    assert len(active) == 1 and active[0]["id"] == "titanic.parquet"
    assert active[0]["rows"] == 5  # from parquet metadata, no full load
    assert active[0]["columns"] == 2
    assert active[0]["column_names"] == ["a", "b"]


def test_sql_source_listed_as_virtual(tmp_path):
    df = pd.DataFrame({"x": [1, 2], "y": [3, 4]})
    ctx = _Ctx(
        df,
        filename="orders",
        metadata={"connection_url": "postgres://…", "temp_table": "t_orders"},
    )
    state = _State({}, data_context=ctx)
    sources = list_sources(state, tmp_path)

    sql = [s for s in sources if s["kind"] == "sql"]
    assert len(sql) == 1
    assert sql[0]["id"] == "sql:t_orders"
    assert sql[0]["column_names"] == ["x", "y"]
    assert sql[0]["rows"] is None  # full table lives in the DB


def test_in_memory_additional_dataset_is_listed(tmp_path):
    # Primary 'titanic' is seeded to disk; an additional upload 'sales' is only
    # in memory (never seeded) and must still appear so linking can see it.
    _seed_files(tmp_path)
    primary = _Ctx(pd.DataFrame({"a": [1]}), "titanic.csv", {})
    extra = _Ctx(pd.DataFrame({"amount": [10], "region": ["x"]}), "sales.csv", {})
    state = _StateWithCtxs({"active_dataset": "titanic"}, [primary, extra])
    sources = list_sources(state, tmp_path)

    # Primary is represented once, by its on-disk file (not double-listed).
    titanic = [s for s in sources if dataset_stem_matches(s, "titanic")]
    assert len(titanic) == 1 and titanic[0]["kind"] == "file"
    # The additional in-memory dataset shows as a dataset source with its columns.
    sales = [s for s in sources if s["kind"] == "dataset" and s["name"] == "sales.csv"]
    assert len(sales) == 1
    assert sales[0]["id"] == "dataset:sales.csv"
    assert sales[0]["column_names"] == ["amount", "region"]


def test_sql_primary_not_double_listed_as_dataset(tmp_path):
    ctx = _Ctx(
        pd.DataFrame({"x": [1]}),
        "orders",
        {"connection_url": "postgres://…", "temp_table": "t_orders"},
    )
    state = _StateWithCtxs({}, [ctx])
    sources = list_sources(state, tmp_path)
    assert [s["kind"] for s in sources] == ["sql"]  # exactly one, as sql not dataset


def dataset_stem_matches(source, stem):
    from workspace.manager import dataset_stem

    return dataset_stem(source["name"]) == stem


def test_render_block_is_compact(tmp_path):
    _seed_files(tmp_path)
    state = _State({"active_dataset": "titanic"})
    block = render_sources_block(list_sources(state, tmp_path))
    assert "Workspace sources" in block
    assert "`titanic.parquet`" in block and "[active]" in block
    assert render_sources_block([]) == ""
