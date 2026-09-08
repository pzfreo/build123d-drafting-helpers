"""Through wording changes native ink without changing other callout content."""

import pytest
from build123d import Align, ExportDXF, ExportSVG, Mode, Text

from build123d_drafting import HoleCallout, draft_preset, helpers


def _text(value, draft):
    return Text(
        value,
        font_size=draft.font_size,
        font=draft.font,
        font_path=helpers._font_path(draft),
        align=(Align.MIN, Align.CENTER),
        mode=Mode.PRIVATE,
    )


@pytest.mark.parametrize("indicator", ["THRU", "", "THROUGH ALL"])
def test_selected_indicator_changes_real_ink_and_bounds(indicator):
    draft = draft_preset()
    options = dict(count=4, through=True, cbore_dia="14 ±0.2", cbore_depth="3 ±0.1", draft=draft)
    baseline = HoleCallout("8 H7", through_indicator="", **options)
    selected = HoleCallout("8 H7", through_indicator=indicator, **options)
    expected_text = _text(indicator, draft) if indicator else None
    extra_width = expected_text.bounding_box().size.X + 0.45 * draft.font_size if indicator else 0
    extra_area = expected_text.area if indicator else 0
    assert selected.area - baseline.area == pytest.approx(extra_area, abs=1e-6)
    assert selected.bounding_box().size.X - baseline.bounding_box().size.X == pytest.approx(
        extra_width
    )
    assert selected.callout_width == pytest.approx(selected.bounding_box().max.X)
    assert selected.covers_diameters == (8, 14)
    assert selected.covers_count == 4


def test_default_matches_explicit_thru_geometry():
    default = HoleCallout("8 ±0.01", through=True)
    explicit = HoleCallout("8 ±0.01", through=True, through_indicator="THRU")
    assert default.area == pytest.approx(explicit.area)
    assert default.bounding_box().size.X == pytest.approx(explicit.bounding_box().size.X)
    assert default.callout_width == explicit.callout_width


@pytest.mark.parametrize("through", [True, False])
@pytest.mark.parametrize("indicator", ["THRU", "", "THROUGH ALL"])
def test_native_text_retains_complete_callout_and_blind_depth(monkeypatch, through, indicator):
    rendered = []
    real_text = helpers.Text

    def capture(*args, **kwargs):
        value = kwargs.get("txt", args[0] if args else None)
        result = real_text(*args, **kwargs)
        assert result.area > 0  # Observing real filled glyphs, not mocking rendering.
        rendered.append(value)
        return result

    monkeypatch.setattr(helpers, "Text", capture)
    callout = HoleCallout(
        "8 ±0.01 H7",
        count=3,
        through=through,
        through_indicator=indicator,
        depth="10 ±0.05",
        cbore_dia="14 ±0.2",
        cbore_depth="3 ±0.1",
        csink_dia="16 ±0.3",
        csink_angle="90 ±0.5",
        suffix="M8x1",
    )
    expected = ["3×", "8 ±0.01 H7"]
    if through:
        if indicator:
            expected.append(indicator)
    else:
        expected.append("10 ±0.05")
    expected += ["14 ±0.2", "3 ±0.1", "16 ±0.3", "× 90 ±0.5°", "M8x1"]
    assert rendered == expected
    assert callout.covers_count == 3
    assert callout.covers_diameters == (8, 14, 16)


@pytest.mark.parametrize(
    "indicator",
    [None, False, 3, "A\nB", "A\rB", "A\tB", "A\vB", "A\fB", "A\u2028B", "A\u2029B", "\x00", " "],
)
def test_invalid_indicator_refuses(indicator):
    with pytest.raises(ValueError, match="through_indicator"):
        HoleCallout(8, through=True, through_indicator=indicator)


@pytest.mark.parametrize("indicator", ["", "THROUGH ALL"])
def test_indicator_exports_as_native_ink(indicator, tmp_path):
    annotation = HoleCallout("8 ±0.01", through=True, through_indicator=indicator)
    for extension, exporter in (("svg", ExportSVG), ("dxf", ExportDXF)):
        path = tmp_path / f"hole.{extension}"
        drawing = exporter()
        drawing.add_shape(annotation)
        drawing.write(path)
        assert path.stat().st_size > 100
