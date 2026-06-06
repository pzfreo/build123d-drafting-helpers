"""Tests for build123d_drafting.make_drawing."""

from pathlib import Path

import pytest
from build123d import Box, export_step

from build123d_drafting.make_drawing import (
    _choose_scale,
    _dedup_diams,
    _fmt,
    make_drawing,
)

# ---------------------------------------------------------------------------
# Pure-function unit tests (fast, no OCP projection)
# ---------------------------------------------------------------------------


class TestFmt:
    def test_integer_value(self):
        assert _fmt(36.0) == "36"

    def test_fractional_value(self):
        assert _fmt(14.7) == "14.7"

    def test_zero(self):
        assert _fmt(0.0) == "0"


class TestDedupDiams:
    def test_empty(self):
        assert _dedup_diams([]) == []

    def test_single(self):
        assert _dedup_diams([{"diameter": 10.0, "area": 1}]) == [10.0]

    def test_deduplicates_close_values(self):
        cyls = [{"diameter": 10.0, "area": 1}, {"diameter": 10.05, "area": 1}]
        result = _dedup_diams(cyls)
        assert len(result) == 1

    def test_keeps_distinct_values(self):
        cyls = [{"diameter": 10.0, "area": 1}, {"diameter": 20.0, "area": 1}]
        result = _dedup_diams(cyls)
        assert len(result) == 2

    def test_sorted_descending(self):
        cyls = [
            {"diameter": 5.0, "area": 1},
            {"diameter": 20.0, "area": 1},
            {"diameter": 10.0, "area": 1},
        ]
        result = _dedup_diams(cyls)
        assert result == [20.0, 10.0, 5.0]


class TestChooseScale:
    def test_tiny_part_fits_A4(self):
        # 20×20×20 mm — 4-view layout at 2:1 is ~320mm wide, so falls back to A4 1:1
        scale, pw, ph, tbw = _choose_scale(20, 20, 20)
        assert int(pw) == 297

    def test_medium_part_gets_A2(self):
        # 80×80×80 mm — too wide for A3 at any scale, goes to A2 1:1
        scale, pw, ph, tbw = _choose_scale(80, 80, 80)
        assert int(pw) == 594

    def test_large_part_gets_bigger_page(self):
        scale, pw, ph, tbw = _choose_scale(300, 300, 300)
        assert pw > 420

    def test_returns_four_values(self):
        result = _choose_scale(50, 50, 50)
        assert len(result) == 4

    def test_result_fits_on_page(self):
        # The chosen scale+page should actually fit the layout
        x, y, z = 60, 60, 15
        scale, pw, ph, tbw = _choose_scale(x, y, z)
        margin, dim_pad = 10.0, 18.0
        bbox_max = max(x, y, z)
        w = (
            margin
            + dim_pad
            + x * scale
            + dim_pad
            + y * scale
            + dim_pad
            + bbox_max * scale * 0.7
            + dim_pad
            + tbw
            + margin
        )
        h = margin + dim_pad + y * scale + dim_pad + z * scale + dim_pad + margin
        assert w <= pw
        assert h <= ph


# ---------------------------------------------------------------------------
# Integration test — requires build123d + OCP (slow)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_make_drawing_box(tmp_path):
    """make_drawing() produces SVG and DXF for a simple box STEP file."""
    # Build a simple box and export to STEP
    box = Box(30, 20, 10)
    step_file = str(tmp_path / "box.step")
    export_step(box, step_file)

    out_stem = str(tmp_path / "box_drawing")
    svg_path, dxf_path = make_drawing(
        step_file,
        out=out_stem,
        title="TEST BOX",
        number="TST-001",
    )

    assert Path(svg_path).exists()
    assert Path(dxf_path).exists()
    assert Path(svg_path).stat().st_size > 1000
    assert Path(dxf_path).stat().st_size > 100

    # SVG should have the full page dimensions injected
    svg_content = Path(svg_path).read_text()
    assert 'mm"' in svg_content  # width/height in mm


@pytest.mark.timeout(120)
def test_make_drawing_default_title(tmp_path):
    """Title defaults to uppercased stem when not provided."""
    box = Box(10, 10, 10)
    step_file = str(tmp_path / "my_part.step")
    export_step(box, step_file)

    svg_path, _ = make_drawing(step_file, out=str(tmp_path / "out"))
    assert Path(svg_path).exists()
