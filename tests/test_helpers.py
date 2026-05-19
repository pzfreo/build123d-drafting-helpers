"""Tests for build123d_drafting."""
import pytest
from build123d import Draft, Compound, Edge, Vector

from build123d_drafting import (
    DimResult, LeaderResult, LintIssue,
    dim_linear, leader, lint_drawing, safe_dim_line, view_axes,
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
        dim = dim_linear((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="20")
        lea = leader((0, 0, 0), (20, 10, 0), "Ra 1.6", draft)
        issues = lint_drawing([dim, lea])
        # No issues expected for clean inputs
        assert issues == []
