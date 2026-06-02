"""build123d drawing helpers — pure build123d, no MCP dependency.

Every annotation builder is a native build123d ``BaseSketchObject`` subclass:
the returned object *is* a ``Sketch``, so it composes inside a ``BuildSketch``,
can be ``.moved()``, exported directly, and queried with ``.faces()`` /
``.bounding_box()``. All geometry (frame boxes, witness lines, GD&T glyphs,
text) is rendered as thin filled *faces* — there is a single ink layer, no
``.lines`` / ``.text`` split and no flooding of closed loops.

    from build123d_drafting import (
        Dimension, SafeDimension, Leader, Centerline, view_axes,
        lint_drawing, find_interferences, find_overlaps, LintIssue,
    )

  Dimension     ExtensionLine with named side ("above"/"below"/"left"/"right")
                instead of raw signed offset.  The sign is computed from the
                path direction's right-hand normal so you never have to guess.

  SafeDimension DimensionLine that won't raise ValueError when the label is
                longer than the dim path — it truncates gracefully.

  Leader        Leader annotation: arrowhead shaft + horizontal shelf + text,
                all as faces on a single ink layer.

  view_axes     Analytic world→page axis mapping for a project_to_viewport call.

  lint_drawing  Duck-typed structural checks on a list of annotation objects.

  find_overlaps Pure-geometry collision: pairs of sketches whose faces intersect.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from build123d import (
    Align,
    Arrow,
    Color,  # noqa: F401 — re-exported convenience
    Compound,
    DimensionLine,
    Draft,
    Edge,
    ExtensionLine,
    GeomType,
    Location,
    Mode,
    Sketch,
    Text,
    Vector,
    trace,
)
from build123d.objects_sketch import BaseSketchObject
from build123d.operations_generic import sweep


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@dataclass
class LintIssue:
    severity: Literal["error", "warning"]
    message: str
    location: tuple[float, float] | None = None
    code: str = ""   # stable machine-readable check id, e.g. "label_vs_measured"


def _segments(edges):
    """Centreline segments [((x0,y0),(x1,y1)), ...] from straight Edges — lint metadata."""
    out = []
    for e in edges:
        try:
            if e.geom_type == GeomType.LINE:
                s, t = e.start_point(), e.end_point()
                out.append(((s.X, s.Y), (t.X, t.Y)))
        except Exception:
            pass
    return out


def _split_circles(edges):
    """Replace any full-circle Edge with two half-arc Edges.

    ``trace()`` builds a clean thin-ring face from two half-arcs, but a single
    closed circle yields a face with a degenerate seam edge that the SVG
    exporter cannot adapt ("BRepAdaptor_Curve::No geometry"). Splitting the
    circle into half-arcs avoids the seam while keeping the same ring geometry.
    """
    out = []
    for e in edges:
        try:
            is_full_circle = (
                e.geom_type == GeomType.CIRCLE and e.is_closed
            )
        except Exception:
            is_full_circle = False
        if is_full_circle:
            r = e.radius
            centre = e.arc_center
            loc = Location(centre)
            for a0, a1 in ((0, 180), (180, 360)):
                out += [arc.moved(loc) for arc in
                        Edge.make_circle(r, start_angle=a0, end_angle=a1).edges()]
        else:
            out.append(e)
    return out


def _strokes_and_text(strokes, text_faces, line_width):
    """Convert centreline Edges to thin filled faces (build123d line-as-face model) and
    group with text faces into one Sketch of faces. Returns (sketch, segments).

    Each stroke is traced *individually* and the faces grouped (not boolean-fused):
    a single trace() over the whole set unions the thin bands, and where a line
    meets an arc/line endpoint exactly (e.g. a datum-target diameter touching its
    ring) that union degenerates to a zero-area face. Per-edge tracing avoids it;
    overlapping thin bands render identically on a fill layer.
    """
    seg = _segments(strokes)
    faces = []
    for e in _split_circles(strokes):
        try:
            faces += trace([e], line_width=line_width).faces()
        except Exception:
            pass
    faces += list(text_faces)
    return Sketch(children=faces), seg


# ---------------------------------------------------------------------------
# Annotation base — transform-aware lint metadata
# ---------------------------------------------------------------------------

def _rot_pt(pt, ang_deg):
    """Rotate (x, y) about the origin by ang_deg degrees (Z axis)."""
    a = math.radians(ang_deg)
    c, s = math.cos(a), math.sin(a)
    return (pt[0] * c - pt[1] * s, pt[0] * s + pt[1] * c)


def _xf_pt(pt, ang_deg, off):
    """Apply a Location's transform to a 2D point: rotate about origin, then translate."""
    rx, ry = _rot_pt(pt, ang_deg)
    return (rx + off[0], ry + off[1])


def _xf_bbox(box, ang_deg, off):
    """Transform an AABB by (rotate, translate) and return the new AABB of its corners."""
    pts = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    t = [_xf_pt(p, ang_deg, off) for p in pts]
    xs = [p[0] for p in t]
    ys = [p[1] for p in t]
    return (min(xs), min(ys), max(xs), max(ys))


def _bake_pt(pt, off, rot):
    """Bake construction-time align (translate by *off*) then rotation (*rot*°) into a point.

    Mirrors BaseSketchObject.__init__, which moves the geometry by the align
    offset first and then rotates it about the origin.
    """
    return _rot_pt((pt[0] + off[0], pt[1] + off[1]), rot)


def _bake_bbox(box, off, rot):
    pts = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    t = [_bake_pt(p, off, rot) for p in pts]
    xs = [p[0] for p in t]
    ys = [p[1] for p in t]
    return (min(xs), min(ys), max(xs), max(ys))


class _Annotation(BaseSketchObject):
    """Shared base for the drawing-annotation sketch objects.

    Centralises the construct-and-store boilerplate and, crucially, keeps the
    lint metadata (``label_bbox`` / ``segments`` and, on leaders, ``tip`` /
    ``elbow``) consistent with the geometry under transforms. The values are
    cached in the object's *build* frame; the properties apply the object's
    current ``.location`` (so ``.moved()`` / ``.located()`` / ``.rotate()`` all
    track), and the construction-time ``rotation`` / ``align`` — which
    ``BaseSketchObject`` bakes into the geometry — is baked into the cache too.
    """

    def __init__(self, sketch, *, label="", label_bbox=None, segments=None,
                 rotation=0, align=None, mode=Mode.ADD):
        # The align offset BaseSketchObject will apply — measured before super()
        # mutates `sketch` (its align path moves the sketch in place).
        off = (0.0, 0.0)
        if align is not None:
            al = align if isinstance(align, (tuple, list)) else (align, align)
            v = sketch.bounding_box().to_align_offset(al)
            off = (v.X, v.Y)
        super().__init__(sketch, rotation=rotation, align=align, mode=mode)
        self._init_off = off
        self._init_rot = rotation
        self.label = label
        self._label_bbox_local = _bake_bbox(label_bbox, off, rotation) if label_bbox else None
        self._segments_local = [
            (_bake_pt(a, off, rotation), _bake_pt(b, off, rotation))
            for a, b in (segments or [])
        ]

    def _loc(self):
        return self.location.orientation.Z, (self.location.position.X, self.location.position.Y)

    def _bake_point(self, pt):
        """Bake construction transform into a build-frame point (for subclass coords)."""
        return _bake_pt(pt, self._init_off, self._init_rot)

    def _live_point(self, pt):
        """Apply the current .location to a baked build-frame point."""
        return _xf_pt(pt, *self._loc())

    @property
    def label_bbox(self):
        if self._label_bbox_local is None:
            return None
        return _xf_bbox(self._label_bbox_local, *self._loc())

    @property
    def segments(self):
        ang, off = self._loc()
        return [(_xf_pt(a, ang, off), _xf_pt(b, ang, off)) for a, b in self._segments_local]


# ---------------------------------------------------------------------------
# Draft preset
# ---------------------------------------------------------------------------

def draft_preset(font_size: float = 2.5, decimal_precision: int = 2,
                 **overrides) -> Draft:
    """A ``Draft`` tuned for clean technical-drawing output.

    Scales the arrowhead to the font and uses a thin line, closer to ISO
    drawing weights::

        arrow_length = 0.9 * font_size
        line_width   = 0.1

    Any field can be overridden by keyword.
    """
    params = dict(
        font_size=font_size,
        decimal_precision=decimal_precision,
        arrow_length=0.9 * font_size,
        line_width=0.1,
    )
    params.update(overrides)
    return Draft(**params)


# ---------------------------------------------------------------------------
# dim_linear  ->  Dimension / SafeDimension
# ---------------------------------------------------------------------------

_SIDE_VECTORS: dict[str, tuple[float, float, float]] = {
    "above": (0.0,  1.0, 0.0),
    "below": (0.0, -1.0, 0.0),
    "left":  (-1.0, 0.0, 0.0),
    "right": ( 1.0, 0.0, 0.0),
}


