"""Tests for build123d_drafting (0.2.0 — native BaseSketchObject API)."""

import math

import pytest
from build123d import Color, Draft, ExportSVG, Sketch

from build123d_drafting import (
    Centerline,
    CompositeFeatureControlFrame,
    DatumFeature,
    DatumTarget,
    Dimension,
    FeatureControlFrame,
    HoleCallout,
    Leader,
    SafeDimension,
    SurfaceFinish,
    TitleBlock,
    annotate,
    clear_page,
    draft_preset,
    find_interferences,
    find_overlaps,
    format_drawing_scale,
    leader_offset,
    lint_drawing,
    place_dims,
    place_labels,
    set_page,
    view_axes,
)
from build123d_drafting.helpers import _GDT_GLYPHS


@pytest.fixture
def draft():
    return Draft(font_size=2.5, decimal_precision=1)


def _export_ink(obj):
    """Export one annotation onto a single ink layer and assert it renders real,
    filled geometry.

    A clean export is NOT enough: the trace-fuse bug that silently dropped the
    DatumTarget ring produced *zero-area* faces that still export without error
    but fill nothing. So we also assert every face has real area — a degenerate
    face means strokes collapsed and the symbol renders blank.
    """
    exp = ExportSVG()
    exp.add_layer("ink", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0))
    exp.add_shape(obj, layer="ink")
    faces = obj.faces()
    assert faces, f"{type(obj).__name__} produced no faces"
    degenerate = [f for f in faces if f.area <= 1e-6]
    assert not degenerate, (
        f"{type(obj).__name__}: {len(degenerate)}/{len(faces)} faces are zero-area "
        f"— strokes collapsed, nothing fills"
    )
    return exp


# ---------------------------------------------------------------------------
# Dimension
# ---------------------------------------------------------------------------


