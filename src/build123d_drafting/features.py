"""features — geometric feature recognition for build123d parts (#87).

Recognises drilled-hole and boss features from a solid's cylindrical faces:

    from build123d_drafting import find_holes, find_bosses
    holes = find_holes(part)    # list[HoleFeature]
    bosses = find_bosses(part)  # list[BossFeature]

A *hole* is a contiguous coaxial stack of internal full cylinders — the
drilled bore plus optional counterbore and spotface steps — with its bottom
classified by probing the adjacent face (``through`` / ``flat`` /
``drill_point``).  A *boss* is an external full cylinder.  Cylinder patches
spanning less than ~half a turn (fillets/rounds) are never features, but a
bore split by a slot or keyway still counts.

This module also hosts the low-level cylinder analysis that
``make_drawing`` builds on (``analyse_cylinders``, ``_full_cyls``).
"""

import logging
import math
from dataclasses import dataclass

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane
from OCP.TopAbs import TopAbs_Orientation

_log = logging.getLogger(__name__)

# Cylinder patches around one axis spanning less than ~half a turn in total
# are edge blends (fillets/rounds), not holes or bosses — exclude them from
# the feature inventory.
_FULL_CYL_MIN_EXTENT = math.pi * 0.9

# Coaxial segments whose axial ranges meet within this gap belong to the same
# stack (a counterbore shoulder is an exact-touch); larger gaps are distinct
# features (e.g. coaxial blind holes drilled from opposite faces).
_STACK_GAP_TOL = 0.1

# A counterbore-like step shallower than this fraction of its diameter is a
# spotface (a facing cut, e.g. ø60×5), deeper is a counterbore (e.g. ø18×6).
_SPOTFACE_MAX_RATIO = 0.2


def analyse_cylinders(part):
    """Return (z_cyls, cross_cyls) from OCP cylindrical face analysis.

    Each entry is a dict with keys: diameter, area, cx, cy, cz, axis,
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
        fc = face.center()
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
            area=face.area,
            cx=fc.X,
            cy=fc.Y,
            cz=fc.Z,
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


def _cyl_group_key(c):
    """Cylinder patches of one hole/boss share axis, diameter, and the axis
    position in the plane perpendicular to it."""
    x, y, z = c["axis_xyz"]
    pos = {"z": (x, y), "x": (y, z), "y": (x, z)}[c["axis"]]
    return (c["axis"], round(c["diameter"], 2), round(pos[0], 1), round(pos[1], 1))


def _full_cyls(cyls):
    """Only the hole/boss cylinder records — patches around one axis must
    span at least ~half a turn in total, so lone fillet faces are excluded
    but a bore split by a slot or keyway still counts."""
    spans: dict = {}
    for c in cyls:
        key = _cyl_group_key(c)
        spans[key] = spans.get(key, 0.0) + c["u_extent"]
    return [c for c in cyls if spans[_cyl_group_key(c)] >= _FULL_CYL_MIN_EXTENT]


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
    ``diameter``/``depth`` describe the bore itself — the deepest segment of
    the stack — with ``depth`` its full-diameter axial extent (a drill point's
    cone is not included).  ``bottom`` is ``"through"``, ``"flat"``,
    ``"drill_point"``, or ``"unknown"`` when the adjacent geometry matches
    none of those.
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


def _line_key(c):
    """Coaxial-stack key: axis letter and axis position, diameter-agnostic."""
    x, y, z = c["axis_xyz"]
    pos = {"z": (x, y), "x": (y, z), "y": (x, z)}[c["axis"]]
    return (c["axis"], round(pos[0], 1), round(pos[1], 1))


def _segments(cyls):
    """Collapse cylinder patches into segments: one per (axis line, diameter,
    contiguous axial range). Keyway-split patches of one bore merge; coaxial
    same-diameter holes from opposite faces stay separate."""
    by_key: dict = {}
    for c in cyls:
        by_key.setdefault(_cyl_group_key(c), []).append(c)
    segments = []
    for patches in by_key.values():
        patches.sort(key=lambda c: c["s_lo"])
        run = [patches[0]]
        for c in patches[1:]:
            if c["s_lo"] <= max(p["s_hi"] for p in run) + _STACK_GAP_TOL:
                run.append(c)
            else:
                segments.append(run)
                run = [c]
        segments.append(run)
    return [
        dict(
            run[0],
            s_lo=min(p["s_lo"] for p in run),
            s_hi=max(p["s_hi"] for p in run),
            faces=[p["face"] for p in run],
        )
        for run in segments
    ]


def _axis_point(seg, s):
    """The 3D point on *seg*'s axis at axial coordinate *s*."""
    ax, ay, az = seg["axis_xyz"]
    dx, dy, dz = seg["dir_xyz"]
    s_ap = ax * dx + ay * dy + az * dz
    t = s - s_ap
    return (ax + t * dx, ay + t * dy, az + t * dz)


