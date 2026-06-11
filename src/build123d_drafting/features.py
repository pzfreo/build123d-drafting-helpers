"""features — geometric feature recognition for build123d parts (#87).

Recognises drilled-hole and boss features from a solid's cylindrical faces:

    from build123d_drafting import find_holes, find_bosses
    holes = find_holes(part)    # list[HoleFeature]
    bosses = find_bosses(part)  # list[BossFeature]

A *hole* is a contiguous coaxial stack of internal full cylinders — the
drilled bore plus optional counterbore and spotface steps — with its bottom
classified by probing the adjacent face (``through`` / ``flat`` /
``drill_point`` / ``unknown``).  A *boss* is an external full cylinder.
Cylinder patches spanning half a turn or less (fillets, rounds, slot end
caps) are never features, but a bore split by a slot or keyway still counts,
and a bore interrupted by a crossing hole is recombined into one feature.

Known approximations: a hole opening onto a slanted or curved surface is
located at the axial extreme of its lip, and its depth includes the lip
overhang; counterbores on the far side of a through hole's bore are not
reported.

This module also hosts the low-level cylinder analysis that
``make_drawing`` builds on (``analyse_cylinders``, ``_full_cyls``).
"""

import logging
import math
from dataclasses import dataclass

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import (
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.TopAbs import TopAbs_Orientation

_log = logging.getLogger(__name__)

# Cylinder patches around one axis spanning half a turn or less in total are
# not holes or bosses: quarter-turn patches are edge blends (fillets/rounds)
# and exactly-half-turn patches are the end caps of milled slots. Real bores
# keep more than half a turn even when a slot or keyway splits them.
_FULL_CYL_MIN_EXTENT = math.pi * 1.05

# Coaxial segments whose axial ranges meet within this gap belong to the same
# stack (a counterbore shoulder is an exact-touch); larger gaps are distinct
# features unless bridged by a shoulder chamfer/fillet or a crossing void
# (see _merge_stacks).
_STACK_GAP_TOL = 0.1

# A counterbore-like step shallower than this fraction of its diameter is a
# spotface (a facing cut, e.g. ø60×5), deeper is a counterbore (e.g. ø18×6).
_SPOTFACE_MAX_RATIO = 0.2


def analyse_cylinders(part):
    """Return (z_cyls, cross_cyls) from OCP cylindrical face analysis.

    Each entry is a dict with keys: diameter, axis (dominant axis letter),
    u_extent (the face's angular span in radians — partial spans are fillets),
    axis_xyz (a point on the cylinder axis), external (True when the face
    is outward-facing — a boss/OD; False for a bore), dir_xyz (unit axis
    direction with its dominant component positive), s_lo/s_hi (the patch's
    axial extent as coordinates along dir_xyz), and face (the source face).
    z_cyls: cylinders whose axis is approximately Z.
    cross_cyls: cylinders whose axis is approximately X or Y.
    """
    z_cyls: list[dict] = []
    cross_cyls: list[dict] = []
    for face in part.faces():
        surf = BRepAdaptor_Surface(face.wrapped)
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        cyl = surf.Cylinder()
        r = cyl.Radius()
        d = cyl.Axis().Direction()
        ap = cyl.Axis().Location()
        comps = [("x", abs(d.X())), ("y", abs(d.Y())), ("z", abs(d.Z()))]
        ax = max(comps, key=lambda t: t[1])[0]
        # Canonical direction (dominant component positive) so coaxial faces
        # report comparable axial coordinates whichever way their frame points
        sign = 1.0 if {"x": d.X(), "y": d.Y(), "z": d.Z()}[ax] > 0 else -1.0
        dir_xyz = (sign * d.X(), sign * d.Y(), sign * d.Z())
        v0, v1 = surf.FirstVParameter(), surf.LastVParameter()
        # s(P) = P·dir for P = ap + v*d  →  s = ap·dir + sign*v
        s_ap = ap.X() * dir_xyz[0] + ap.Y() * dir_xyz[1] + ap.Z() * dir_xyz[2]
        s0, s1 = s_ap + sign * v0, s_ap + sign * v1
        rec = dict(
            diameter=round(r * 2, 2),
            axis=ax,
            u_extent=surf.LastUParameter() - surf.FirstUParameter(),
            axis_xyz=(ap.X(), ap.Y(), ap.Z()),
            dir_xyz=dir_xyz,
            s_lo=min(s0, s1),
            s_hi=max(s0, s1),
            face=face,
            # Outward material (boss/OD) vs bore: a right-handed cylinder's
            # natural normal points away from the axis, so FORWARD means
            # external — but mirroring makes the frame left-handed and flips
            # both, so compare against the frame handedness
            external=(face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_FORWARD)
            == cyl.Position().Direct(),
        )
        (z_cyls if ax == "z" else cross_cyls).append(rec)
    return z_cyls, cross_cyls


def _line_key(c):
    """Coaxial-stack key: axis letter plus the axis point projected onto the
    plane perpendicular to the axis direction (so it is position-independent
    along the axis, and exact for slanted axes too)."""
    px, py, pz = c["axis_xyz"]
    dx, dy, dz = c["dir_xyz"]
    t = px * dx + py * dy + pz * dz
    return (
        c["axis"],
        round(px - t * dx, 3),
        round(py - t * dy, 3),
        round(pz - t * dz, 3),
    )


def _cyl_group_key(c):
    """Cylinder patches of one hole/boss share an axis line and a diameter."""
    return (*_line_key(c), round(c["diameter"], 2))


def _merge_runs(items, key_fn):
    """Group *items* by *key_fn*, then split each group into runs of
    contiguous axial ranges (gap > _STACK_GAP_TOL starts a new run)."""
    by_key: dict = {}
    for item in items:
        by_key.setdefault(key_fn(item), []).append(item)
    runs = []
    for group in by_key.values():
        group.sort(key=lambda c: c["s_lo"])
        run, hi = [group[0]], group[0]["s_hi"]
        for c in group[1:]:
            if c["s_lo"] <= hi + _STACK_GAP_TOL:
                run.append(c)
                hi = max(hi, c["s_hi"])
            else:
                runs.append(run)
                run, hi = [c], c["s_hi"]
        runs.append(run)
    return runs


def _full_cyls(cyls):
    """Only the hole/boss cylinder records — patches around one axis must
    total more than half a turn within one contiguous axial range, so fillet
    faces and slot end caps are excluded (even coaxial caps at different
    heights) but a bore split by a slot or keyway still counts."""
    keep = []
    for run in _merge_runs(cyls, _cyl_group_key):
        if sum(c["u_extent"] for c in run) >= _FULL_CYL_MIN_EXTENT:
            keep.extend(run)
    return keep


# ---------------------------------------------------------------------------
# Hole / boss recognition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterBore:
    """A counterbore or spotface step of a hole: its diameter and axial depth."""

    diameter: float
    depth: float


@dataclass(frozen=True)
class HoleFeature:
    """A drilled hole: the bore plus optional counterbore/spotface steps.

    ``axis`` is the drilling direction (unit vector pointing from the opening
    into the hole) and ``location`` the axis point at the opening surface.
    ``diameter``/``depth`` describe the bore itself — the narrowest segment of
    the stack — with ``depth`` measured from the top of the bore to the hole's
    deep end (a bottom relief groove counts, a drill point's cone does not).
    ``bottom`` is ``"through"``, ``"flat"``, ``"drill_point"``, or
    ``"unknown"`` when the adjacent geometry matches none of those.
    """

    axis: tuple
    location: tuple
    diameter: float
    depth: float
    bottom: str
    cbore: CounterBore | None = None
    spotface: CounterBore | None = None


@dataclass(frozen=True)
class BossFeature:
    """An external cylindrical boss (including a turned part's OD).

    ``axis`` points from the base toward the free end, ``location`` is the
    axis point at the free end, and ``height`` the axial extent.
    """

    axis: tuple
    location: tuple
    diameter: float
    height: float


def _unit(v):
    """Normalise negative zeros out of a direction tuple."""
    return tuple(0.0 if c == 0 else c for c in v)


def _segments(cyls):
    """Collapse cylinder patches into segments: one per (axis line, diameter,
    contiguous axial range). Keyway-split patches of one bore merge; coaxial
    same-diameter holes from opposite faces stay separate."""
    return [
        dict(
            run[0],
            s_lo=min(p["s_lo"] for p in run),
            s_hi=max(p["s_hi"] for p in run),
            faces=[p["face"] for p in run],
        )
        for run in _merge_runs(cyls, _cyl_group_key)
    ]


def _axis_point(seg, s):
    """The 3D point on *seg*'s axis at axial coordinate *s*."""
    ax, ay, az = seg["axis_xyz"]
    dx, dy, dz = seg["dir_xyz"]
    s_ap = ax * dx + ay * dy + az * dz
    t = s - s_ap
    return (ax + t * dx, ay + t * dy, az + t * dz)


def _end_partners(seg, s_end, edge_faces):
    """The faces beyond one axial end of *seg*: partners of edges that lie at
    that end. An opening edge on a slanted or curved surface dips away from
    the end plane (by the lip sagitta), so edges match within a margin — but
    stay well clear of the segment's other end."""
    dx, dy, dz = seg["dir_xyz"]
    margin = max(_STACK_GAP_TOL, min(0.45 * (seg["s_hi"] - seg["s_lo"]), 0.5 * seg["diameter"]))
    partners = []
    for face in seg["faces"]:
        for edge in face.edges():
            pts = [edge.center()] + [v.center() for v in edge.vertices()]
            if not all(abs(p.X * dx + p.Y * dy + p.Z * dz - s_end) <= margin for p in pts):
                continue
            for partner in edge_faces.get(edge, ()):
                if not any(partner.is_same(f) for f in seg["faces"]):
                    partners.append(partner)
    return partners


def _classify_end(seg, s_end, hi_end, edge_faces):
    """Classify one axial end of a cylinder segment from the face beyond it.

    Returns ``"open"`` (the bore exits, or the boss's free end), ``"flat"``
    (closed by a plane facing back into the segment, or a boss's base),
    ``"drill_point"`` (a bore closed by a cone), or ``"unknown"``.

    Planes, cones, and tori are decisive; a curved wall (cylinder/sphere) is
    a weak signal — an exit for a bore, a base for a boss — that only counts
    when no decisive partner is present (a crossing port near a flat bottom
    must not outvote the bottom).

    An adjacent cone is read through the segment's internal/external context
    and its apex direction: for a bore, apex outward closes it (drill point)
    while apex inward widens it (an entry chamfer or countersink — open);
    for a boss the senses flip (apex outward is a chamfered free end, apex
    inward a base draft).  Tori follow the corner they round: one curling
    inward (major radius below the segment's) is a closed corner — a blind
    bore's bottom or a boss's base — and one flaring outward is an opening
    lip or a free end.
    """
    dx, dy, dz = seg["dir_xyz"]
    e_sign = 1.0 if hi_end else -1.0
    weak = None
    for partner in _end_partners(seg, s_end, edge_faces):
        surf = BRepAdaptor_Surface(partner.wrapped)
        kind = surf.GetType()
        if kind == GeomAbs_Cone:
            cone = surf.Cone()
            apex = cone.Apex()
            apex_s = apex.X() * dx + apex.Y() * dy + apex.Z() * dz
            outward = (apex_s - s_end) * e_sign > 0
            if not seg["external"]:
                if outward:
                    # A deburr chamfer on a flat floor's rim is also an
                    # apex-outward cone — closed either way, but it has the
                    # floor plane right next to it where a true drill point
                    # has nothing beyond its apex.
                    for e2 in partner.edges():
                        for n in edge_faces.get(e2, ()):
                            if n.is_same(partner) or any(n.is_same(f) for f in seg["faces"]):
                                continue
                            n_surf = BRepAdaptor_Surface(n.wrapped)
                            if n_surf.GetType() != GeomAbs_Plane:
                                continue
                            nv = n.normal_at(n.center())
                            if abs(nv.X * dx + nv.Y * dy + nv.Z * dz) > 0.9:
                                return "flat"
                    return "drill_point"
                return "open"
            return "open" if outward else "flat"
        if kind == GeomAbs_Torus:
            curls_in = surf.Torus().MajorRadius() < seg["diameter"] / 2
            if not seg["external"]:
                return "flat" if curls_in else "open"
            return "open" if curls_in else "flat"
        if kind == GeomAbs_Plane:
            n = partner.normal_at(partner.center())
            dot = (n.X * dx + n.Y * dy + n.Z * dz) * e_sign
            if dot < -0.5:
                return "flat"
            if dot > 0.5:
                return "open"
        elif kind == GeomAbs_Sphere:
            # Convex (material inside the sphere): the bore exits through a
            # spherical surface. Concave (a ball-nose cavity): a closed
            # bottom — reported as "flat" (no rounded-bottom category).
            convex = (
                partner.wrapped.Orientation() == TopAbs_Orientation.TopAbs_FORWARD
            ) == surf.Sphere().Position().Direct()
            if not seg["external"]:
                weak = "open" if convex else "flat"
            else:
                weak = "flat" if convex else "open"
        elif kind == GeomAbs_Cylinder:
            weak = "open" if not seg["external"] else "flat"
    return weak or "unknown"


def _edge_face_map(part):
    """Map every edge of *part* to the faces that share it."""
    edge_faces: dict = {}
    for f in part.faces():
        for e in f.edges():
            edge_faces.setdefault(e, []).append(f)
    return edge_faces


def _shared_transition(a, b, edge_faces):
    """True when a cone or torus face spans the gap between segment *a*'s
    high end and segment *b*'s low end — the shoulder chamfer or fillet that
    makes the two segments steps of one hole. The transition face touches
    one segment directly and may reach the other through the shoulder ring
    plane, so one adjacency hop is followed. Solid material between two
    unrelated coaxial features has no such connecting face."""
    a_partners = _end_partners(a, a["s_hi"], edge_faces)
    b_partners = _end_partners(b, b["s_lo"], edge_faces)
    for own, other in ((a_partners, b_partners), (b_partners, a_partners)):
        for t in own:
            if BRepAdaptor_Surface(t.wrapped).GetType() not in (GeomAbs_Cone, GeomAbs_Torus):
                continue
            if any(t.is_same(o) for o in other):
                return True
            neighbours = [f for e in t.edges() for f in edge_faces.get(e, ())]
            if any(n.is_same(o) for n in neighbours for o in other):
                return True
    return False


def _merge_stacks(stacks, edge_faces):
    """Recombine coaxial stacks that are one hole:

    - same bore diameter on both sides of a crossing void, neither facing
      end closed (a flat bottom or drill point means genuinely separate
      holes, e.g. blind holes drilled from opposite faces);
    - different diameters whose gap is bridged by a shoulder chamfer or
      fillet face (the steps of a counterbored hole with a deburred
      shoulder).
    """
    by_line: dict = {}
    for stack in stacks:
        by_line.setdefault(_line_key(stack[0]), []).append(stack)
    merged = []
    for line_stacks in by_line.values():
        line_stacks.sort(key=lambda st: min(s["s_lo"] for s in st))
        cur = line_stacks[0]
        for nxt in line_stacks[1:]:
            a = max(cur, key=lambda s: s["s_hi"])
            b = min(nxt, key=lambda s: s["s_lo"])
            closed = ("flat", "drill_point")
            if (
                abs(a["diameter"] - b["diameter"]) < 0.01
                and _classify_end(a, a["s_hi"], True, edge_faces) not in closed
                and _classify_end(b, b["s_lo"], False, edge_faces) not in closed
            ):
                joined = dict(a, s_hi=b["s_hi"], faces=a["faces"] + b["faces"])
                cur = [s for s in cur if s is not a] + [joined] + [s for s in nxt if s is not b]
            elif b["s_lo"] - a["s_hi"] <= _STACK_GAP_TOL + abs(
                a["diameter"] - b["diameter"]
            ) and _shared_transition(a, b, edge_faces):
                cur = cur + nxt
            else:
                merged.append(cur)
                cur = nxt
        merged.append(cur)
    return merged


def find_holes(part) -> list:
    """Recognise drilled holes on *part* (see :class:`HoleFeature`).

    Coaxial internal cylinders are grouped into stacks — drill + optional
    counterbore + optional spotface become one hole, and a bore interrupted
    by a crossing hole is recombined.  The bottom is classified by probing
    the face adjacent to the deep end.  Countersinks are not recognised as
    steps (the cone is treated as an opening); steps on the far side of the
    bore (e.g. a second counterbore from the back face) are not reported.
    """
    z_cyls, cross_cyls = analyse_cylinders(part)
    internal = [c for c in _full_cyls(z_cyls) + _full_cyls(cross_cyls) if not c["external"]]
    if not internal:
        return []
    edge_faces = _edge_face_map(part)
    stacks = _merge_stacks(_merge_runs(_segments(internal), _line_key), edge_faces)

    holes = []
    for stack in stacks:
        d = stack[0]["dir_xyz"]
        lo_seg = min(stack, key=lambda s: s["s_lo"])
        hi_seg = max(stack, key=lambda s: s["s_hi"])
        lo_state = _classify_end(lo_seg, lo_seg["s_lo"], False, edge_faces)
        hi_state = _classify_end(hi_seg, hi_seg["s_hi"], True, edge_faces)

        # The opening is the open end; with both ends open (a through hole)
        # prefer the wider segment's end (counterbores sit at the opening),
        # falling back to the high-coordinate end (drilled from the top).
        if lo_state == "open" and hi_state != "open":
            from_hi = False
        elif hi_state == "open" and lo_state != "open":
            from_hi = True
        else:
            from_hi = hi_seg["diameter"] >= lo_seg["diameter"]
        opening_seg, opening_s = (hi_seg, hi_seg["s_hi"]) if from_hi else (lo_seg, lo_seg["s_lo"])
        bottom_state = lo_state if from_hi else hi_state
        bottom = {"open": "through"}.get(bottom_state, bottom_state)

        # Order segments from the opening inward; the bore is the narrowest
        # (not the farthest — a through hole counterbored from both sides has
        # a step beyond the bore) and only steps on the opening side count.
        ordered = sorted(stack, key=lambda s: s["s_hi"], reverse=from_hi)
        bore_i = min(range(len(ordered)), key=lambda i: ordered[i]["diameter"])
        bore = ordered[bore_i]

        # Steps narrow monotonically from the opening to the bore; a wider
        # segment between same-diameter lands is a groove (e.g. an O-ring
        # gland inside a counterbore), not a step. Lands of one step span
        # their groove.
        spans: dict = {}
        step_order = []
        min_d = math.inf
        for step in ordered[:bore_i]:
            if step["diameter"] > min_d + 0.01:
                continue
            min_d = step["diameter"]
            key = round(step["diameter"], 2)
            if key not in spans:
                spans[key] = [step["s_lo"], step["s_hi"]]
                step_order.append(key)
            else:
                spans[key][0] = min(spans[key][0], step["s_lo"])
                spans[key][1] = max(spans[key][1], step["s_hi"])
        cbore = spotface = None
        for key in step_order:
            lo, hi = spans[key]
            spec = CounterBore(key, round(hi - lo, 2))
            if spec.depth < _SPOTFACE_MAX_RATIO * spec.diameter:
                spotface = spotface or spec
            else:
                cbore = cbore or spec

        # The bore's depth runs from its top to the hole's deep end: bore
        # lands span a mid-bore groove, and a blind hole's depth includes a
        # bottom relief groove — but not a through hole's far-side steps.
        bore_segs = [s for s in stack if abs(s["diameter"] - bore["diameter"]) < 0.01]
        deep_segs = bore_segs if bottom == "through" else stack
        if from_hi:
            depth = max(s["s_hi"] for s in bore_segs) - min(s["s_lo"] for s in deep_segs)
        else:
            depth = max(s["s_hi"] for s in deep_segs) - min(s["s_lo"] for s in bore_segs)
        holes.append(
            HoleFeature(
                axis=_unit(tuple(-c for c in d) if from_hi else d),
                location=_axis_point(opening_seg, opening_s),
                diameter=bore["diameter"],
                depth=round(depth, 2),
                bottom=bottom,
                cbore=cbore,
                spotface=spotface,
            )
        )
    return holes


def find_bosses(part) -> list:
    """Recognise external cylindrical bosses on *part* (one
    :class:`BossFeature` per coaxial external cylinder segment, including a
    turned part's OD — callers wanting only local bosses can filter on
    diameter against the part envelope)."""
    z_cyls, cross_cyls = analyse_cylinders(part)
    external = [c for c in _full_cyls(z_cyls) + _full_cyls(cross_cyls) if c["external"]]
    if not external:
        return []
    edge_faces = _edge_face_map(part)

    bosses = []
    for seg in _segments(external):
        d = seg["dir_xyz"]
        lo_state = _classify_end(seg, seg["s_lo"], False, edge_faces)
        hi_state = _classify_end(seg, seg["s_hi"], True, edge_faces)
        # The free end is the open one (its cap faces away from the segment);
        # default to the high end when both or neither are open.
        from_hi = not (lo_state == "open" and hi_state != "open")
        bosses.append(
            BossFeature(
                axis=_unit(d if from_hi else tuple(-c for c in d)),
                location=_axis_point(seg, seg["s_hi"] if from_hi else seg["s_lo"]),
                diameter=seg["diameter"],
                height=round(seg["s_hi"] - seg["s_lo"], 2),
            )
        )
    return bosses
