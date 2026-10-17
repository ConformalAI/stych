"""
Unit tests for the SQL-side diff (base table vs stych_temp working copy):
predicate/SQL generation, change-kind narrowing, page cell-meta shaping, the
degraded no-key path, and spec caching. No database required.
"""

from types import SimpleNamespace

from api import sql_diff_service as sds
from api.sql_diff_service import (
    _DiffSpec,
    _DisplayCol,
    build_filtered_count_sql,
    build_join_sql,
    build_mode_where,
    build_page_sql,
    build_summary_sql,
    changed_predicate,
)


def _spec():
    return _DiffSpec(
        key="id",
        join_on='b."id" = t."id"',
        display=[
            _DisplayCol("id", "integer", "number", "id", "integer", "integer", 0),
            _DisplayCol(
                "amt",
                "double precision",
                "number",
                "amt",
                "double precision",
                "double precision",
                1,
            ),
            _DisplayCol("city", "text", "string", "town", "text", "text", 2),  # renamed
            _DisplayCol("note", "text", "string", None),  # added column
        ],
        col_class={
            "amt": {"kind": "transformed", "label": "standardized", "imputed_label": None},
            "city": {"kind": "changed", "label": None, "imputed_label": "filled"},
        },
    )


# ----------------------------------------------------------------- predicates


def test_changed_predicate_exact_float_and_recast():
    assert changed_predicate('b."a"', 't."a"', "integer", "integer") == (
        '(b."a" IS DISTINCT FROM t."a")'
    )
    tol = changed_predicate('b."a"', 't."a"', "double precision", "double precision")
    assert "IS DISTINCT FROM" in tol and "GREATEST(abs(" in tol  # np.isclose parity
    recast = changed_predicate('b."a"', 't."a"', "text", "integer")
    assert recast == '(CAST(b."a" AS TEXT) IS DISTINCT FROM CAST(t."a" AS TEXT))'


# ------------------------------------------------------------------ join sql


def test_join_sql_display_flags_and_renamed_base_col():
    sql = build_join_sql(_spec(), '"public"."o"', '"stych_temp"."w"')
    assert 'FULL OUTER JOIN "stych_temp"."w" t ON b."id" = t."id"' in sql
    assert '(b."id" IS NULL) AS _added' in sql and '(t."id" IS NULL) AS _ghost' in sql
    # Renamed column compares base "town" against target "city".
    assert 'b."town"' in sql and "AS _old2" in sql
    # Added column has no chg/old aliases and displays only the target side.
    assert "_chg3" not in sql and 't."note" AS d3' in sql
    # Ghost rows show base values for shared columns.
    assert 'CASE WHEN t."id" IS NULL THEN b."amt" ELSE t."amt" END AS d1' in sql


def test_summary_sql_aggregates():
    sql = build_summary_sql(_spec(), "B", "T")
    assert 'COUNT(*) FILTER (WHERE b."id" IS NULL) AS rows_added' in sql
    assert 'COUNT(*) FILTER (WHERE t."id" IS NULL) AS rows_removed' in sql
    assert "AS rows_changed" in sql and "AS rows_unchanged" in sql
    assert "AS c0" in sql and "AS c2" in sql and "AS c3" not in sql


# --------------------------------------------------------------- mode where


def test_mode_where_change_kinds():
    spec = _spec()
    where, _ = build_mode_where(spec, changes_only=True)
    assert "q._added OR q._ghost OR q._chg0 OR q._chg1 OR q._chg2" in where

    where, _ = build_mode_where(spec, change_kind="added")
    assert where == " WHERE q._added"
    where, _ = build_mode_where(spec, change_kind="removed")
    assert where == " WHERE q._ghost"
    where, _ = build_mode_where(spec, change_kind="imputed")
    assert "(q._chg0 AND q._old0 IS NULL)" in where and "q._old2 IS NULL" in where
    # "transformed" binds only to columns classified transformed (amt).
    where, _ = build_mode_where(spec, change_kind="transformed")
    assert "q._chg1 AND NOT q._old1 IS NULL" in where and "_chg2" not in where
    # "changed" binds to plain-changed columns (city), not transformed ones.
    where, _ = build_mode_where(spec, change_kind="changed")
    assert "_chg2" in where and "_chg1" not in where


def test_mode_where_filters_and_search_over_display_aliases():
    where, params = build_mode_where(_spec(), filters={"note": "50%", "ghost": "x"}, search="q")
    assert "CAST(q.d3 AS TEXT) ILIKE :f0" in where  # note -> d3; unknown col dropped
    assert params["f0"] == "%50\\%%"
    assert "CAST(q.d0 AS TEXT) ILIKE :srch" in where and params["srch"] == "%q%"


