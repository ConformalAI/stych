"""VizSpec chart-color contract tests."""

import pytest
from pydantic import ValidationError

from viz.spec import VizSpec


def test_palette_round_trips_in_wire_format():
    spec = VizSpec.model_validate({"config": {"palette": ["#2563EB", "#0FB981"]}})

    assert spec.to_json_dict()["config"]["palette"] == ["#2563EB", "#0FB981"]


def test_explicit_color_modes_and_assignments_round_trip():
    spec = VizSpec.model_validate(
        {
            "config": {
                "color": {
                    "mode": "categorical",
                    "value": "#111827",
                    "binding": {"channel": "x", "field": "region"},
                    "palette": ["#2563EB", "#0FB981"],
                    "assignments": [{"value": "East", "color": "#DC2626"}],
                },
                "labelFontSize": 14,
                "labelColor": "#F9FAFB",
            }
        }
    )

    wire = spec.to_json_dict()["config"]
    assert wire["color"]["assignments"] == [{"value": "East", "color": "#DC2626"}]
    assert wire["color"]["value"] == "#111827"
    assert wire["labelFontSize"] == 14
    assert wire["labelColor"] == "#F9FAFB"


@pytest.mark.parametrize("label_size", [7, 33])
def test_label_font_size_is_bounded(label_size):
    with pytest.raises(ValidationError):
        VizSpec.model_validate({"config": {"labelFontSize": label_size}})


def test_label_color_requires_hex():
    with pytest.raises(ValidationError):
        VizSpec.model_validate({"config": {"labelColor": "white"}})


def test_color_mode_requires_matching_shape():
    with pytest.raises(ValidationError):
        VizSpec.model_validate({"config": {"color": {"mode": "single"}}})
    with pytest.raises(ValidationError):
        VizSpec.model_validate(
            {
                "config": {
                    "color": {
                        "mode": "categorical",
                    }
                }
            }
        )
    with pytest.raises(ValidationError):
        VizSpec.model_validate(
            {
                "config": {
                    "color": {
                        "mode": "continuous",
                        "binding": {"channel": "color", "field": "revenue"},
                        "palette": ["#2563EB"],
                    }
                }
            }
        )


def test_texture_round_trips_and_defaults_off():
    default = VizSpec.model_validate({})
    textured = VizSpec.model_validate(
        {"config": {"texture": {"enabled": True, "pattern": "crosshatch"}}}
    )

    assert default.config.texture.enabled is False
    assert textured.to_json_dict()["config"]["texture"] == {
        "enabled": True,
        "pattern": "crosshatch",
    }


def test_unknown_texture_rejected():
    with pytest.raises(ValidationError):
        VizSpec.model_validate(
            {"config": {"texture": {"enabled": True, "pattern": "checkerboard"}}}
        )


@pytest.mark.parametrize(
    "palette",
    [
        [],
        ["blue"],
        ["#12345"],
        ["#12345G"],
        ["#000000"] * 13,
    ],
)
def test_palette_rejects_invalid_values(palette):
    with pytest.raises(ValidationError):
        VizSpec.model_validate({"config": {"palette": palette}})
