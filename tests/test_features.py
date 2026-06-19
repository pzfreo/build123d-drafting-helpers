"""Tests for build123d_drafting.features (find_holes / find_bosses, #87)."""

import math

import pytest
from build123d import (
    Align,
    Axis,
    Box,
    Circle,
    Compound,
    Cone,
    Cylinder,
    GeomType,
    Plane,
    Pos,
    Rectangle,
    Sphere,
    chamfer,
    extrude,
    fillet,
    mirror,
)

from build123d_drafting import (
    BossFeature,
    CounterBore,
    HoleFeature,
    analyse_cylinders,
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
        # Two opposed keyway notches split the bore wall into two arc patches
        # (a single solid). The angular patches must still recombine into one
        # ⌀10 hole. (The earlier full-width slot bisected the block into two
        # separate solids — two half-bores, not a keyed hole; coaxial bores in
        # *different* solids are now kept apart, see test_features for #68.)
        part = (
            Box(60, 40, 10)
            - Cylinder(5, 12)
            - Pos(0, 5, 0) * Box(2, 4, 12)
            - Pos(0, -5, 0) * Box(2, 4, 12)
        )
        assert len(part.solids()) == 1
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.bottom == "through"

    @pytest.mark.timeout(60)
    def test_coaxial_bores_in_separate_solids_are_distinct_holes(self):
        # #68: two collinear bores in different bodies of an assembly must not
        # merge into one hole — that measured a depth across the gap between the
        # bodies (e.g. ⌀9.8 ↓111.4 where each bore is only 12 deep). Here a thin
        # plate's through-bore abuts a block's blind bore on the same axis.
        plate = Pos(-1, 0, 0) * (Box(2, 40, 12) - Cylinder(4.9, 40, rotation=(0, 90, 0)))
        block = Pos(60.5, 0, 0) * (
            Box(119, 40, 12) - Pos(-59.5, 0, 0) * Cylinder(4.9, 24, rotation=(0, 90, 0))
        )
        whole = Compound(children=[plate, block])
        holes = find_holes(whole)
        # The plate's through-bore (≈2 deep) and the block's blind bore (12 deep)
        # stay separate; no hole spans the inter-body gap.
        assert len(holes) == 2
        depths = sorted(h.depth for h in holes)
        assert depths == pytest.approx([2.0, 12.0])
        assert {h.bottom for h in holes} == {"through", "flat"}

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
    def test_chamfered_opening_stays_through(self):
        # An entry chamfer is a cone at the opening — it must not read as a
        # drill point and flip the hole's axis/location to the bottom face.
        part = Box(60, 60, 20) - Cylinder(5, 20)
        part = chamfer(part.edges().filter_by(GeomType.CIRCLE).sort_by(Axis.Z)[-1], 1.0)
        (hole,) = find_holes(part)
        assert hole.bottom == "through"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))
        assert hole.location[2] == pytest.approx(9.0)  # bore lip, below the chamfer

    @pytest.mark.timeout(60)
    def test_countersunk_opening_stays_through(self):
        part = Box(60, 60, 20) - Cylinder(2.5, 20) - Pos(0, 0, 7.5) * Cone(2.5, 5, 5)
        (hole,) = find_holes(part)
        assert hole.bottom == "through"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))
        assert hole.diameter == pytest.approx(5.0)

    @pytest.mark.timeout(60)
    def test_double_counterbored_through_hole_keeps_the_bore(self):
        # Counterbored from both faces: the bore is the narrowest segment,
        # not the farthest from the opening; the far-side step is not a cbore.
        part = (
            Box(60, 60, 20)
            - Cylinder(5, 20)
            - Pos(0, 0, 7) * Cylinder(9, 6)
            - Pos(0, 0, -7) * Cylinder(9, 6)
        )
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.depth == pytest.approx(8.0)
        assert hole.bottom == "through"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)

    @pytest.mark.timeout(60)
    def test_rounded_end_slot_is_not_holes(self):
        # Slot end caps span exactly half a turn — below the feature threshold
        slot = Box(20, 10, 20) + Pos(10, 0, 0) * Cylinder(5, 20) + Pos(-10, 0, 0) * Cylinder(5, 20)
        assert find_holes(Box(60, 60, 20) - slot) == []

    @pytest.mark.timeout(60)
    def test_bore_interrupted_by_crossing_hole_is_one_hole(self):
        # A ø6 cross-drilling severed by the larger ø10 vertical bore must
        # recombine into one through hole, not two short 'unknown' stubs.
        part = Box(60, 60, 40) - Cylinder(5, 40) - Cylinder(3, 60, rotation=(0, 90, 0))
        holes = sorted(find_holes(part), key=lambda h: h.diameter)
        assert len(holes) == 2
        assert holes[0].diameter == pytest.approx(6.0)
        assert holes[0].depth == pytest.approx(60.0)
        assert holes[0].bottom == "through"
        assert holes[1].diameter == pytest.approx(10.0)
        assert holes[1].depth == pytest.approx(40.0)

    @pytest.mark.timeout(60)
    def test_radial_hole_through_solid_shaft(self):
        # The hole exits through curved OD faces — still classified through
        part = Cylinder(15, 60, rotation=(0, 90, 0)) - Cylinder(3, 40)
        (hole,) = find_holes(part)
        assert hole.bottom == "through"
        assert hole.depth == pytest.approx(30.0, abs=0.1)

    @pytest.mark.timeout(60)
    def test_closely_spaced_parallel_holes_stay_separate(self):
        # 0.08 mm apart (PCB scale) — position bucketing must not merge them
        part = (
            Box(20, 20, 5)
            - Pos(2.46, 0, 0) * Cylinder(0.15, 5)
            - Pos(2.54, 0, 0) * Cylinder(0.15, 5)
        )
        assert len(find_holes(part)) == 2

    @pytest.mark.timeout(60)
    def test_slanted_counterbored_hole_groups_as_one(self):
        # The stack key projects axis points perpendicular to the axis, so a
        # 45° hole's faces share a line key (depth/location at slanted lips
        # are documented approximations — only grouping is asserted here).
        s = math.sin(math.radians(45))
        pl = Plane(origin=(0, 0, 0), z_dir=(s, 0, math.cos(math.radians(45))))
        part = Box(60, 60, 20) - (pl * Cylinder(5, 80)) - (pl.offset(8) * Cylinder(9, 30))
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.cbore is not None
        assert hole.cbore.diameter == pytest.approx(18.0)

    @pytest.mark.timeout(60)
    def test_chamfered_counterbore_shoulder_stays_one_hole(self):
        # A deburr chamfer on the cbore shoulder creates an axial gap between
        # the cbore and bore segments — the stack must bridge it, not split
        # into a phantom ø18 blind hole plus a cbore-less bore.
        part = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 7) * Cylinder(9, 6)
        edge = [
            e
            for e in part.edges().filter_by(GeomType.CIRCLE)
            if abs(e.center().Z - 4) < 0.01 and abs(e.radius - 5) < 0.01
        ]
        (hole,) = find_holes(chamfer(edge, 1.0))
        assert hole.diameter == pytest.approx(10.0)
        assert hole.bottom == "through"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)
        assert hole.location[2] == pytest.approx(10.0)

    @pytest.mark.timeout(60)
    def test_filleted_counterbore_shoulder_stays_one_hole(self):
        part = Box(60, 60, 20) - Cylinder(5, 20) - Pos(0, 0, 7) * Cylinder(9, 6)
        edge = [
            e
            for e in part.edges().filter_by(GeomType.CIRCLE)
            if abs(e.center().Z - 4) < 0.01 and abs(e.radius - 5) < 0.01
        ]
        (hole,) = find_holes(fillet(edge, 1.0))
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))
        assert hole.bottom == "through"
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)

    @pytest.mark.timeout(60)
    def test_filleted_blind_bottom_is_flat(self):
        # The bottom-corner fillet ring is a torus curling inward — closed
        part = Box(60, 60, 20) - Pos(0, 0, 4) * Cylinder(5, 12)
        edge = [e for e in part.edges().filter_by(GeomType.CIRCLE) if abs(e.center().Z + 2) < 0.01]
        (hole,) = find_holes(fillet(edge, 1.5))
        assert hole.bottom == "flat"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))

    @pytest.mark.timeout(60)
    def test_filleted_opening_lip_stays_through(self):
        # The lip fillet is a torus flaring outward — an opening
        part = Box(60, 60, 20) - Cylinder(5, 20)
        edge = [e for e in part.edges().filter_by(GeomType.CIRCLE) if abs(e.center().Z - 10) < 0.01]
        (hole,) = find_holes(fillet(edge, 1.0))
        assert hole.bottom == "through"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))

    @pytest.mark.timeout(60)
    def test_oring_groove_does_not_shorten_a_through_bore(self):
        part = Box(60, 60, 20) - Cylinder(5, 20) - Cylinder(6, 3)
        (hole,) = find_holes(part)
        assert hole.diameter == pytest.approx(10.0)
        assert hole.depth == pytest.approx(20.0)
        assert hole.bottom == "through"

    @pytest.mark.timeout(60)
    def test_coaxial_slot_caps_are_not_holes(self):
        # Two parallel open slots whose rounded ends share an axis, cut from
        # opposite faces: each cap spans π, and their spans must not be
        # summed across disjoint axial ranges into a phantom full circle.
        slot_2d = Rectangle(8, 30, align=(Align.CENTER, Align.MIN)) + Circle(4)
        slot = extrude(Plane.XY * slot_2d, 10)
        part = Box(60, 60, 30) - Pos(0, 0, 5) * slot - Pos(0, 0, -15) * slot
        assert find_holes(part) == []

    @pytest.mark.timeout(60)
    def test_coaxial_features_behind_a_wall_stay_separate(self):
        # ø20 blind from the top, ø10 blind from the bottom, 5 mm of solid
        # between — must be two blind features, never one "through" hole.
        part = (
            Box(60, 60, 40) - Pos(0, 0, 12.5) * Cylinder(10, 15) - Pos(0, 0, -10) * Cylinder(5, 20)
        )
        holes = sorted(find_holes(part), key=lambda h: h.diameter)
        assert len(holes) == 2
        assert all(h.bottom == "flat" for h in holes)
        assert holes[0].diameter == pytest.approx(10.0)
        assert holes[1].diameter == pytest.approx(20.0)

    @pytest.mark.timeout(60)
    def test_groove_inside_counterbore_keeps_the_cbore(self):
        # An O-ring gland splitting the cbore into lands must not demote the
        # cbore to a shallow spotface or surface the groove as a step.
        part = (
            Box(60, 60, 20)
            - Cylinder(5, 20)
            - Pos(0, 0, 7) * Cylinder(9, 6)
            - Pos(0, 0, 7) * Cylinder(10, 2)
        )
        (hole,) = find_holes(part)
        assert hole.cbore == CounterBore(diameter=18.0, depth=6.0)
        assert hole.spotface is None

    @pytest.mark.timeout(60)
    def test_crossing_port_near_flat_bottom_stays_flat(self):
        # The port's curved wall is a weak signal; the flat bottom plane at
        # the same end must win regardless of face iteration order.
        part = (
            Box(60, 60, 40) - Pos(0, 0, 9) * Cylinder(5, 22) - Cylinder(2, 60, rotation=(0, 90, 0))
        )
        (hole,) = (h for h in find_holes(part) if h.diameter == pytest.approx(10.0))
        assert hole.bottom == "flat"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))

    @pytest.mark.timeout(60)
    def test_drill_point_clipped_by_crossing_hole_stays_drill_point(self):
        part = (
            Box(60, 60, 40)
            - _drill_tool(4, 15, top_z=20)
            - Pos(0, 0, 4) * Cylinder(2.5, 60, rotation=(0, 90, 0))
        )
        (hole,) = (h for h in find_holes(part) if h.diameter == pytest.approx(8.0))
        assert hole.bottom == "drill_point"

    @pytest.mark.timeout(60)
    def test_hole_through_a_sphere_is_through(self):
        (hole,) = find_holes(Sphere(20) - Cylinder(4, 50))
        assert hole.bottom == "through"

    @pytest.mark.timeout(60)
    def test_ball_nose_bottom_is_closed(self):
        # A concave spherical cap closes the bore; and two opposed ball-
        # bottom holes must not merge across the solid wall between them.
        part = Box(60, 60, 40) - Pos(0, 0, 8) * Cylinder(5, 24) - Pos(0, 0, -4) * Sphere(5)
        (hole,) = find_holes(part)
        assert hole.bottom == "flat"
        assert hole.axis == pytest.approx((0.0, 0.0, -1.0))
        opposed = (
            Box(60, 60, 40)
            - Pos(0, 0, 11) * Cylinder(5, 18)
            - Pos(0, 0, 2) * Sphere(5)
            - Pos(0, 0, -11) * Cylinder(5, 18)
            - Pos(0, 0, -2) * Sphere(5)
        )
        assert len(find_holes(opposed)) == 2

    @pytest.mark.timeout(60)
    def test_chamfered_flat_floor_is_not_a_drill_point(self):
        # A deburr chamfer on the floor rim is an apex-outward cone like a
        # drill point, but it never reaches the axis — the bottom is flat.
        part = Box(60, 60, 40) - Pos(0, 0, 17.5) * Cylinder(15, 5)
        edge = [e for e in part.edges().filter_by(GeomType.CIRCLE) if abs(e.center().Z - 15) < 0.01]
        (hole,) = find_holes(chamfer(edge, 1.0))
        assert hole.bottom == "flat"

    @pytest.mark.timeout(60)
    def test_bottom_relief_groove_extends_depth(self):
        # A thread-relief groove at the bottom of a blind bore: depth runs to
        # the true bottom, not to the last bore land above the groove.
        part = (
            Box(60, 60, 40) - Pos(0, 0, 12.5) * Cylinder(4.25, 15) - Pos(0, 0, 6) * Cylinder(5.0, 2)
        )
        edge = [e for e in part.edges().filter_by(GeomType.CIRCLE) if abs(e.center().Z - 20) < 0.01]
        (hole,) = find_holes(chamfer(edge, 1.0))
        assert hole.diameter == pytest.approx(8.5)
        assert hole.depth == pytest.approx(14.0)
        assert hole.bottom == "flat"

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
    def test_chamfered_free_end_keeps_orientation(self):
        # A chamfer on the boss's free end is a cone — it must not flip the
        # documented base→free-end axis/location contract.
        part = Box(60, 60, 10) + Pos(0, 0, -9) * Cylinder(12, 8)
        part = chamfer(part.edges().filter_by(GeomType.CIRCLE).sort_by(Axis.Z)[0], 1.0)
        (boss,) = find_bosses(part)
        assert boss.axis == pytest.approx((0.0, 0.0, -1.0))
        assert boss.location[2] == pytest.approx(-12.0)  # free end, above the chamfer

    @pytest.mark.timeout(60)
    def test_filleted_free_end_keeps_orientation(self):
        part = Box(60, 60, 10) + Pos(0, 0, 9) * Cylinder(12, 8)
        edge = [e for e in part.edges().filter_by(GeomType.CIRCLE) if abs(e.center().Z - 13) < 0.01]
        (boss,) = find_bosses(fillet(edge, 1.0))
        assert boss.axis == pytest.approx((0.0, 0.0, 1.0))
        assert boss.location[2] == pytest.approx(12.0)  # free end, below the fillet

    @pytest.mark.timeout(60)
    def test_radial_boss_on_a_pipe_points_outward(self):
        # The base sits on a curved wall (weak 'flat' signal); the boss on
        # the negative-X side must still point away from the pipe.
        pipe = Cylinder(20, 60) - Cylinder(15, 60)
        part = pipe + Pos(-24, 0, 0) * Cylinder(5, 12, rotation=(0, 90, 0))
        (boss,) = (b for b in find_bosses(part) if b.diameter == pytest.approx(10.0))
        assert boss.axis[0] == pytest.approx(-1.0)
        assert boss.location[0] == pytest.approx(-30.0)

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

    @pytest.mark.timeout(60)
    def test_precomputed_cyls_matches_self_scan(self):
        # Passing a precomputed analyse_cylinders() result must give the same
        # bosses as letting find_bosses scan the solid itself (#149).
        part = Box(60, 60, 10) + Pos(0, 0, 9) * Cylinder(12, 8)
        cyls = analyse_cylinders(part)
        assert find_bosses(part, cyls=cyls) == find_bosses(part)