def _classify_end(seg, s_end, end_dir, edge_faces):
    """Classify one axial end of a cylinder segment from the face beyond it.

    Returns ``"open"`` (the bore exits, or the boss's free end), ``"flat"``
    (closed by a plane facing back into the segment), ``"drill_point"``
    (closed by a cone), or ``"unknown"``.
    """
    partners = []
    for face in seg["faces"]:
        for edge in face.edges():
            ec = edge.center()
            s_edge = ec.X * seg["dir_xyz"][0] + ec.Y * seg["dir_xyz"][1] + ec.Z * seg["dir_xyz"][2]
            if abs(s_edge - s_end) > _STACK_GAP_TOL:
                continue
            for partner in edge_faces.get(edge, ()):
                if not any(partner.is_same(f) for f in seg["faces"]):
                    partners.append(partner)
    if not partners:
        return "unknown"
    for partner in partners:
        surf = BRepAdaptor_Surface(partner.wrapped)
        kind = surf.GetType()
        if kind == GeomAbs_Cone:
            return "drill_point"
        if kind == GeomAbs_Plane:
            n = partner.normal_at(partner.center())
            dot = n.X * end_dir[0] + n.Y * end_dir[1] + n.Z * end_dir[2]
            if dot < -0.5:
                return "flat"
            if dot > 0.5:
                return "open"
    return "unknown"


def _edge_face_map(part):
    """Map every edge of *part* to the faces that share it."""
    edge_faces: dict = {}
    for f in part.faces():
        for e in f.edges():
            edge_faces.setdefault(e, []).append(f)
    return edge_faces


def _stacks(segments):
    """Group segments into contiguous coaxial stacks (one stack per hole)."""
    by_line: dict = {}
    for seg in segments:
        by_line.setdefault(_line_key(seg), []).append(seg)
    stacks = []
    for segs in by_line.values():
        segs.sort(key=lambda s: s["s_lo"])
        run = [segs[0]]
        for seg in segs[1:]:
            if seg["s_lo"] <= max(s["s_hi"] for s in run) + _STACK_GAP_TOL:
                run.append(seg)
            else:
                stacks.append(run)
                run = [seg]
        stacks.append(run)
    return stacks


def find_holes(part) -> list:
    """Recognise drilled holes on *part* (see :class:`HoleFeature`).

    Coaxial internal cylinders are grouped into stacks — drill + optional
    counterbore + optional spotface become one hole.  The bottom is
    classified by probing the face adjacent to the deep end.  Stacks with
    more than two steps above the bore are reported best-effort (first
    spotface-shaped and first counterbore-shaped step win).
    """
    z_cyls, cross_cyls = analyse_cylinders(part)
    internal = [c for c in _full_cyls(z_cyls) + _full_cyls(cross_cyls) if not c["external"]]
    if not internal:
        return []
    edge_faces = _edge_face_map(part)

    holes = []
    for stack in _stacks(_segments(internal)):
        d = stack[0]["dir_xyz"]
        lo_seg = min(stack, key=lambda s: s["s_lo"])
        hi_seg = max(stack, key=lambda s: s["s_hi"])
        lo_state = _classify_end(lo_seg, lo_seg["s_lo"], tuple(-c for c in d), edge_faces)
        hi_state = _classify_end(hi_seg, hi_seg["s_hi"], d, edge_faces)

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

        # Order segments from the opening inward; the last one is the bore
        ordered = sorted(stack, key=lambda s: s["s_hi"], reverse=from_hi)
        bore = ordered[-1]
        cbore = spotface = None
        for step in ordered[:-1]:
            spec = CounterBore(step["diameter"], round(step["s_hi"] - step["s_lo"], 2))
            if spec.depth < _SPOTFACE_MAX_RATIO * spec.diameter:
                spotface = spotface or spec
            else:
                cbore = cbore or spec
        holes.append(
            HoleFeature(
                axis=_unit(tuple(-c for c in d) if from_hi else d),
                location=_axis_point(opening_seg, opening_s),
                diameter=bore["diameter"],
                depth=round(bore["s_hi"] - bore["s_lo"], 2),
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
        lo_state = _classify_end(seg, seg["s_lo"], tuple(-c for c in d), edge_faces)
        hi_state = _classify_end(seg, seg["s_hi"], d, edge_faces)
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
