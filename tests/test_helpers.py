"""Tests for build123d_drafting."""
import math

import pytest
from build123d import Draft, Compound, Edge, Vector

from build123d_drafting import (
    CenterlineResult, DatumFeatureResult, DatumTargetResult, DimResult,
    FeatureControlFrameResult,
    LeaderResult, LintIssue, TitleBlockResult,
    SurfaceFinishResult,
    add_to_layers,
    centerline, datum_feature, datum_target, dim_linear, feature_control_frame,
    find_interferences,
    iso_title_block, leader, leader_offset, lint_drawing,
    place_dims, place_labels,
    safe_dim_line, surface_finish_mark, view_axes,
)
from build123d_drafting.helpers import _GDT_GLYPHS


@pytest.fixture
def draft():
    return Draft(font_size=2.5, decimal_precision=1)


# ---------------------------------------------------------------------------
# dim_linear
# ---------------------------------------------------------------------------

class TestDimLinear:
    def test_above_places_dim_in_positive_y(self, draft):
        # Horizontal segment along x-axis; "above" should give positive Y bbox
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        bb = res.shape.bounding_box()
        assert bb.min.Y > 0, "dim 'above' should be in positive-Y region"

    def test_below_places_dim_in_negative_y(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "below", 8, draft, label="20")
        bb = res.shape.bounding_box()
        assert bb.max.Y < 0, "dim 'below' should be in negative-Y region"

    def test_right_places_dim_in_positive_x(self, draft):
        # Vertical segment along y-axis; "right" should give positive X bbox
        res = dim_linear((0, -10, 0), (0, 10, 0), "right", 8, draft, label="20")
        bb = res.shape.bounding_box()
        assert bb.min.X > 0, "dim 'right' should be in positive-X region"

    def test_left_places_dim_in_negative_x(self, draft):
        res = dim_linear((0, -10, 0), (0, 10, 0), "left", 8, draft, label="20")
        bb = res.shape.bounding_box()
        assert bb.max.X < 0, "dim 'left' should be in negative-X region"

    def test_vector_side_equivalent_to_named(self, draft):
        named = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        vector = dim_linear((-10, 0, 0), (10, 0, 0), (0, 1, 0), 8, draft, label="20")
        # Both should be in positive-Y region
        assert named.shape.bounding_box().min.Y > 0
        assert vector.shape.bounding_box().min.Y > 0

    def test_returns_dim_result(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft)
        assert isinstance(res, DimResult)
        assert isinstance(res.shape, Compound)

    def test_measured_length_correct(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        assert abs(res.measured_length - 20.0) < 0.01

    def test_label_str_set(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20 mm")
        assert res.label_str == "20 mm"

    def test_auto_label_when_none(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft)
        assert res.label_str != ""  # some label was generated

    def test_tolerance_accepted(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, tolerance=0.1)
        assert res.shape is not None

    def test_label_offset_x_shifts_label(self, draft):
        """label_offset_x=10 should place label_bbox min_x > midpoint_x."""
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft,
                         label="Ø5.0 H8", label_offset_x=10)
        assert res.label_bbox is not None
        midpoint_x = 0.0  # midpoint of -10 to 10
        lmin_x, _lmin_y, _lmax_x, _lmax_y = res.label_bbox
        assert lmin_x > midpoint_x, (
            f"label_bbox min_x={lmin_x:.2f} should be > midpoint {midpoint_x}"
        )

    def test_centerline_overlap_flagged(self, draft):
        """Vertical centerline through x=0 should collide with dim label at midpoint."""
        dim = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8")
        cl = centerline((0, 0, 0), (0, 20, 0))
        issues = lint_drawing([dim, cl])
        assert any("centerline" in i.message.lower() for i in issues), (
            f"Expected label_centerline_overlap; got: {[i.message for i in issues]}"
        )

    def test_centerline_no_overlap_with_offset(self, draft):
        """With label_offset_x=15 the label clears the centerline at x=0."""
        dim = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft,
                         label="Ø5.0 H8", label_offset_x=15)
        cl = centerline((0, 0, 0), (0, 20, 0))
        issues = lint_drawing([dim, cl])
        centerline_issues = [i for i in issues if "centerline" in i.message.lower()]
        assert centerline_issues == [], (
            f"Unexpected centerline overlap: {[i.message for i in centerline_issues]}"
        )

    def test_short_path_does_not_crash(self, draft):
        # 4 mm path — would crash vanilla ExtensionLine
        res = dim_linear((-2, 0, 0), (2, 0, 0), "above", 5, draft, label="4")
        assert isinstance(res, DimResult)
        assert res.shape is not None
        assert res.label_str == "4"

    def test_short_path_label_bbox_set(self, draft):
        res = dim_linear((-2, 0, 0), (2, 0, 0), "above", 5, draft, label="4")
        assert res.label_bbox is not None


# ---------------------------------------------------------------------------
# place_dims
# ---------------------------------------------------------------------------