class TestDimension:
    def test_is_a_sketch(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert isinstance(d, Sketch)
        assert len(d.faces()) > 0

    def test_above_places_dim_in_positive_y(self, draft):
        bb = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20").bounding_box()
        assert bb.min.Y > 0

    def test_below_places_dim_in_negative_y(self, draft):
        bb = Dimension((-10, 0, 0), (10, 0, 0), "below", 8, draft, label="20").bounding_box()
        assert bb.max.Y < 0

    def test_right_places_dim_in_positive_x(self, draft):
        bb = Dimension((0, -10, 0), (0, 10, 0), "right", 8, draft, label="20").bounding_box()
        assert bb.min.X > 0

    def test_left_places_dim_in_negative_x(self, draft):
        bb = Dimension((0, -10, 0), (0, 10, 0), "left", 8, draft, label="20").bounding_box()
        assert bb.max.X < 0

    def test_vector_side_equivalent_to_named(self, draft):
        named = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        vec = Dimension((-10, 0, 0), (10, 0, 0), (0, 1, 0), 8, draft, label="20")
        assert named.bounding_box().min.Y > 0
        assert vec.bounding_box().min.Y > 0

    def test_measured_length_correct(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert abs(d.measured_length - 20.0) < 0.01

    def test_label_set(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20 mm")
        assert d.label == "20 mm"

    def test_auto_label_when_none(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft)
        assert d.label != ""

    def test_tolerance_accepted(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, tolerance=0.1)
        assert len(d.faces()) > 0

    def test_renders_on_single_ink_layer(self, draft):
        # flood-guard: the whole annotation is faces on one layer, no .lines/.text split
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8")
        _export_ink(d)  # must not raise

    def test_label_offset_x_shifts_label(self, draft):
        d = Dimension(
            (-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8", label_offset_x=10
        )
        assert d.label_bbox is not None
        lmin_x, *_ = d.label_bbox
        assert lmin_x > 0.0

    def test_centerline_overlap_flagged(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8")
        cl = Centerline((0, 0, 0), (0, 20, 0))
        issues = lint_drawing([d, cl])
        assert any("centerline" in i.message.lower() for i in issues)

    def test_centerline_no_overlap_with_offset(self, draft):
        d = Dimension(
            (-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8", label_offset_x=15
        )
        cl = Centerline((0, 0, 0), (0, 20, 0))
        issues = [i for i in lint_drawing([d, cl]) if "centerline" in i.message.lower()]
        assert issues == []

    def test_short_path_does_not_crash(self, draft):
        d = Dimension((-2, 0, 0), (2, 0, 0), "above", 5, draft, label="4")
        assert isinstance(d, Sketch)
        assert d.label == "4"

    def test_short_path_label_bbox_set(self, draft):
        d = Dimension((-2, 0, 0), (2, 0, 0), "above", 5, draft, label="4")
        assert d.label_bbox is not None


# ---------------------------------------------------------------------------
# place_dims
# ---------------------------------------------------------------------------


class TestPlaceDims:
    def test_single_dim_gets_base_distance(self, draft):
        results = place_dims([((-10, 0, 0), (10, 0, 0), "above", "20")], draft, base_distance=8.0)
        assert len(results) == 1
        assert results[0].bounding_box().max.Y > 8.0

    def test_overlapping_dims_on_different_tiers(self, draft):
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        assert results[1].bounding_box().max.Y > results[0].bounding_box().max.Y + 3.0

    def test_non_overlapping_dims_share_tier(self, draft):
        specs = [
            ((-30, 0, 0), (-10, 0, 0), "above", "20"),
            ((10, 0, 0), (30, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        assert abs(results[0].bounding_box().max.Y - results[1].bounding_box().max.Y) < 1.0

    def test_three_overlapping_dims_three_tiers(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", "20")] * 3
        results = place_dims(specs, draft, base_distance=8.0)
        max_ys = sorted(r.bounding_box().max.Y for r in results)
        assert max_ys[1] > max_ys[0] + 3.0
        assert max_ys[2] > max_ys[1] + 3.0

    def test_stacked_result_passes_lint(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", "20")] * 2
        results = place_dims(specs, draft, base_distance=8.0)
        overlap = [i for i in lint_drawing(results) if "overlap" in i.message.lower()]
        assert overlap == []

    def test_tolerance_accepted(self, draft):
        results = place_dims([((-10, 0, 0), (10, 0, 0), "above", "20", 0.1)], draft)
        assert results[0].label.startswith("20")

    def test_returns_dimensions(self, draft):
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((10, 0, 0), (30, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft)
        assert all(isinstance(r, Dimension) for r in results)


# ---------------------------------------------------------------------------
# place_labels
# ---------------------------------------------------------------------------


class TestPlaceLabels:
    def test_no_centerlines_unchanged(self, draft):
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "20")], draft, centerlines=[])
        assert len(results) == 1
        assert isinstance(results[0], Dimension)
        assert results[0].label_bbox is not None

    def test_clears_vertical_centerline(self, draft):
        cl = Centerline((0, 0, 0), (0, 20, 0))
        results = place_labels(
            [((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")], draft, centerlines=[cl]
        )
        lmin_x, _, lmax_x, _ = results[0].label_bbox
        assert not (lmin_x < 0.0 < lmax_x)

    def test_cleared_dim_passes_lint(self, draft):
        cl = Centerline((0, 0, 0), (0, 20, 0))
        results = place_labels(
            [((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")], draft, centerlines=[cl]
        )
        issues = [i for i in lint_drawing(results + [cl]) if "centerline" in i.message.lower()]
        assert issues == []

    def test_no_shift_when_no_crossing(self, draft):
        cl = Centerline((50, 0, 0), (50, 20, 0))
        results = place_labels(
            [((-10, 0, 0), (10, 0, 0), "above", 8, "20")], draft, centerlines=[cl]
        )
        original = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert abs(results[0].label_bbox[0] - original.label_bbox[0]) < 0.5

    def test_multiple_specs(self, draft):
        cl = Centerline((0, 0, 0), (0, 30, 0))
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", 8, "20"),
            ((-15, 0, 0), (15, 0, 0), "above", 18, "30"),
        ]
        results = place_labels(specs, draft, centerlines=[cl])
        assert len(results) == 2
        for dim in results:
            lmin_x, _, lmax_x, _ = dim.label_bbox
            assert not (lmin_x < 0.0 < lmax_x)

    def test_tolerance_spec_accepted(self, draft):
        results = place_labels(
            [((-10, 0, 0), (10, 0, 0), "above", 8, "20", 0.1)], draft, centerlines=[]
        )
        assert results[0].label.startswith("20")


# ---------------------------------------------------------------------------
# SafeDimension
# ---------------------------------------------------------------------------


class TestSafeDimension:
    def test_normal_label_works(self, draft):
        d = SafeDimension([(0, 0, 0), (20, 0, 0)], "20", draft)
        assert isinstance(d, Sketch)
        assert d.measured_length == pytest.approx(20.0, abs=0.01)

    def test_long_label_does_not_raise(self, draft):
        long_label = "this label is extremely long and should cause a crash in plain DimensionLine"
        d = SafeDimension([(0, 0, 0), (3, 0, 0)], long_label, draft)
        assert isinstance(d, Sketch)
        assert len(d.faces()) > 0

    def test_fallback_label_used(self, draft):
        long_label = "X" * 80
        d = SafeDimension([(0, 0, 0), (3, 0, 0)], long_label, draft, fallback_label="~")
        assert d.label in ("~", long_label)

    def test_measured_length_from_path(self, draft):
        d = SafeDimension([(0, 0, 0), (15, 0, 0)], "15", draft)
        assert d.measured_length == pytest.approx(15.0, abs=0.01)


# ---------------------------------------------------------------------------
# Leader
# ---------------------------------------------------------------------------


class TestLeader:
    def test_is_a_sketch(self, draft):
        ld = Leader((5, 5, 0), (15, 10, 0), "⌀7.93 H7", draft)
        assert isinstance(ld, Sketch)
        assert len(ld.faces()) > 0

    def test_text_does_not_overlap_elbow(self, draft):
        elbow = (15, 10)
        ld = Leader((5, 5, 0), elbow, "⌀7.93 H7", draft)
        bx = ld.label_bbox
        assert not (bx[0] <= elbow[0] <= bx[2] and bx[1] <= elbow[1] <= bx[3])

    def test_text_placed_right_of_elbow_when_elbow_right_of_tip(self, draft):
        ld = Leader((0, 0, 0), (10, 5, 0), "label", draft)
        assert ld.label_bbox[0] > 10 - 0.1

    def test_text_placed_left_of_elbow_when_elbow_left_of_tip(self, draft):
        ld = Leader((20, 0, 0), (10, 5, 0), "label", draft)
        assert ld.label_bbox[2] < 10 + 0.1

    def test_label_set(self, draft):
        ld = Leader((0, 0, 0), (10, 5, 0), "Ra 1.6", draft)
        assert ld.label == "Ra 1.6"

    def test_renders_on_single_ink_layer(self, draft):
        ld = Leader((5, 5, 0), (15, 10, 0), "⌀7.93 H7", draft)
        _export_ink(ld)  # must not raise

    def test_lines_do_not_extend_into_text(self, draft):
        # Regression #120: shelf must stop before text starts. Compare leader
        # segments (structural lines) against the label bbox.
        ld = Leader((0, 0, 0), (20, 0, 0), "⌀8.00 H7", draft)
        line_max_x = max(max(p[0], q[0]) for p, q in ld.segments)
        assert line_max_x <= ld.label_bbox[0] + 0.5

    def test_lines_do_not_extend_into_text_left_going(self, draft):
        ld = Leader((30, 0, 0), (10, 0, 0), "⌀8.00 H7", draft)
        line_min_x = min(min(p[0], q[0]) for p, q in ld.segments)
        assert line_min_x >= ld.label_bbox[2] - 0.5

    # text_side override (#64)

    def test_vertical_leader_defaults_text_right(self, draft):
        # elbow directly above tip: auto places text to the right
        ld = Leader((10, 0, 0), (10, 20, 0), "label", draft)
        assert ld.label_bbox[0] > 10 - 0.1

    def test_text_side_left_overrides_auto(self, draft):
        # rightward leader forced left: label ends left of the elbow
        ld = Leader((0, 0, 0), (10, 5, 0), "label", draft, text_side="left")
        assert ld.label_bbox[2] < 10 + 0.1

    def test_text_side_right_overrides_auto(self, draft):
        # leftward leader forced right: label starts right of the elbow
        ld = Leader((20, 0, 0), (10, 5, 0), "label", draft, text_side="right")
        assert ld.label_bbox[0] > 10 - 0.1

    def test_text_side_auto_matches_default(self, draft):
        a = Leader((0, 0, 0), (10, 5, 0), "label", draft)
        b = Leader((0, 0, 0), (10, 5, 0), "label", draft, text_side="auto")
        assert a.label_bbox == pytest.approx(b.label_bbox)

    def test_text_side_forced_line_stops_before_text(self, draft):
        # Regression #120 invariant must hold for the forced side too:
        # vertical leader forced left — shelf stops before the text begins.
        ld = Leader((10, 0, 0), (10, 20, 0), "⌀8.00 H7", draft, text_side="left")
        line_min_x = min(min(p[0], q[0]) for p, q in ld.segments)
        assert line_min_x >= ld.label_bbox[2] - 0.5

    def test_text_side_invalid_raises(self, draft):
        with pytest.raises(ValueError, match="text_side"):
            Leader((0, 0, 0), (10, 5, 0), "label", draft, text_side="up")

    def test_text_side_through_text_raises(self, draft):
        # Horizontal leader with the label forced back toward the tip: the
        # shaft would strike through the text — refuse at construction.
        with pytest.raises(ValueError, match="through the label text"):
            Leader((0, 0, 0), (20, 0, 0), "⌀8.00 H7", draft, text_side="left")

    def test_text_side_shallow_leader_through_text_raises(self, draft):
        with pytest.raises(ValueError, match="through the label text"):
            Leader((0, 0, 0), (20, 2, 0), "⌀8.00 H7", draft, text_side="left")

    def test_text_side_steep_leader_forced_side_ok(self, draft):
        # Steep leader: the shaft clears the label on either side — no error.
        ld = Leader((10, 0, 0), (12, 20, 0), "⌀8.00 H7", draft, text_side="left")
        assert ld.label_bbox[2] < 12 + 0.1


# ---------------------------------------------------------------------------
# leader_offset
# ---------------------------------------------------------------------------


class TestLeaderOffset:
    def test_returns_leader(self, draft):
        assert isinstance(leader_offset((10, 10), "NE", 12.0, "label", draft), Leader)

    def test_compass_string_matches_equivalent_angle(self, draft):
        a = leader_offset((10, 10), "NE", 12.0, "label", draft)
        b = leader_offset((10, 10), 45.0, 12.0, "label", draft)
        assert a.elbow == pytest.approx(b.elbow)

    def test_compass_string_case_insensitive(self, draft):
        a = leader_offset((0, 0), "ne", 10.0, "x", draft)
        b = leader_offset((0, 0), "NE", 10.0, "x", draft)
        assert a.elbow == pytest.approx(b.elbow)

    def test_east_is_positive_x(self, draft):
        ld = leader_offset((0, 0), "E", 10.0, "x", draft)
        assert ld.elbow[0] == pytest.approx(10.0)
        assert ld.elbow[1] == pytest.approx(0.0, abs=1e-9)

    def test_north_is_positive_y(self, draft):
        ld = leader_offset((0, 0), "N", 10.0, "x", draft)
        assert ld.elbow[0] == pytest.approx(0.0, abs=1e-9)
        assert ld.elbow[1] == pytest.approx(10.0)

    def test_text_side_passed_through(self, draft):
        # A north leader defaults text right; text_side="left" must reach Leader.
        ld = leader_offset((0, 0), "N", 10.0, "x", draft, text_side="left")
        assert ld.label_bbox[2] < 0 + 0.1

    def test_unknown_direction_raises(self, draft):
        with pytest.raises(ValueError):
            leader_offset((0, 0), "XX", 10.0, "x", draft)


# ---------------------------------------------------------------------------
# view_axes
# ---------------------------------------------------------------------------


class TestViewAxes:
    def test_top_view_x_maps_to_page_x_positive(self):
        assert view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))["world_X"] == ("page_X", 1.0)

    def test_top_view_y_maps_to_page_y_positive(self):
        assert view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))["world_Y"] == ("page_Y", 1.0)

    def test_top_view_z_is_depth(self):
        assert view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))["world_Z"][0] == "depth"

    def test_bottom_view_x_maps_to_page_x_negative(self):
        assert view_axes((0, 0, -100), (0, 1, 0), (0, 0, 0))["world_X"] == ("page_X", -1.0)

    def test_front_view_x_maps_to_page_x(self):
        assert view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))["world_X"] == ("page_X", 1.0)

    def test_front_view_z_maps_to_page_y(self):
        assert view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))["world_Z"] == ("page_Y", 1.0)

    def test_front_view_y_is_depth(self):
        assert view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))["world_Y"][0] == "depth"

    def test_returns_all_three_world_axes(self):
        assert set(view_axes((0, 0, 100)).keys()) == {"world_X", "world_Y", "world_Z"}


# ---------------------------------------------------------------------------
# lint_drawing
# ---------------------------------------------------------------------------


class TestSetPage:
    def setup_method(self):
        clear_page()  # reset between tests

    def teardown_method(self):
        clear_page()

    def test_set_page_returns_dict(self):
        page = set_page(297, 210, margin=10)
        assert page["width"] == 297
        assert page["height"] == 210
        assert page["min_x"] == 10 and page["max_x"] == 287
        assert page["min_y"] == 10 and page["max_y"] == 200

    def test_out_of_bounds_annotation_flagged(self, draft):
        set_page(50, 50, margin=5)  # tiny page
        # Dimension placed well outside the page
        d = Dimension((-100, -100, 0), (100, -100, 0), "below", 8, draft, label="200")
        issues = [i for i in lint_drawing([d]) if i.code == "annotation_out_of_bounds"]
        assert issues, "annotation outside page should be flagged"
        assert all(i.severity == "error" for i in issues)

    def test_in_bounds_annotation_not_flagged(self, draft):
        # Page is (0,0)-(200,150), margin 10 → drawable (10,10)-(190,140).
        # Place a dim well within those bounds.
        set_page(200, 150, margin=10)
        d = Dimension((80, 50, 0), (120, 50, 0), "above", 8, draft, label="40")
        issues = [i for i in lint_drawing([d]) if i.code == "annotation_out_of_bounds"]
        assert issues == []

    def test_explicit_page_bbox_overrides_module_state(self, draft):
        set_page(1000, 1000, margin=0)  # very large page (all in bounds)
        d = Dimension((-100, -100, 0), (100, -100, 0), "below", 8, draft, label="200")
        # Explicit tiny bbox triggers out-of-bounds despite large module-level page
        issues = [
            i
            for i in lint_drawing([d], page_bbox=(0, 0, 10, 10))
            if i.code == "annotation_out_of_bounds"
        ]
        assert issues

    def test_no_page_means_no_bounds_check(self, draft):
        clear_page()  # no page set
        d = Dimension((-100, -100, 0), (100, -100, 0), "below", 8, draft, label="200")
        issues = [i for i in lint_drawing([d]) if i.code == "annotation_out_of_bounds"]
        assert issues == []

    def test_set_page_importable_from_package(self):
        from build123d_drafting import annotate as ann
        from build123d_drafting import set_page as sp

        assert callable(sp)
        assert callable(ann)

    def test_titleblock_text_overflow_flagged(self, draft):
        # #151: a title block whose long text spills past its frame is caught by
        # the page-bounds check when the block is passed to lint_drawing as an
        # item. The page is derived from the *normal* block's own bbox plus a
        # small slack, so the assertion is self-calibrating across platforms'
        # font metrics — only the relative overflow of the long string matters.
        normal = TitleBlock("Part", "001", draft=draft)
        bb = normal.bounding_box()
        page = (bb.min.X - 2, bb.min.Y - 2, bb.max.X + 2, bb.max.Y + 2)
        clean = [
            i
            for i in lint_drawing([normal], page_bbox=page)
            if i.code == "annotation_out_of_bounds"
        ]
        assert clean == [], "a title block that fits must not be flagged"

        overflowing = TitleBlock(
            "Gear c13-10  Wall 1.1 mm  Mtg 3.2 THRU x6  10.0 SQ tube  Tol ISO 2768-m  extra notes",
            "001",
            draft=draft,
        )
        flagged = [
            i
            for i in lint_drawing([overflowing], page_bbox=page)
            if i.code == "annotation_out_of_bounds"
        ]
        assert flagged, "title block whose text spills past the page edge should be flagged"


class TestAnnotate:
    def test_annotate_no_op_on_native_objects(self, draft):
        # annotate() on a Dimension (which already has .label) should be a no-op
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        original_label = d.label
        annotate(d, "width")
        assert d.label == original_label  # unchanged

    def test_annotate_attaches_label_to_vanilla_object(self, draft):
        from build123d import ExtensionLine

        el = ExtensionLine(border=[(-10, 0, 0), (10, 0, 0)], offset=8, draft=draft, label="20")
        annotate(el, "width", label="20")
        assert getattr(el, "_annotate_label", None) == "20"


class TestLintDrawing:
    def test_empty_list_returns_no_issues(self):
        assert lint_drawing([]) == []

    def test_label_value_matches_length_no_issue(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = [
            i for i in lint_drawing([d]) if "axis swap" in i.message or "differs from" in i.message
        ]
        assert issues == []

    def test_label_value_diverges_from_length(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="35")
        issues = lint_drawing([d])
        assert any("35" in i.message or "differs from" in i.message for i in issues)
        assert any(i.severity == "warning" for i in issues)

    def test_dim_overlapping_part_flagged(self, draft):
        d = Dimension((-5, 0, 0), (5, 0, 0), "above", 1, draft, label="10")

        class FakeBBox:
            class _pt:
                pass

            min = _pt()
            min.X = -20
            min.Y = -20
            max = _pt()
            max.X = 20
            max.Y = 20

        issues = lint_drawing([d], part_bbox=FakeBBox())
        assert any("overlap" in i.message.lower() for i in issues)

    def test_leader_elbow_outside_text_no_issue(self, draft):
        ld = Leader((0, 0, 0), (20, 10, 0), "label", draft)
        assert [i for i in lint_drawing([ld]) if "Leader" in i.message] == []

    def test_mixed_items_checked(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        ld = Leader((50, 0, 0), (70, 10, 0), "Ra 1.6", draft)
        assert lint_drawing([d, ld]) == []

    def test_overlapping_dims_flagged(self, draft):
        a = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        b = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert any("overlap" in i.message.lower() for i in lint_drawing([a, b]))

    def test_stacked_dims_not_flagged(self, draft):
        inner = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        outer = Dimension((-10, 0, 0), (10, 0, 0), "above", 18, draft, label="20")
        assert [i for i in lint_drawing([inner, outer]) if "overlap" in i.message.lower()] == []

    def test_duck_typed_namespace_dim(self, draft):
        # lint must work on a lightweight SimpleNamespace stand-in (the MCP uses these)
        from types import SimpleNamespace

        ns = SimpleNamespace(label="999", measured_length=20.0, label_bbox=None)
        codes = {i.code for i in lint_drawing([ns])}
        assert "label_vs_measured" in codes


# ---------------------------------------------------------------------------
# drawing_scale (issue #147): N:1 drawings without false label_vs_measured
# ---------------------------------------------------------------------------


class TestDrawingScale:
    def test_format_enlargement(self):
        assert format_drawing_scale(5.0) == "5:1"
        assert format_drawing_scale(2.5) == "2.5:1"

    def test_format_unity(self):
        assert format_drawing_scale(1.0) == "1:1"

    def test_format_reduction(self):
        assert format_drawing_scale(0.5) == "1:2"
        assert format_drawing_scale(0.1) == "1:10"

    def test_format_rejects_non_positive(self):
        with pytest.raises(ValueError):
            format_drawing_scale(0.0)
        with pytest.raises(ValueError):
            format_drawing_scale(-2.0)

    def test_lint_rejects_non_positive_scale(self, draft):
        # lint_drawing must reject a bad scale loudly (same contract as
        # format_drawing_scale / TitleBlock), not silently skip the division.
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="10")
        with pytest.raises(ValueError):
            lint_drawing([d], drawing_scale=0.0)
        with pytest.raises(ValueError):
            lint_drawing([d], drawing_scale=-5.0)

    def test_scaled_dim_with_real_label_passes(self, draft):
        # 20 mm of geometry drawn at 2:1 represents a real 10 mm feature.
        # The label carries the real value; lint must accept it.
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="10")
        codes = {i.code for i in lint_drawing([d], drawing_scale=2.0)}
        assert "label_vs_measured" not in codes

    def test_scaled_dim_with_unscaled_label_flagged(self, draft):
        # labelling the *measured* 20 mm instead of the real 10 mm is the
        # mistake the check should still catch at 2:1.
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        codes = {i.code for i in lint_drawing([d], drawing_scale=2.0)}
        assert "label_vs_measured" in codes

    def test_default_scale_unchanged(self, draft):
        # drawing_scale defaults to 1.0 → identical to the pre-existing behaviour.
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert "label_vs_measured" not in {i.code for i in lint_drawing([d])}
        bad = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="999")
        assert "label_vs_measured" in {i.code for i in lint_drawing([bad])}


# ---------------------------------------------------------------------------
# TitleBlock
# ---------------------------------------------------------------------------


class TestTitleBlock:
    def test_is_a_sketch(self, draft):
        tb = TitleBlock("My Part", "DRW-001", draft=draft)
        assert isinstance(tb, Sketch)
        assert len(tb.faces()) > 0

    def test_label_is_part_name(self, draft):
        assert TitleBlock("My Part", "DRW-001", draft=draft).label == "My Part"

    def test_block_bbox_correct_dimensions(self, draft):
        tb = TitleBlock("Part", "001", width=170, cell_height=8, draft=draft)
        assert tb.block_bbox["width"] == pytest.approx(170.0)
        assert tb.block_bbox["height"] == pytest.approx(16.0)
        assert tb.block_bbox["min_x"] == pytest.approx(0.0)
        assert tb.block_bbox["min_y"] == pytest.approx(0.0)

    def test_custom_width_respected(self, draft):
        tb = TitleBlock("Part", "001", width=210, draft=draft)
        assert tb.block_bbox["width"] == pytest.approx(210.0)
        assert tb.block_bbox["max_x"] == pytest.approx(210.0)

    def test_default_draft_used_when_none(self):
        assert isinstance(TitleBlock("Part", "DRW-001"), Sketch)

    def test_optional_fields_empty_string(self, draft):
        tb = TitleBlock("Part", "001", draft=draft)
        assert len(tb.faces()) > 0

    def test_all_fields_populated(self, draft):
        tb = TitleBlock(
            part_name="Bracket",
            drawing_number="BRK-042",
            scale="2:1",
            material="Al 6061",
            general_tolerance="ISO 2768-m",
            designed_by="J. Smith",
            date="2026-05-19",
            draft=draft,
        )
        assert isinstance(tb, Sketch)
        # outer border (4) + row divider (1) + top dividers (4) + bottom divider (1)
        assert len(tb.segments) >= 8

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(TitleBlock("Part", "001", material="Al", date="x", draft=draft))

    @staticmethod
    def _fingerprint(tb):
        # Glyphs render as exact filled faces, so (count, total area) is a
        # deterministic, font-stable fingerprint of a title block's rendered
        # content. The border strokes are identical across variants, so any
        # difference comes purely from the scale-cell text.
        faces = tb.faces()
        return (len(faces), round(sum(f.area for f in faces), 6))

    def test_numeric_drawing_scale_derives_cell(self, draft):
        # drawing_scale=5.0 must render exactly what scale="5:1" renders — proves
        # the derived string actually reaches the scale cell, not just that
        # geometry exists.
        derived = TitleBlock("Part", "001", drawing_scale=5.0, draft=draft)
        explicit = TitleBlock("Part", "001", scale="5:1", draft=draft)
        assert self._fingerprint(derived) == self._fingerprint(explicit)
        # ...and it is not silently a no-op equal to the "1:1" default.
        default = TitleBlock("Part", "001", scale="1:1", draft=draft)
        assert self._fingerprint(derived) != self._fingerprint(default)

    def test_drawing_scale_overrides_scale_string(self, draft):
        # both given: the numeric drawing_scale wins, so the result matches the
        # equivalent "5:1" string and ignores the explicit "1:1".
        both = TitleBlock("Part", "001", scale="1:1", drawing_scale=5.0, draft=draft)
        explicit = TitleBlock("Part", "001", scale="5:1", draft=draft)
        assert self._fingerprint(both) == self._fingerprint(explicit)

    # --- ISO 7200 fields: revision and legal_owner ---

    def test_revision_renders_in_col5(self, draft):
        # revision="A" and date="A" render the same value glyph in col5.
        # Use show_labels=False so the "REV" vs "DATE" label text doesn't confound.
        with_revision = TitleBlock("Part", "001", revision="A", show_labels=False, draft=draft)
        with_date = TitleBlock("Part", "001", date="A", show_labels=False, draft=draft)
        assert self._fingerprint(with_revision) == self._fingerprint(with_date)

    def test_revision_overrides_date(self, draft):
        # When both are set, revision wins — result equals revision-only.
        # Use show_labels=False to isolate value content from label content.
        both = TitleBlock(
            "Part", "001", revision="B", date="2026-01-01", show_labels=False, draft=draft
        )
        rev_only = TitleBlock("Part", "001", revision="B", show_labels=False, draft=draft)
        assert self._fingerprint(both) == self._fingerprint(rev_only)

    def test_legal_owner_increases_height(self, draft):
        without = TitleBlock("Part", "001", cell_height=8, draft=draft)
        with_lo = TitleBlock("Part", "001", legal_owner="ACME Corp", cell_height=8, draft=draft)
        assert with_lo.block_bbox["height"] == pytest.approx(24.0)
        assert without.block_bbox["height"] == pytest.approx(16.0)

    def test_legal_owner_bbox_height_is_three_rows(self, draft):
        tb = TitleBlock("Part", "001", legal_owner="X", width=170, cell_height=8, draft=draft)
        assert tb.block_bbox["max_y"] == pytest.approx(24.0)

    def test_no_legal_owner_bbox_unchanged(self, draft):
        tb = TitleBlock("Part", "001", width=170, cell_height=8, draft=draft)
        assert tb.block_bbox["height"] == pytest.approx(16.0)

    def test_legal_owner_produces_extra_stroke(self, draft):
        # With legal_owner, one extra horizontal divider is added (row 1/2 separator).
        without = TitleBlock("Part", "001", draft=draft)
        with_lo = TitleBlock("Part", "001", legal_owner="ACME", draft=draft)
        assert len(with_lo.segments) > len(without.segments)

    def test_legal_owner_renders_as_sketch(self, draft):
        tb = TitleBlock("Part", "DRW-001", legal_owner="ACME Corp", revision="B", draft=draft)
        assert isinstance(tb, Sketch)
        assert len(tb.faces()) > 0

    def test_show_labels_adds_faces(self, draft):
        # Labels add glyph faces, so the labelled block has more faces than unlabelled.
        with_labels = TitleBlock("Part", "001", show_labels=True, draft=draft)
        without_labels = TitleBlock("Part", "001", show_labels=False, draft=draft)
        assert len(with_labels.faces()) > len(without_labels.faces())

    def test_show_labels_false_matches_legacy(self, draft):
        # show_labels=False should match the pre-0.4.0 fingerprint (no label glyphs).
        legacy = TitleBlock("Part", "001", show_labels=False, draft=draft)
        assert isinstance(legacy, Sketch)
        assert len(legacy.faces()) > 0

    def test_rev_label_differs_from_date_label(self, draft):
        # "REV" label glyph differs from "DATE" label — fingerprints must differ.
        with_rev = TitleBlock("Part", "001", revision="A", show_labels=True, draft=draft)
        with_date = TitleBlock("Part", "001", date="A", show_labels=True, draft=draft)
        assert self._fingerprint(with_rev) != self._fingerprint(with_date)

    def test_legal_owner_label_present(self, draft):
        # Block with legal_owner + labels should have more faces than without label.
        with_lo_labelled = TitleBlock("Part", "001", legal_owner="X", show_labels=True, draft=draft)
        with_lo_unlabelled = TitleBlock(
            "Part", "001", legal_owner="X", show_labels=False, draft=draft
        )
        assert len(with_lo_labelled.faces()) > len(with_lo_unlabelled.faces())

    def test_show_labels_does_not_affect_block_bbox(self, draft):
        # Labels are glyphs only — they must not change the reported block dimensions.
        with_labels = TitleBlock(
            "Part", "001", legal_owner="X", cell_height=8, show_labels=True, draft=draft
        )
        without_labels = TitleBlock(
            "Part", "001", legal_owner="X", cell_height=8, show_labels=False, draft=draft
        )
        assert with_labels.block_bbox == without_labels.block_bbox

    def test_whitespace_only_legal_owner_treated_as_empty(self, draft):
        # "   " is truthy in Python but must not create a spurious third row.
        tb_spaces = TitleBlock("Part", "001", legal_owner="   ", cell_height=8, draft=draft)
        tb_empty = TitleBlock("Part", "001", legal_owner="", cell_height=8, draft=draft)
        assert tb_spaces.block_bbox["height"] == pytest.approx(tb_empty.block_bbox["height"])
        assert tb_spaces.block_bbox["height"] == pytest.approx(16.0)

    def test_labels_suppressed_when_cell_too_short(self, draft):
        # At cell_height=4 the label text would overlap content text; _label_fits
        # should suppress all labels, so the face count equals show_labels=False.
        small = TitleBlock("Part", "001", cell_height=4, show_labels=True, draft=draft)
        no_labels = TitleBlock("Part", "001", cell_height=4, show_labels=False, draft=draft)
        assert len(small.faces()) == len(no_labels.faces())

    def test_labels_shown_at_standard_cell_height(self, draft):
        # At the default cell_height=8 labels fit without overlap; they must appear.
        with_labels = TitleBlock("Part", "001", cell_height=8, show_labels=True, draft=draft)
        without_labels = TitleBlock("Part", "001", cell_height=8, show_labels=False, draft=draft)
        assert len(with_labels.faces()) > len(without_labels.faces())


# ---------------------------------------------------------------------------
# SurfaceFinish
# ---------------------------------------------------------------------------


class TestSurfaceFinish:
    def test_is_a_sketch(self, draft):
        assert isinstance(SurfaceFinish("Ra 1.6", (10, 20), draft=draft), Sketch)

    def test_label_preserved(self, draft):
        assert SurfaceFinish("Ra 3.2", (0, 0), draft=draft).label == "Ra 3.2"

    def test_position_stored(self, draft):
        assert SurfaceFinish("Ra 1.6", (15.0, 25.0), draft=draft).mark_position == pytest.approx(
            (15.0, 25.0)
        )

    def test_three_stroke_segments(self, draft):
        # leg1 (diagonal), leg2 (vertical), shelf (horizontal)
        assert len(SurfaceFinish("Ra 1.6", (0, 0), draft=draft).segments) == 3

    def test_tip_is_at_position(self, draft):
        # The tip sits at the position; the thin-faced strokes extend at most
        # half a line width (~0.075 mm) beyond it.
        px, py = 10.0, 20.0
        bb = SurfaceFinish("Ra 1.6", (px, py), draft=draft).bounding_box()
        assert bb.min.Y == pytest.approx(py, abs=0.1)
        assert bb.min.X == pytest.approx(px, abs=0.1)

    def test_rotation_changes_bbox(self, draft):
        bb0 = SurfaceFinish("Ra 1.6", (0, 0), angle=0.0, draft=draft).bounding_box()
        bb90 = SurfaceFinish("Ra 1.6", (0, 0), angle=90.0, draft=draft).bounding_box()
        assert abs(bb0.size.X - bb90.size.X) > 0.5

    def test_label_bbox_is_text_extents(self, draft):
        # The Ra text sits up-right of the tip — its bbox must exclude the
        # tip, which is what lets lint test the text rather than the mark (#69)
        lb = SurfaceFinish("Ra 1.6", (10, 20), draft=draft).label_bbox
        assert lb is not None
        assert lb[0] > 10  # text starts right of the tip
        assert lb[1] > 20  # and above it

    def test_label_bbox_follows_angle(self, draft):
        # angle is baked into the build frame, so label_bbox must track it
        lb0 = SurfaceFinish("Ra 1.6", (0, 0), angle=0.0, draft=draft).label_bbox
        lb90 = SurfaceFinish("Ra 1.6", (0, 0), angle=90.0, draft=draft).label_bbox
        assert lb90[0] != pytest.approx(lb0[0], abs=0.5)

    def test_default_draft_used_when_none(self):
        assert isinstance(SurfaceFinish("Ra 1.6", (0, 0)), Sketch)

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(SurfaceFinish("Ra 1.6", (5, 5), draft=draft))

    def test_ra_value_sits_above_shelf(self, draft):
        # ISO 1302: the Ra value must sit ABOVE the lead-out shelf line.
        # The bottom of the text area (label_bbox[1]) must be >= the tip Y.
        sf = SurfaceFinish("Ra 1.6", (0, 0), draft=draft)
        tip_y = sf.mark_position[1]
        # The annotation extends upward from the tip; the label occupies the
        # upper portion.  Verify the text-area min-Y is above the tip.
        bb = sf.bounding_box()
        assert bb.max.Y > tip_y + draft.font_size * 0.5, (
            "Ra text should extend above the shelf/tip; it may be below the shelf"
        )


# ---------------------------------------------------------------------------
# FeatureControlFrame
# ---------------------------------------------------------------------------


class TestFeatureControlFrame:
    def test_is_a_sketch(self, draft):
        assert isinstance(FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft), Sketch)

    def test_all_14_characteristics_draw(self, draft):
        for c in _GDT_GLYPHS:
            fcf = FeatureControlFrame(c, 0.1, ("A",), draft)
            # Threshold: frame (4 edges → faces) + characteristic glyph + tolerance
            # text + datum letter. Every characteristic needs at least 6 faces so
            # a glyph that loses all its strokes (silently collapses) is detected.
            assert len(fcf.faces()) >= 6, (
                f"{c} produced only {len(fcf.faces())} faces — glyph may have collapsed"
            )
            _export_ink(fcf)  # every characteristic must export cleanly (no flood)

    def test_bottom_left_at_origin(self, draft):
        bb = FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).bounding_box()
        assert bb.min.X == pytest.approx(0.0, abs=0.1)
        assert bb.min.Y == pytest.approx(0.0, abs=0.1)

    def test_datums_stored(self, draft):
        assert FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).datums == (
            "A",
            "B",
            "C",
        )

    def test_float_tolerance_formatted_to_precision(self, draft):
        assert FeatureControlFrame("position", 0.5, (), draft).tolerance_str == "0.5"

    def test_string_tolerance_passed_through(self, draft):
        assert FeatureControlFrame("position", "0.05", (), draft).tolerance_str == "0.05"

    def test_more_datums_widens_frame(self, draft):
        one = FeatureControlFrame("position", 0.5, ("A",), draft).bounding_box().size.X
        three = FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).bounding_box().size.X
        assert three > one

    def test_diameter_widens_tolerance_compartment(self, draft):
        plain = FeatureControlFrame("position", 0.5, ("A",), draft).bounding_box().size.X
        dia = (
            FeatureControlFrame("position", 0.5, ("A",), draft, diameter=True).bounding_box().size.X
        )
        assert dia > plain

    def test_modifier_widens_tolerance_compartment(self, draft):
        plain = FeatureControlFrame("position", 0.5, ("A",), draft).bounding_box().size.X
        mmc = (
            FeatureControlFrame("position", 0.5, ("A",), draft, modifier="M").bounding_box().size.X
        )
        assert mmc > plain

    def test_label_is_tolerance(self, draft):
        assert FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).label == "0.5"

    def test_unknown_characteristic_raises(self, draft):
        with pytest.raises(ValueError):
            FeatureControlFrame("bogus", 0.1, (), draft)

    def test_unknown_modifier_raises(self, draft):
        with pytest.raises(ValueError):
            FeatureControlFrame("position", 0.1, (), draft, modifier="Z")

    def test_default_draft_used_when_none(self):
        assert isinstance(FeatureControlFrame("position", 0.5, ("A",)), Sketch)

    def test_no_datums_allowed(self, draft):
        assert FeatureControlFrame("flatness", 0.1, (), draft).datums == ()

    def test_datum_modifier_adds_geometry(self, draft):
        plain = FeatureControlFrame("position", 0.5, ("A",), draft)
        modded = FeatureControlFrame("position", 0.5, ("A",), draft, datum_modifiers={"A": "M"})
        assert len(modded.faces()) > len(plain.faces())


