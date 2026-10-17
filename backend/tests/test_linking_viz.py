"""Structural tests for linking Vega-Lite / Vega spec generators."""

from unittest.mock import patch

import pytest

from agents.linking.inspect import public_context
from agents.linking.viz import (
    KIND_BIPARTITE,
    KIND_COORDINATED,
    KIND_SCORE_MATRIX,
    VIZ_KINDS,
    VizError,
    bipartite_spec,
    build_viz,
    coordinated_tables_spec,
    normalize_candidates,
    score_matrix_spec,
)


def _sample_candidates():
    return [
        {
            "source_row_id": 0,
            "candidate_row_id": 10,
            "score": 0.95,
            "strategy": "token_set",
            "source_value": "Apple",
            "candidate_value": "Apple Inc",
        },
        {
            "source_row_id": 0,
            "candidate_row_id": 11,
            "score": 0.88,
            "strategy": "token_set",
            "source_value": "Apple",
            "candidate_value": "Apple Corp",
        },
        {
            "source_row_id": 0,
            "candidate_row_id": 12,
            "score": 0.40,
            "strategy": "token_set",
            "source_value": "Apple",
            "candidate_value": "Alphabet",
        },
        {
            "source_row_id": 1,
            "candidate_row_id": 13,
            "score": 0.91,
            "strategy": "token_set",
            "source_value": "Google",
            "candidate_value": "Alphabet Inc",
        },
        {
            "source_row_id": 0,
            "candidate_row_id": 10,
            "score": 0.70,
            "strategy": "semantic",
            "source_value": "Apple",
            "candidate_value": "Apple Inc",
        },
        {
            "source_row_id": 0,
            "candidate_row_id": 10,
            "score": 0.60,
            "strategy": "token_set",
            "source_value": "Apple",
            "candidate_value": "Apple Inc",
        },
    ]


def test_normalize_filters_family_threshold_and_dedupes_max_score():
    rows = normalize_candidates(_sample_candidates(), strategy="lexical", threshold=0.5)
    keys = {(r["source_row_id"], r["candidate_row_id"]) for r in rows}
    assert (0, 10) in keys
    apple_inc = next(r for r in rows if r["source_row_id"] == 0 and r["candidate_row_id"] == 10)
    assert apple_inc["score"] == 0.95
    assert apple_inc["strategy"] == "token_set"
    assert all(r["score"] >= 0.5 for r in rows)
    assert all(r["strategy"] != "semantic" for r in rows)
    assert (0, 12) not in keys
    assert all("source_label" in r and "candidate_label" in r for r in rows)


def test_normalize_semantic_family():
    rows = normalize_candidates(_sample_candidates(), strategy="semantic")
    assert len(rows) == 1
    assert rows[0]["strategy"] == "semantic"
    assert rows[0]["score"] == 0.7


def test_score_matrix_is_binned_rect_heatmap():
    spec = score_matrix_spec(_sample_candidates(), strategy="lexical", threshold=0.5)
    assert spec["$schema"].endswith("vega-lite/v5.json")
    assert spec["mark"]["type"] == "rect"
    enc = spec["encoding"]
    assert enc["x"]["field"] == "candidate_label"
    assert enc["y"]["field"] == "source_label"
    assert enc["color"]["field"] == "score"
    assert enc["color"]["type"] == "quantitative"
    values = spec["data"]["values"]
    assert values
    assert all("score" in row for row in values)


def test_coordinated_concat_shares_source_row_id_selection():
    spec = coordinated_tables_spec(_sample_candidates(), strategy="lexical", threshold=0.5)
    assert spec["$schema"].endswith("vega-lite/v5.json")
    assert "hconcat" in spec
    assert len(spec["hconcat"]) == 2
    assert "sources" in spec["datasets"]
    assert "edges" in spec["datasets"]

    params = list(spec.get("params") or [])
    for view in spec["hconcat"]:
        params.extend(view.get("params") or [])
    assert len(params) == 1
    assert params[0]["name"] == "picked_source"
    assert params[0]["select"]["fields"] == ["source_row_id"]
    assert params[0].get("views") == ["sources_view", "scores_view"]

    source_view, score_view = spec["hconcat"]
    assert source_view["data"]["name"] == "sources"
    assert score_view["data"]["name"] == "edges"
    assert score_view["mark"]["type"] == "bar"
    assert "picked_source" in str(score_view["encoding"]["opacity"])