class TestPlaceDims:
    def test_single_dim_gets_base_distance(self, draft):
        results = place_dims([((-10, 0, 0), (10, 0, 0), "above", "20")], draft,
                             base_distance=8.0)
        assert len(results) == 1
        bb = results[0].shape.bounding_box()
        # Dim line Y should be close to base_distance (plus extension stub)
        assert bb.max.Y > 8.0

    def test_overlapping_dims_on_different_tiers(self, draft):
        # Both dims span the full x range — must be on different tiers.
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        bb0 = results[0].shape.bounding_box()
        bb1 = results[1].shape.bounding_box()
        assert bb1.max.Y > bb0.max.Y + 3.0, "Second dim should be on a higher tier"

    def test_non_overlapping_dims_share_tier(self, draft):
        # Dims on completely separate X segments share tier 0.
        specs = [
            ((-30, 0, 0), (-10, 0, 0), "above", "20"),
            (( 10, 0, 0), ( 30, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        bb0 = results[0].shape.bounding_box()
        bb1 = results[1].shape.bounding_box()
        assert abs(bb0.max.Y - bb1.max.Y) < 1.0, "Non-overlapping dims should share a tier"

    def test_three_overlapping_dims_three_tiers(self, draft):
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        max_ys = sorted(r.shape.bounding_box().max.Y for r in results)
        assert max_ys[1] > max_ys[0] + 3.0
        assert max_ys[2] > max_ys[1] + 3.0

    def test_stacked_result_passes_lint(self, draft):
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft, base_distance=8.0)
        issues = lint_drawing(results)
        overlap = [i for i in issues if "overlap" in i.message.lower()]
        assert overlap == [], f"place_dims output should not overlap: {[i.message for i in overlap]}"

    def test_tolerance_accepted(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", "20", 0.1)]
        results = place_dims(specs, draft)
        assert results[0].label_str.startswith("20")

    def test_returns_dim_results(self, draft):
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", "20"),
            (( 10, 0, 0), (30, 0, 0), "above", "20"),
        ]
        results = place_dims(specs, draft)
        assert all(isinstance(r, DimResult) for r in results)


# ---------------------------------------------------------------------------
# place_labels
# ---------------------------------------------------------------------------

class TestPlaceLabels:
    def test_no_centerlines_unchanged(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", 8, "20")]
        results = place_labels(specs, draft, centerlines=[])
        assert len(results) == 1
        assert isinstance(results[0], DimResult)
        assert results[0].label_bbox is not None

    def test_clears_vertical_centerline(self, draft):
        # Centerline at x=0 crosses the label midpoint; label should shift clear.
        specs = [((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")]
        cl = centerline((0, 0, 0), (0, 20, 0))
        results = place_labels(specs, draft, centerlines=[cl])
        dim = results[0]
        assert dim.label_bbox is not None
        lmin_x, _, lmax_x, _ = dim.label_bbox
        # Centerline at x=0 must not be inside the label bbox
        assert not (lmin_x < 0.0 < lmax_x), (
            f"Label bbox {lmin_x:.2f}..{lmax_x:.2f} still crosses centerline at x=0"
        )

    def test_cleared_dim_passes_lint(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8")]
        cl = centerline((0, 0, 0), (0, 20, 0))
        results = place_labels(specs, draft, centerlines=[cl])
        issues = lint_drawing(results + [cl])
        centerline_issues = [i for i in issues if "centerline" in i.message.lower()]
        assert centerline_issues == [], f"Still overlapping: {[i.message for i in centerline_issues]}"

    def test_no_shift_when_no_crossing(self, draft):
        # Centerline at x=50 is far from the label near x=0; no shift expected.
        specs = [((-10, 0, 0), (10, 0, 0), "above", 8, "20")]
        cl = centerline((50, 0, 0), (50, 20, 0))
        results = place_labels(specs, draft, centerlines=[cl])
        original = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        # label_bbox should be approximately the same as an unshifted dim
        assert results[0].label_bbox is not None
        assert original.label_bbox is not None
        assert abs(results[0].label_bbox[0] - original.label_bbox[0]) < 0.5

    def test_multiple_specs(self, draft):
        cl = centerline((0, 0, 0), (0, 30, 0))
        specs = [
            ((-10, 0, 0), (10, 0, 0), "above", 8, "20"),
            ((-15, 0, 0), (15, 0, 0), "above", 18, "30"),
        ]
        results = place_labels(specs, draft, centerlines=[cl])
        assert len(results) == 2
        for dim in results:
            lmin_x, _, lmax_x, _ = dim.label_bbox
            assert not (lmin_x < 0.0 < lmax_x), "Label still crosses centerline"

    def test_tolerance_spec_accepted(self, draft):
        specs = [((-10, 0, 0), (10, 0, 0), "above", 8, "20", 0.1)]
        results = place_labels(specs, draft, centerlines=[])
        assert results[0].label_str.startswith("20")


# ---------------------------------------------------------------------------
# safe_dim_line
# ---------------------------------------------------------------------------

class TestSafeDimLine:
    def test_normal_label_works(self, draft):
        res = safe_dim_line([(0, 0, 0), (20, 0, 0)], "20", draft)
        assert isinstance(res, DimResult)
        assert res.measured_length == pytest.approx(20.0, abs=0.01)

    def test_long_label_does_not_raise(self, draft):
        # Very long label on a short path — would crash plain DimensionLine
        long_label = "this label is extremely long and should cause a crash in plain DimensionLine"
        res = safe_dim_line([(0, 0, 0), (3, 0, 0)], long_label, draft)
        assert isinstance(res, DimResult)
        assert res.shape is not None

    def test_fallback_label_used(self, draft):
        long_label = "X" * 80
        res = safe_dim_line([(0, 0, 0), (3, 0, 0)], long_label, draft, fallback_label="~")
        assert res.label_str in ("~", long_label)  # either original worked or fallback used

    def test_measured_length_from_path(self, draft):
        res = safe_dim_line([(0, 0, 0), (15, 0, 0)], "15", draft)
        assert res.measured_length == pytest.approx(15.0, abs=0.01)


# ---------------------------------------------------------------------------
# leader
# ---------------------------------------------------------------------------

class TestLeader:
    def test_returns_leader_result(self, draft):
        res = leader((5, 5, 0), (15, 10, 0), "⌀7.93 H7", draft)
        assert isinstance(res, LeaderResult)

    def test_lines_is_compound(self, draft):
        res = leader((5, 5, 0), (15, 10, 0), "⌀7.93 H7", draft)
        assert isinstance(res.lines, Compound)

    def test_text_is_compound(self, draft):
        res = leader((5, 5, 0), (15, 10, 0), "⌀7.93 H7", draft)
        assert isinstance(res.text, Compound)

    def test_text_does_not_overlap_elbow(self, draft):
        # Text bbox should not contain the elbow point (line stops before text)
        elbow = (15, 10)
        res = leader((5, 5, 0), elbow, "⌀7.93 H7", draft)
        tb = res.text.bounding_box()
        elbow_in_text = (tb.min.X <= elbow[0] <= tb.max.X and
                         tb.min.Y <= elbow[1] <= tb.max.Y)
        assert not elbow_in_text, "elbow point must not be inside the text bbox"

    def test_text_placed_right_of_elbow_when_elbow_right_of_tip(self, draft):
        res = leader((0, 0, 0), (10, 5, 0), "label", draft)
        tb = res.text.bounding_box()
        # Text centre should be to the right of the elbow x-coordinate
        assert tb.min.X > 10 - 0.1

    def test_text_placed_left_of_elbow_when_elbow_left_of_tip(self, draft):
        res = leader((20, 0, 0), (10, 5, 0), "label", draft)
        tb = res.text.bounding_box()
        # Text should be to the left
        assert tb.max.X < 10 + 0.1

    def test_label_str_set(self, draft):
        res = leader((0, 0, 0), (10, 5, 0), "Ra 1.6", draft)
        assert res.label_str == "Ra 1.6"

    def test_shape_property_combines_lines_and_text(self, draft):
        res = leader((0, 0, 0), (10, 5, 0), "X", draft)
        combined = res.shape
        assert isinstance(combined, Compound)

    def test_lines_do_not_extend_into_text(self, draft):
        # Regression for issue #120: shelf must stop before text starts.
        res = leader((0, 0, 0), (20, 0, 0), "⌀8.00 H7", draft)
        lb = res.lines.bounding_box()
        tb = res.text.bounding_box()
        assert lb.max.X <= tb.min.X + 0.01, (
            f"lines extend to {lb.max.X:.2f} but text starts at {tb.min.X:.2f} "
            f"— shelf passes through label text"
        )

    def test_lines_do_not_extend_into_text_left_going(self, draft):
        # Same check for the left-going (tip to the right of elbow) case.
        res = leader((30, 0, 0), (10, 0, 0), "⌀8.00 H7", draft)
        lb = res.lines.bounding_box()
        tb = res.text.bounding_box()
        assert lb.min.X >= tb.max.X - 0.01, (
            f"lines start at {lb.min.X:.2f} but text ends at {tb.max.X:.2f} "
            f"— shelf passes through label text (left-going)"
        )


# ---------------------------------------------------------------------------
# leader_offset
# ---------------------------------------------------------------------------

class TestLeaderOffset:
    def test_compass_string_matches_equivalent_angle(self, draft):
        # "NE" should produce the same elbow as direction=45.0 → identical geometry
        a = leader_offset((10, 10), "NE", 12.0, "label", draft)
        b = leader_offset((10, 10), 45.0, 12.0, "label", draft)
        assert a.elbow == pytest.approx(b.elbow)

    def test_compass_string_case_insensitive(self, draft):
        a = leader_offset((0, 0), "ne", 10.0, "x", draft)
        b = leader_offset((0, 0), "NE", 10.0, "x", draft)
        assert a.elbow == pytest.approx(b.elbow)

    def test_east_is_positive_x(self, draft):
        res = leader_offset((0, 0), "E", 10.0, "x", draft)
        assert res.elbow[0] == pytest.approx(10.0)
        assert res.elbow[1] == pytest.approx(0.0, abs=1e-9)

    def test_north_is_positive_y(self, draft):
        res = leader_offset((0, 0), "N", 10.0, "x", draft)
        assert res.elbow[0] == pytest.approx(0.0, abs=1e-9)
        assert res.elbow[1] == pytest.approx(10.0)

    def test_unknown_direction_raises(self, draft):
        with pytest.raises(ValueError):
            leader_offset((0, 0), "XX", 10.0, "x", draft)


# ---------------------------------------------------------------------------
# view_axes
# ---------------------------------------------------------------------------

class TestViewAxes:
    def test_top_view_x_maps_to_page_x_positive(self):
        axes = view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))
        assert axes["world_X"] == ("page_X", 1.0)

    def test_top_view_y_maps_to_page_y_positive(self):
        axes = view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))
        assert axes["world_Y"] == ("page_Y", 1.0)

    def test_top_view_z_is_depth(self):
        axes = view_axes((0, 0, 100), (0, 1, 0), (0, 0, 0))
        assert axes["world_Z"][0] == "depth"

    def test_bottom_view_x_maps_to_page_x_negative(self):
        # Camera below looking up — world-X flips on page
        axes = view_axes((0, 0, -100), (0, 1, 0), (0, 0, 0))
        assert axes["world_X"] == ("page_X", -1.0), (
            "Bottom view should flip world-X → page-X with sign -1"
        )

    def test_front_view_x_maps_to_page_x(self):
        # Camera in front (negative Y), looking back, up = world-Z
        axes = view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))
        assert axes["world_X"] == ("page_X", 1.0)

    def test_front_view_z_maps_to_page_y(self):
        axes = view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))
        assert axes["world_Z"] == ("page_Y", 1.0)

    def test_front_view_y_is_depth(self):
        axes = view_axes((0, -100, 0), (0, 0, 1), (0, 0, 0))
        assert axes["world_Y"][0] == "depth"

    def test_returns_all_three_world_axes(self):
        axes = view_axes((0, 0, 100))
        assert set(axes.keys()) == {"world_X", "world_Y", "world_Z"}


