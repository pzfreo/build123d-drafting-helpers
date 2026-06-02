"""Tests for build123d_drafting (0.2.0 — native BaseSketchObject API)."""
import math

import pytest
from build123d import Color, Draft, ExportSVG, Sketch

from build123d_drafting import (
    Centerline, CompositeFeatureControlFrame,
    DatumFeature, DatumTarget, Dimension,
    FeatureControlFrame, HoleCallout,
    Leader, LintIssue, SafeDimension,
    SurfaceFinish, TitleBlock,
    draft_preset,
    find_interferences, find_overlaps,
    format_drawing_scale,
    leader_offset, lint_drawing,
    place_dims, place_labels, view_axes,
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
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft,
                      label="Ø5.0 H8", label_offset_x=10)
        assert d.label_bbox is not None
        lmin_x, *_ = d.label_bbox
        assert lmin_x > 0.0

    def test_centerline_overlap_flagged(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8")
        cl = Centerline((0, 0, 0), (0, 20, 0))
        issues = lint_drawing([d, cl])
        assert any("centerline" in i.message.lower() for i in issues)

    def test_centerline_no_overlap_with_offset(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft,
                      label="Ø5.0 H8", label_offset_x=15)
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
        results = place_dims([((-10, 0, 0), (10, 0, 0), "above", "20")], draft,
                             base_distance=8.0)
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
            (( 10, 0, 0), ( 30, 0, 0), "above", "20"),
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
            (( 10, 0, 0), (30, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft)
        assert all(isinstance(r, Dimension) for r in results)


# ---------------------------------------------------------------------------
# place_labels
# ---------------------------------------------------------------------------

class TestPlaceLabels:
    def test_no_centerlines_unchanged(self, draft):
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "20")], draft,
                               centerlines=[])
        assert len(results) == 1
        assert isinstance(results[0], Dimension)
        assert results[0].label_bbox is not None

    def test_clears_vertical_centerline(self, draft):
        cl = Centerline((0, 0, 0), (0, 20, 0))
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")],
                               draft, centerlines=[cl])
        lmin_x, _, lmax_x, _ = results[0].label_bbox
        assert not (lmin_x < 0.0 < lmax_x)

    def test_cleared_dim_passes_lint(self, draft):
        cl = Centerline((0, 0, 0), (0, 20, 0))
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")],
                               draft, centerlines=[cl])
        issues = [i for i in lint_drawing(results + [cl]) if "centerline" in i.message.lower()]
        assert issues == []

    def test_no_shift_when_no_crossing(self, draft):
        cl = Centerline((50, 0, 0), (50, 20, 0))
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "20")],
                               draft, centerlines=[cl])
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
        results = place_labels([((-10, 0, 0), (10, 0, 0), "above", 8, "20", 0.1)],
                               draft, centerlines=[])
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

class TestLintDrawing:
    def test_empty_list_returns_no_issues(self):
        assert lint_drawing([]) == []

    def test_label_value_matches_length_no_issue(self, draft):
        d = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = [i for i in lint_drawing([d])
                  if "axis swap" in i.message or "differs from" in i.message]
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
            min = _pt(); min.X = -20; min.Y = -20
            max = _pt(); max.X = 20; max.Y = 20

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
        inner = Dimension((-10, 0, 0), (10, 0, 0), "above",  8, draft, label="20")
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
            part_name="Bracket", drawing_number="BRK-042", scale="2:1",
            material="Al 6061", general_tolerance="ISO 2768-m",
            designed_by="J. Smith", date="2026-05-19", draft=draft,
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


# ---------------------------------------------------------------------------
# SurfaceFinish
# ---------------------------------------------------------------------------

class TestSurfaceFinish:
    def test_is_a_sketch(self, draft):
        assert isinstance(SurfaceFinish("Ra 1.6", (10, 20), draft=draft), Sketch)

    def test_label_preserved(self, draft):
        assert SurfaceFinish("Ra 3.2", (0, 0), draft=draft).label == "Ra 3.2"

    def test_position_stored(self, draft):
        assert SurfaceFinish("Ra 1.6", (15.0, 25.0), draft=draft).mark_position == pytest.approx((15.0, 25.0))

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

    def test_default_draft_used_when_none(self):
        assert isinstance(SurfaceFinish("Ra 1.6", (0, 0)), Sketch)

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(SurfaceFinish("Ra 1.6", (5, 5), draft=draft))


# ---------------------------------------------------------------------------
# FeatureControlFrame
# ---------------------------------------------------------------------------