def test_bipartite_is_vega_with_weighted_rule_marks():
    spec = bipartite_spec(_sample_candidates(), strategy="lexical", threshold=0.5)
    assert spec["$schema"].endswith("vega/v5.json")
    assert any(m.get("type") == "rule" for m in spec["marks"])
    names = {m.get("name") for m in spec["marks"]}
    assert "linkMarks" in names
    assert "leftNodes" in names
    assert "rightNodes" in names
    datasets = {d["name"] for d in spec["data"]}
    assert {"edges", "left", "right", "links"} <= datasets
    scale_names = {s["name"] for s in spec["scales"]}
    assert "edgeWidth" in scale_names
    assert "edgeOpacity" in scale_names
    rule = next(m for m in spec["marks"] if m["type"] == "rule")
    assert rule["encode"]["update"]["strokeWidth"]["field"] == "score"


def test_empty_input_still_returns_valid_specs():
    for kind, builder, schema_tail in (
        (KIND_SCORE_MATRIX, score_matrix_spec, "vega-lite/v5.json"),
        (KIND_COORDINATED, coordinated_tables_spec, "vega-lite/v5.json"),
        (KIND_BIPARTITE, bipartite_spec, "vega/v5.json"),
    ):
        spec = builder([])
        assert spec["$schema"].endswith(schema_tail)
        payload = build_viz(kind, [])
        assert payload["kind"] == kind
        assert payload["n_edges"] == 0
        assert payload["spec"]["$schema"] == spec["$schema"]


def test_build_viz_dispatches_library_and_rejects_unknown_kind():
    heat = build_viz(KIND_SCORE_MATRIX, _sample_candidates(), strategy="lexical")
    assert heat["library"] == "vega-lite"
    coord = build_viz(KIND_COORDINATED, _sample_candidates())
    assert coord["library"] == "vega-lite"
    bi = build_viz(KIND_BIPARTITE, _sample_candidates())
    assert bi["library"] == "vega"
    with pytest.raises(VizError):
        build_viz("sankey", _sample_candidates())


def test_colliding_labels_are_disambiguated():
    rows = normalize_candidates(
        [
            {
                "source_row_id": 0,
                "candidate_row_id": 1,
                "score": 0.9,
                "strategy": "token_set",
                "source_value": "Acme",
                "candidate_value": "Acme",
            },
            {
                "source_row_id": 2,
                "candidate_row_id": 3,
                "score": 0.8,
                "strategy": "token_set",
                "source_value": "Acme",
                "candidate_value": "Acme",
            },
        ]
    )
    source_labels = {r["source_label"] for r in rows}
    assert len(source_labels) == 2
    assert any(label.startswith("0:") or label.startswith("2:") for label in source_labels)


def test_heatmap_caps_large_matrices():
    bulky = []
    for i in range(50):
        for j in range(50):
            bulky.append(
                {
                    "source_row_id": i,
                    "candidate_row_id": j,
                    "score": (i + j) / 200,
                    "strategy": "token_set",
                    "source_value": f"s{i}",
                    "candidate_value": f"c{j}",
                }
            )
    spec = score_matrix_spec(bulky, threshold=0.0)
    n_src = len({r["source_row_id"] for r in spec["data"]["values"]})
    n_cand = len({r["candidate_row_id"] for r in spec["data"]["values"]})
    assert n_src <= 40
    assert n_cand <= 40


def test_public_context_advertises_viz_kinds():
    ctx = public_context(
        {
            "candidates": [],
            "committed": False,
            "context": {
                "left": {"name": "a"},
                "right": {"name": "b"},
                "plan": {},
            },
        }
    )
    assert ctx is not None
    assert ctx["viz"]["kinds"] == list(VIZ_KINDS)


def test_dark_theme_uses_different_axis_colors():
    light = score_matrix_spec([], dark=False)
    dark = score_matrix_spec([], dark=True)
    assert light["config"]["axis"]["labelColor"] != dark["config"]["axis"]["labelColor"]


def test_bipartite_cap_boundary():
    at_limit = [
        {
            "source_row_id": i,
            "candidate_row_id": i + 100,
            "score": 0.9,
            "strategy": "token_set",
            "source_value": f"s{i}",
            "candidate_value": f"c{i}",
        }
        for i in range(80)
    ]
    spec = bipartite_spec(at_limit)
    assert len(spec["data"][0]["values"]) == 80
    over_limit = at_limit + [
        {
            "source_row_id": 99,
            "candidate_row_id": 199,
            "score": 0.5,
            "strategy": "token_set",
            "source_value": "s99",
            "candidate_value": "c99",
        }
    ]
    spec2 = bipartite_spec(over_limit)
    assert len(spec2["data"][0]["values"]) == 80
    assert "top edges" in spec2["description"]


def test_build_viz_normalizes_only_once():
    with patch("agents.linking.viz.normalize_candidates", wraps=normalize_candidates) as mock_norm:
        build_viz(KIND_SCORE_MATRIX, _sample_candidates())
        assert mock_norm.call_count == 1