# ---------------------------------------------------------------------------
# DatumFeature
# ---------------------------------------------------------------------------


class TestDatumFeature:
    def test_is_a_sketch(self, draft):
        assert isinstance(DatumFeature("A", draft), Sketch)

    def test_letter_stored(self, draft):
        assert DatumFeature("B", draft).letter == "B"

    def test_tip_at_origin(self, draft):
        assert DatumFeature("A", draft).bounding_box().min.Y == pytest.approx(0.0, abs=0.1)

    def test_filled_has_solid_triangle(self, draft):
        # filled=True: triangle is a solid Face (large area).
        # filled=False: triangle is three thin stroke-faces (each small area).
        filled = DatumFeature("A", draft, filled=True)
        outline = DatumFeature("A", draft, filled=False)
        # The filled variant should have at least one face whose area is
        # significantly larger than any stroke face (i.e. the solid triangle).
        filled_max_area = max(f.area for f in filled.faces())
        outline_max_area = max(f.area for f in outline.faces())
        # At font_size=2.5 the solid triangle (~5.5 mm²) is ~5.5× the largest
        # stroke face (~1.0 mm², from the datum box).  Use 5× as a tight lower
        # bound — still catches a degraded fill that produces only strokes.
        assert filled_max_area > outline_max_area * 5, (
            "filled=True should produce a large solid triangle face "
            f"(max area {filled_max_area:.2f}) larger than outline's "
            f"max stroke area ({outline_max_area:.2f})"
        )

    def test_outline_triangle_has_only_thin_strokes(self, draft):
        # ISO 5459 outline mode: the triangle must stroke (thin faces from trace).
        # All faces should be thin (small area) — no solid polygon.
        outline = DatumFeature("A", draft, filled=False)
        areas = sorted(f.area for f in outline.faces())
        # The largest face in outline mode should be a thin stroke.
        # At font_size=2.5, line_width=0.1: stroke area ≈ 0.5 mm²; use 1.5×
        # headroom. Expressed as a multiple of font_size² so the threshold
        # scales if the fixture ever changes.
        draft_area_unit = draft.font_size * draft.font_size
        assert areas[-1] < draft_area_unit * 0.25, (
            f"outline=False has a large face (area {areas[-1]:.2f} mm²) — "
            "the triangle may be rendering as a solid"
        )

    def test_default_draft_used_when_none(self):
        assert isinstance(DatumFeature("A"), Sketch)

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(DatumFeature("A", draft))

    def test_label_bbox_is_letter_frame(self, draft):
        # The label bbox is the framed datum letter — above the triangle and
        # connector, never including the tip at the origin (#69)
        lb = DatumFeature("A", draft).label_bbox
        assert lb is not None
        box = 2.0 * draft.font_size
        assert lb[1] > 0  # frame sits above the triangle tip
        assert lb[2] - lb[0] == pytest.approx(box)
        assert lb[3] - lb[1] == pytest.approx(box)

    def test_label_bbox_tracks_moved(self, draft):
        from build123d import Location

        df = DatumFeature("A", draft)
        lb0 = df.label_bbox
        lb = df.moved(Location((30, 7, 0))).label_bbox
        assert lb[0] == pytest.approx(lb0[0] + 30)
        assert lb[1] == pytest.approx(lb0[1] + 7)