def test_page_sql_ordering_and_count_sql():
    sql, params = build_page_sql(
        _spec(), "B", "T", offset=100, limit=50, sort_by="amt", sort_dir="desc"
    )
    assert "ORDER BY q._ghost ASC, q.d1 DESC NULLS LAST, q._ord" in sql
    assert params == {"limit": 50, "offset": 100}
    count_sql, _ = build_filtered_count_sql(_spec(), "B", "T", changes_only=True)
    assert count_sql.startswith("SELECT COUNT(*) FROM (")


def test_sqlserver_diff_sql_uses_tsql_predicates_and_pagination():
    token = sds._diff_dialect.set("sqlserver")
    try:
        spec = _spec()
        spec.join_on = "b.[id] = t.[id]"
        predicate = changed_predicate("b.[amt]", "t.[amt]", "float", "float")
        join_sql = build_join_sql(spec, "[dbo].[orders]", "[stych_temp].[working]")
        page_sql, _ = build_page_sql(
            spec,
            "[dbo].[orders]",
            "[stych_temp].[working]",
            offset=10,
            limit=20,
            sort_by="amt",
            sort_dir="desc",
        )
    finally:
        sds._diff_dialect.reset(token)

    assert "IS DISTINCT FROM" not in predicate
    assert "GREATEST" not in predicate
    assert "CASE WHEN" in join_sql and "AS _added" in join_sql
    assert "NULLS LAST" not in page_sql and " LIMIT " not in page_sql
    assert "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY" in page_sql
    assert "CASE WHEN q.d1 IS NULL THEN 1 ELSE 0 END" in page_sql


def test_pull_sample_normalizes_uppercase_warehouse_aliases():
    """Snowflake returns unquoted ``AS b__0`` aliases as ``B__0``."""

    class _SampleAdapter:
        source_ref = '"PUBLIC"."CAFE"'

        def __init__(self, columns):
            self._columns = [SimpleNamespace(name=name) for name in columns]

        def schema(self):
            return self._columns

        def run(self, *_args, **_kwargs):
            return (
                ["B__0", "B__1", "T__0", "T__1"],
                [("k1", "10", "k1", "11")],
            )

    base = _SampleAdapter(["id", "value"])
    target = _SampleAdapter(["id", "value"])
    base_sample, target_sample = sds._pull_sample(base, target, "id", 'b."id" = t."id"')

    assert base_sample.to_dict("records") == [{"id": "k1", "value": "10"}]
    assert target_sample.to_dict("records") == [{"id": "k1", "value": "11"}]


def test_page_result_index_normalizes_uppercase_warehouse_aliases(monkeypatch):
    """Snowflake returns the diff page aliases as D0/_ADDED/etc."""

    spec = _spec()
    spec.summary = {
        "rows_added": 0,
        "rows_removed": 0,
        "rows_changed": 0,
        "rows_unchanged": 1,
    }

    class _PageAdapter:
        source_ref = '"PUBLIC"."CAFE"'
        cache_info = SimpleNamespace(key=("fake",), ttl=60)

        def run(self, *_args, **_kwargs):
            return (
                [
                    "D0",
                    "D1",
                    "D2",
                    "D3",
                    "_ADDED",
                    "_GHOST",
                    "_CHG0",
                    "_OLD0",
                    "_CHG1",
                    "_OLD1",
                    "_CHG2",
                    "_OLD2",
                    "_ORD",
                ],
                [(1, 2.0, "x", "n", False, False, False, None, False, None, False, None, "1")],
            )

    monkeypatch.setattr(sds, "get_cached_spec", lambda *_args: spec)
    page = sds._build_sql_diff_page_impl(
        _PageAdapter(),
        _PageAdapter(),
        session_id="snowflake",
    )

    assert page["rows"] == [[1, 2.0, "x", "n"]]
    assert page["cell_meta"] == [[None, None, None, {"k": "added"}]]


# ------------------------------------------------------------------ row meta


def _index_for(spec):
    keys = [f"d{j}" for j in range(len(spec.display))]
    keys += ["_added", "_ghost", "_chg0", "_old0", "_chg1", "_old1", "_chg2", "_old2", "_ord"]
    return {k: i for i, k in enumerate(keys)}


def _raw(d, added=False, ghost=False, chg=(False, False, False), old=(None, None, None)):
    return tuple(d + [added, ghost, chg[0], old[0], chg[1], old[1], chg[2], old[2], "k"])