# ---------------------------------------------------------------------------
# lint_drawing
# ---------------------------------------------------------------------------

class TestLintDrawing:
    def test_empty_list_returns_no_issues(self, draft):
        assert lint_drawing([]) == []

    def test_label_value_matches_length_no_issue(self, draft):
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = lint_drawing([res])
        label_issues = [i for i in issues if "axis swap" in i.message or "differs from" in i.message]
        assert label_issues == []

    def test_label_value_diverges_from_length(self, draft):
        # Label says 35 but the segment is only 20mm — >5% divergence
        res = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="35")
        issues = lint_drawing([res])
        assert any("35" in i.message or "differs from" in i.message for i in issues)
        assert any(i.severity == "warning" for i in issues)

    def test_dim_overlapping_part_flagged(self, draft):
        # Place a dim INSIDE the part bbox so the overlap fraction is large
        res = dim_linear((-5, 0, 0), (5, 0, 0), "above", 1, draft, label="10")

        class FakeBBox:
            class _pt:
                pass
            min = _pt(); min.X = -20; min.Y = -20
            max = _pt(); max.X = 20; max.Y = 20

        issues = lint_drawing([res], part_bbox=FakeBBox())
        assert any("overlap" in i.message.lower() for i in issues)

    def test_leader_elbow_outside_text_no_issue(self, draft):
        res = leader((0, 0, 0), (20, 10, 0), "label", draft)
        issues = lint_drawing([res])
        leader_issues = [i for i in issues if "Leader" in i.message]
        assert leader_issues == []

    def test_mixed_items_checked(self, draft):
        # Dim above a segment; leader placed well to the right — no spatial overlap.
        dim = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lea = leader((50, 0, 0), (70, 10, 0), "Ra 1.6", draft)
        issues = lint_drawing([dim, lea])
        assert issues == []

    def test_overlapping_dims_flagged(self, draft):
        # Two identical dims occupy the same space — should be flagged.
        a = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        b = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        issues = lint_drawing([a, b])
        assert any("overlap" in i.message.lower() for i in issues)

    def test_stacked_dims_not_flagged(self, draft):
        # Stacked at distinct offsets — must not be flagged as overlapping.
        inner = dim_linear((-10, 0, 0), (10, 0, 0), "above",  8, draft, label="20")
        outer = dim_linear((-10, 0, 0), (10, 0, 0), "above", 18, draft, label="20")
        issues = lint_drawing([inner, outer])
        overlap_issues = [i for i in issues if "overlap" in i.message.lower()]
        assert overlap_issues == []