class TestDatumTarget:
    def test_is_a_sketch(self, draft):
        assert isinstance(DatumTarget("A1", draft=draft), Sketch)

    def test_identifier_stored(self, draft):
        assert DatumTarget("B2", draft=draft).identifier == "B2"

    def test_circle_centred_on_origin(self, draft):
        # The divider runs across the circle through y=0 from -r..r, so its
        # midpoint is the circle centre at the origin.
        dt = DatumTarget("A1", draft=draft)
        divider = next(s for s in dt.segments if abs(s[0][1]) < 0.01 and abs(s[1][1]) < 0.01)
        cx = (divider[0][0] + divider[1][0]) / 2.0
        assert cx == pytest.approx(0.0, abs=0.1)

    def test_area_label_adds_geometry(self, draft):
        without = DatumTarget("A1", draft=draft)
        withlab = DatumTarget("A1", area_label="⌀6", draft=draft)
        assert len(withlab.faces()) > len(without.faces())
        assert withlab.area_label == "⌀6"

    def test_no_area_label_is_blank(self, draft):
        assert DatumTarget("A1", draft=draft).area_label == ""

    def test_divider_present(self, draft):
        # one horizontal divider segment through the circle centre
        dt = DatumTarget("A1", draft=draft)
        horizontals = [s for s in dt.segments if abs(s[0][1]) < 0.01 and abs(s[1][1]) < 0.01]
        assert len(horizontals) >= 1

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(DatumTarget("A1", area_label="⌀6", draft=draft))

    def test_default_draft_used_when_none(self):
        assert isinstance(DatumTarget("A1"), Sketch)