def _offset_sign(p1, p2, toward) -> int:
    """Return +1 or -1 so ExtensionLine(offset=sign*d) places the dim toward *toward*."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    right = Vector(dy, -dx, 0.0)
    if right.length < 1e-10:
        return 1
    right = right.normalized()
    tw = Vector(*toward).normalized()
    return 1 if right.dot(tw) >= 0 else -1


def _format_label(length, draft, tolerance) -> str:
    """Approximate the label string build123d would generate, for lint checks."""
    prec = draft.decimal_precision
    s = f"{round(length, prec):.{prec}f}"
    if tolerance is None:
        return s
    if isinstance(tolerance, (int, float)):
        t = f"{round(tolerance, prec):.{prec}f}"
        return f"{s} ±{t}"
    lo = f"{round(tolerance[0], prec):.{prec}f}"
    hi = f"{round(tolerance[1], prec):.{prec}f}"
    return f"{s} +{hi} -{lo}"


class Dimension(_Annotation):
    """ExtensionLine wrapper with named placement side, as a native Sketch.

    Args:
        p1, p2: endpoints of the segment to dimension (3-tuple or 2-tuple).
        side:   "above", "below", "left", "right", or an explicit world-direction
                vector (e.g. (0, 1, 0) for above).
        distance: perpendicular distance from the segment to the dim line (>0).
        draft:  Draft config.
        label:  override label string; if None the measured length is formatted.
        tolerance: symmetric float or (lower, upper) pair appended to the label.
        label_offset_x: signed distance (mm) to shift the label along the dim line
            away from the midpoint. Positive shifts toward p2; negative toward p1.
        basic: draw a rectangle around the value, marking it a *basic*
            (theoretically-exact) dimension per ISO 1101 / ASME Y14.5. The box is
            four separate Edges so it strokes cleanly.

    Metadata attributes: ``.label``, ``.label_bbox`` (min_x, min_y, max_x, max_y),
    ``.measured_length``, ``.dim_level_y``, ``.is_basic``, ``.segments``.
    """

    def __init__(
        self,
        p1: tuple,
        p2: tuple,
        side,
        distance: float,
        draft: Draft,
        label: str | None = None,
        tolerance=None,
        label_offset_x: float = 0.0,
        basic: bool = False,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        toward = _SIDE_VECTORS[side] if isinstance(side, str) else tuple(side)
        sign = _offset_sign(p1, p2, toward)
        offset = sign * abs(distance)

        measured_label = label
        if label_offset_x != 0.0:
            measured_label = ""  # suppress built-in label; we'll place our own Text

        _force_external = False
        try:
            el = ExtensionLine(
                border=[p1, p2],
                offset=offset,
                draft=draft,
                label=measured_label,
                tolerance=tolerance,
                mode=Mode.PRIVATE,
            )
        except ValueError:
            el = ExtensionLine(border=[p1, p2], offset=offset, draft=draft,
                               label=None, tolerance=None, mode=Mode.PRIVATE)
            label_offset_x = label_offset_x or 0.0
            _force_external = True

        measured = el.dimension
        label_str = label if label is not None else _format_label(measured, draft, tolerance)

        bb = el.bounding_box()
        dim_level_y = bb.max.Y if abs(bb.max.Y) >= abs(bb.min.Y) else bb.min.Y

        mid_x = (p1[0] + p2[0]) / 2.0
        mid_y = (p1[1] + p2[1]) / 2.0
        dxp, dyp = p2[0] - p1[0], p2[1] - p1[1]
        plen = math.hypot(dxp, dyp) or 1.0
        ux, uy = dxp / plen, dyp / plen        # path direction
        nx, ny = uy, -ux                       # right-hand normal
        label_cx = mid_x + nx * offset + ux * label_offset_x
        label_cy = mid_y + ny * offset + uy * label_offset_x
        vertical = abs(dyp) > abs(dxp)

        probe = Text(
            txt=label_str, font_size=draft.font_size, font=draft.font,
            align=Align.CENTER, mode=Mode.PRIVATE,
        )
        text_bb = probe.bounding_box()
        half_w = text_bb.size.X / 2.0
        half_h = text_bb.size.Y / 2.0
        hx, hy = (half_h, half_w) if vertical else (half_w, half_h)
        label_bbox_tuple = (label_cx - hx, label_cy - hy, label_cx + hx, label_cy + hy)

        # ExtensionLine geometry: keep its faces directly (already filled).
        faces = list(el.faces())
        strokes: list[Edge] = []
        extra_text: list = []

        if label_offset_x != 0.0 or _force_external:
            extra_text.append(Text(
                txt=label_str, font_size=draft.font_size, font=draft.font,
                align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE,
            ).moved(Location(Vector(label_cx, label_cy, 0.0),
                             Vector(0, 0, 1), 90.0 if vertical else 0.0)))

        if basic:
            bpad = 0.4 * draft.font_size
            bx0, by0, bx1, by1 = label_bbox_tuple
            bx0 -= bpad; by0 -= bpad; bx1 += bpad; by1 += bpad
            corners = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1), (bx0, by0)]
            strokes += [Edge.make_line(Vector(a[0], a[1], 0), Vector(b[0], b[1], 0))
                        for a, b in zip(corners, corners[1:])]
            label_bbox_tuple = (bx0, by0, bx1, by1)

        # Combine ExtensionLine faces, any box strokes (as thin faces) and extra text.
        if strokes:
            faces += trace(strokes, line_width=line_width).faces()
        faces += extra_text
        sk = Sketch(children=faces)

        # segments come from the ExtensionLine's straight edges + box strokes
        seg = _segments(el.edges()) + _segments(strokes)

        super().__init__(sk, label=label_str, label_bbox=label_bbox_tuple,
                         segments=seg, rotation=rotation, align=align, mode=mode)
        self.measured_length = measured
        self.dim_level_y = dim_level_y
        self.is_basic = basic


def _truncate(s: str, max_len: int = 6) -> str:
    return s[:max_len] + "…" if len(s) > max_len else s


class SafeDimension(_Annotation):
    """DimensionLine wrapper that won't crash on labels longer than the path.

    build123d raises ValueError("Can't get geom adaptor of empty wire") when the
    label is wider than the dimension path. This retries with a truncated label;
    if both attempts fail, it falls back to a bare line.

    Metadata: ``.label``, ``.label_bbox`` (None), ``.measured_length``, ``.segments``.
    """

    def __init__(
        self,
        path,
        label: str,
        draft: Draft,
        fallback_label: str | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        if isinstance(path, (list, tuple)):
            edge = Edge.make_line(Vector(*path[0][:3]), Vector(*path[1][:3]))
        else:
            edge = path
        measured = edge.length

        chosen_label = fallback_label or _truncate(label)
        faces = None
        seg: list = []
        for lbl in [label, fallback_label or _truncate(label)]:
            try:
                dl = DimensionLine(path=path, draft=draft, label=lbl, mode=Mode.PRIVATE)
                # compute segments before committing `faces`, so a failure here can't
                # leave faces set with seg unbound (the else-branch would then NameError).
                seg = _segments(dl.edges())
                faces = list(dl.faces())
                chosen_label = lbl
                break
            except Exception:
                continue

        if faces is None:
            # Last resort — bare edge so the drawing isn't missing the line
            sk, seg = _strokes_and_text([edge], [], line_width)
        else:
            sk = Sketch(children=faces)

        super().__init__(sk, label=chosen_label, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.measured_length = measured


# ---------------------------------------------------------------------------
# centerline  ->  Centerline
# ---------------------------------------------------------------------------

class Centerline(_Annotation):
    """A centreline between two points — a single thin line rendered as a face.

    Metadata: ``.label`` (""), ``.segments``, ``.is_centerline`` (True).
    """

    def __init__(
        self,
        p1: tuple,
        p2: tuple,
        draft: Draft | None = None,  # noqa: ARG002 — reserved for dash patterns
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        v1 = Vector(p1[0], p1[1], p1[2] if len(p1) > 2 else 0.0)
        v2 = Vector(p2[0], p2[1], p2[2] if len(p2) > 2 else 0.0)
        edge = Edge.make_line(v1, v2)
        sk, seg = _strokes_and_text([edge], [], line_width)
        super().__init__(sk, label="", label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.is_centerline = True


# ---------------------------------------------------------------------------
# leader  ->  Leader
# ---------------------------------------------------------------------------

class Leader(_Annotation):
    """Leader annotation with arrowhead at *tip* and label hanging from *elbow*.

    The horizontal shelf runs away from *tip* so the label text sits cleanly
    after the shelf end — the line never passes through the label bbox.

    Args:
        tip:   arrow point on the part feature (x, y[, z]).
        elbow: where the shaft bends to become the horizontal shelf (x, y[, z]).
        label: annotation text.
        draft: Draft config.
        all_around: draw the ISO 1101 all-around circle at the kink.
        all_over: draw the all-over double circle at the kink.

    Metadata: ``.label``, ``.label_bbox``, ``.tip``, ``.elbow``, ``.segments``.
    """

    def __init__(
        self,
        tip: tuple,
        elbow: tuple,
        label: str,
        draft: Draft,
        all_around: bool = False,
        all_over: bool = False,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        tip_v = Vector(tip[0], tip[1], 0.0)
        elbow_v = Vector(elbow[0], elbow[1], 0.0)

        probe = Text(
            txt=label, font_size=draft.font_size, font=draft.font,
            align=Align.CENTER, mode=Mode.PRIVATE,
        )
        text_w = probe.bounding_box().size.X  # noqa: F841 — kept for clarity/parity
        gap = draft.pad_around_text

        shelf_dir = 1.0 if elbow_v.X >= tip_v.X else -1.0
        shelf_len = gap
        shelf_end_v = Vector(elbow_v.X + shelf_dir * shelf_len, elbow_v.Y, 0.0)

        # Arrow: shaft from tip to elbow with arrowhead at tip (filled faces)
        shaft_edge = Edge.make_line(tip_v, elbow_v)
        arrow_shape = Arrow(
            arrow_size=draft.arrow_length,
            shaft_path=shaft_edge,
            shaft_width=draft.line_width,
            head_at_start=True,
            mode=Mode.PRIVATE,
        )

        # Horizontal shelf as a swept filled rectangle (Face)
        shelf_edge = Edge.make_line(elbow_v, shelf_end_v)
        shelf_pen = shelf_edge.perpendicular_line(draft.line_width, 0)
        shelf_shape = sweep(shelf_pen, shelf_edge, mode=Mode.PRIVATE)

        faces = list(arrow_shape.faces()) + list(shelf_shape.faces())
        ring_strokes: list[Edge] = []

        if all_around or all_over:
            r = 0.7 * draft.arrow_length
            radii = [r, 1.7 * r] if all_over else [r]
            for rad in radii:
                for a0, a1 in ((0, 180), (180, 360)):
                    ring_strokes += [
                        e.moved(Location(elbow_v))
                        for e in Edge.make_circle(rad, start_angle=a0, end_angle=a1).edges()
                    ]
        if ring_strokes:
            faces += trace(ring_strokes, line_width=line_width).faces()

        # Text centred vertically at shelf height, inset by one gap
        if shelf_dir > 0:
            text_align = (Align.MIN, Align.CENTER)
            text_x = elbow_v.X + gap
        else:
            text_align = (Align.MAX, Align.CENTER)
            text_x = elbow_v.X - gap

        text_shape = Text(
            txt=label, font_size=draft.font_size, font=draft.font,
            align=text_align, mode=Mode.PRIVATE,
        ).moved(Location(Vector(text_x, elbow_v.Y, 0.0)))
        _tb = text_shape.bounding_box()
        faces.append(text_shape)

        sk = Sketch(children=faces)
        seg = (_segments([shaft_edge, shelf_edge]))

        super().__init__(sk, label=label,
                         label_bbox=(_tb.min.X, _tb.min.Y, _tb.max.X, _tb.max.Y),
                         segments=seg, rotation=rotation, align=align, mode=mode)
        self._tip_local = self._bake_point((tip_v.X, tip_v.Y))
        self._elbow_local = self._bake_point((elbow_v.X, elbow_v.Y))

    @property
    def tip(self):
        return self._live_point(self._tip_local)

    @property
    def elbow(self):
        return self._live_point(self._elbow_local)


_COMPASS_ANGLES = {
    "E":  0.0, "NE": 45.0, "N":  90.0, "NW": 135.0,
    "W":  180.0, "SW": 225.0, "S":  270.0, "SE": 315.0,
}


def leader_offset(
    tip: tuple,
    direction,
    length: float,
    label: str,
    draft: Draft,
) -> Leader:
    """Leader with the elbow placed by direction + distance instead of absolute coords.

    Computes ``elbow = tip + (cos θ, sin θ) * length`` and returns a ``Leader``.

    Args:
        direction: compass string ("N", "NE", … case-insensitive) or an angle in
                   degrees CCW from +X.
    """
    if isinstance(direction, str):
        key = direction.strip().upper()
        if key not in _COMPASS_ANGLES:
            raise ValueError(
                f"direction {direction!r} not recognised; expected one of "
                f"{sorted(_COMPASS_ANGLES)} or a numeric angle in degrees"
            )
        angle_deg = _COMPASS_ANGLES[key]
    else:
        angle_deg = float(direction)

    theta = math.radians(angle_deg)
    elbow = (tip[0] + math.cos(theta) * length, tip[1] + math.sin(theta) * length)
    return Leader(tip=tip, elbow=elbow, label=label, draft=draft)


# ---------------------------------------------------------------------------
# view_axes — pure Python helpers (no OCC import)
# ---------------------------------------------------------------------------

def _dot3(a, b): return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]
def _sub3(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def _scale3(s, v): return (s*v[0], s*v[1], s*v[2])
def _cross3(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _norm3(v):
    mag = math.sqrt(_dot3(v, v))
    return (v[0]/mag, v[1]/mag, v[2]/mag) if mag > 1e-10 else (0.0, 0.0, 0.0)


def view_axes(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> dict[str, tuple[str, float]]:
    """Return the world→page axis mapping for a project_to_viewport call."""
    vo = tuple(float(x) for x in viewport_origin)
    la = tuple(float(x) for x in look_at)
    vu = tuple(float(x) for x in viewport_up)

    view_dir = _norm3(_sub3(la, vo))
    page_y = _norm3(_sub3(vu, _scale3(_dot3(vu, view_dir), view_dir)))
    page_x = _cross3(view_dir, page_y)

    result: dict[str, tuple[str, float]] = {}
    for name, world_v in [
        ("world_X", (1.0, 0.0, 0.0)),
        ("world_Y", (0.0, 1.0, 0.0)),
        ("world_Z", (0.0, 0.0, 1.0)),
    ]:
        px = _dot3(world_v, page_x)
        py = _dot3(world_v, page_y)
        if abs(px) < 1e-9 and abs(py) < 1e-9:
            result[name] = ("depth", 0.0)
        elif abs(px) >= abs(py):
            result[name] = ("page_X", float(round(px / abs(px), 1)))
        else:
            result[name] = ("page_Y", float(round(py / abs(py), 1)))

    return result


# ---------------------------------------------------------------------------
# place_dims
# ---------------------------------------------------------------------------

def place_dims(
    specs: list[tuple],
    draft: Draft,
    base_distance: float = 8.0,
    tier_spacing: float | None = None,
) -> list[Dimension]:
    """Build a stack of dims with automatically assigned offsets.

    Specs without a distance argument are placed on successive tiers when their
    spans overlap; non-overlapping spans share a tier.

    Args:
        specs: list of (p1, p2, side, label) or (p1, p2, side, label, tolerance).

    Returns:
        list[Dimension], same length and order as specs.
    """
    if tier_spacing is None:
        tier_spacing = draft.font_size * 3.0 + draft.arrow_length

    tier_occupancy: list[list[tuple[float, float]]] = []
    results = []
    for spec in specs:
        p1, p2, side = spec[0], spec[1], spec[2]
        label = spec[3]
        tolerance = spec[4] if len(spec) > 4 else None

        toward = _SIDE_VECTORS[side] if isinstance(side, str) else tuple(side)
        use_x = abs(toward[1]) >= abs(toward[0])
        span = (
            (min(p1[0], p2[0]), max(p1[0], p2[0])) if use_x
            else (min(p1[1], p2[1]), max(p1[1], p2[1]))
        )

        assigned = None
        for t, occupied in enumerate(tier_occupancy):
            if all(span[1] <= s[0] or span[0] >= s[1] for s in occupied):
                assigned = t
                occupied.append(span)
                break
        if assigned is None:
            assigned = len(tier_occupancy)
            tier_occupancy.append([span])

        offset = base_distance + assigned * tier_spacing
        results.append(
            Dimension(p1, p2, side, offset, draft, label=label, tolerance=tolerance)
        )

    return results


# ---------------------------------------------------------------------------
# place_labels
# ---------------------------------------------------------------------------

def _centerline_extent(cl_item):
    """Return (min_x, min_y, max_x, max_y) for a centreline.

    Prefers the zero-width ``.segments`` (the true centreline) so a thin-faced
    centreline still reads as a zero-width vertical/horizontal line; falls back
    to the rendered ``.bounding_box()`` (which is line_width wide).
    """
    segs = getattr(cl_item, "segments", None)
    if segs:
        xs = [p[0] for s in segs for p in s]
        ys = [p[1] for s in segs for p in s]
        return (min(xs), min(ys), max(xs), max(ys))
    bb = cl_item.bounding_box()
    return (bb.min.X, bb.min.Y, bb.max.X, bb.max.Y)


def _compute_label_offset_x(dim: Dimension, centerlines, gap: float) -> float:
    """Cumulative label_offset_x to clear all crossing vertical centerlines."""
    if dim.label_bbox is None:
        return 0.0
    lmin_x, _lmin_y, lmax_x, _lmax_y = dim.label_bbox
    label_cx = (lmin_x + lmax_x) / 2.0
    half_w = (lmax_x - lmin_x) / 2.0
    total = 0.0
    for cl in centerlines:
        if not getattr(cl, "is_centerline", False):
            continue
        try:
            cl_min_x, _, cl_max_x, _ = _centerline_extent(cl)
        except Exception:
            continue
        if cl_max_x - cl_min_x >= 0.1:
            continue  # not a vertical centerline
        cl_x = (cl_min_x + cl_max_x) / 2.0
        eff_lmin = lmin_x + total
        eff_lmax = lmax_x + total
        if not (eff_lmin < cl_x < eff_lmax):
            continue
        eff_cx = label_cx + total
        shift_right = cl_x + half_w + gap - eff_cx
        shift_left = cl_x - half_w - gap - eff_cx
        total += shift_right if abs(shift_right) <= abs(shift_left) else shift_left
    return total


def place_labels(
    specs: list[tuple],
    draft: Draft,
    centerlines: list,
    gap: float = 1.0,
) -> list[Dimension]:
    """Build dims from specs, auto-shifting labels to clear vertical centerlines.

    Args:
        specs: list of (p1, p2, side, distance, label) or (..., tolerance) tuples.
        centerlines: list of Centerline objects to avoid.

    Returns:
        list[Dimension], same length as specs.
    """
    results = []
    for spec in specs:
        p1, p2, side, distance, label = spec[:5]
        tolerance = spec[5] if len(spec) > 5 else None
        dim = Dimension(p1, p2, side, distance, draft, label=label, tolerance=tolerance)
        offset_x = _compute_label_offset_x(dim, centerlines, gap)
        if offset_x != 0.0:
            dim = Dimension(p1, p2, side, distance, draft, label=label,
                            tolerance=tolerance, label_offset_x=offset_x)
        results.append(dim)
    return results


# ---------------------------------------------------------------------------
# lint_drawing — generic / duck-typed
# ---------------------------------------------------------------------------

def lint_drawing(items, part_bbox=None, drawing_scale: float = 1.0) -> list[LintIssue]:
    """Structural checks on a composed annotation list, duck-typed.

    Dispatch is by attribute presence, not type:

    - leader-like  (``.elbow is not None``): elbow-through-label check.
    - dimension-like (``.measured_length is not None``): label-vs-measured and
      dim-inside-part checks.
    - centerline-like (``.is_centerline``): pairwise overlap against dims.

    Args:
        items: annotation objects exposing the relevant attrs (or SimpleNamespace
            stand-ins).
        part_bbox: optional BoundBox of the projected part outline.
        drawing_scale: the N:1 factor the geometry was scaled by before
            projecting (e.g. ``5.0`` for a 7.5 mm feature drawn at 5:1). The
            label-vs-measured check divides each measured path length by this
            before comparing to the label value, so labels carry the *real*
            dimension while the geometry is drawn enlarged. Defaults to ``1.0``
            (no scaling). See :func:`format_drawing_scale` to render the
            matching "5:1" indicator in the title block.

    Returns:
        list[LintIssue].

    Raises:
        ValueError: if ``drawing_scale`` is not positive (matches
            :func:`format_drawing_scale` / :class:`TitleBlock`).
    """
    if drawing_scale <= 0:
        raise ValueError(f"drawing_scale must be positive, got {drawing_scale}")

    issues: list[LintIssue] = []

    for item in items:
        if getattr(item, "elbow", None) is not None:
            _lint_leader(item, issues)
        elif getattr(item, "measured_length", None) is not None:
            _lint_dim(item, part_bbox, issues, drawing_scale)

    for i, item_a in enumerate(items):
        for item_b in items[i + 1:]:
            try:
                is_cl_a = getattr(item_a, "is_centerline", False)
                is_cl_b = getattr(item_b, "is_centerline", False)

                if is_cl_a and is_cl_b:
                    continue

                if is_cl_a or is_cl_b:
                    dim_item = item_b if is_cl_a else item_a
                    cl_item = item_a if is_cl_a else item_b
                    _lint_centerline_dim_overlap(dim_item, cl_item, issues)
                    continue

                level_a = getattr(item_a, "dim_level_y", None)
                level_b = getattr(item_b, "dim_level_y", None)
                if (level_a is not None and level_b is not None
                        and abs(level_a - level_b) > 3.0):
                    continue
                ba = item_a.bounding_box()
                bb = item_b.bounding_box()
                ox = max(0.0, min(ba.max.X, bb.max.X) - max(ba.min.X, bb.min.X))
                oy = max(0.0, min(ba.max.Y, bb.max.Y) - max(ba.min.Y, bb.min.Y))
                if ox > 0.5 and oy > 0.5:
                    la = getattr(item_a, "label", "?")
                    lb = getattr(item_b, "label", "?")
                    issues.append(LintIssue(
                        severity="warning",
                        message=(
                            f"annotations '{la}' and '{lb}' overlap by "
                            f"{ox:.1f}×{oy:.1f} mm — increase offset or spacing"
                        ),
                        code="annotation_overlap",
                    ))
            except Exception:
                pass

    return issues


def _lint_centerline_dim_overlap(dim_item, cl_item, issues) -> None:
    """Flag label-vs-centerline overlap for a (dim, centerline) pair."""
    try:
        cl_min_x, cl_min_y, cl_max_x, cl_max_y = _centerline_extent(cl_item)

        label_bbox = getattr(dim_item, "label_bbox", None)
        if label_bbox is not None:
            lmin_x, lmin_y, lmax_x, lmax_y = label_bbox
        else:
            db = dim_item.bounding_box()
            lmin_x, lmin_y = db.min.X, db.min.Y
            lmax_x, lmax_y = db.max.X, db.max.Y

        cl_w = cl_max_x - cl_min_x
        cl_h = cl_max_y - cl_min_y

        if cl_w < 0.1:
            cl_x = (cl_min_x + cl_max_x) / 2.0
            ox = min(cl_x - lmin_x, lmax_x - cl_x) if lmin_x < cl_x < lmax_x else 0.0
        else:
            ox = max(0.0, min(lmax_x, cl_max_x) - max(lmin_x, cl_min_x))

        if cl_h < 0.1:
            cl_y = (cl_min_y + cl_max_y) / 2.0
            oy = min(cl_y - lmin_y, lmax_y - cl_y) if lmin_y < cl_y < lmax_y else 0.0
        else:
            oy = max(0.0, min(lmax_y, cl_max_y) - max(lmin_y, cl_min_y))

        if ox > 0.5 and oy > 0.5:
            dim_label = getattr(dim_item, "label", "?")
            issues.append(LintIssue(
                severity="warning",
                message=(
                    f"label '{dim_label}' overlaps centerline by "
                    f"{ox:.1f}×{oy:.1f} mm — use label_offset_x to shift "
                    f"or increase dim offset to clear the centerline"
                ),
                code="label_centerline_overlap",
            ))
    except Exception:
        pass


def _lint_dim(item, part_bbox, issues, drawing_scale: float = 1.0) -> None:
    label = getattr(item, "label", "") or ""
    measured = getattr(item, "measured_length", None)

    nums = re.findall(r"\d+\.?\d*", label.split("±")[0].split("+")[0].lstrip("ø⌀Rr"))
    if nums and measured is not None:
        try:
            label_val = float(nums[0])
            # When drawing_scale != 1.0 the geometry was scaled up before projecting
            # (e.g. part.scale(5) for a 7.5 mm feature drawn at 5:1). The measured
            # path length is the *scaled* length; the label carries the *real* value.
            # Divide measured by the scale factor before comparing so a 37.5 mm
            # measured segment with label "7.5" at 5:1 is accepted, not flagged.
            # drawing_scale is guaranteed positive by lint_drawing()'s validation.
            effective_measured = measured / drawing_scale
            if effective_measured > 1e-6:
                ratio = abs(label_val - effective_measured) / effective_measured
                if ratio > 0.005:
                    issues.append(LintIssue(
                        severity="warning",
                        message=(
                            f"Dim '{label}': label value {label_val:.3f} differs from "
                            f"measured path length {measured:.3f}"
                            + (f" (÷{drawing_scale} = {effective_measured:.3f})"
                               if drawing_scale != 1.0 else "")
                            + f" by {ratio*100:.1f}% "
                            f"— possible axis swap or wrong endpoint"
                        ),
                        code="label_vs_measured",
                    ))
        except ValueError:
            pass

    if part_bbox is not None:
        try:
            db = item.bounding_box()
        except Exception:
            return
        ox = max(0.0, min(db.max.X, part_bbox.max.X) - max(db.min.X, part_bbox.min.X))
        oy = max(0.0, min(db.max.Y, part_bbox.max.Y) - max(db.min.Y, part_bbox.min.Y))
        overlap = ox * oy
        dim_area = max((db.max.X - db.min.X) * (db.max.Y - db.min.Y), 1e-9)
        if overlap / dim_area > 0.10:
            issues.append(LintIssue(
                severity="warning",
                message=(
                    f"Dim '{label}': annotation bbox overlaps part outline by "
                    f"{overlap/dim_area*100:.0f}% — offset sign may place it inside the view"
                ),
                code="dim_inside_part",
            ))


def _lint_leader(item, issues) -> None:
    try:
        box = getattr(item, "label_bbox", None)
        if box is not None:
            minx, miny, maxx, maxy = box
        else:
            tb = item.bounding_box()
            minx, miny, maxx, maxy = tb.min.X, tb.min.Y, tb.max.X, tb.max.Y
        ex, ey = item.elbow
        if minx <= ex <= maxx and miny <= ey <= maxy:
            issues.append(LintIssue(
                severity="error",
                message=(
                    f"Leader '{getattr(item, 'label', '?')}': elbow point "
                    f"({ex:.2f}, {ey:.2f}) is inside the label bbox — leader "
                    f"line passes through the text"
                ),
                location=item.elbow,
                code="leader_line_through_text",
            ))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# iso_title_block  ->  TitleBlock
# ---------------------------------------------------------------------------

def format_drawing_scale(scale: float) -> str:
    """Format an N:1 drawing-scale factor as a conventional ISO scale string.

    Enlargements (``scale > 1``) render as ``"N:1"``; reductions
    (``scale < 1``) as ``"1:M"``; ``1.0`` as ``"1:1"``. Integer ratios drop the
    trailing ``.0`` ("5:1", not "5.0:1"); non-integer ratios keep their
    significant decimals ("2.5:1").

    This produces the indicator string that matches the ``drawing_scale`` passed
    to :func:`lint_drawing` — pass it to :class:`TitleBlock` (or build123d's
    ``TechnicalDrawing``) so the printed scale and the linted scale agree.

    Raises:
        ValueError: if ``scale`` is not positive.
    """
    if scale <= 0:
        raise ValueError(f"drawing_scale must be positive, got {scale}")
    if scale >= 1.0:
        return f"{scale:g}:1"
    return f"1:{1.0 / scale:g}"


_TB_COL_FRACTIONS = [0.40, 0.20, 0.15, 0.15, 0.10]


class TitleBlock(_Annotation):
    """ISO-style 2-row title block built at the origin (bottom-left at (0, 0)).

    Layout::

        ┌──────────────────┬─────────┬──────┬──────┬──────┐
        │  part_name       │ dwg_no  │scale │ mat  │ date │
        ├──────────────────┼─────────┴──────┴──────┴──────┤
        │ general_tolerance│    designed_by               │
        └──────────────────┴─────────────────────────────-┘

    Column proportions 40 / 20 / 15 / 15 / 10 %.

    The scale cell takes either an explicit ``scale`` string ("1:1") or, for
    scaled drawings, a numeric ``drawing_scale`` (e.g. ``5.0``) which is
    formatted to "5:1" via :func:`format_drawing_scale` and overrides ``scale``.
    Pass the same ``drawing_scale`` to :func:`lint_drawing` so the printed
    indicator and the label-vs-measured check stay in agreement.

    Metadata: ``.label`` (part_name), ``.label_bbox`` (None), ``.segments``,
    ``.block_bbox`` dict ({min_x, min_y, max_x, max_y, width, height}).
    """

    def __init__(
        self,
        part_name: str,
        drawing_number: str,
        scale: str = "1:1",
        material: str = "",
        general_tolerance: str = "",
        designed_by: str = "",
        date: str = "",
        cell_height: float = 8.0,
        width: float = 170.0,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
        drawing_scale: float | None = None,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)

        # A numeric drawing_scale is the single source of truth: it derives the
        # printed "5:1" indicator AND is the divisor lint_drawing() uses for the
        # label-vs-measured check, so the two can never drift. It overrides any
        # explicit `scale` string.
        if drawing_scale is not None:
            scale = format_drawing_scale(drawing_scale)

        col_widths = [f * width for f in _TB_COL_FRACTIONS]
        x: list[float] = [0.0]
        for w in col_widths:
            x.append(x[-1] + w)

        y0, y1, y2 = 0.0, cell_height, 2.0 * cell_height

        strokes: list[Edge] = []
        strokes.append(Edge.make_line(Vector(x[0], y0, 0), Vector(x[-1], y0, 0)))
        strokes.append(Edge.make_line(Vector(x[-1], y0, 0), Vector(x[-1], y2, 0)))
        strokes.append(Edge.make_line(Vector(x[-1], y2, 0), Vector(x[0], y2, 0)))
        strokes.append(Edge.make_line(Vector(x[0], y2, 0), Vector(x[0], y0, 0)))
        strokes.append(Edge.make_line(Vector(x[0], y1, 0), Vector(x[-1], y1, 0)))
        for xi in x[1:-1]:
            strokes.append(Edge.make_line(Vector(xi, y1, 0), Vector(xi, y2, 0)))
        strokes.append(Edge.make_line(Vector(x[1], y0, 0), Vector(x[1], y1, 0)))

        fs = draft.font_size
        font = draft.font

        def _cell_txt(value, cx, cy):
            if not value:
                return None
            return Text(txt=value, font_size=fs, font=font,
                        align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE,
                        ).moved(Location(Vector(cx, cy, 0.0)))

        top_y_mid = (y1 + y2) / 2.0
        top_cells = [
            (part_name,      (x[0] + x[1]) / 2.0),
            (drawing_number, (x[1] + x[2]) / 2.0),
            (scale,          (x[2] + x[3]) / 2.0),
            (material,       (x[3] + x[4]) / 2.0),
            (date,           (x[4] + x[5]) / 2.0),
        ]
        bot_y_mid = (y0 + y1) / 2.0
        bot_cells = [
            (general_tolerance, (x[0] + x[1]) / 2.0),
            (designed_by,       (x[1] + x[-1]) / 2.0),
        ]

        text_faces = [_cell_txt(v, cx, top_y_mid) for v, cx in top_cells]
        text_faces += [_cell_txt(v, cx, bot_y_mid) for v, cx in bot_cells]
        text_faces = [t for t in text_faces if t is not None]

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(sk, label=part_name, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.block_bbox = {
            "min_x": 0.0, "min_y": 0.0,
            "max_x": width, "max_y": y2,
            "width": width, "height": y2,
        }


# ---------------------------------------------------------------------------
# surface_finish_mark  ->  SurfaceFinish
# ---------------------------------------------------------------------------

class SurfaceFinish(_Annotation):
    """ISO 1302 surface finish check-mark symbol with Ra annotation.

    Args:
        ra_value: surface finish annotation (e.g. "Ra 1.6").
        position: world XY position of the symbol tip (x, y[, z]).
        angle:    rotation in degrees, CCW around the tip (default 0).
        draft:    Draft config; defaults to 2.5 mm font.
        size:     diagonal leg length in mm; defaults to 2 × font_size.

    Metadata: ``.label``, ``.label_bbox`` (None), ``.position``, ``.segments``.
    """

    def __init__(
        self,
        ra_value: str,
        position: tuple,
        angle: float = 0.0,
        draft: Draft | None = None,
        size: float | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        leg_len = size if size is not None else 2.0 * draft.font_size

        leg_angle = math.radians(60.0)
        elbow_x = leg_len * math.cos(leg_angle)
        elbow_y = leg_len * math.sin(leg_angle)
        elbow_v = Vector(elbow_x, elbow_y, 0.0)
        top_v = Vector(elbow_x, 1.5 * elbow_y, 0.0)

        leg1 = Edge.make_line(Vector(0.0, 0.0, 0.0), elbow_v)
        leg2 = Edge.make_line(elbow_v, top_v)

        probe = Text(
            txt=ra_value, font_size=draft.font_size, font=draft.font,
            align=Align.CENTER, mode=Mode.PRIVATE,
        )
        text_w = probe.bounding_box().size.X
        gap = draft.pad_around_text
        shelf_end_v = Vector(elbow_x + gap + text_w + gap, elbow_y, 0.0)
        shelf = Edge.make_line(elbow_v, shelf_end_v)

        strokes = [leg1, leg2, shelf]

        v_gap = 0.3 * draft.font_size
        label_text = Text(
            txt=ra_value, font_size=draft.font_size, font=draft.font,
            align=(Align.MIN, Align.MIN), mode=Mode.PRIVATE,
        ).moved(Location(Vector(elbow_x + gap, elbow_y + v_gap, 0.0)))

        # Rotate around origin then translate to position — apply to geometry
        # *before* building the faces so the symbol geometry is correct.
        if angle != 0.0:
            rot_loc = Location((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), angle)
            strokes = [e.moved(rot_loc) for e in strokes]
            label_text = label_text.moved(rot_loc)

        pos_v = Vector(position[0], position[1], 0.0)
        trans_loc = Location(pos_v)
        strokes = [e.moved(trans_loc) for e in strokes]
        label_text = label_text.moved(trans_loc)

        sk, seg = _strokes_and_text(strokes, [label_text], line_width)
        super().__init__(sk, label=ra_value, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.mark_position = (pos_v.X, pos_v.Y)


# ---------------------------------------------------------------------------
# GD&T — feature control frame and datum symbols (ISO 1101 / ISO 5459)
# ---------------------------------------------------------------------------

GDTCharacteristic = Literal[
    "straightness", "flatness", "circularity", "cylindricity",
    "profile_line", "profile_surface",
    "angularity", "perpendicularity", "parallelism",
    "position", "concentricity", "symmetry",
    "circular_runout", "total_runout",
]

_GDT_GLYPHS: dict[str, str] = {
    "straightness": "—", "flatness": "▱", "circularity": "○",
    "cylindricity": "⌭", "profile_line": "⌒", "profile_surface": "⌓",
    "angularity": "∠", "perpendicularity": "⊥", "parallelism": "∥",
    "position": "⌖", "concentricity": "◎", "symmetry": "⌯",
    "circular_runout": "↗", "total_runout": "⏍",
}

_MODIFIER_LETTER = {"M": "M", "L": "L", "P": "P"}


def _arrowhead(tip, back, size) -> list[Edge]:
    """Two short barbs forming an open arrowhead at *tip*, opening toward *back*."""
    tx, ty = tip
    bx, by = back
    ang = math.atan2(by - ty, bx - tx)
    out = []
    for da in (math.radians(20), math.radians(-20)):
        a = ang + da
        out.append(Edge.make_line(
            Vector(tx, ty, 0),
            Vector(tx + size * math.cos(a), ty + size * math.sin(a), 0),
        ))
    return out


def _characteristic_edges(name: str, h: float) -> list[Edge]:
    """Geometric-characteristic glyph as Edges, centred on the local origin."""
    s = 0.42 * h

    if name == "straightness":
        return [Edge.make_line(Vector(-s, 0, 0), Vector(s, 0, 0))]
    if name == "flatness":
        p1, p2 = Vector(-s, -0.5 * s, 0), Vector(0.3 * s, -0.5 * s, 0)
        p3, p4 = Vector(s, 0.5 * s, 0), Vector(-0.3 * s, 0.5 * s, 0)
        return [Edge.make_line(p1, p2), Edge.make_line(p2, p3),
                Edge.make_line(p3, p4), Edge.make_line(p4, p1)]
    if name == "circularity":
        return list(Edge.make_circle(s).edges())
    if name == "cylindricity":
        ring = list(Edge.make_circle(0.55 * s).edges())
        left = Edge.make_line(Vector(-s, -0.6 * s, 0), Vector(-0.55 * s, 0.9 * s, 0))
        right = Edge.make_line(Vector(0.55 * s, -0.9 * s, 0), Vector(s, 0.6 * s, 0))
        return ring + [left, right]
    if name == "profile_line":
        return list(Edge.make_circle(s, start_angle=0, end_angle=180).edges())
    if name == "profile_surface":
        arc = list(Edge.make_circle(s, start_angle=0, end_angle=180).edges())
        chord = Edge.make_line(Vector(-s, 0, 0), Vector(s, 0, 0))
        return arc + [chord]
    if name == "angularity":
        return [Edge.make_line(Vector(-s, -s, 0), Vector(s, s, 0)),
                Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0))]
    if name == "perpendicularity":
        return [Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0)),
                Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0))]
    if name == "parallelism":
        return [Edge.make_line(Vector(-0.7 * s, -s, 0), Vector(0.1 * s, s, 0)),
                Edge.make_line(Vector(-0.1 * s, -s, 0), Vector(0.7 * s, s, 0))]
    if name == "position":
        ring = list(Edge.make_circle(0.6 * s).edges())
        return ring + [Edge.make_line(Vector(-s, 0, 0), Vector(s, 0, 0)),
                       Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0))]
    if name == "concentricity":
        return list(Edge.make_circle(s).edges()) + list(Edge.make_circle(0.5 * s).edges())
    if name == "symmetry":
        return [Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0)),
                Edge.make_line(Vector(-0.7 * s, 0.45 * s, 0), Vector(0.7 * s, 0.45 * s, 0)),
                Edge.make_line(Vector(-0.7 * s, -0.45 * s, 0), Vector(0.7 * s, -0.45 * s, 0))]
    if name == "circular_runout":
        shaft = Edge.make_line(Vector(-s, -s, 0), Vector(s, s, 0))
        return [shaft] + _arrowhead((s, s), (-s, -s), 0.5 * s)
    if name == "total_runout":
        out = []
        for dx in (-0.25 * s, 0.35 * s):
            out.append(Edge.make_line(Vector(-s + dx, -s, 0), Vector(s - 0.6 * s + dx, s, 0)))
            out += _arrowhead((s - 0.6 * s + dx, s), (-s + dx, -s), 0.45 * s)
        return out

    raise ValueError(
        f"Unknown characteristic '{name}'. Supported: {', '.join(_GDT_GLYPHS)}"
    )


class FeatureControlFrame(_Annotation):
    """ISO 1101 feature control frame, e.g. ``| ⌖ | ⌀0.5 Ⓜ | A | B | C |``.

    Built at the origin with its bottom-left corner at (0, 0).

    Metadata: ``.label`` (tolerance str), ``.label_bbox`` (None), ``.segments``,
    ``.characteristic``, ``.tolerance_str``, ``.datums``.
    """

    def __init__(
        self,
        characteristic: GDTCharacteristic,
        tolerance,
        datums=(),
        draft: Draft | None = None,
        diameter: bool = False,
        modifier: str | None = None,
        datum_modifiers: dict | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        name = characteristic.lower()
        if name not in _GDT_GLYPHS:
            raise ValueError(
                f"Unknown characteristic '{characteristic}'. "
                f"Supported: {', '.join(_GDT_GLYPHS)}"
            )

        h = draft.font_size
        H = 2.0 * h
        pad = 0.6 * h
        datum_modifiers = datum_modifiers or {}

        if isinstance(tolerance, str):
            tol_str = tolerance
        else:
            prec = draft.decimal_precision
            tol_str = f"{round(tolerance, prec):.{prec}f}"

        def _text(txt, fs):
            return Text(txt=txt, font_size=fs, font=draft.font,
                        align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE)

        w_sym = H
        r_pre = 0.42 * h
        mr = 0.62 * h
        val_w = _text(tol_str, h).bounding_box().size.X
        w_tol = pad + val_w + pad
        if diameter:
            w_tol += 2 * r_pre + pad
        if modifier:
            w_tol += 2 * mr + pad
        w_dat = H

        widths = [w_sym, w_tol] + [w_dat] * len(datums)
        xs = [0.0]
        for w in widths:
            xs.append(xs[-1] + w)
        total_w = xs[-1]
        cy = H / 2.0

        strokes: list[Edge] = [
            Edge.make_line(Vector(0, 0, 0), Vector(total_w, 0, 0)),
            Edge.make_line(Vector(total_w, 0, 0), Vector(total_w, H, 0)),
            Edge.make_line(Vector(total_w, H, 0), Vector(0, H, 0)),
            Edge.make_line(Vector(0, H, 0), Vector(0, 0, 0)),
        ]
        for x in xs[1:-1]:
            strokes.append(Edge.make_line(Vector(x, 0, 0), Vector(x, H, 0)))

        text_faces = []

        sym_loc = Location(Vector(xs[0] + w_sym / 2, cy, 0))
        for e in _characteristic_edges(name, h):
            strokes.append(e.moved(sym_loc))

        x_cursor = xs[1] + pad
        if diameter:
            dia_cx = x_cursor + r_pre
            strokes += [e.moved(Location(Vector(dia_cx, cy, 0)))
                        for e in Edge.make_circle(r_pre).edges()]
            strokes.append(Edge.make_line(
                Vector(dia_cx + 0.9 * r_pre, cy - 0.9 * r_pre, 0),
                Vector(dia_cx - 0.9 * r_pre, cy + 0.9 * r_pre, 0)))
            x_cursor = dia_cx + r_pre + pad

        val_cx = x_cursor + val_w / 2
        text_faces.append(_text(tol_str, h).moved(Location(Vector(val_cx, cy, 0))))
        x_cursor = val_cx + val_w / 2 + pad

        if modifier:
            m = modifier.upper()
            if m not in _MODIFIER_LETTER:
                raise ValueError(f"Unknown modifier '{modifier}'. Use M, L, or P.")
            mod_cx = x_cursor + mr
            strokes += [e.moved(Location(Vector(mod_cx, cy, 0)))
                        for e in Edge.make_circle(mr).edges()]
            text_faces.append(_text(_MODIFIER_LETTER[m], h * 0.8)
                              .moved(Location(Vector(mod_cx, cy, 0))))

        for i, letter in enumerate(datums):
            cx = (xs[2 + i] + xs[3 + i]) / 2
            dm = datum_modifiers.get(letter)
            if dm:
                text_faces.append(_text(letter, h).moved(Location(Vector(cx - 0.35 * h, cy, 0))))
                mod_cx = cx + 0.5 * h
                strokes += [e.moved(Location(Vector(mod_cx, cy, 0)))
                            for e in Edge.make_circle(0.55 * h).edges()]
                text_faces.append(_text(dm.upper(), h * 0.7).moved(Location(Vector(mod_cx, cy, 0))))
            else:
                text_faces.append(_text(letter, h).moved(Location(Vector(cx, cy, 0))))

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(sk, label=tol_str, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.characteristic = name
        self.tolerance_str = tol_str
        self.datums = tuple(datums)


class DatumFeature(_Annotation):
    """ISO 5459 datum feature symbol: a (filled) triangle on a short leader to a
    framed datum letter. Triangle tip at the origin pointing down (-Y).

    Metadata: ``.label`` (letter), ``.label_bbox`` (None), ``.segments``, ``.letter``.
    """

    def __init__(
        self,
        letter: str,
        draft: Draft | None = None,
        filled: bool = True,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        h = draft.font_size
        tri = 1.4 * h
        box = 2.0 * h

        strokes: list[Edge] = []
        extra_faces: list = []

        apex = Vector(0, 0, 0)
        bl = Vector(-tri / 2, tri * 0.9, 0)
        br = Vector(tri / 2, tri * 0.9, 0)
        if filled:
            from build123d import Face, Wire
            extra_faces.append(Face(Wire.make_polygon([apex, bl, br, apex])))
        else:
            strokes += [Edge.make_line(apex, bl), Edge.make_line(bl, br),
                        Edge.make_line(br, apex)]

        conn_y0 = tri * 0.9
        conn_y1 = conn_y0 + 0.8 * h
        strokes.append(Edge.make_line(Vector(0, conn_y0, 0), Vector(0, conn_y1, 0)))

        by0 = conn_y1
        by1 = conn_y1 + box
        strokes += [
            Edge.make_line(Vector(-box / 2, by0, 0), Vector(box / 2, by0, 0)),
            Edge.make_line(Vector(box / 2, by0, 0), Vector(box / 2, by1, 0)),
            Edge.make_line(Vector(box / 2, by1, 0), Vector(-box / 2, by1, 0)),
            Edge.make_line(Vector(-box / 2, by1, 0), Vector(-box / 2, by0, 0)),
        ]

        glyph = Text(txt=letter, font_size=h, font=draft.font,
                     align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE
                     ).moved(Location(Vector(0, (by0 + by1) / 2, 0)))

        sk, seg = _strokes_and_text(strokes, extra_faces + [glyph], line_width)
        super().__init__(sk, label=letter, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.letter = letter


class DatumTarget(_Annotation):
    """ISO 5459 datum-target symbol: a circle split by a horizontal line into an
    upper compartment (target-area size) and a lower compartment (identifier).
    Centred at the origin.

    Metadata: ``.label`` (identifier), ``.label_bbox`` (None), ``.segments``,
    ``.identifier``, ``.area_label``.
    """

    def __init__(
        self,
        identifier: str,
        area_label: str | None = None,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        h = draft.font_size
        r = 1.7 * h

        strokes = list(Edge.make_circle(r).edges())
        strokes.append(Edge.make_line(Vector(-r, 0, 0), Vector(r, 0, 0)))

        fs = 0.8 * h
        text_faces = [Text(txt=identifier, font_size=fs, font=draft.font,
                           align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE
                           ).moved(Location(Vector(0, -r / 2, 0)))]
        if area_label:
            text_faces.append(Text(txt=area_label, font_size=fs, font=draft.font,
                                   align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE
                                   ).moved(Location(Vector(0, r / 2, 0))))

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(sk, label=identifier, label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.identifier = identifier
        self.area_label = area_label or ""


def _feature_symbol_edges(name: str, h: float) -> list[Edge]:
    """Hole-feature callout glyphs as Edges, centred on the local origin."""
    s = 0.42 * h
    if name == "diameter":
        ring = list(Edge.make_circle(s).edges())
        return ring + [Edge.make_line(Vector(0.9 * s, -0.9 * s, 0),
                                      Vector(-0.9 * s, 0.9 * s, 0))]
    if name == "counterbore":
        return [Edge.make_line(Vector(-s, s, 0), Vector(-s, -s, 0)),
                Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0)),
                Edge.make_line(Vector(s, -s, 0), Vector(s, s, 0))]
    if name == "countersink":
        return [Edge.make_line(Vector(-s, s, 0), Vector(0, -s, 0)),
                Edge.make_line(Vector(s, s, 0), Vector(0, -s, 0))]
    if name == "depth":
        return [Edge.make_line(Vector(0, s, 0), Vector(0, -s, 0))] + \
               _arrowhead((0, -s), (0, s), 0.55 * s)
    raise ValueError(f"Unknown feature symbol '{name}'.")


class CompositeFeatureControlFrame(_Annotation):
    """ISO 1101 *composite* feature control frame — two (or more) tolerance rows
    sharing a single characteristic-symbol cell. Bottom-left at (0, 0).

    Args:
        rows: one dict per row, top → bottom. Keys: ``tolerance`` (required),
            ``datums``, ``diameter``, ``modifier``, ``datum_modifiers``.

    Metadata: ``.label`` (top tolerance), ``.label_bbox`` (None), ``.segments``,
    ``.characteristic``, ``.tolerances``.
    """

    def __init__(
        self,
        characteristic: GDTCharacteristic,
        rows: list[dict],
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        name = characteristic.lower()
        if name not in _GDT_GLYPHS:
            raise ValueError(f"Unknown characteristic '{characteristic}'. "
                             f"Supported: {', '.join(_GDT_GLYPHS)}")
        if not rows:
            raise ValueError("composite frame needs at least one row")

        h = draft.font_size
        H = 2.0 * h
        pad = 0.6 * h
        r_pre = 0.42 * h
        mr = 0.62 * h
        prec = draft.decimal_precision

        def _text(txt, fs):
            return Text(txt=txt, font_size=fs, font=draft.font,
                        align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE)

        def _tol_str(t):
            return t if isinstance(t, str) else f"{round(t, prec):.{prec}f}"

        tol_strs = [_tol_str(r["tolerance"]) for r in rows]
        tol_w = 0.0
        for r, ts in zip(rows, tol_strs):
            w = pad + _text(ts, h).bounding_box().size.X + pad
            if r.get("diameter"):
                w += 2 * r_pre + pad
            if r.get("modifier"):
                w += 2 * mr + pad
            tol_w = max(tol_w, w)
        w_sym = H
        max_datums = max(len(r.get("datums", ())) for r in rows)
        x2 = w_sym + tol_w
        total_w = x2 + max_datums * H
        n = len(rows)
        total_h = n * H

        strokes: list[Edge] = [
            Edge.make_line(Vector(0, 0, 0), Vector(total_w, 0, 0)),
            Edge.make_line(Vector(total_w, 0, 0), Vector(total_w, total_h, 0)),
            Edge.make_line(Vector(total_w, total_h, 0), Vector(0, total_h, 0)),
            Edge.make_line(Vector(0, total_h, 0), Vector(0, 0, 0)),
            Edge.make_line(Vector(w_sym, 0, 0), Vector(w_sym, total_h, 0)),
        ]
        for k in range(1, n):
            strokes.append(Edge.make_line(Vector(w_sym, k * H, 0), Vector(total_w, k * H, 0)))

        text_faces = []
        for e in _characteristic_edges(name, h):
            strokes.append(e.moved(Location(Vector(w_sym / 2, total_h / 2, 0))))

        for i, r in enumerate(rows):
            cy = total_h - (i + 0.5) * H
            top, bot = total_h - i * H, total_h - (i + 1) * H
            strokes.append(Edge.make_line(Vector(x2, bot, 0), Vector(x2, top, 0)))

            x_cursor = w_sym + pad
            if r.get("diameter"):
                dia_cx = x_cursor + r_pre
                strokes += [e.moved(Location(Vector(dia_cx, cy, 0)))
                            for e in Edge.make_circle(r_pre).edges()]
                strokes.append(Edge.make_line(
                    Vector(dia_cx + 0.9 * r_pre, cy - 0.9 * r_pre, 0),
                    Vector(dia_cx - 0.9 * r_pre, cy + 0.9 * r_pre, 0)))
                x_cursor = dia_cx + r_pre + pad
            val_w = _text(tol_strs[i], h).bounding_box().size.X
            val_cx = x_cursor + val_w / 2
            text_faces.append(_text(tol_strs[i], h).moved(Location(Vector(val_cx, cy, 0))))
            x_cursor = val_cx + val_w / 2 + pad
            mod = r.get("modifier")
            if mod:
                m = mod.upper()
                if m not in _MODIFIER_LETTER:
                    raise ValueError(f"Unknown modifier '{mod}'. Use M, L, or P.")
                mod_cx = x_cursor + mr
                strokes += [e.moved(Location(Vector(mod_cx, cy, 0)))
                            for e in Edge.make_circle(mr).edges()]
                text_faces.append(_text(_MODIFIER_LETTER[m], h * 0.8)
                                  .moved(Location(Vector(mod_cx, cy, 0))))

            datums = r.get("datums", ())
            dmods = r.get("datum_modifiers", {}) or {}
            for j, letter in enumerate(datums):
                cx = x2 + j * H + H / 2
                xr = x2 + (j + 1) * H
                if xr < total_w - 1e-6:
                    strokes.append(Edge.make_line(Vector(xr, bot, 0), Vector(xr, top, 0)))
                dm = dmods.get(letter)
                if dm:
                    text_faces.append(_text(letter, h).moved(Location(Vector(cx - 0.35 * h, cy, 0))))
                    mcx = cx + 0.5 * h
                    strokes += [e.moved(Location(Vector(mcx, cy, 0)))
                                for e in Edge.make_circle(0.55 * h).edges()]
                    text_faces.append(_text(dm.upper(), h * 0.7).moved(Location(Vector(mcx, cy, 0))))
                else:
                    text_faces.append(_text(letter, h).moved(Location(Vector(cx, cy, 0))))

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(sk, label=tol_strs[0], label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.characteristic = name
        self.tolerances = tuple(tol_strs)


class HoleCallout(_Annotation):
    """Single-line hole note built from geometry symbols, e.g. ``4× ⌀8.5 THRU``.
    Bottom-left at (0, 0).

    Metadata: ``.label`` (""), ``.label_bbox`` (None), ``.segments``,
    ``.callout_width``, ``.callout_height``.
    """

    def __init__(
        self,
        diameter,
        *,
        count: int | None = None,
        through: bool = False,
        depth: float | None = None,
        cbore_dia=None,
        cbore_depth: float | None = None,
        csink_dia=None,
        csink_angle: float | None = None,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5, decimal_precision=1)
        h = draft.font_size
        prec = draft.decimal_precision
        gap = 0.45 * h
        sym_w = h

        def _fmt(v):
            return v if isinstance(v, str) else f"{round(v, prec):.{prec}f}"

        tokens: list[tuple[str, str]] = []
        if count:
            tokens.append(("text", f"{count}×"))
        tokens += [("sym", "diameter"), ("text", _fmt(diameter))]
        if through:
            tokens.append(("text", "THRU"))
        elif depth is not None:
            tokens += [("sym", "depth"), ("text", _fmt(depth))]
        if cbore_dia is not None:
            tokens += [("sym", "counterbore"), ("sym", "diameter"), ("text", _fmt(cbore_dia))]
            if cbore_depth is not None:
                tokens += [("sym", "depth"), ("text", _fmt(cbore_depth))]
        if csink_dia is not None:
            tokens += [("sym", "countersink"), ("sym", "diameter"), ("text", _fmt(csink_dia))]
            if csink_angle is not None:
                tokens.append(("text", f"× {_fmt(csink_angle)}°"))

        strokes: list[Edge] = []
        text_faces = []
        x = 0.0
        for kind, val in tokens:
            if kind == "sym":
                cx = x + sym_w / 2
                strokes += [e.moved(Location(Vector(cx, 0, 0)))
                            for e in _feature_symbol_edges(val, h)]
                x += sym_w + gap
            else:
                t = Text(txt=val, font_size=h, font=draft.font,
                         align=(Align.MIN, Align.CENTER), mode=Mode.PRIVATE
                         ).moved(Location(Vector(x, 0, 0)))
                text_faces.append(t)
                x += t.bounding_box().size.X + gap

        width = max(x - gap, 0.0)
        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(sk, label="", label_bbox=None, segments=seg,
                         rotation=rotation, align=align, mode=mode)
        self.callout_width = width
        self.callout_height = h


# ---------------------------------------------------------------------------
# find_overlaps — pure-geometry collision
# ---------------------------------------------------------------------------

def find_overlaps(sketches, *, min_area: float = 0.01) -> list[LintIssue]:
    """Pure-geometry collision check: pairs of sketches whose filled faces
    actually intersect with area > *min_area*.

    Works on any build123d ``Sketch`` with zero metadata — uses the boolean
    intersect ``(a & b)`` and tests the resulting area.

    Returns:
        list[LintIssue] with code ``faces_overlap`` (severity ``"warning"``).
    """
    items = list(sketches)
    issues: list[LintIssue] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            try:
                inter = items[i] & items[j]
                area = inter.area
            except Exception:
                continue
            if area > min_area:
                la = getattr(items[i], "label", None) or f"#{i}"
                lb = getattr(items[j], "label", None) or f"#{j}"
                issues.append(LintIssue(
                    severity="warning",
                    message=(f"sketches '{la}' and '{lb}' overlap by "
                             f"{area:.2f} mm² of filled area"),
                    code="faces_overlap",
                ))
    return issues


# ---------------------------------------------------------------------------
# Interference detection — generic / duck-typed
# ---------------------------------------------------------------------------

_MIN_STRUCT_LEN = 3.5  # mm — ignore glyph strokes / arrowhead edges below this


def _annotation_geom(item):
    """Decompose an annotation into (label_box, [segments], name), duck-typed.

    label_box: ``getattr(item, "label_bbox", None)``; falls back to the item's
    own text/face bbox when None. segments: ``getattr(item, "segments", None)``;
    falls back to extracting straight LINE edges >= _MIN_STRUCT_LEN from the
    item's geometry (doubled rails are an acceptable degraded fallback).
    """
    label_box = getattr(item, "label_bbox", None)
    if label_box is None:
        try:
            faces = item.faces()
        except Exception:
            faces = None
        if faces:
            tb = item.bounding_box()
            label_box = (tb.min.X, tb.min.Y, tb.max.X, tb.max.Y)

    segs = getattr(item, "segments", None)
    if segs is None:
        segs = []
        try:
            edges = item.edges()
        except Exception:
            edges = []
        for e in edges:
            try:
                if e.geom_type != GeomType.LINE or e.length <= _MIN_STRUCT_LEN:
                    continue
                a, b = e.position_at(0), e.position_at(1)
                if label_box is not None:
                    mx, my = (a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0
                    bx0, by0, bx1, by1 = label_box
                    if bx0 - 0.3 <= mx <= bx1 + 0.3 and by0 - 0.3 <= my <= by1 + 0.3:
                        continue
                segs.append(((a.X, a.Y), (b.X, b.Y)))
            except Exception:
                continue

    name = getattr(item, "label", None) or "?"
    return label_box, segs, name


def _seg_hits_box(p, q, box, pad=0.2):
    """Liang–Barsky: does segment p->q intersect the padded AABB box?"""
    minx, miny, maxx, maxy = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, x0 - minx), (dx, maxx - x0), (-dy, y0 - miny), (dy, maxy - y0)):
        if abs(pp) < 1e-12:
            if qq < 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                if r > t1:
                    return False
                t0 = max(t0, r)
            else:
                if r < t0:
                    return False
                t1 = min(t1, r)
    return t0 <= t1


def _box_overlap(a, b, minov):
    return (min(a[2], b[2]) - max(a[0], b[0]) > minov
            and min(a[3], b[3]) - max(a[1], b[1]) > minov)


def _box_inside(inner, outer):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def _as_box(o):
    """Normalise a BoundBox or a (min_x, min_y, max_x, max_y) tuple to a tuple."""
    if hasattr(o, "min") and hasattr(o, "max"):
        return (o.min.X, o.min.Y, o.max.X, o.max.Y)
    return tuple(o)


def _collinear_overlap(seg_a, seg_b, tol=0.15):
    """Length (mm) over which two segments lie on the same line and overlap."""
    (ax0, ay0), (ax1, ay1) = seg_a
    (bx0, by0), (bx1, by1) = seg_b
    dax, day = ax1 - ax0, ay1 - ay0
    la = math.hypot(dax, day)
    if la < 1e-9:
        return 0.0
    ux, uy = dax / la, day / la
    if (abs((bx0 - ax0) * uy - (by0 - ay0) * ux) > tol
            or abs((bx1 - ax0) * uy - (by1 - ay0) * ux) > tol):
        return 0.0
    pb0 = (bx0 - ax0) * ux + (by0 - ay0) * uy
    pb1 = (bx1 - ax0) * ux + (by1 - ay0) * uy
    lo = max(0.0, min(pb0, pb1))
    hi = min(la, max(pb0, pb1))
    return max(0.0, hi - lo)


def find_interferences(items, *, part_bbox=None, page_bbox=None, obstacles=None,
                       min_overlap=0.5, pad=0.2, min_run=1.5):
    """Geometry-precise interference detection between drafting annotations.

    Duck-typed: each item is decomposed into a **label box**
    (``.label_bbox`` or its own face bbox) and **structural line segments**
    (``.segments`` or straight LINE edges from its geometry). Works on the
    native annotation objects and on lightweight ``SimpleNamespace`` stand-ins.

    Checks (codes preserved): ``labels_overlap``, ``label_out_of_frame``,
    ``label_on_part``, ``label_over_geometry``, ``line_pierces_label``,
    ``redundant_lines``.

    Returns:
        list[LintIssue]. Real collisions are ``"error"``; redundant collinear
        overlaps are ``"warning"``.
    """
    geoms = [_annotation_geom(it) for it in items]
    issues: list[LintIssue] = []

    for i, (box_i, _, name_i) in enumerate(geoms):
        if box_i is None:
            continue
        for box_j, _, name_j in geoms[i + 1:]:
            if box_j is not None and _box_overlap(box_i, box_j, min_overlap):
                issues.append(LintIssue(
                    severity="error",
                    message=f'labels "{name_i}" and "{name_j}" overlap',
                    code="labels_overlap",
                ))
        if page_bbox is not None:
            pb = (page_bbox.min.X, page_bbox.min.Y, page_bbox.max.X, page_bbox.max.Y)
            if not _box_inside(box_i, pb):
                issues.append(LintIssue(
                    severity="error",
                    message=f'label "{name_i}" extends outside the drawing frame',
                    code="label_out_of_frame",
                ))
        if part_bbox is not None:
            pb = (part_bbox.min.X, part_bbox.min.Y, part_bbox.max.X, part_bbox.max.Y)
            if _box_overlap(box_i, pb, min_overlap):
                issues.append(LintIssue(
                    severity="error",
                    message=f'label "{name_i}" sits on the part outline',
                    code="label_on_part",
                ))
        if obstacles:
            if any(_box_overlap(box_i, _as_box(o), min_overlap) for o in obstacles):
                issues.append(LintIssue(
                    severity="error",
                    message=f'label "{name_i}" lands over drawing geometry',
                    code="label_over_geometry",
                ))

    for i, (_, segs_i, name_i) in enumerate(geoms):
        for j, (box_j, _, name_j) in enumerate(geoms):
            if i == j or box_j is None:
                continue
            if any(_seg_hits_box(p, q, box_j, pad) for (p, q) in segs_i):
                issues.append(LintIssue(
                    severity="error",
                    message=f'a line from "{name_i}" pierces label "{name_j}"',
                    code="line_pierces_label",
                ))

    for i, (_, segs_i, name_i) in enumerate(geoms):
        for j in range(i + 1, len(geoms)):
            _, segs_j, name_j = geoms[j]
            if any(_collinear_overlap(a, b) > min_run
                   for a in segs_i for b in segs_j):
                issues.append(LintIssue(
                    severity="warning",
                    message=(f'redundant overlapping lines between "{name_i}" '
                             f'and "{name_j}" — shared witness/edge drawn twice'),
                    code="redundant_lines",
                ))

    return issues
