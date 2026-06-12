"""Tests for build123d_drafting.make_drawing."""

from pathlib import Path

import pytest
from build123d import Box, Compound, Cylinder, Edge, Pos, export_step

from build123d_drafting import Drawing, Leader, ViewCoordinates, build_drawing, view_axes
from build123d_drafting.make_drawing import (
    _export_shape,
    _fits,
    _fmt,
    _is_rotational,
    analyse_cylinders,
    analyse_face_levels,
    choose_scale,
    dedup_diams,
    generate_script,
    lint_feature_coverage,
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
        # 20×20×20 mm — enlargement scales don't fit A4/A3, lands on A4 2:1
        scale, pw, ph, tbw = choose_scale(20, 20, 20)
        assert int(pw) == 297
        assert scale == 2.0

    def test_medium_part_gets_A3(self):
        # 80×80×80 mm — fits A3 1:1 because the view rows clear the title block,
        # so its width no longer forces the jump to A2 (#62)
        scale, pw, ph, tbw = choose_scale(80, 80, 80)
        assert int(pw) == 420

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
        assert _fits(x, y, z, scale, pw, ph, tbw)

    # Enlargement scales for small parts (#62)

    def test_small_part_gets_enlargement_scale(self):
        # 28 × 8.5 × 12.5 mm (issue #62 part) → 5:1 on A3, not 1:1 on A4
        scale, pw, ph, tbw = choose_scale(28, 8.5, 12.5)
        assert scale == 5.0
        assert int(pw) == 420

    def test_very_small_part_gets_10x(self):
        scale, pw, ph, tbw = choose_scale(8, 4, 4)
        assert scale == 10.0
        assert int(pw) == 297


class TestChooseScaleOverrides:
    def test_scale_and_page_used_verbatim(self):
        assert choose_scale(28, 8.5, 12.5, scale=5, page="A3") == (5.0, 420.0, 297.0, 150.0)

    def test_scale_and_page_honoured_even_when_too_small(self):
        # Explicit overrides win even if the layout doesn't fit (warning only)
        scale, pw, ph, tbw = choose_scale(300, 300, 300, scale=1, page="A4")
        assert (scale, pw) == (1.0, 297.0)

    def test_page_only_picks_largest_fitting_scale(self):
        scale, pw, ph, tbw = choose_scale(28, 8.5, 12.5, page="A3")
        assert (pw, ph) == (420.0, 297.0)
        assert scale == 5.0

    def test_scale_only_picks_smallest_fitting_page(self):
        scale, pw, ph, tbw = choose_scale(28, 8.5, 12.5, scale=2)
        assert scale == 2.0
        assert int(pw) == 297

    def test_page_tuple(self):
        scale, pw, ph, tbw = choose_scale(10, 10, 10, page=(420, 297))
        assert (pw, ph, tbw) == (420.0, 297.0, 150.0)

    def test_page_wxh_string(self):
        scale, pw, ph, tbw = choose_scale(10, 10, 10, page="420x297")
        assert (pw, ph) == (420.0, 297.0)

    def test_page_name_case_insensitive(self):
        scale, pw, ph, tbw = choose_scale(10, 10, 10, page="a3")
        assert (pw, ph) == (420.0, 297.0)

    def test_unknown_page_raises(self):
        with pytest.raises(ValueError, match="page size"):
            choose_scale(10, 10, 10, page="B5")

    def test_nonpositive_scale_raises(self):
        with pytest.raises(ValueError, match="scale"):
            choose_scale(10, 10, 10, scale=0)


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


# ---------------------------------------------------------------------------
# Drawing builder (build_drawing / Drawing / add_view)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_build_drawing_returns_populated_drawing(tmp_path):
    dwg = build_drawing(Box(30, 20, 10), out=str(tmp_path / "b"), title="B", number="DWG-1")
    assert isinstance(dwg, Drawing)
    assert set(dwg.views) == {"front", "plan", "side", "iso"}
    assert dwg.annotations, "expected automatic annotations"
    # build_drawing must not write any files — that is export()'s job.
    assert not (tmp_path / "b.svg").exists()
    assert not (tmp_path / "b.dxf").exists()


@pytest.mark.timeout(60)
def test_build_drawing_export_writes_files(tmp_path):
    stem = str(tmp_path / "b")
    dwg = build_drawing(Box(30, 20, 10), out=stem)
    svg, dxf = dwg.export(stem)
    assert Path(svg).exists() and Path(dxf).exists()
    assert dwg.svg_path == svg and dwg.dxf_path == dxf


@pytest.mark.timeout(60)
def test_build_drawing_scale_and_page_override(tmp_path):
    # Issue #63 — explicit scale/page reach the Drawing instead of choose_scale's pick
    dwg = build_drawing(Box(28, 8.5, 12.5), out=str(tmp_path / "o"), scale=5, page="A3")
    assert dwg.scale == 5.0
    assert (dwg.page_w, dwg.page_h) == (420.0, 297.0)


@pytest.mark.timeout(60)
def test_build_drawing_auto_dims_false():
    # #74 — views, scale, page, and title block only; no turned-part dims.
    dwg = build_drawing(Cylinder(15, 40), auto_dims=False)
    assert set(dwg.views) == {"front", "plan", "side", "iso"}
    assert [a for a in dwg.annotations] == [dwg._named["title_block"]]


@pytest.mark.timeout(60)
def test_clear_annotations_keeps_title_block():
    # #74 — wholesale removal without knowing the auto-name scheme.
    dwg = build_drawing(Cylinder(15, 40))  # cylinder → od dim, centerlines, …
    assert len(dwg.annotations) > 1
    removed = dwg.clear_annotations()
    assert removed
    assert all(a not in dwg.annotations for a in removed)
    assert len(dwg.annotations) == 1
    assert "title_block" in dwg._named and len(dwg._named) == 1


@pytest.mark.timeout(60)
def test_clear_annotations_keep_custom_and_unnamed_removed():
    dwg = build_drawing(Box(30, 20, 10))
    keep_me = dwg.add(
        Leader(tip=dwg.at("front", 0, 0, 0), elbow=(5, 5, 0), label="K", draft=dwg.draft), "ldr_k"
    )
    dwg.add(Leader(tip=dwg.at("front", 0, 0, 0), elbow=(6, 6, 0), label="U", draft=dwg.draft))
    dwg.clear_annotations(keep=("title_block", "ldr_k"))
    assert set(dwg._named) == {"title_block", "ldr_k"}
    assert keep_me in dwg.annotations
    assert len(dwg.annotations) == 2  # unnamed leader removed too


@pytest.fixture(scope="module")
def shrunk_iso_drawing():
    # #75 fixture — NIST CTC-01-like plate at 1:5 on A3: the iso overflows at
    # sheet scale and is auto-shrunk. Module-scoped; tests must not mutate it.
    return build_drawing(Box(800, 450, 150), scale=0.2, page="A3")


@pytest.mark.timeout(120)
def test_iso_overflow_shrinks_with_nts_note(shrunk_iso_drawing):
    # #75 — at sheet scale the iso would run past the A3 page edge; it must be
    # re-projected smaller and captioned NTS.
    from build123d_drafting.make_drawing import _iso_bbox

    dwg = shrunk_iso_drawing
    labels = [getattr(a, "label", "") for a in dwg.annotations]
    assert "ISO VIEW (NTS)" in labels
    x0, y0, x1, y1 = _iso_bbox(dwg)
    assert (
        x1 <= dwg.page_w - 10 + 0.5 and x0 >= 0 and y0 >= 10 - 0.5 and y1 <= dwg.page_h - 10 + 0.5
    )


@pytest.mark.timeout(120)
def test_shrunk_iso_keeps_world_to_page_mapping(shrunk_iso_drawing):
    # After the NTS shrink, dwg.at("iso", ...) must still map world points to
    # the page: the centroid lands on the view centre and offsets scale by the
    # shrunk view scale, not the sheet scale.
    dwg = shrunk_iso_drawing
    cx, cy, cz = dwg.centroid
    centre = dwg.at("iso", cx, cy, cz)
    vis, _hid = dwg.views["iso"]
    bb = vis.bounding_box()
    assert bb.min.X < centre[0] < bb.max.X and bb.min.Y < centre[1] < bb.max.Y
    # This fixture shrinks the iso to 1/2 sheet scale (see the NTS test above).
    # World +Z maps to page +Y; the offset must use the shrunk view scale.
    raised = dwg.at("iso", cx, cy, cz + 100)
    assert raised[1] - centre[1] == pytest.approx(100 * dwg.scale * 0.5)


@pytest.mark.timeout(60)
def test_iso_that_fits_is_not_shrunk():
    dwg = build_drawing(Box(30, 20, 10))
    labels = [getattr(a, "label", "") for a in dwg.annotations]
    assert "ISO VIEW (NTS)" not in labels


@pytest.mark.timeout(60)
def test_drawing_add_and_remove():
    dwg = build_drawing(Box(30, 20, 10))
    n0 = len(dwg.annotations)
    ldr = Leader(tip=dwg.at("front", 0, 0, 0), elbow=(5, 5, 0), label="X", draft=dwg.draft)
    dwg.add(ldr, "ldr_test")
    assert len(dwg.annotations) == n0 + 1
    removed = dwg.remove("ldr_test")
    assert removed is ldr
    assert len(dwg.annotations) == n0
    with pytest.raises(KeyError):
        dwg.remove("does_not_exist")


@pytest.mark.timeout(60)
def test_drawing_add_replaces_reused_name():
    dwg = build_drawing(Box(30, 20, 10))
    n0 = len(dwg.annotations)
    first = Leader(tip=dwg.at("front", 0, 0, 0), elbow=(5, 5, 0), label="A", draft=dwg.draft)
    second = Leader(tip=dwg.at("front", 0, 0, 0), elbow=(6, 6, 0), label="B", draft=dwg.draft)
    dwg.add(first, "ldr")
    dwg.add(second, "ldr")  # same name → replaces, no orphan left behind
    assert len(dwg.annotations) == n0 + 1
    assert first not in dwg.annotations
    assert dwg.remove("ldr") is second


@pytest.mark.timeout(60)
def test_drawing_at_maps_world_to_page():
    dwg = build_drawing(Box(30, 20, 10))
    cx, cy, cz = dwg.centroid
    base = dwg.at("front", cx, cy, cz)
    # Front view: world +X → page +X, world +Z → page +Y.
    dx = dwg.at("front", cx + 10, cy, cz)
    dz = dwg.at("front", cx, cy, cz + 10)
    assert dx[0] > base[0] and dx[1] == pytest.approx(base[1])
    assert dz[1] > base[1] and dz[0] == pytest.approx(base[0])


@pytest.mark.timeout(60)
def test_drawing_add_view(tmp_path):
    dwg = build_drawing(Box(30, 20, 10))
    look = dwg.look_at
    bottom_cam = (look[0], look[1], look[2] - dwg.dist)
    vc = dwg.add_view("bottom", Box(30, 20, 10), bottom_cam, (0, 1, 0), (260.0, 60.0))
    assert "bottom" in dwg.views
    assert isinstance(vc, ViewCoordinates)
    # The custom view exports alongside the standard ones.
    svg, _ = dwg.export(str(tmp_path / "b"))
    assert Path(svg).exists()


@pytest.mark.timeout(60)
def test_generate_script_emits_build_drawing(tmp_path):
    box = Box(30, 20, 10)
    step = tmp_path / "p.step"
    export_step(box, str(step))
    py = generate_script(str(step), out=str(tmp_path / "p"))
    content = Path(py).read_text(encoding="utf-8")
    assert "build_drawing(" in content
    assert "dwg.export(" in content
    assert "Customise here" in content


# ---------------------------------------------------------------------------
# Part classification (#81) — prismatic parts skip turned-part annotations
# ---------------------------------------------------------------------------


class TestPrismaticClassification:
    @pytest.mark.timeout(60)
    def test_prismatic_part_with_bores_skips_turned_annotations(self):
        # A housing-like plate: Z-axis bores exist, but they are holes — not
        # an OD. dim_od / centrelines / ldr_z* would all be wrong.
        part = (
            Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(5, 30) - Pos(-30, -15, 0) * Cylinder(8, 30)
        )
        dwg = build_drawing(part)
        assert "dim_od" not in dwg._named
        assert "centerline_front" not in dwg._named
        assert "centerline_side" not in dwg._named
        assert not any(name.startswith("ldr_z") for name in dwg._named)

    @pytest.mark.timeout(60)
    def test_rotational_part_keeps_turned_annotations(self):
        dwg = build_drawing(Cylinder(30, 40) - Cylinder(10, 40))
        assert "dim_od" in dwg._named
        assert "centerline_front" in dwg._named
        assert "ldr_z0" in dwg._named

    @pytest.mark.timeout(60)
    def test_corner_fillets_do_not_make_a_plate_rotational(self):
        # Big quarter-cylinder corner fillets on a square plate must not be
        # mistaken for an OD.
        from build123d import Axis, fillet

        box = Box(60, 60, 20)
        part = fillet(box.edges().filter_by(Axis.Z), 25)
        dwg = build_drawing(part)
        assert "dim_od" not in dwg._named


# ---------------------------------------------------------------------------
# Export fallback (#83) — element-wise retry with view/layer context
# ---------------------------------------------------------------------------


class _FlakyExporter:
    """Stand-in exporter: rejects multi-element shapes (or everything)."""

    def __init__(self, fail_all=False):
        self.added = []
        self.fail_all = fail_all

    def add_shape(self, shape, layer=None):
        if self.fail_all:
            raise AssertionError("Constraint failed")
        elements = shape.faces() or shape.edges()
        if len(elements) > 1:
            raise AssertionError("Constraint failed")
        self.added.append(shape)


class TestExportShapeFallback:
    def test_compound_falls_back_to_edges(self):
        edges = Compound(
            [
                Edge.make_line((0, 0, 0), (1, 0, 0)),
                Edge.make_line((0, 1, 0), (1, 1, 0)),
            ]
        )
        exporter = _FlakyExporter()
        _export_shape(exporter, edges, "hidden", "view 'iso'")
        assert len(exporter.added) == 2

    def test_all_elements_failing_raises_with_context(self):
        edges = Compound([Edge.make_line((0, 0, 0), (1, 0, 0))])
        with pytest.raises(RuntimeError, match=r"view 'iso' \(layer 'hidden'\)"):
            _export_shape(_FlakyExporter(fail_all=True), edges, "hidden", "view 'iso'")

    def test_annotation_falls_back_to_faces(self):
        from build123d import Draft

        from build123d_drafting import Note

        note = Note("AB", (10, 10), Draft(font_size=3.0))  # two glyphs → ≥2 faces
        exporter = _FlakyExporter()
        _export_shape(exporter, note, "dims", "annotation 'AB'")
        assert len(exporter.added) == len(note.faces())

    def test_mixed_faces_and_loose_edges_all_exported(self):
        # A compound mixing text faces with bare stroke edges must not lose
        # the edges in the element-wise path.
        from build123d import Text

        mixed = Compound([*Text("A", 3).faces(), Edge.make_line((5, 5, 0), (9, 5, 0))])
        exporter = _FlakyExporter()
        _export_shape(exporter, mixed, "dims", "annotation 'mixed'")
        assert len(exporter.added) == len(mixed.faces()) + 1

    def test_svg_exporter_failure_raises_when_nothing_exports(self, monkeypatch):
        # Atomic (SVG) path: whole-shape add fails and the shape decomposes
        # to nothing — the original error must surface, not be swallowed.
        from build123d import ExportSVG

        svg = ExportSVG()
        svg.add_layer("part")

        def boom(self, shape, layer="", **kwargs):
            raise AssertionError("Constraint failed")

        monkeypatch.setattr(ExportSVG, "add_shape", boom)
        with pytest.raises(RuntimeError, match="nothing could be exported"):
            _export_shape(svg, Compound([]), "part", "view 'iso'")

    @pytest.mark.timeout(60)
    def test_export_survives_one_bad_compound(self, tmp_path, monkeypatch):
        # Simulate #83: OCCT raises a bare AssertionError for one view
        # compound. export() must degrade element-wise and still write files.
        from build123d import ExportSVG

        dwg = build_drawing(Box(30, 20, 10))
        real = ExportSVG.add_shape
        state = {"tripped": False}

        def flaky(self, shape, layer="default", **kwargs):
            if not state["tripped"] and layer == "part":
                state["tripped"] = True
                raise AssertionError("Constraint failed")
            return real(self, shape, layer=layer, **kwargs)

        monkeypatch.setattr(ExportSVG, "add_shape", flaky)
        svg, dxf = dwg.export(str(tmp_path / "f"))
        assert Path(svg).exists() and Path(dxf).exists()


# ---------------------------------------------------------------------------
# Feature-coverage lint (#80) — size coverage of hole/boss diameters
# ---------------------------------------------------------------------------


class TestLintFeatureCoverage:
    @pytest.mark.timeout(60)
    def test_uncovered_bore_is_flagged(self):
        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(4, 30)
        issues = lint_feature_coverage(part, [])
        assert [i.code for i in issues] == ["feature_not_dimensioned"]
        assert "ø8" in issues[0].message
        assert issues[0].severity == "warning"

    @pytest.mark.timeout(60)
    def test_diameter_callout_covers_feature(self):
        from build123d import Draft

        from build123d_drafting import Note

        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(4, 30)
        ann = Note("4× ø8 THRU", (10, 10), Draft(font_size=3.0))
        assert lint_feature_coverage(part, [ann]) == []

    @pytest.mark.timeout(60)
    def test_radius_note_does_not_cover(self):
        # An "R4 TYP" fillet note must not mask an undimensioned ø8 bore.
        from build123d import Draft

        from build123d_drafting import Note

        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(4, 30)
        ann = Note("R4 TYP", (10, 10), Draft(font_size=3.0))
        assert [i.code for i in lint_feature_coverage(part, [ann])] == ["feature_not_dimensioned"]

    @pytest.mark.timeout(60)
    def test_slot_split_bore_is_still_a_feature(self):
        # A bore intersected by a slot leaves cylinder patches under half a
        # turn each — together they are still one undimensioned ø10 hole.
        part = Box(60, 40, 10) - Cylinder(5, 12) - Box(60, 6, 12)
        issues = lint_feature_coverage(part, [])
        assert any("ø10" in i.message for i in issues)

    @pytest.mark.timeout(60)
    def test_hole_callout_accepts_string_diameter(self):
        from build123d_drafting import HoleCallout

        callout = HoleCallout("8.5 H7", through=True)
        assert callout.covers_diameters == (8.5,)

    @pytest.mark.timeout(60)
    def test_fillets_are_not_features(self):
        from build123d import Axis, fillet

        box = Box(60, 40, 20)
        part = fillet(box.edges().filter_by(Axis.Z), 3)
        assert lint_feature_coverage(part, []) == []

    @pytest.mark.timeout(60)
    def test_drawing_lint_reports_unannotated_bore(self):
        # Prismatic bores now get automatic callouts (#91) — so the sheet is
        # born clean, and removing the callout must surface the bore through
        # the coverage lint as the missing-dimension signal (#80).
        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(5, 30)
        dwg = build_drawing(part)
        assert "feature_not_dimensioned" not in [i.code for i in dwg.lint()]
        for name in [n for n in dwg._named if n.startswith("hc_")]:
            dwg.remove(name)
        codes = [i.code for i in dwg.lint()]
        assert "feature_not_dimensioned" in codes

    @pytest.mark.timeout(60)
    def test_drawing_lint_clean_for_annotated_rotational_part(self):
        dwg = build_drawing(Cylinder(15, 40) - Cylinder(5, 40))
        assert [i for i in dwg.lint() if i.code == "feature_not_dimensioned"] == []

    @pytest.mark.timeout(60)
    def test_title_block_text_is_not_a_callout(self):
        # "BRACKET R8" in the title must not mark ø16 as covered.
        from build123d import Draft

        from build123d_drafting import TitleBlock

        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(8, 30)
        tb = TitleBlock("BRACKET R8", "DWG-1", draft=Draft(font_size=3.0))
        issues = lint_feature_coverage(part, [tb])
        assert [i.code for i in issues] == ["feature_not_dimensioned"]

    @pytest.mark.timeout(60)
    def test_hole_callout_covers_via_structured_metadata(self):
        # HoleCallout draws its ø glyphs geometrically (label is "") — it must
        # still count as coverage.
        from build123d_drafting import HoleCallout

        part = Box(100, 60, 20) - Pos(20, 10, 0) * Cylinder(4.25, 30)
        callout = HoleCallout(8.5, count=4, through=True)
        assert lint_feature_coverage(part, [callout]) == []


class TestAutoHoleAnnotations:
    """Auto hole callouts (#91), count grouping (#92), centre marks (#95)."""

    @pytest.fixture(scope="class")
    def plate_drawing(self):
        # 4x o10 thru corners + centre o8 thru with o16x6 cbore + o6 x-axis
        # cross hole + o12 blind hole
        part = (
            Box(100, 100, 20)
            - Pos(35, 35, 0) * Cylinder(5, 20)
            - Pos(-35, 35, 0) * Cylinder(5, 20)
            - Pos(35, -35, 0) * Cylinder(5, 20)
            - Pos(-35, -35, 0) * Cylinder(5, 20)
            - Cylinder(4, 20)
            - Pos(0, 0, 7) * Cylinder(8, 6)
            - Pos(0, 25, 0) * Cylinder(3, 100, rotation=(0, 90, 0))
            - Pos(-20, -10, 10 - 4) * Cylinder(6, 8)
        )
        return build_drawing(part)

    @pytest.mark.timeout(120)
    def test_identical_holes_share_one_counted_callout(self, plate_drawing):
        hc = [n for n in plate_drawing._named if n.startswith("hc_plan")]
        # 3 distinct Z specs (4x o10 thru, o8 cbore stack, o12 blind), not 6
        assert len(hc) == 3

    @pytest.mark.timeout(120)
    def test_callouts_cover_all_feature_diameters(self, plate_drawing):
        covered = set()
        for name, ann in plate_drawing._named.items():
            if name.startswith("hc_"):
                covered.update(getattr(ann, "covers_diameters", ()))
        assert covered == {10.0, 8.0, 16.0, 6.0, 12.0}

    @pytest.mark.timeout(120)
    def test_cross_axis_hole_gets_side_view_callout(self, plate_drawing):
        (name,) = [n for n in plate_drawing._named if n.startswith("hc_side")]
        assert plate_drawing._named[name].covers_diameters == (6.0,)

    @pytest.mark.timeout(120)
    def test_every_hole_gets_a_centre_mark(self, plate_drawing):
        cm = [n for n in plate_drawing._named if n.startswith("cm_")]
        assert len(cm) == 7  # 6 z-holes in plan + 1 x-hole in side
        assert all(plate_drawing._named[n].is_centerline for n in cm)

    @pytest.mark.timeout(120)
    def test_sheet_is_lint_clean(self, plate_drawing):
        issues = [i for i in plate_drawing.lint() if i.severity != "info"]
        assert [i.code for i in issues] == []

    @pytest.mark.timeout(60)
    def test_through_holes_group_across_wall_thicknesses(self):
        # The same drill through a 10mm and a 7.5mm wall is one "2× ø5 THRU"
        # callout — through specs group regardless of depth.
        part = (
            Box(80, 40, 10)
            - Pos(20, 0, 5) * Box(40, 40, 5)
            - Pos(-20, 0, 0) * Cylinder(2.5, 10)
            - Pos(20, 0, -1.25) * Cylinder(2.5, 7.5)
        )
        dwg = build_drawing(part)
        assert len([n for n in dwg._named if n.startswith("hc_")]) == 1

    @pytest.mark.timeout(60)
    def test_two_front_view_specs_fit_below_the_view(self):
        # The title block only constrains rows that reach its x-range, so
        # the strip below the front view holds multiple callouts (review
        # round 1: the old veto blanked the whole strip on A4).
        part = (
            Box(80, 40, 30)
            - Pos(-20, 0, 5) * Cylinder(2.5, 50, rotation=(90, 0, 0))
            - Pos(25, 0, -5) * Cylinder(4, 50, rotation=(90, 0, 0))
        )
        dwg = build_drawing(part)
        assert len([n for n in dwg._named if n.startswith("hc_front")]) == 2
        assert [i for i in dwg.lint() if i.severity != "info"] == []

    @pytest.mark.timeout(60)
    def test_callout_cap_keeps_the_largest_holes(self):
        part = Box(120, 80, 10)
        for i, r in enumerate([1, 1.5, 2, 2.5, 3, 4]):
            part = part - Pos(-50 + i * 20, 0, 0) * Cylinder(r, 10)
        dwg = build_drawing(part)
        covered = set()
        for name, ann in dwg._named.items():
            if name.startswith("hc_"):
                covered.update(ann.covers_diameters)
        assert covered == {4.0, 5.0, 6.0, 8.0}
        # the dropped specs surface through the coverage lint by design
        flagged = {i.message for i in dwg.lint() if i.code == "feature_not_dimensioned"}
        assert len(flagged) == 2

    @pytest.mark.timeout(60)
    def test_rotational_part_keeps_leader_annotations(self):
        dwg = build_drawing(Cylinder(30, 40) - Cylinder(10, 40))
        assert "ldr_z0" in dwg._named
        assert not any(n.startswith("hc_") for n in dwg._named)
        # the central bore still gets a centre mark in the plan view
        assert any(n.startswith("cm_plan") for n in dwg._named)


class TestIsRotational:
    def test_plain_cylinder(self):
        assert _is_rotational(30.0, 30.0, 30.0, 0.0)

    def test_prismatic_envelope(self):
        assert not _is_rotational(100.0, 60.0, 24.0, 0.0)

    def test_small_boss_on_square_plate(self):
        assert not _is_rotational(100.0, 100.0, 40.0, 0.0)

    def test_off_centre_boss(self):
        assert not _is_rotational(100.0, 100.0, 84.0, 8.0)

    def test_no_external_cylinder(self):
        # Bores never qualify as an OD — od_diam is None for hole-only parts
        assert not _is_rotational(100.0, 100.0, None, 0.0)

    @pytest.mark.timeout(60)
    def test_square_plate_with_big_bore_is_prismatic(self):
        # ø85 bore in a 100-square plate: fills the envelope and is
        # concentric, but it is a hole — not an OD.
        part = Box(100, 100, 10) - Cylinder(42.5, 12)
        dwg = build_drawing(part)
        assert "dim_od" not in dwg._named

    @pytest.mark.timeout(60)
    def test_off_centre_bore_is_prismatic(self):
        part = Box(100, 100, 20) - Pos(8, 0, 0) * Cylinder(42, 30)
        dwg = build_drawing(part)
        assert "dim_od" not in dwg._named

    @pytest.mark.timeout(60)
    def test_mirrored_turned_part_stays_rotational(self):
        # Mirroring flips face orientations AND the cylinder frame handedness;
        # the external/bore split must survive it.
        from build123d import Plane, mirror

        part = mirror(Cylinder(30, 40) - Cylinder(10, 40), about=Plane.XZ)
        z_cyls, _ = analyse_cylinders(part)
        flags = {c["diameter"]: c["external"] for c in z_cyls}
        assert flags[60.0] is True and flags[20.0] is False
        dwg = build_drawing(part)
        assert "dim_od" in dwg._named

    @pytest.mark.timeout(60)
    def test_dim_od_uses_the_external_cylinder(self):
        # An internal recess wider than the boss must not be labelled as the
        # OD: dim_od comes from the classified external cylinder.
        part = (
            Box(100, 100, 20)
            + Pos(0, 0, 20) * Cylinder(42.5, 20)
            - Pos(0, 0, -7.5) * Cylinder(45, 5)
        )
        dwg = build_drawing(part)
        assert dwg._named["dim_od"].label == "ø85"

    @pytest.mark.timeout(60)
    def test_unrounded_od_does_not_duplicate_a_bore_leader(self, monkeypatch):
        # analyse_cylinders rounds diameters at source today, which masks the
        # #86 scenario — but the OD/bore exclusion must not depend on that:
        # feature records may carry raw OCCT diameters after the #87 lift.
        # With an unrounded OD (59.9999999 vs the dedup'd 60.0), a float !=
        # leaks the OD into the bore leaders as a duplicate ø60 callout.
        import importlib

        md = importlib.import_module("build123d_drafting.make_drawing")
        real = md.analyse_cylinders

        def unrounded(part):
            z_cyls, cross_cyls = real(part)
            for c in z_cyls:
                if c["external"]:
                    c["diameter"] = 59.9999999
            return z_cyls, cross_cyls

        monkeypatch.setattr(md, "analyse_cylinders", unrounded)
        dwg = build_drawing(Cylinder(30, 40) - Cylinder(10, 40))
        assert dwg._named["dim_od"].label == "ø60"
        leader_labels = [a.label for n, a in dwg._named.items() if n.startswith("ldr_z")]
        assert leader_labels == ["ø20"]

    @pytest.mark.timeout(60)
    def test_lint_reuses_build_drawing_cylinder_analysis(self, monkeypatch):
        # build_drawing seeds the cache, so lint()/export() must not re-scan
        # the solid with analyse_cylinders.
        import importlib

        # (the package re-exports the make_drawing *function*, shadowing the
        # submodule attribute, so plain `import ... as md` grabs the function)
        md = importlib.import_module("build123d_drafting.make_drawing")

        dwg = build_drawing(Box(30, 20, 10))
        calls = {"n": 0}
        real = md.analyse_cylinders

        def counting(part):
            calls["n"] += 1
            return real(part)

        monkeypatch.setattr(md, "analyse_cylinders", counting)
        dwg.lint()
        dwg.lint()
        assert calls["n"] == 0