class TestBasicDimension:
    def test_basic_flag_sets_is_basic(self, draft):
        assert Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True).is_basic is True

    def test_default_is_not_basic(self, draft):
        assert Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft).is_basic is False

    def test_basic_box_adds_segments_around_label(self, draft):
        plain = Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft)
        boxed = Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        # four box strokes added to the structural segments
        assert len(boxed.segments) >= len(plain.segments) + 4

    def test_basic_box_encloses_label(self, draft):
        res = Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        x0, y0, x1, y1 = res.label_bbox
        # the four box strokes lie on the label bbox rectangle
        on_box = [
            s
            for s in res.segments
            if math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]) == pytest.approx(x1 - x0, abs=0.05)
            or math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]) == pytest.approx(y1 - y0, abs=0.05)
        ]
        assert len(on_box) >= 4

    def test_basic_box_strokes_not_floods(self, draft):
        # the basic box must render cleanly on one ink layer (no closed-wire flood)
        _export_ink(Dimension((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True))

    def test_basic_vertical_dim(self, draft):
        res = Dimension((0, 0, 0), (0, 20, 0), "left", 8, draft, basic=True)
        assert res.is_basic is True
        assert len(res.segments) >= 4


class TestCompositeFeatureControlFrame:
    def _rows(self):
        return [
            {"tolerance": 0.25, "datums": ("A", "B", "C"), "diameter": True},
            {"tolerance": 0.1, "datums": ("A",), "diameter": True},
        ]

    def test_is_a_sketch(self, draft):
        assert isinstance(CompositeFeatureControlFrame("position", self._rows(), draft), Sketch)

    def test_two_rows_height_is_two_frames(self, draft):
        bb = CompositeFeatureControlFrame("position", self._rows(), draft).bounding_box()
        assert bb.size.Y == pytest.approx(2 * (2 * draft.font_size), abs=0.3)

    def test_bottom_left_at_origin(self, draft):
        bb = CompositeFeatureControlFrame("position", self._rows(), draft).bounding_box()
        assert bb.min.X == pytest.approx(0.0, abs=0.1)
        assert bb.min.Y == pytest.approx(0.0, abs=0.1)

    def test_tolerances_recorded_top_to_bottom(self, draft):
        res = CompositeFeatureControlFrame("position", self._rows(), draft)
        assert res.tolerances == ("0.2", "0.1")  # precision 1 rounds 0.25 -> 0.2

    def test_label_is_top_tolerance(self, draft):
        assert CompositeFeatureControlFrame("position", self._rows(), draft).label == "0.2"

    def test_single_row_allowed(self, draft):
        res = CompositeFeatureControlFrame(
            "position", [{"tolerance": 0.1, "datums": ("A",)}], draft
        )
        assert res.bounding_box().size.Y == pytest.approx(2 * draft.font_size, abs=0.3)

    def test_unknown_characteristic_raises(self, draft):
        with pytest.raises(ValueError):
            CompositeFeatureControlFrame("bogus", self._rows(), draft)

    def test_empty_rows_raises(self, draft):
        with pytest.raises(ValueError):
            CompositeFeatureControlFrame("position", [], draft)

    def test_modifier_accepted(self, draft):
        res = CompositeFeatureControlFrame(
            "position", [{"tolerance": 0.25, "datums": ("A",), "modifier": "M"}], draft
        )
        assert isinstance(res, Sketch)

    def test_default_draft_used_when_none(self):
        assert isinstance(
            CompositeFeatureControlFrame("position", [{"tolerance": 0.1, "datums": ("A",)}]), Sketch
        )

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(CompositeFeatureControlFrame("position", self._rows(), draft))


class TestHoleCallout:
    def test_is_a_sketch(self, draft):
        assert isinstance(HoleCallout(8.5, through=True, draft=draft), Sketch)

    def test_diameter_symbol_drawn(self, draft):
        # ⌀ ring contributes faces
        assert len(HoleCallout(8.5, through=True, draft=draft).faces()) >= 2

    def test_thru_renders(self, draft):
        assert len(HoleCallout(8.5, through=True, draft=draft).faces()) >= 1

    def test_count_prefix_widens(self, draft):
        a = HoleCallout(8.5, through=True, draft=draft).callout_width
        b = HoleCallout(8.5, count=4, through=True, draft=draft).callout_width
        assert b > a

    def test_counterbore_adds_geometry(self, draft):
        plain = HoleCallout(8.5, depth=20, draft=draft)
        cbore = HoleCallout(8.5, depth=20, cbore_dia=15, cbore_depth=6, draft=draft)
        assert cbore.callout_width > plain.callout_width
        assert len(cbore.faces()) > len(plain.faces())

    def test_countersink_accepted(self, draft):
        res = HoleCallout(8.5, csink_dia=15, csink_angle=90, draft=draft)
        assert isinstance(res, Sketch)
        assert res.callout_width > 0

    def test_bottom_left_origin_x(self, draft):
        bb = HoleCallout(8.5, through=True, draft=draft).bounding_box()
        assert bb.min.X == pytest.approx(0.0, abs=0.6 * draft.font_size)

    def test_default_draft_used_when_none(self):
        assert isinstance(HoleCallout(8.5, through=True), Sketch)

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(
            HoleCallout(
                8.5,
                count=4,
                depth=20,
                cbore_dia=15,
                cbore_depth=6,
                csink_dia=18,
                csink_angle=90,
                draft=draft,
            )
        )


class TestLeaderAllAround:
    def test_all_around_adds_geometry(self, draft):
        plain = Leader((0, 0, 0), (10, 10, 0), "0.2", draft)
        ring = Leader((0, 0, 0), (10, 10, 0), "0.2", draft, all_around=True)
        assert len(ring.faces()) > len(plain.faces())

    def test_all_over_more_than_all_around(self, draft):
        around = Leader((0, 0, 0), (10, 10, 0), "0.2", draft, all_around=True)
        over = Leader((0, 0, 0), (10, 10, 0), "0.2", draft, all_over=True)
        assert len(over.faces()) > len(around.faces())

    def test_ring_renders_on_single_ink_layer(self, draft):
        # the ring must stroke (open arcs) — exports cleanly with no flood
        _export_ink(Leader((0, 0, 0), (10, 10, 0), "0.2", draft, all_over=True))

    def test_ring_centred_on_elbow(self, draft):
        bb = Leader((0, 0, 0), (10, 10, 0), "0.2", draft, all_around=True).bounding_box()
        assert bb.min.X <= 10 <= bb.max.X
        assert bb.min.Y <= 10 <= bb.max.Y


class TestDraftPreset:
    def test_scales_arrow_to_font_and_thins_line(self):
        d = draft_preset(font_size=2.0)
        assert d.font_size == pytest.approx(2.0)
        assert d.arrow_length == pytest.approx(1.8)
        assert d.line_width == pytest.approx(0.1)

    def test_lighter_than_build123d_default(self):
        assert draft_preset(font_size=2.5).arrow_length < Draft().arrow_length
        assert draft_preset(font_size=2.5).line_width < Draft().line_width

    def test_overrides_win(self):
        d = draft_preset(font_size=2.0, arrow_length=5.0, line_width=0.3)
        assert d.arrow_length == pytest.approx(5.0)
        assert d.line_width == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Transform-aware lint metadata (label_bbox / segments / elbow track the geometry)
# ---------------------------------------------------------------------------


class TestTransformMetadata:
    def test_moved_shifts_label_bbox(self, draft):
        from build123d import Location, Vector

        d = Dimension((0, 0, 0), (30, 0, 0), "below", 6, draft, label="30")
        cx0 = (d.label_bbox[0] + d.label_bbox[2]) / 2.0
        m = d.moved(Location(Vector(50, 20, 0)))
        cx1 = (m.label_bbox[0] + m.label_bbox[2]) / 2.0
        cy1 = (m.label_bbox[1] + m.label_bbox[3]) / 2.0
        assert cx1 == pytest.approx(cx0 + 50, abs=0.01)
        assert cy1 == pytest.approx((d.label_bbox[1] + d.label_bbox[3]) / 2.0 + 20, abs=0.01)

    def test_moved_shifts_segments(self, draft):
        from build123d import Location, Vector

        d = Dimension((0, 0, 0), (30, 0, 0), "below", 6, draft, label="30")
        seg0 = d.segments[0]
        m = d.moved(Location(Vector(50, 0, 0)))
        seg1 = m.segments[0]
        assert seg1[0][0] == pytest.approx(seg0[0][0] + 50, abs=0.01)

    def test_moved_leader_elbow_tracks(self, draft):
        from build123d import Location, Vector

        ld = Leader((0, 0, 0), (9, 7, 0), "X", draft)
        m = ld.moved(Location(Vector(100, 0, 0)))
        assert m.elbow[0] == pytest.approx(ld.elbow[0] + 100, abs=0.01)
        # elbow stays consistent with the (also-moved) label box — lint correctness
        assert m.label_bbox[0] == pytest.approx(ld.label_bbox[0] + 100, abs=0.01)

    def test_construction_rotation_bakes_into_metadata(self, draft):
        # rotation=90 rotates the geometry; the cached label_bbox must follow it
        d = Dimension((0, 0, 0), (30, 0, 0), "below", 6, draft, label="30", rotation=90)
        bb = d.bounding_box()
        lx0, ly0, lx1, ly1 = d.label_bbox
        assert bb.min.X - 0.5 <= lx0 and lx1 <= bb.max.X + 0.5
        assert bb.min.Y - 0.5 <= ly0 and ly1 <= bb.max.Y + 0.5

    def test_moved_object_lints_correctly(self, draft):
        # a leader whose elbow sits in its label, then moved, still flags the pierce
        from build123d import Location, Vector

        # build a leader whose elbow is inside its own label region is hard; instead
        # check that moving a clean leader keeps it clean (no false positive from stale coords)
        ld = Leader((0, 0, 0), (40, 5, 0), "PART", draft).moved(Location(Vector(70, 70, 0)))
        errs = [i for i in lint_drawing([ld]) if i.severity == "error"]
        assert errs == []


# ---------------------------------------------------------------------------
# find_overlaps (pure geometry)
# ---------------------------------------------------------------------------


class TestFindOverlaps:
    def test_identical_sketches_overlap(self, draft):
        a = FeatureControlFrame("position", 0.5, ("A",), draft)
        b = FeatureControlFrame("position", 0.5, ("A",), draft)
        issues = find_overlaps([a, b])
        assert any(i.code == "faces_overlap" for i in issues)

    def test_separated_sketches_no_overlap(self, draft):
        a = HoleCallout(8.5, through=True, draft=draft)
        b = HoleCallout(8.5, through=True, draft=draft).moved(
            __import__("build123d").Pos(200, 200, 0)
        )
        assert find_overlaps([a, b]) == []

    def test_empty_is_safe(self):
        assert find_overlaps([]) == []

    def test_works_on_bare_sketch(self, draft):
        # zero metadata — just two Sketches
        a = Centerline((0, 0, 0), (10, 0, 0))
        b = Centerline((0, 0, 0), (0, 10, 0))
        # they cross at origin -> thin faces intersect
        issues = find_overlaps([a, b], min_area=0.001)
        assert isinstance(issues, list)

    def test_aabb_prefilter_skips_distant_pairs(self, draft, monkeypatch):
        # Two sketches far apart: AABB pre-filter should skip the OCC boolean
        # entirely.  Verify by patching Shape.__and__ to raise — if it were
        # called the test would fail with geometry_check_failed, not a clean [].
        import build123d.topology.shape_core as sc
        from build123d import Pos

        def _should_not_run(self, other):
            raise AssertionError("AABB filter should have skipped this pair")

        monkeypatch.setattr(sc.Shape, "__and__", _should_not_run)
        a = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        b = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20").moved(
            Pos(1000, 1000, 0)
        )
        issues = find_overlaps([a, b])
        assert issues == []

    def test_error_on_boolean_surfaces_warning(self, draft, monkeypatch):
        # When the OCC boolean raises, a geometry_check_failed warning is emitted
        # rather than silently continuing (so the caller knows the check didn't run).
        import build123d.topology.shape_core as sc

        import build123d_drafting.helpers as h

        a = Dimension((-5, 0, 0), (5, 0, 0), "above", 6, draft, label="10")
        b = Dimension((-5, 0, 0), (5, 0, 0), "above", 6, draft, label="10")

        def broken_and(self, other):
            raise RuntimeError("simulated OCC failure")

        monkeypatch.setattr(sc.Shape, "__and__", broken_and)
        issues = h.find_overlaps([a, b])
        assert any(i.code == "geometry_check_failed" for i in issues), (
            "expected geometry_check_failed warning when boolean raises"
        )