# ---------------------------------------------------------------------------
# iso_title_block
# ---------------------------------------------------------------------------

class TestIsoTitleBlock:
    def test_returns_title_block_result(self, draft):
        res = iso_title_block("My Part", "DRW-001", draft=draft)
        assert isinstance(res, TitleBlockResult)

    def test_lines_is_compound(self, draft):
        res = iso_title_block("My Part", "DRW-001", draft=draft)
        assert isinstance(res.lines, Compound)

    def test_text_is_compound(self, draft):
        res = iso_title_block("My Part", "DRW-001", draft=draft)
        assert isinstance(res.text, Compound)

    def test_bbox_dict_correct_dimensions(self, draft):
        res = iso_title_block("Part", "001", width=170, cell_height=8, draft=draft)
        assert res.bbox["width"] == pytest.approx(170.0)
        assert res.bbox["height"] == pytest.approx(16.0)  # 2 rows × 8mm
        assert res.bbox["min_x"] == pytest.approx(0.0)
        assert res.bbox["min_y"] == pytest.approx(0.0)

    def test_custom_width_respected(self, draft):
        res = iso_title_block("Part", "001", width=210, draft=draft)
        assert res.bbox["width"] == pytest.approx(210.0)
        assert res.bbox["max_x"] == pytest.approx(210.0)

    def test_shape_property_returns_compound(self, draft):
        res = iso_title_block("Part", "001", draft=draft)
        assert isinstance(res.shape, Compound)

    def test_default_draft_used_when_none(self):
        # Should not raise when draft is omitted
        res = iso_title_block("Part", "DRW-001")
        assert isinstance(res, TitleBlockResult)

    def test_optional_fields_empty_string(self, draft):
        # All optional fields empty — should still produce a valid result
        res = iso_title_block("Part", "001", draft=draft)
        assert res.lines is not None
        assert res.text is not None

    def test_all_fields_populated(self, draft):
        res = iso_title_block(
            part_name="Bracket",
            drawing_number="BRK-042",
            scale="2:1",
            material="Al 6061",
            general_tolerance="ISO 2768-m",
            designed_by="J. Smith",
            date="2026-05-19",
            draft=draft,
        )
        assert isinstance(res, TitleBlockResult)
        # lines compound should have edges (outer border + dividers = at least 8)
        assert len(res.lines.edges()) >= 8