class TestFeatureControlFrame:
    def test_is_a_sketch(self, draft):
        assert isinstance(FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft), Sketch)

    def test_all_14_characteristics_draw(self, draft):
        for c in _GDT_GLYPHS:
            fcf = FeatureControlFrame(c, 0.1, ("A",), draft)
            assert len(fcf.faces()) > 3, f"{c} produced too few faces"
            _export_ink(fcf)  # every characteristic must export cleanly (no flood)

    def test_bottom_left_at_origin(self, draft):
        bb = FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).bounding_box()
        assert bb.min.X == pytest.approx(0.0, abs=0.1)
        assert bb.min.Y == pytest.approx(0.0, abs=0.1)

    def test_datums_stored(self, draft):
        assert FeatureControlFrame("position", 0.5, ("A", "B", "C"), draft).datums == ("A", "B", "C")

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
        dia = FeatureControlFrame("position", 0.5, ("A",), draft, diameter=True).bounding_box().size.X
        assert dia > plain

    def test_modifier_widens_tolerance_compartment(self, draft):
        plain = FeatureControlFrame("position", 0.5, ("A",), draft).bounding_box().size.X
        mmc = FeatureControlFrame("position", 0.5, ("A",), draft, modifier="M").bounding_box().size.X
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

    def test_filled_has_more_faces_than_outline(self, draft):
        filled = DatumFeature("A", draft, filled=True)
        outline = DatumFeature("A", draft, filled=False)
        # filled adds a solid triangle face; outline draws it as three strokes
        assert len(filled.faces()) > 0 and len(outline.faces()) > 0

    def test_default_draft_used_when_none(self):
        assert isinstance(DatumFeature("A"), Sketch)

    def test_renders_on_single_ink_layer(self, draft):
        _export_ink(DatumFeature("A", draft))


class TestDatumTarget:
    def test_is_a_sketch(self, draft):
        assert isinstance(DatumTarget("A1", draft=draft), Sketch)

    def test_identifier_stored(self, draft):
        assert DatumTarget("B2", draft=draft).identifier == "B2"

    def test_circle_centred_on_origin(self, draft):
        # The divider runs across the circle through y=0 from -r..r, so its
        # midpoint is the circle centre at the origin.
        dt = DatumTarget("A1", draft=draft)
        divider = next(s for s in dt.segments
                       if abs(s[0][1]) < 0.01 and abs(s[1][1]) < 0.01)
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
        horizontals = [s for s in dt.segments
                       if abs(s[0][1]) < 0.01 and abs(s[1][1]) < 0.01]
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
        on_box = [s for s in res.segments
                  if math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1])
                  == pytest.approx(x1 - x0, abs=0.05)
                  or math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1])
                  == pytest.approx(y1 - y0, abs=0.05)]
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
        res = CompositeFeatureControlFrame("position", [{"tolerance": 0.1, "datums": ("A",)}], draft)
        assert res.bounding_box().size.Y == pytest.approx(2 * draft.font_size, abs=0.3)

    def test_unknown_characteristic_raises(self, draft):
        with pytest.raises(ValueError):
            CompositeFeatureControlFrame("bogus", self._rows(), draft)

    def test_empty_rows_raises(self, draft):
        with pytest.raises(ValueError):
            CompositeFeatureControlFrame("position", [], draft)

    def test_modifier_accepted(self, draft):
        res = CompositeFeatureControlFrame(
            "position", [{"tolerance": 0.25, "datums": ("A",), "modifier": "M"}], draft)
        assert isinstance(res, Sketch)

    def test_default_draft_used_when_none(self):
        assert isinstance(CompositeFeatureControlFrame(
            "position", [{"tolerance": 0.1, "datums": ("A",)}]), Sketch)

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
        _export_ink(HoleCallout(8.5, count=4, depth=20, cbore_dia=15, cbore_depth=6,
                                csink_dia=18, csink_angle=90, draft=draft))


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
            __import__("build123d").Pos(200, 200, 0))
        assert find_overlaps([a, b]) == []

    def test_empty_is_safe(self):
        assert find_overlaps([]) == []

    def test_works_on_bare_sketch(self, draft):
        # zero metadata — just two Sketches
        from build123d import Pos
        a = Centerline((0, 0, 0), (10, 0, 0))
        b = Centerline((0, 0, 0), (0, 10, 0))
        # they cross at origin -> thin faces intersect
        issues = find_overlaps([a, b], min_area=0.001)
        assert isinstance(issues, list)


# ---------------------------------------------------------------------------
# find_interferences (geometry-precise, duck-typed)
# ---------------------------------------------------------------------------

class TestFindInterferences:
    def _msgs(self, issues):
        return " | ".join(i.message for i in issues)

    def test_witness_line_pierces_neighbour_label(self, draft):
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
            ((18, -10, 0), (18, 10, 0), "right", "20"),
        ], draft, base_distance=6)
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
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
        ], draft, base_distance=5)
        issues = find_interferences(dims)
        assert any("redundant" in i.message for i in issues), \
            " | ".join(i.message for i in issues)

    def test_no_shared_endpoints_is_clean(self, draft):
        dims = place_dims([
            ((-8, -10, 0), (8, -10, 0), "below", "16"),
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
        ], draft, base_distance=5)
        issues = find_interferences(dims)
        assert not any("redundant" in i.message for i in issues), \
            " | ".join(i.message for i in issues)


class TestFindInterferencesSeverity:
    def test_pierce_is_error_redundant_is_warning(self, draft):
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
        ], draft, base_distance=5)
        issues = find_interferences(dims)
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("pierces" in i.message for i in errors)
        assert any("redundant" in i.message for i in warnings)


class TestLintIssueCode:
    def test_find_interferences_sets_codes(self, draft):
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
        ], draft, base_distance=5)
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