# ---------------------------------------------------------------------------
# find_interferences (geometry-precise, duck-typed)
# ---------------------------------------------------------------------------


class TestFindInterferences:
    def _msgs(self, issues):
        return " | ".join(i.message for i in issues)

    def test_witness_line_pierces_neighbour_label(self, draft):
        dims = place_dims(
            [
                ((-18, -10, 0), (18, -10, 0), "below", "36"),
                ((-18, -10, 0), (0, -10, 0), "below", "18"),
                ((18, -10, 0), (18, 10, 0), "right", "20"),
            ],
            draft,
            base_distance=6,
        )
        issues = find_interferences(dims)
        assert any("pierces" in i.message for i in issues), self._msgs(issues)

    def test_clean_layout_has_no_issues(self, draft):
        dims = [
            Dimension((-18, -10, 0), (18, -10, 0), "below", 6, draft, label="36"),
            Dimension((-18, 10, 0), (18, 10, 0), "above", 6, draft, label="36"),
        ]
        assert find_interferences(dims) == []

    def test_overlapping_labels_flagged(self, draft):
        dims = [
            Dimension((-8, -10, 0), (8, -10, 0), "below", 6, draft, label="12.50"),
            Dimension((-8, -10, 0), (8, -10, 0), "below", 6, draft, label="R3.20"),
        ]
        assert any("overlap" in i.message for i in find_interferences(dims))

    def test_label_outside_page_frame_flagged(self, draft):
        from build123d import Box

        dims = [Dimension((-18, -10, 0), (18, -10, 0), "below", 6, draft, label="36")]
        page = Box(40, 5, 1).bounding_box()
        issues = find_interferences(dims, page_bbox=page)
        assert any("frame" in i.message for i in issues), self._msgs(issues)

    def test_empty_list_is_safe(self):
        assert find_interferences([]) == []

    def test_duck_typed_namespace(self, draft):
        # SimpleNamespace stand-ins with .label_bbox/.segments must work
        from types import SimpleNamespace

        a = SimpleNamespace(label="A", label_bbox=(0, 0, 10, 4), segments=[])
        b = SimpleNamespace(label="B", label_bbox=(2, 1, 12, 5), segments=[])
        issues = find_interferences([a, b])
        assert any(i.code == "labels_overlap" for i in issues)


