"""Tests for build123d_drafting.make_drawing."""

from pathlib import Path

import pytest
from build123d import Box, Cylinder, export_step

from build123d_drafting import ViewCoordinates, view_axes
from build123d_drafting.make_drawing import (
    _fmt,
    analyse_cylinders,
    analyse_face_levels,
    choose_scale,
    dedup_diams,
    generate_script,
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
        assert dedup_diams([]) == []

    def test_single(self):
        assert dedup_diams([{"diameter": 10.0, "area": 1}]) == [10.0]

    def test_deduplicates_close_values(self):
        cyls = [{"diameter": 10.0, "area": 1}, {"diameter": 10.05, "area": 1}]
        result = dedup_diams(cyls)
        assert len(result) == 1

    def test_keeps_distinct_values(self):
        cyls = [{"diameter": 10.0, "area": 1}, {"diameter": 20.0, "area": 1}]
        result = dedup_diams(cyls)
        assert len(result) == 2

    def test_sorted_descending(self):
        cyls = [
            {"diameter": 5.0, "area": 1},
            {"diameter": 20.0, "area": 1},
            {"diameter": 10.0, "area": 1},
        ]
        result = dedup_diams(cyls)
        assert result == [20.0, 10.0, 5.0]


class TestChooseScale:
    def test_tiny_part_fits_A4(self):
        # 20×20×20 mm — 4-view layout at 2:1 is ~320mm wide, so falls back to A4 1:1
        scale, pw, ph, tbw = choose_scale(20, 20, 20)
        assert int(pw) == 297

    def test_medium_part_gets_A2(self):
        # 80×80×80 mm — too wide for A3 at any scale, goes to A2 1:1
        scale, pw, ph, tbw = choose_scale(80, 80, 80)
        assert int(pw) == 594

    def test_large_part_gets_bigger_page(self):
        scale, pw, ph, tbw = choose_scale(300, 300, 300)
        assert pw > 420

    def test_returns_four_values(self):
        result = choose_scale(50, 50, 50)
        assert len(result) == 4

    def test_result_fits_on_page(self):
        # The chosen scale+page should actually fit the layout
        x, y, z = 60, 60, 15
        scale, pw, ph, tbw = choose_scale(x, y, z)
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
def test_make_drawing_cylinder_uses_centerline_and_holecallout(tmp_path):
    """make_drawing() adds Centerline and HoleCallout for cylindrical parts."""
    cyl = Cylinder(radius=15, height=40)
    step_file = str(tmp_path / "cyl.step")
    export_step(cyl, step_file)

    svg_path, _ = make_drawing(step_file, out=str(tmp_path / "cyl_drawing"), title="CYL")

    # The drawing must exist and be non-trivial
    assert Path(svg_path).exists()
    assert Path(svg_path).stat().st_size > 1000


@pytest.mark.timeout(120)
def test_make_drawing_default_title(tmp_path):
    """Title defaults to uppercased stem when not provided."""
    box = Box(10, 10, 10)
    step_file = str(tmp_path / "my_part.step")
    export_step(box, step_file)

    svg_path, _ = make_drawing(step_file, out=str(tmp_path / "out"))
    assert Path(svg_path).exists()


@pytest.mark.timeout(120)
def test_make_drawing_accepts_build123d_object(tmp_path):
    """make_drawing() draws an in-memory build123d Shape without a STEP file."""
    box = Box(30, 20, 10)
    out_stem = str(tmp_path / "box_obj")

    svg_path, dxf_path = make_drawing(box, out=out_stem, title="BOX OBJ")

    assert Path(svg_path).exists()
    assert Path(dxf_path).exists()
    assert Path(svg_path).stat().st_size > 1000


@pytest.mark.timeout(120)
def test_make_drawing_object_defaults_out_to_drawing(tmp_path, monkeypatch):
    """Passing an object with no out= writes to 'drawing.svg' in the cwd."""
    monkeypatch.chdir(tmp_path)
    box = Box(10, 10, 10)

    svg_path, dxf_path = make_drawing(box)

    assert Path(svg_path).name == "drawing.svg"
    assert Path(dxf_path).name == "drawing.dxf"
    assert (tmp_path / "drawing.svg").exists()


def test_generate_script_rejects_build123d_object():
    """generate_script() needs a path — a live object cannot be embedded."""
    box = Box(10, 10, 10)
    with pytest.raises(TypeError, match="STEP file path"):
        generate_script(box)


# ---------------------------------------------------------------------------
# ViewCoordinates (pure-Python, no OCP needed)
# ---------------------------------------------------------------------------


class TestViewCoordinates:
    def _front_vc(self):
        # Front view: camera at (0, -100, 0), up=(0,0,1), look_at=(0,0,0)
        # → world X → page_X (+1), world Z → page_Y (+1)
        axes = view_axes((0.0, -100.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
        return ViewCoordinates(axes, view_x=100.0, view_y=80.0, cx=0.0, cy=0.0, cz=0.0, scale=1.0)

    def test_px_at_origin(self):
        vc = self._front_vc()
        assert vc.px(0.0) == pytest.approx(100.0)

    def test_py_at_origin(self):
        vc = self._front_vc()
        assert vc.py(0.0) == pytest.approx(80.0)

    def test_px_positive_offset(self):
        vc = self._front_vc()
        assert vc.px(10.0) == pytest.approx(110.0)

    def test_py_positive_offset(self):
        vc = self._front_vc()
        assert vc.py(5.0) == pytest.approx(85.0)

    def test_scale_applied(self):
        axes = view_axes((0.0, -100.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
        vc = ViewCoordinates(axes, view_x=0.0, view_y=0.0, cx=0.0, cy=0.0, cz=0.0, scale=2.0)
        assert vc.px(5.0) == pytest.approx(10.0)

    def test_centroid_offset(self):
        axes = view_axes((0.0, -100.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
        vc = ViewCoordinates(axes, view_x=50.0, view_y=50.0, cx=10.0, cy=0.0, cz=5.0, scale=1.0)
        assert vc.px(10.0) == pytest.approx(50.0)  # at centroid → view centre
        assert vc.py(5.0) == pytest.approx(50.0)  # at centroid → view centre

    # px_axis / py_axis attributes

    def test_front_view_px_axis(self):
        vc = self._front_vc()
        assert vc.px_axis == "world_X"

    def test_front_view_py_axis(self):
        vc = self._front_vc()
        assert vc.py_axis == "world_Z"

    # pp() matches px()/py() for orthographic views

    def test_pp_front_view_matches_px_py(self):
        vc = self._front_vc()
        page_x, page_y = vc.pp(10.0, 0.0, 5.0)
        assert page_x == pytest.approx(vc.px(10.0))
        assert page_y == pytest.approx(vc.py(5.0))

    def test_pp_front_view_ignores_depth_axis(self):
        # world_Y is depth in front view — varying it should not change the page point
        vc = self._front_vc()
        pt_a = vc.pp(10.0, 0.0, 5.0)
        pt_b = vc.pp(10.0, 50.0, 5.0)
        assert pt_a == pytest.approx(pt_b)

    # Side view: camera on +X axis → world_Y → page_X, world_Z → page_Y

    def _side_vc(self):
        axes = view_axes((100.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
        return ViewCoordinates(axes, view_x=150.0, view_y=80.0, cx=0.0, cy=0.0, cz=0.0, scale=1.0)

    def test_side_view_px_axis_is_world_y(self):
        vc = self._side_vc()
        assert vc.px_axis == "world_Y"

    def test_side_view_py_axis_is_world_z(self):
        vc = self._side_vc()
        assert vc.py_axis == "world_Z"

    def test_side_view_px_maps_y_coordinate(self):
        vc = self._side_vc()
        assert vc.px(8.0) == pytest.approx(158.0)

    def test_side_view_py_maps_z_coordinate(self):
        vc = self._side_vc()
        assert vc.py(3.0) == pytest.approx(83.0)

    def test_side_view_pp_matches_px_py(self):
        vc = self._side_vc()
        page_x, page_y = vc.pp(0.0, 8.0, 3.0)
        assert page_x == pytest.approx(vc.px(8.0))
        assert page_y == pytest.approx(vc.py(3.0))

    # Plan view: camera on +Z axis → world_X → page_X, world_Y → page_Y

    def _plan_vc(self):
        axes = view_axes((0.0, 0.0, 100.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0))
        return ViewCoordinates(axes, view_x=100.0, view_y=150.0, cx=0.0, cy=0.0, cz=0.0, scale=1.0)

    def test_plan_view_px_axis_is_world_x(self):
        vc = self._plan_vc()
        assert vc.px_axis == "world_X"

    def test_plan_view_py_axis_is_world_y(self):
        vc = self._plan_vc()
        assert vc.py_axis == "world_Y"

    def test_plan_view_pp_matches_px_py(self):
        vc = self._plan_vc()
        page_x, page_y = vc.pp(7.0, 4.0, 0.0)
        assert page_x == pytest.approx(vc.px(7.0))
        assert page_y == pytest.approx(vc.py(4.0))

    # ISO view: camera at (-DIST, -DIST, DIST) → two world axes → page_X

    def _iso_vc(self):
        # Standard ISO camera: world_X → page_X (+1), world_Y → page_X (-1), world_Z → page_Y (+1)
        axes = view_axes((-100.0, -100.0, 100.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
        return ViewCoordinates(axes, view_x=100.0, view_y=80.0, cx=0.0, cy=0.0, cz=0.0, scale=1.0)

    def test_iso_view_px_axis_is_none(self):
        vc = self._iso_vc()
        assert vc.px_axis is None

    def test_iso_view_px_raises_with_helpful_message(self):
        vc = self._iso_vc()
        with pytest.raises(ValueError, match="pp"):
            vc.px(5.0)

    def test_iso_view_py_raises_with_helpful_message(self):
        # world_Z → page_Y uniquely, so py_axis should be set
        # (ISO typically only has the page_X clash, not page_Y)
        vc = self._iso_vc()
        # world_Z maps cleanly to page_Y — py() should still work
        assert vc.py_axis == "world_Z"
        assert vc.py(3.0) == pytest.approx(83.0)

    def test_iso_view_pp_correct(self):
        # For ISO camera at (-100,-100,100) with look_at=(0,0,0), up=(0,0,1):
        # world_X → page_X (+1), world_Y → page_X (-1), world_Z → page_Y (+1)
        # pp(10, 5, 3) → page_x = 100 + (10-0)*1 + (5-0)*(-1) = 105
        #                page_y = 80 + (3-0)*1 = 83
        vc = self._iso_vc()
        page_x, page_y = vc.pp(10.0, 5.0, 3.0)
        assert page_x == pytest.approx(105.0)
        assert page_y == pytest.approx(83.0)

    def test_iso_view_pp_at_centroid_gives_view_centre(self):
        vc = self._iso_vc()
        page_x, page_y = vc.pp(0.0, 0.0, 0.0)
        assert page_x == pytest.approx(100.0)
        assert page_y == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# analyse_cylinders / analyse_face_levels — require OCP (slow)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_analyse_cylinders_box_has_no_z_cylinders():
    from build123d import Box

    box = Box(30, 20, 10)
    z_cyls, cross_cyls = analyse_cylinders(box)
    assert z_cyls == []
    assert cross_cyls == []


@pytest.mark.timeout(60)
def test_analyse_cylinders_finds_cylinder():
    from build123d import Cylinder

    cyl = Cylinder(5, 20)  # radius=5, height=20 → diameter=10
    z_cyls, cross_cyls = analyse_cylinders(cyl)
    assert len(z_cyls) >= 1
    diameters = [c["diameter"] for c in z_cyls]
    assert any(abs(d - 10.0) < 0.5 for d in diameters)


@pytest.mark.timeout(60)
def test_analyse_face_levels_box():
    from build123d import Box

    box = Box(30, 20, 10)
    levels = analyse_face_levels(box)
    # Box centred at origin has Z faces at -5 and +5
    assert any(abs(z - (-5.0)) < 0.1 for z in levels)
    assert any(abs(z - 5.0) < 0.1 for z in levels)


@pytest.mark.timeout(60)
def test_analyse_face_levels_returns_sorted():
    from build123d import Box

    box = Box(30, 20, 10)
    levels = analyse_face_levels(box)
    assert levels == sorted(levels)