# ---------------------------------------------------------------------------
# surface_finish_mark
# ---------------------------------------------------------------------------

class TestSurfaceFinishMark:
    def test_returns_surface_finish_result(self, draft):
        res = surface_finish_mark("Ra 1.6", (10, 20), draft=draft)
        assert isinstance(res, SurfaceFinishResult)

    def test_label_str_preserved(self, draft):
        res = surface_finish_mark("Ra 3.2", (0, 0), draft=draft)
        assert res.label_str == "Ra 3.2"

    def test_position_stored(self, draft):
        res = surface_finish_mark("Ra 1.6", (15.0, 25.0), draft=draft)
        assert res.position == pytest.approx((15.0, 25.0))

    def test_lines_is_compound(self, draft):
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        assert isinstance(res.lines, Compound)

    def test_text_is_compound(self, draft):
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        assert isinstance(res.text, Compound)

    def test_shape_property_returns_compound(self, draft):
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        assert isinstance(res.shape, Compound)

    def test_symbol_has_three_edges(self, draft):
        # leg1 (diagonal), leg2 (vertical), shelf (horizontal)
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        assert len(res.lines.edges()) == 3

    def test_tip_is_at_position(self, draft):
        px, py = 10.0, 20.0
        res = surface_finish_mark("Ra 1.6", (px, py), draft=draft)
        bb = res.shape.bounding_box()
        # The tip is the lowest point — bbox min_y should equal py
        assert bb.min.Y == pytest.approx(py, abs=0.01)
        # bbox min_x should equal px (tip is leftmost point for 0° rotation)
        assert bb.min.X == pytest.approx(px, abs=0.01)

    def test_rotation_changes_bbox(self, draft):
        res0 = surface_finish_mark("Ra 1.6", (0, 0), angle=0.0, draft=draft)
        res90 = surface_finish_mark("Ra 1.6", (0, 0), angle=90.0, draft=draft)
        bb0 = res0.shape.bounding_box()
        bb90 = res90.shape.bounding_box()
        # At 90° rotation the extents in X and Y should differ noticeably
        assert abs(bb0.size.X - bb90.size.X) > 0.5

    def test_default_draft_used_when_none(self):
        res = surface_finish_mark("Ra 1.6", (0, 0))
        assert isinstance(res, SurfaceFinishResult)

    def test_bbox_method_works(self, draft):
        res = surface_finish_mark("Ra 1.6", (5, 5), draft=draft)
        bb = res.bbox()
        assert bb is not None

    def test_ra_value_sits_above_shelf(self, draft):
        # ISO 1302: the Ra value rests above the horizontal extension line, so
        # the shelf must not strike through the text. The shelf is at elbow_y.
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        elbow_y = (2.0 * draft.font_size) * math.sin(math.radians(60.0))
        assert res.text.bounding_box().min.Y >= elbow_y - 1e-6


# ---------------------------------------------------------------------------
# add_to_layers (SVG routing helper)
# ---------------------------------------------------------------------------

class _FakeExporter:
    """Records (shape-kind, layer) for each add_shape call."""
    def __init__(self):
        self.layers = []

    def add_shape(self, shape, layer):
        self.layers.append(layer)


class TestAddToLayers:
    def test_routes_lines_then_text(self, draft):
        fcf = feature_control_frame("position", 0.5, datums=("A",), draft=draft)
        exp = _FakeExporter()
        add_to_layers(exp, fcf, line_layer="L", text_layer="T")
        assert exp.layers == ["L", "T"]

    def test_default_layer_names(self, draft):
        res = surface_finish_mark("Ra 1.6", (0, 0), draft=draft)
        exp = _FakeExporter()
        add_to_layers(exp, res)
        assert exp.layers == ["lines", "text"]

    def test_rejects_shape_only_result(self, draft):
        d = dim_linear((0, 0, 0), (10, 0, 0), "below", 5, draft, label="10")
        with pytest.raises(TypeError):
            add_to_layers(_FakeExporter(), d)


# ---------------------------------------------------------------------------
# feature_control_frame
# ---------------------------------------------------------------------------