class TestFindInterferencesRedundantLines:
    def test_shared_endpoint_witness_lines_flagged(self, draft):
        dims = place_dims(
            [
                ((-18, -10, 0), (18, -10, 0), "below", "36"),
                ((-18, -10, 0), (0, -10, 0), "below", "18"),
            ],
            draft,
            base_distance=5,
        )
        issues = find_interferences(dims)
        assert any("redundant" in i.message for i in issues), " | ".join(i.message for i in issues)

    def test_no_shared_endpoints_is_clean(self, draft):
        dims = place_dims(
            [
                ((-8, -10, 0), (8, -10, 0), "below", "16"),
                ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ],
            draft,
            base_distance=5,
        )
        issues = find_interferences(dims)
        assert not any("redundant" in i.message for i in issues), " | ".join(
            i.message for i in issues
        )


class TestFindInterferencesSeverity:
    def test_pierce_is_error_redundant_is_warning(self, draft):
        dims = place_dims(
            [
                ((-18, -10, 0), (18, -10, 0), "below", "36"),
                ((-18, -10, 0), (0, -10, 0), "below", "18"),
            ],
            draft,
            base_distance=5,
        )
        issues = find_interferences(dims)
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("pierces" in i.message for i in errors)
        assert any("redundant" in i.message for i in warnings)


class TestLintIssueCode:
    def test_find_interferences_sets_codes(self, draft):
        dims = place_dims(
            [
                ((-18, -10, 0), (18, -10, 0), "below", "36"),
                ((-18, -10, 0), (0, -10, 0), "below", "18"),
            ],
            draft,
            base_distance=5,
        )
        codes = {i.code for i in find_interferences(dims)}
        assert "line_pierces_label" in codes
        assert "redundant_lines" in codes
        assert "" not in codes

    def test_lint_drawing_sets_codes(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="999")
        codes = {i.code for i in lint_drawing([d])}
        assert "label_vs_measured" in codes


class TestLeaderLabelBbox:
    def test_leader_sets_label_bbox(self, draft):
        ld = Leader((0, 0, 0), (10, 8, 0), "Ø7.93 H7", draft)
        assert ld.label_bbox is not None

    def test_lint_leader_uses_label_bbox_without_geometry(self):
        # duck-typed stand-in: only label_bbox + elbow, no Sketch geometry
        from types import SimpleNamespace

        r = SimpleNamespace(label="X", elbow=(5, 1), label_bbox=(0, 0, 10, 2))
        codes = {i.code for i in lint_drawing([r])}
        assert "leader_line_through_text" in codes


