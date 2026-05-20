"""Tests for build123d_drafting."""
import pytest
from build123d import Draft, Compound, Edge, Vector

from build123d_drafting import (
    CenterlineResult, DimResult, LeaderResult, LintIssue, TitleBlockResult,
    SurfaceFinishResult,
    centerline, dim_linear, iso_title_block, leader, lint_drawing, place_labels,
    safe_dim_line, surface_finish_mark, view_axes,
)


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