class TestFeatureControlFrame:
    def test_returns_result(self, draft):
        res = feature_control_frame("position", 0.5, ("A", "B", "C"), draft)
        assert isinstance(res, FeatureControlFrameResult)

    def test_lines_and_text_are_compounds(self, draft):
        res = feature_control_frame("position", 0.5, ("A",), draft)
        assert isinstance(res.lines, Compound)
        assert isinstance(res.text, Compound)

    def test_all_14_characteristics_draw(self, draft):
        for c in _GDT_GLYPHS:
            res = feature_control_frame(c, 0.1, ("A",), draft)
            assert len(res.lines.edges()) > 4, f"{c} produced too few edges"

    def test_frame_height_is_two_font_sizes(self, draft):
        res = feature_control_frame("flatness", 0.1, (), draft)
        assert res.height == pytest.approx(2 * draft.font_size)

    def test_bottom_left_at_origin(self, draft):
        res = feature_control_frame("position", 0.5, ("A", "B", "C"), draft)
        bb = res.lines.bounding_box()
        assert bb.min.X == pytest.approx(0.0, abs=0.01)
        assert bb.min.Y == pytest.approx(0.0, abs=0.01)

    def test_datums_stored(self, draft):
        res = feature_control_frame("position", 0.5, ("A", "B", "C"), draft)
        assert res.datums == ("A", "B", "C")

    def test_float_tolerance_formatted_to_precision(self, draft):
        res = feature_control_frame("position", 0.5, (), draft)
        assert res.tolerance_str == "0.5"

    def test_string_tolerance_passed_through(self, draft):
        res = feature_control_frame("position", "0.05", (), draft)
        assert res.tolerance_str == "0.05"

    def test_more_datums_widens_frame(self, draft):
        one = feature_control_frame("position", 0.5, ("A",), draft)
        three = feature_control_frame("position", 0.5, ("A", "B", "C"), draft)
        assert three.width > one.width

    def test_diameter_widens_tolerance_compartment(self, draft):
        plain = feature_control_frame("position", 0.5, ("A",), draft)
        dia = feature_control_frame("position", 0.5, ("A",), draft, diameter=True)
        assert dia.width > plain.width

    def test_modifier_widens_tolerance_compartment(self, draft):
        plain = feature_control_frame("position", 0.5, ("A",), draft)
        mmc = feature_control_frame("position", 0.5, ("A",), draft, modifier="M")
        assert mmc.width > plain.width

    def test_datum_letters_appear_in_text(self, draft):
        res = feature_control_frame("position", 0.5, ("A", "B", "C"), draft)
        # tolerance value + 3 datum letters = at least 4 text glyph groups
        assert len(res.text.faces()) >= 4

    def test_unknown_characteristic_raises(self, draft):
        with pytest.raises(ValueError):
            feature_control_frame("bogus", 0.1, (), draft)

    def test_unknown_modifier_raises(self, draft):
        with pytest.raises(ValueError):
            feature_control_frame("position", 0.1, (), draft, modifier="Z")

    def test_default_draft_used_when_none(self):
        res = feature_control_frame("position", 0.5, ("A",))
        assert isinstance(res, FeatureControlFrameResult)

    def test_shape_property_combines(self, draft):
        res = feature_control_frame("position", 0.5, ("A",), draft)
        assert isinstance(res.shape, Compound)

    def test_no_datums_allowed(self, draft):
        # form tolerances (flatness, straightness) take no datum
        res = feature_control_frame("flatness", 0.1, (), draft)
        assert res.datums == ()

    def test_datum_modifier_adds_geometry(self, draft):
        # A per-datum modifier draws a circled letter beside the datum letter,
        # adding ring edges and a second text glyph in that compartment.
        plain = feature_control_frame("position", 0.5, ("A",), draft)
        modded = feature_control_frame(
            "position", 0.5, ("A",), draft, datum_modifiers={"A": "M"}
        )
        assert len(modded.lines.edges()) > len(plain.lines.edges())
        assert len(modded.text.faces()) > len(plain.text.faces())


# ---------------------------------------------------------------------------
# datum_feature
# ---------------------------------------------------------------------------

class TestDatumFeature:
    def test_returns_result(self, draft):
        res = datum_feature("A", draft)
        assert isinstance(res, DatumFeatureResult)

    def test_letter_stored(self, draft):
        res = datum_feature("B", draft)
        assert res.letter == "B"

    def test_lines_and_text_are_compounds(self, draft):
        res = datum_feature("A", draft)
        assert isinstance(res.lines, Compound)
        assert isinstance(res.text, Compound)

    def test_tip_at_origin(self, draft):
        res = datum_feature("A", draft)
        bb = res.shape.bounding_box()
        assert bb.min.Y == pytest.approx(0.0, abs=0.01)

    def test_filled_triangle_has_a_face(self, draft):
        res = datum_feature("A", draft, filled=True)
        assert len(res.lines.faces()) >= 1

    def test_outline_triangle_has_no_face(self, draft):
        res = datum_feature("A", draft, filled=False)
        # outline mode: triangle is three edges, the only faces come from text
        assert len(res.lines.faces()) == 0

    def test_default_draft_used_when_none(self):
        res = datum_feature("A")
        assert isinstance(res, DatumFeatureResult)