def test_row_meta_added_and_ghost_rows():
    spec = _spec()
    idx = _index_for(spec)
    added = sds._row_meta(spec, _raw([1, 2.0, "x", "n"], added=True), idx)
    assert all(m == {"k": "added"} for m in added)
    ghost = sds._row_meta(spec, _raw([1, 2.0, "x", "n"], ghost=True), idx)
    # Removed cells carry no "o" — frontend contract for whole-row removal.
    assert all(m == {"k": "removed"} for m in ghost)


def test_row_meta_changed_imputed_and_added_column_cells():
    spec = _spec()
    idx = _index_for(spec)
    raw = _raw(
        [1, 0.5, "Paris", "note"],
        chg=(False, True, True),
        old=(None, 2.5, None),  # amt changed value->value; city was NULL
    )
    meta = sds._row_meta(spec, raw, idx)
    assert meta[0] is None
    assert meta[1] == {"k": "transformed", "l": "standardized", "o": 2.5}
    assert meta[2] == {"k": "imputed", "l": "filled", "o": None}
    assert meta[3] == {"k": "added"}  # wholly-new column tints every cell


def test_row_meta_missing_token_base_is_imputed():
    """A base cell stored as an empty string / literal missing-token (common in
    Postgres/Excel sources) is treated as missing -> the fill is an imputation,
    not a value->value 'changed'/'remapped'."""
    spec = _spec()
    idx = _index_for(spec)
    for token in ("", "UNKNOWN", " NaN ", "n/a"):
        raw = _raw(
            [1, 0.5, "Paris", "note"],
            chg=(False, False, True),
            old=(None, None, token),  # city base was a missing-token, filled
        )
        meta = sds._row_meta(spec, raw, idx)
        assert meta[2] == {"k": "imputed", "l": "filled", "o": None}, token


def test_missing_sql_text_vs_numeric():
    # Numeric columns can only be missing via NULL.
    assert sds.missing_sql("q._old0", "integer") == "q._old0 IS NULL"
    # Text columns also match literal missing-tokens (case/space-insensitive).
    text_pred = sds.missing_sql("q._old2", "text")
    assert "q._old2 IS NULL OR btrim(lower(CAST(q._old2 AS TEXT))) IN (" in text_pred
    assert "'unknown'" in text_pred and "''" in text_pred


# ------------------------------------------------------------ degraded / cache


class _FakeAdapter:
    """Just enough surface for _degraded_spec/_degraded_page."""

    def __init__(self, cols, total):
        self._cols = [SimpleNamespace(name=n, db_type=t, kind=k) for n, t, k in cols]
        self._total = total
        self.cache_info = SimpleNamespace(key=("fake", total), ttl=60)

    def schema(self):
        return self._cols

    def count(self, filters=None, search=None):
        return SimpleNamespace(value=self._total, approx=False)

    def fetch_page(self, **kwargs):
        self.last_page_args = kwargs
        return [[1, "a"]]


def test_degraded_spec_and_page():
    base = _FakeAdapter([("a", "integer", "number"), ("b", "text", "string")], 100)
    temp = _FakeAdapter([("a", "integer", "number"), ("c", "text", "string")], 120)
    spec = sds._degraded_spec(base, temp, temp.schema(), ["a"], ["c"], ["b"])
    s = spec.summary
    assert s["alignment"] == "none" and "No unique key" in s["warning"]
    assert s["rows_added"] == 20 and s["rows_removed"] == 0 and s["cells_changed"] == 0
    assert s["columns_added"] == ["c"] and s["columns_removed"] == ["b"]

    page = sds._degraded_page(
        spec, temp, page=1, page_size=50, sort_by="a", sort_dir="asc", filters=None, search=None
    )
    assert page["kind"] == "diff" and page["total_rows"] == 120
    assert page["cell_meta"] == [[None, None]]
    assert temp.last_page_args["sort_by"] == "a"  # pushdown still applies


def test_spec_cache_keyed_and_bounded(monkeypatch):
    sds.clear_spec_cache()
    calls = []
    monkeypatch.setattr(sds, "_build_spec", lambda b, t: calls.append(1) or _spec())
    b = _FakeAdapter([("a", "integer", "number")], 1)
    t = _FakeAdapter([("a", "integer", "number")], 1)
    sds.get_cached_spec(("s1", "k", 0), b, t)
    sds.get_cached_spec(("s1", "k", 0), b, t)
    assert len(calls) == 1
    # A bumped generation is a different key -> rebuild.
    sds.get_cached_spec(("s1", "k", 1), b, t)
    assert len(calls) == 2
    sds.clear_spec_cache()