class TestFindHolePatterns:
    @staticmethod
    def _bc_plate(n=6, r=30, hole_r=4, phase=15.0):
        part = Box(100, 100, 12)
        for i in range(n):
            ang = math.radians(360 / n * i + phase)
            part = part - Pos(r * math.cos(ang), r * math.sin(ang), 0) * Cylinder(hole_r, 12)
        return part

    @pytest.mark.timeout(60)
    def test_six_hole_bolt_circle(self):
        from build123d_drafting import BoltCircle, find_hole_patterns

        (pat,) = find_hole_patterns(find_holes(self._bc_plate()))
        assert isinstance(pat, BoltCircle)
        assert pat.diameter == pytest.approx(60.0)
        assert len(pat.holes) == 6
        assert pat.center[0] == pytest.approx(0.0, abs=0.01)
        assert pat.center[1] == pytest.approx(0.0, abs=0.01)

    @pytest.mark.timeout(60)
    def test_three_equally_spaced_holes_are_a_bolt_circle(self):
        from build123d_drafting import BoltCircle, find_hole_patterns

        (pat,) = find_hole_patterns(find_holes(self._bc_plate(n=3, r=25)))
        assert isinstance(pat, BoltCircle)
        assert pat.diameter == pytest.approx(50.0)

    @pytest.mark.timeout(60)
    def test_linear_array(self):
        from build123d_drafting import LinearArray, find_hole_patterns

        part = Box(120, 40, 10)
        for i in range(5):
            part = part - Pos(-40 + i * 20, 0, 0) * Cylinder(3, 10)
        (pat,) = find_hole_patterns(find_holes(part))
        assert isinstance(pat, LinearArray)
        assert pat.pitch == pytest.approx(20.0)
        assert len(pat.holes) == 5
        assert abs(pat.direction[0]) == pytest.approx(1.0)

    @pytest.mark.timeout(60)
    def test_three_collinear_holes_are_an_array_not_a_circle(self):
        # any three points are concyclic — collinearity must win
        from build123d_drafting import LinearArray, find_hole_patterns

        part = (
            Box(100, 40, 10)
            - Pos(-20, 0, 0) * Cylinder(3, 10)
            - Cylinder(3, 10)
            - Pos(20, 0, 0) * Cylinder(3, 10)
        )
        (pat,) = find_hole_patterns(find_holes(part))
        assert isinstance(pat, LinearArray)

    @pytest.mark.timeout(60)
    def test_scattered_holes_are_no_pattern(self):
        from build123d_drafting import find_hole_patterns

        part = (
            Box(100, 100, 10)
            - Pos(10, 5, 0) * Cylinder(3, 10)
            - Pos(-30, 22, 0) * Cylinder(3, 10)
            - Pos(17, -38, 0) * Cylinder(3, 10)
            - Pos(-5, -11, 0) * Cylinder(3, 10)
        )
        assert find_hole_patterns(find_holes(part)) == []

    @pytest.mark.timeout(60)
    def test_uneven_spacing_is_not_a_bolt_circle(self):
        from build123d_drafting import find_hole_patterns

        part = Box(100, 100, 10)
        for deg in (0, 60, 100, 240):
            ang = math.radians(deg)
            part = part - Pos(30 * math.cos(ang), 30 * math.sin(ang), 0) * Cylinder(4, 10)
        assert find_hole_patterns(find_holes(part)) == []

    @pytest.mark.timeout(60)
    def test_mixed_diameters_do_not_pattern(self):
        from build123d_drafting import find_hole_patterns

        part = Box(100, 100, 10)
        for i, r in zip(range(4), (3, 3, 4, 3), strict=True):
            ang = math.radians(90 * i)
            part = part - Pos(30 * math.cos(ang), 30 * math.sin(ang), 0) * Cylinder(r, 10)
        assert find_hole_patterns(find_holes(part)) == []

    @pytest.mark.timeout(60)
    def test_rectangle_corners_are_not_a_bolt_circle(self):
        # 100×80 rectangle corners are equidistant from the centre but not
        # equally spaced (77.3°/102.7°) — must not read as EQ SP ON BC.
        from build123d_drafting import find_hole_patterns

        part = Box(140, 120, 10)
        for sx in (-50, 50):
            for sy in (-40, 40):
                part = part - Pos(sx, sy, 0) * Cylinder(3, 10)
        assert find_hole_patterns(find_holes(part)) == []

    @pytest.mark.timeout(60)
    def test_axis_epsilon_noise_does_not_split_a_pattern(self):
        # Mixed construction history leaves ~1e-16 components on cross-axis
        # hole axes; the spec key snaps them so the pattern still groups.
        from build123d import Circle, extrude

        from build123d_drafting import LinearArray, find_hole_patterns

        part = Box(20, 90, 30)
        part = part - Pos(0, -30, 0) * Cylinder(4, 20, rotation=(0, 90, 0))
        part = part - extrude(Plane.YZ * Circle(4), 10, both=True)
        part = part - Pos(0, 30, 0) * Cylinder(4, 20, rotation=(0, 90, 0))
        (pat,) = find_hole_patterns(find_holes(part))
        assert isinstance(pat, LinearArray)
        assert len(pat.holes) == 3

    @pytest.mark.timeout(60)
    def test_radius_jitter_beyond_tolerance_rejected(self):
        from build123d_drafting import find_hole_patterns

        part = Box(100, 100, 10)
        for i, r in zip(range(5), (30, 30, 30, 32, 30), strict=True):
            ang = math.radians(72 * i)
            part = part - Pos(r * math.cos(ang), r * math.sin(ang), 0) * Cylinder(4, 10)
        assert find_hole_patterns(find_holes(part)) == []


class TestEdgeFaceMap:
    @pytest.mark.timeout(60)
    def test_shared_edges_map_to_multiple_faces(self):
        # The bottom-classification chain depends on a topologically-shared edge
        # hashing/comparing equal across the faces meeting at it. A solid box has
        # edges shared by two faces; if Edge hashing ever regressed, each edge
        # would map to a single face and this would fail. (#150)
        from build123d_drafting.features import _edge_face_map

        counts = [len(faces) for faces in _edge_face_map(Box(10, 10, 10)).values()]
        assert counts and max(counts) >= 2