class TestFindInterferencesObstacles:
    def test_label_over_obstacle_flagged(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lb = d.label_bbox
        obstacle = (lb[0] - 1, lb[1] - 1, lb[2] + 1, lb[3] + 1)
        issues = find_interferences([d], obstacles=[obstacle])
        assert any(i.code == "label_over_geometry" for i in issues)

    def test_clear_label_not_flagged(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = find_interferences([d], obstacles=[(100, 100, 120, 120)])
        assert not any(i.code == "label_over_geometry" for i in issues)

    def test_accepts_boundbox(self, draft):
        from build123d import Box, Pos

        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lb = d.label_bbox
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        bb = (Pos(cx, cy, 0) * Box(lb[2] - lb[0] + 2, lb[3] - lb[1] + 2, 1)).bounding_box()
        issues = find_interferences([d], obstacles=[bb])
        assert any(i.code == "label_over_geometry" for i in issues)

    def test_obstacles_are_label_only(self, draft):
        ld = Leader((0, 0, 0), (20, 12, 0), "Ø5", draft)
        issues = find_interferences([ld], obstacles=[(-2, -2, 2, 2)])
        assert not any(i.code == "label_over_geometry" for i in issues)


class TestAnnotationOverlapLabelBbox:
    """Regression: annotation_overlap must compare label text boxes, not full
    bboxes. Full bboxes include witness lines that legitimately overlap for
    stacked dimensions — that was always a false positive. Issue #149."""

    def test_stacked_dims_from_same_anchor_no_false_positive(self, draft):
        # Four stacked height dims from the same left anchor — all witness lines
        # share the same X and their full bboxes nest inside each other. Only
        # the label text regions should be compared; they don't overlap.
        dims = place_dims(
            [
                ((0, 0, 0), (0, 15, 0), "left", "15"),
                ((0, 0, 0), (0, 30, 0), "left", "30"),
                ((0, 0, 0), (0, 45, 0), "left", "45"),
                ((0, 0, 0), (0, 60, 0), "left", "60"),
            ],
            draft,
            base_distance=8,
        )
        overlaps = [i for i in lint_drawing(dims) if i.code == "annotation_overlap"]
        assert overlaps == [], "stacked dims should not produce annotation_overlap — " + "; ".join(
            i.message for i in overlaps
        )

    def test_truly_overlapping_labels_still_flagged(self, draft):
        # Two dims placed at the same offset with the same span → labels land
        # on top of each other → should still be flagged.
        d1 = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        d2 = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        overlaps = [i for i in lint_drawing([d1, d2]) if i.code == "annotation_overlap"]
        assert overlaps, "identical dims with overlapping labels should fire annotation_overlap"


class TestLintViewShapes:
    """Tests for view_shapes parameter — #159 (view vs annotation) and #160 (view vs view)."""

    def _make_box_shape(self, x, y, w, h):
        """Return a build123d Box located at (x+w/2, y+h/2) — stands in for a projected view."""
        from build123d import Box, Pos

        return Pos(x + w / 2, y + h / 2, 0) * Box(w, h, 0.01)

    # --- no view_shapes: existing behaviour unchanged ---

    def test_no_view_shapes_no_new_codes(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        codes = {i.code for i in lint_drawing([d])}
        assert "view_annotation_overlap" not in codes
        assert "view_overlap" not in codes

    # --- #159: view vs annotation ---

    def test_view_overlapping_annotation_flagged(self, draft):
        # View at x=0–40, y=0–30; dim label sits inside that region
        view = self._make_box_shape(0, 0, 40, 30)
        d = Dimension((5, 10, 0), (15, 10, 0), "above", 4, draft, label="10")
        codes = {i.code for i in lint_drawing([d], view_shapes=[view])}
        assert "view_annotation_overlap" in codes

    def test_view_not_overlapping_annotation_not_flagged(self, draft):
        # View at x=0–40, y=0–30; dim far away at x=100
        view = self._make_box_shape(0, 0, 40, 30)
        d = Dimension((100, 10, 0), (120, 10, 0), "above", 4, draft, label="20")
        codes = {i.code for i in lint_drawing([d], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_view_annotation_overlap_severity_warning(self, draft):
        view = self._make_box_shape(0, 0, 40, 30)
        d = Dimension((5, 10, 0), (15, 10, 0), "above", 4, draft, label="10")
        issues = [
            i for i in lint_drawing([d], view_shapes=[view]) if i.code == "view_annotation_overlap"
        ]
        assert issues and issues[0].severity == "warning"

    # --- #65: line-work that legitimately touches the view must not fire ---

    def test_centerline_crossing_view_not_flagged(self):
        # A centreline must cross the feature it marks — never a finding.
        view = self._make_box_shape(0, 0, 40, 30)
        cl = Centerline((20, -5, 0), (20, 35, 0))
        codes = {i.code for i in lint_drawing([cl], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_dim_witness_lines_into_view_not_flagged(self, draft):
        # Witness lines run from the feature (inside the view) out to the dim
        # line; full bbox overlaps the view but the label sits outside it.
        view = self._make_box_shape(0, 0, 40, 30)
        d = Dimension((5, 25, 0), (15, 25, 0), "above", 10, draft, label="10")
        codes = {i.code for i in lint_drawing([d], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_leader_tip_in_view_label_outside_not_flagged(self, draft):
        # Leader tips touch the part outline by definition.
        view = self._make_box_shape(0, 0, 40, 30)
        ld = Leader(tip=(20, 15, 0), elbow=(50, 15, 0), label="ø4", draft=draft)
        codes = {i.code for i in lint_drawing([ld], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_leader_label_inside_view_still_flagged(self, draft):
        # The real failure mode — label text sitting on the part — must still fire.
        view = self._make_box_shape(0, 0, 40, 30)
        ld = Leader(tip=(45, 15, 0), elbow=(25, 15, 0), label="ø4", draft=draft)
        codes = {i.code for i in lint_drawing([ld], view_shapes=[view])}
        assert "view_annotation_overlap" in codes

    def test_datum_triangle_in_view_not_flagged(self, draft):
        # #69 — the datum triangle attaches to a part edge by design; only the
        # letter frame matters. Tip on the view's top edge, frame above it.
        from build123d import Location

        view = self._make_box_shape(0, 0, 40, 30)
        df = DatumFeature("A", draft).moved(Location((20, 27, 0)))
        codes = {i.code for i in lint_drawing([df], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_datum_letter_frame_in_view_still_flagged(self, draft):
        from build123d import Location

        view = self._make_box_shape(0, 0, 40, 30)
        df = DatumFeature("A", draft).moved(Location((20, 10, 0)))
        codes = {i.code for i in lint_drawing([df], view_shapes=[view])}
        assert "view_annotation_overlap" in codes

    def test_surface_finish_mark_in_view_not_flagged(self, draft):
        # #69 — the check-mark sits on the surface line; the Ra text is
        # outside the view here, so nothing should fire.
        view = self._make_box_shape(0, 0, 40, 30)
        sf = SurfaceFinish("Ra 1.6", (38, 15), draft=draft)
        codes = {i.code for i in lint_drawing([sf], view_shapes=[view])}
        assert "view_annotation_overlap" not in codes

    def test_surface_finish_text_in_view_still_flagged(self, draft):
        view = self._make_box_shape(0, 0, 40, 30)
        sf = SurfaceFinish("Ra 1.6", (10, 15), draft=draft)
        codes = {i.code for i in lint_drawing([sf], view_shapes=[view])}
        assert "view_annotation_overlap" in codes

    # --- #160: view vs view ---

    def test_overlapping_views_flagged(self):
        v1 = self._make_box_shape(0, 0, 60, 40)
        v2 = self._make_box_shape(50, 0, 60, 40)  # overlaps v1 by 10mm in X
        codes = {i.code for i in lint_drawing([], view_shapes=[v1, v2])}
        assert "view_overlap" in codes

    def test_non_overlapping_views_not_flagged(self):
        v1 = self._make_box_shape(0, 0, 60, 40)
        v2 = self._make_box_shape(70, 0, 60, 40)  # 10mm gap
        codes = {i.code for i in lint_drawing([], view_shapes=[v1, v2])}
        assert "view_overlap" not in codes

    def test_view_overlap_severity_warning(self):
        v1 = self._make_box_shape(0, 0, 60, 40)
        v2 = self._make_box_shape(50, 0, 60, 40)
        issues = [i for i in lint_drawing([], view_shapes=[v1, v2]) if i.code == "view_overlap"]
        assert issues and issues[0].severity == "warning"

    def test_three_views_only_adjacent_pairs_flagged(self):
        # v1 overlaps v2; v2 overlaps v3; v1 and v3 do not overlap each other
        v1 = self._make_box_shape(0, 0, 60, 40)
        v2 = self._make_box_shape(50, 0, 60, 40)  # overlaps v1
        v3 = self._make_box_shape(100, 0, 60, 40)  # overlaps v2, clear of v1
        issues = [i for i in lint_drawing([], view_shapes=[v1, v2, v3]) if i.code == "view_overlap"]
        assert len(issues) == 2

    def test_empty_view_shapes_list_no_error(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = lint_drawing([d], view_shapes=[])
        assert not any(i.code in ("view_overlap", "view_annotation_overlap") for i in issues)

    def test_invalid_view_shape_skipped_gracefully(self, draft):
        # A non-shape object with no bounding_box should be silently skipped
        from types import SimpleNamespace

        bad = SimpleNamespace()  # no bounding_box attribute
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = lint_drawing([d], view_shapes=[bad])  # must not raise
        assert not any(i.code == "view_annotation_overlap" for i in issues)

    def test_same_shape_in_items_and_view_shapes_no_self_overlap(self):
        # A shape passed in both lists must not generate a spurious self-overlap warning
        view = self._make_box_shape(0, 0, 40, 30)
        issues = lint_drawing([view], view_shapes=[view])
        assert not any(i.code == "view_annotation_overlap" for i in issues)
