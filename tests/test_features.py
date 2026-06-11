"""Tests for build123d_drafting.features (find_holes / find_bosses, #87)."""

import math

import pytest
from build123d import Axis, Box, Cone, Cylinder, Plane, Pos, fillet, mirror

from build123d_drafting import (
    BossFeature,
    CounterBore,
    HoleFeature,
    find_bosses,
    find_holes,
)


def _drill_tool(radius, depth, top_z):
    """A drill-shaped cut tool: cylinder of *depth* plus a 118° point."""
    tip = radius / math.tan(math.radians(59))
    bottom = top_z - depth
    return Pos(0, 0, top_z - depth / 2) * Cylinder(radius, depth) + Pos(
        0, 0, bottom - tip / 2
    ) * Cone(0, radius, tip)


class TestFindHoles:
    @pytest.mark.timeout(60)
    def test_plain_through_hole(self):
        holes = find_holes(Box(60, 60, 20) - Cylinder(5, 20))
        assert holes == [
            HoleFeature(
                axis=(0.0, 0.0, -1.0),
                location=(0.0, 0.0, 10.0),
                diameter=10.0,
                depth=20.0,
                bottom="through",
            )
        ]

    @pytest.mark.timeout(60)
    def test_blind_flat_hole(self):
        part = Box(60, 60, 20) - Pos(0, 0, 10 - 6) * Cylinder(5, 12)
        (hole,) = find_holes(part)
        assert hole.bottom == "flat"
        assert hole.depth == pytest.approx(12.0)
        assert hole.location == pytest.approx((0.0, 0.0, 10.0))
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))

    @pytest.mark.timeout(60)
    def test_drill_point_hole(self):
        part = Box(60, 60, 20) - _drill_tool(5, 12, top_z=10)
        (hole,) = find_holes(part)
        assert hole.bottom == "drill_point"
        # depth is the full-diameter extent; the cone tip is not included
        assert hole.depth == pytest.approx(12.0)

    @pytest.mark.timeout(60)
    def test_counterbored_through_hole(self):
        part = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 10 - 3) * Cylinder(9, 6)
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.bottom == "through"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)
        assert hole.spotface is None
        # depth is the bore segment's own extent, below the counterbore
        assert hole.depth == pytest.approx(14.0)

    @pytest.mark.timeout(60)
    def test_spotface_cbore_drill_stack(self):
        # The mcp#264 example: spotface ø60×5, cbore ø18×6, drill ø10.1×15
        block = Box(100, 100, 40)  # top at z=20
        part = (
            block
            - Pos(0, 0, 20 - 2.5) * Cylinder(30, 5)
            - Pos(0, 0, 20 - 5 - 3) * Cylinder(9, 6)
            - Pos(0, 0, 20 - 11 - 7.5) * Cylinder(5.05, 15)
        )
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.1)
        assert hole.depth == pytest.approx(15.0)
        assert hole.bottom == "flat"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)
        assert hole.spotface == CounterBore(diameter=60.0, depth=5.0)
        assert hole.location == pytest.approx((0.0, 0.0, 20.0))

    @pytest.mark.timeout(60)
    def test_cross_axis_hole(self):
        part = Box(60, 60, 20) - Cylinder(4, 60, rotation=(0, 90, 0))
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(8.0)
        assert hole.bottom == "through"
        assert abs(hole.axis[0]) == pytest.approx(1.0)
        assert hole.axis[1] == pytest.approx(0.0, abs=1e-9)
        assert hole.axis[2] == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.timeout(60)
    def test_opposed_coaxial_blind_holes_stay_separate(self):
        part = (
            Box(60, 60, 40)
            - Pos(0, 0, 20 - 5) * Cylinder(5, 10)
            - Pos(0, 0, -20 + 5) * Cylinder(5, 10)
        )
        holes = sorted(find_holes(part), key=lambda h: h.location[2])
        assert len(holes) == 2
        assert holes[0].location[2] == pytest.approx(-20.0)
        assert holes[0].axis == pytest.approx((0.0, 0.0, 1.0))
        assert holes[1].location[2] == pytest.approx(20.0)
        assert holes[1].axis == pytest.approx((0.0, 0.0, -1.0))
        assert all(h.bottom == "flat" and h.depth == pytest.approx(10.0) for h in holes)

    @pytest.mark.timeout(60)
    def test_keyway_split_bore_is_one_hole(self):
        part = Box(60, 40, 10) - Cylinder(5, 12) - Box(60, 6, 12)
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.bottom == "through"

    @pytest.mark.timeout(60)
    def test_corner_fillets_are_not_holes(self):
        part = fillet(Box(60, 60, 20).edges().filter_by(Axis.Z), 8)
        assert find_holes(part) == []
        assert find_bosses(part) == []

    @pytest.mark.timeout(60)
    def test_mirrored_part_keeps_classification(self):
        part = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 10 - 3) * Cylinder(9, 6)
        (hole,) = find_holes(mirror(part, about=Plane.XZ))
        assert hole.bottom == "through"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)

    @pytest.mark.timeout(60)
    def test_plain_box_has_no_features(self):
        assert find_holes(Box(20, 20, 20)) == []
        assert find_bosses(Box(20, 20, 20)) == []

    @pytest.mark.timeout(60)
    def test_turned_part_bore_is_through(self):
        (hole,) = find_holes(Cylinder(30, 40) - Cylinder(10, 40))
        assert hole.diameter == pytest.approx(20.0)
        assert hole.depth == pytest.approx(40.0)
        assert hole.bottom == "through"


class TestFindBosses:
    @pytest.mark.timeout(60)
    def test_boss_on_plate(self):
        part = Box(60, 60, 10) + Pos(0, 0, 5 + 4) * Cylinder(12, 8)
        assert find_bosses(part) == [
            BossFeature(
                axis=(0.0, 0.0, 1.0),
                location=(0.0, 0.0, 13.0),
                diameter=24.0,
                height=8.0,
            )
        ]

    @pytest.mark.timeout(60)
    def test_turned_part_od_is_a_boss(self):
        (boss,) = find_bosses(Cylinder(30, 40) - Cylinder(10, 40))
        assert boss.diameter == pytest.approx(60.0)
        assert boss.height == pytest.approx(40.0)

    @pytest.mark.timeout(60)
    def test_bore_is_not_a_boss(self):
        part = Box(60, 60, 20) - Cylinder(5, 20)
        assert find_bosses(part) == []
        assert len(find_holes(part)) == 1