class TestDatumTarget:
    def test_returns_result(self, draft):
        res = datum_target("A1", draft=draft)
        assert isinstance(res, DatumTargetResult)

    def test_identifier_stored(self, draft):
        res = datum_target("B2", draft=draft)
        assert res.identifier == "B2"

    def test_lines_and_text_are_compounds(self, draft):
        res = datum_target("A1", draft=draft)
        assert isinstance(res.lines, Compound)
        assert isinstance(res.text, Compound)

    def test_circle_centred_on_origin(self, draft):
        res = datum_target("A1", draft=draft)
        bb = res.lines.bounding_box()
        assert bb.center().X == pytest.approx(0.0, abs=0.01)
        assert bb.center().Y == pytest.approx(0.0, abs=0.01)

    def test_identifier_only_one_text_glyph_group(self, draft):
        res = datum_target("A1", draft=draft)
        assert len(res.text.faces()) >= 1   # the identifier renders

    def test_area_label_adds_upper_text(self, draft):
        without = datum_target("A1", draft=draft)
        withlab = datum_target("A1", area_label="⌀6", draft=draft)
        assert len(withlab.text.faces()) > len(without.text.faces())
        assert withlab.area_label == "⌀6"

    def test_no_area_label_is_blank(self, draft):
        res = datum_target("A1", draft=draft)
        assert res.area_label == ""

    def test_divider_splits_circle(self, draft):
        # circle edges + one horizontal divider line
        res = datum_target("A1", draft=draft)
        lines = res.lines.edges()
        horizontals = [e for e in lines
                       if abs(e.start_point().Y) < 0.01 and abs(e.end_point().Y) < 0.01]
        assert len(horizontals) >= 1

    def test_lines_have_no_faces(self, draft):
        # the symbol must stroke, not flood — no faces on the .lines compound
        res = datum_target("A1", draft=draft)
        assert len(res.lines.faces()) == 0

    def test_default_draft_used_when_none(self):
        res = datum_target("A1")
        assert isinstance(res, DatumTargetResult)

    def test_add_to_layers_accepts_result(self, draft):
        from build123d import Color, ExportSVG
        res = datum_target("A1", area_label="⌀6", draft=draft)
        exp = ExportSVG()
        exp.add_layer("lines", line_color=Color(0, 0, 0))
        exp.add_layer("text", fill_color=Color(0, 0, 0))
        add_to_layers(exp, res)   # must not raise


class TestBasicDimension:
    def test_basic_flag_sets_is_basic(self, draft):
        res = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        assert res.is_basic is True

    def test_default_is_not_basic(self, draft):
        res = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft)
        assert res.is_basic is False

    def test_basic_box_adds_four_edges_around_label(self, draft):
        plain = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft)
        boxed = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        assert len(boxed.shape.edges()) >= len(plain.shape.edges()) + 4

    def test_basic_box_encloses_label(self, draft):
        res = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        x0, y0, x1, y1 = res.label_bbox
        # the four box edges all lie on the bbox rectangle
        box_edges = [e for e in res.shape.edges()
                     if e.length == pytest.approx(x1 - x0, abs=0.05)
                     or e.length == pytest.approx(y1 - y0, abs=0.05)]
        assert len(box_edges) >= 4

    def test_basic_box_strokes_not_floods(self, draft):
        # box must be four separate edges (no closed wire) → no extra faces vs plain
        plain = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft)
        boxed = dim_linear((0, 0, 0), (20, 0, 0), "above", 8, draft, basic=True)
        assert len(boxed.shape.faces()) == len(plain.shape.faces())

    def test_basic_vertical_dim(self, draft):
        res = dim_linear((0, 0, 0), (0, 20, 0), "left", 8, draft, basic=True)
        assert res.is_basic is True
        assert len(res.shape.edges()) >= 4


class TestDraftPreset:
    def test_scales_arrow_to_font_and_thins_line(self):
        from build123d_drafting import draft_preset
        d = draft_preset(font_size=2.0)
        assert d.font_size == pytest.approx(2.0)
        assert d.arrow_length == pytest.approx(1.8)   # 0.9 * font_size
        assert d.line_width == pytest.approx(0.1)

    def test_lighter_than_build123d_default(self):
        from build123d import Draft
        from build123d_drafting import draft_preset
        assert draft_preset(font_size=2.5).arrow_length < Draft().arrow_length
        assert draft_preset(font_size=2.5).line_width < Draft().line_width

    def test_overrides_win(self):
        from build123d_drafting import draft_preset
        d = draft_preset(font_size=2.0, arrow_length=5.0, line_width=0.3)
        assert d.arrow_length == pytest.approx(5.0)
        assert d.line_width == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# find_interferences (geometry-precise)
# ---------------------------------------------------------------------------

class TestFindInterferences:
    def _msgs(self, issues):
        return " | ".join(i.message for i in issues)

    def test_witness_line_pierces_neighbour_label(self, draft):
        # place_dims: the "18" extension line at x=0 spears the "36" label.
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
            ((18, -10, 0), (18, 10, 0), "right", "20"),
        ], draft, base_distance=6)
        issues = find_interferences(dims)
        assert any("pierces" in i.message for i in issues), self._msgs(issues)

    def test_clean_layout_has_no_issues(self, draft):
        # dims on opposite sides of the part — nothing collides.
        dims = [
            dim_linear((-18, -10, 0), (18, -10, 0), "below", 6, draft, label="36"),
            dim_linear((-18, 10, 0), (18, 10, 0), "above", 6, draft, label="36"),
        ]
        assert find_interferences(dims) == []

    def test_overlapping_labels_flagged(self, draft):
        # two dims over the same span at the same offset -> labels coincide.
        dims = [
            dim_linear((-8, -10, 0), (8, -10, 0), "below", 6, draft, label="12.50"),
            dim_linear((-8, -10, 0), (8, -10, 0), "below", 6, draft, label="R3.20"),
        ]
        issues = find_interferences(dims)
        assert any("overlap" in i.message for i in issues), self._msgs(issues)

    def test_label_outside_page_frame_flagged(self, draft):
        from build123d import Box
        dims = [dim_linear((-18, -10, 0), (18, -10, 0), "below", 6, draft, label="36")]
        # a frame that does not contain the (negative-Y) label
        page = Box(40, 5, 1).bounding_box()  # y in [-2.5, 2.5]; label is near y=-16
        issues = find_interferences(dims, page_bbox=page)
        assert any("frame" in i.message for i in issues), self._msgs(issues)

    def test_empty_list_is_safe(self):
        assert find_interferences([]) == []


class TestFindInterferencesRedundantLines:
    def test_shared_endpoint_witness_lines_flagged(self, draft):
        # "36" (-18..18) and "18" (-18..0) share the x=-18 witness line.
        dims = place_dims([
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
            ((-18, -10, 0), (0, -10, 0), "below", "18"),
        ], draft, base_distance=5)
        issues = find_interferences(dims)
        assert any("redundant" in i.message for i in issues), \
            " | ".join(i.message for i in issues)

    def test_no_shared_endpoints_is_clean(self, draft):
        # "16" (-8..8) and "36" (-18..18) share no endpoint -> no duplicate lines.
        dims = place_dims([
            ((-8, -10, 0), (8, -10, 0), "below", "16"),
            ((-18, -10, 0), (18, -10, 0), "below", "36"),
        ], draft, base_distance=5)
        issues = find_interferences(dims)
        assert not any("redundant" in i.message for i in issues), \
            " | ".join(i.message for i in issues)


class TestFindInterferencesSeverity:
    def test_pierce_is_error_redundant_is_warning(self, draft):
        # Layout with both a pierce ("18" line through "36") and a shared
        # witness line (redundant) — verify the severity split.
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
        assert "" not in codes  # every issue carries a code

    def test_lint_drawing_sets_codes(self, draft):
        # a dim whose label disagrees with the measured length -> label_vs_measured
        d = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="999")
        codes = {i.code for i in lint_drawing([d])}
        assert "label_vs_measured" in codes


class TestLeaderLabelBbox:
    def test_leader_sets_label_bbox(self, draft):
        ld = leader((0, 0, 0), (10, 8, 0), "Ø7.93 H7", draft)
        assert ld.label_bbox is not None
        tb = ld.text.bounding_box()
        bx = ld.label_bbox
        assert bx[0] <= tb.min.X + 1e-6 and bx[2] >= tb.max.X - 1e-6

    def test_lint_leader_uses_label_bbox_without_text(self):
        # Reconstruct a leader with only label_bbox + elbow (no text geometry).
        # Elbow inside the label box -> still flags leader_line_through_text.
        r = LeaderResult(lines=Compound(children=[]), text=Compound(children=[]),
                         label_str="X", tip=(0, 0), elbow=(5, 1),
                         label_bbox=(0, 0, 10, 2))
        codes = {i.code for i in lint_drawing([r])}
        assert "leader_line_through_text" in codes


class TestFindInterferencesObstacles:
    def test_label_over_obstacle_flagged(self, draft):
        d = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lb = d.label_bbox
        obstacle = (lb[0] - 1, lb[1] - 1, lb[2] + 1, lb[3] + 1)   # covers the label
        issues = find_interferences([d], obstacles=[obstacle])
        assert any(i.code == "label_over_geometry" for i in issues)

    def test_clear_label_not_flagged(self, draft):
        d = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        far = (100, 100, 120, 120)
        issues = find_interferences([d], obstacles=[far])
        assert not any(i.code == "label_over_geometry" for i in issues)

    def test_accepts_boundbox(self, draft):
        from build123d import Box, Pos
        d = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lb = d.label_bbox
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        bb = (Pos(cx, cy, 0) * Box(lb[2] - lb[0] + 2, lb[3] - lb[1] + 2, 1)).bounding_box()
        issues = find_interferences([d], obstacles=[bb])
        assert any(i.code == "label_over_geometry" for i in issues)

    def test_obstacles_are_label_only(self, draft):
        # A leader line entering an obstacle (its tip is on the geometry) must
        # NOT be flagged — only labels are tested against obstacles.
        ld = leader((0, 0, 0), (20, 12, 0), "Ø5", draft)
        tip_box = (-2, -2, 2, 2)   # around the leader tip / line, not the label
        issues = find_interferences([ld], obstacles=[tip_box])
        assert not any(i.code == "label_over_geometry" for i in issues)
