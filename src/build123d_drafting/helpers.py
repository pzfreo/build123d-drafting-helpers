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

import logging
import math
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

from build123d import (
    Align,
    Arrow,
    ArrowHead,
    Color,  # noqa: F401 — re-exported convenience
    DimensionLine,
    Draft,
    Edge,
    Face,
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

_log = logging.getLogger(__name__)

# A freely-redistributable font (Liberation Sans, SIL OFL 1.1) vendored under
# ``fonts/`` and used by default so text geometry is identical on every platform.
# ``build123d.Draft.font`` defaults to the *name* "Arial", which the OS font stack
# silently substitutes (Liberation/DejaVu on Linux, real Arial on macOS) — yielding
# different glyph outlines and metrics per machine. Pinning to a bundled file fixes
# that. Liberation Sans is metric-compatible with Arial, so output barely shifts.
DEFAULT_FONT_PATH: str = str(files("build123d_drafting") / "fonts" / "LiberationSans-Regular.ttf")

# Sentinel: distinguishes "caller did not pass font_path" (use the bundled default)
# from an explicit ``font_path=None`` (opt out — resolve the ``font`` name instead).
_UNSET = object()


def _font_path(draft: Draft) -> str | None:
    """Font-file path used to render ``draft``'s text.

    Resolution order:

    1. ``draft.font_path`` if the attribute is set — a file path (or ``None`` to
       opt out and resolve the ``font`` *name* through the OS font stack);
    2. otherwise :data:`DEFAULT_FONT_PATH`, the bundled Liberation Sans.

    A file path yields byte-identical glyph geometry across platforms, where a
    font *name* does not. Pin a font via :func:`draft_preset`'s ``font_path=``
    argument or by assigning ``draft.font_path = "/path.ttf"``; pass
    ``font_path=None`` to fall back to name-based, system-font behaviour.
    """
    return getattr(draft, "font_path", DEFAULT_FONT_PATH)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass
class LintIssue:
    """A single lint finding.

    .. deprecated::
        Linting has moved to the ``draftwright`` package (ADR 0007 — "helpers
        renders; draftwright reasons"). This type is vendored-and-frozen for
        backward compatibility; importing it through ``build123d_drafting`` emits
        a ``DeprecationWarning`` and it will be removed in a future release. Use
        ``draftwright``'s linting package instead.
    """

    severity: Literal["error", "warning", "info"]
    message: str
    location: tuple[float, float] | None = None
    code: str = ""  # stable machine-readable check id, e.g. "label_vs_measured"


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


def _circle_arcs(radius, center):
    """A full circle as two half-arc Edges centred at *center* (a Vector).

    ``trace()`` builds a clean thin ring from two half-arcs, but a single closed
    circle yields a degenerate seam edge; splitting into semicircles avoids it
    while keeping the same ring geometry.
    """
    loc = Location(center)
    return [
        arc.moved(loc)
        for a0, a1 in ((0, 180), (180, 360))
        for arc in Edge.make_circle(radius, start_angle=a0, end_angle=a1).edges()
    ]


def _chainline_pattern(draft: Draft) -> tuple[float, float, float]:
    """ISO 128 long-dash–short-dash chain-line element lengths (long, gap, short),
    scaled from the Draft font size."""
    h = draft.font_size
    return 3.0 * h, 0.4 * h, 0.6 * h


def _dash_spans(length: float, draft: Draft) -> list[tuple[float, float]]:
    """Chain-line dash intervals ``(a, b)`` within ``[-length/2, length/2]``,
    symmetric about the centre: a short dash centred on 0, long dashes reaching
    outward, and the outermost dash extended to the end so the full extent of
    the centreline is preserved. A path too short for a pattern is returned as a
    single solid span — small centre marks stay solid, as ISO draws them."""
    long, gap, short = _chainline_pattern(draft)
    half = length / 2.0
    if half <= short / 2.0 + gap + long * 0.5:
        return [(-half, half)]
    pos_dashes: list[tuple[float, float]] = []
    pos = short / 2.0
    seq = ((gap, False), (long, True), (gap, False), (short, True))
    i = 0
    while pos < half - 1e-9:
        seglen, is_dash = seq[i % 4]
        end = pos + seglen
        if is_dash:
            pos_dashes.append((pos, min(end, half)))
        pos = end
        i += 1
    if pos_dashes and pos_dashes[-1][1] < half - 1e-9:
        # a gap straddled the end; extend the outer dash so the tip is covered
        pos_dashes[-1] = (pos_dashes[-1][0], half)
    spans = [(-short / 2.0, short / 2.0)]
    for a, b in pos_dashes:
        spans.append((a, b))
        spans.append((-b, -a))
    return spans


def _dash_line(v1: Vector, v2: Vector, draft: Draft) -> list[Edge]:
    """Chain-line dash Edges between Vectors *v1* and *v2*."""
    length = (v2 - v1).length
    if length < 1e-9:
        return [Edge.make_line(v1, v2)]
    u = (v2 - v1) * (1.0 / length)
    mid = (v1 + v2) * 0.5
    return [Edge.make_line(mid + u * a, mid + u * b) for a, b in _dash_spans(length, draft)]


def _dash_arcs(center: Vector, radius: float, draft: Draft) -> list[Edge]:
    """Chain-line dash arc Edges around a circle of *radius* at *center*."""
    loc = Location(center)
    arcs: list[Edge] = []
    for a, b in _dash_spans(2.0 * math.pi * radius, draft):
        sa = math.degrees(a / radius) + 180.0
        sb = math.degrees(b / radius) + 180.0
        arcs += [
            e.moved(loc) for e in Edge.make_circle(radius, start_angle=sa, end_angle=sb).edges()
        ]
    return arcs


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
            is_full_circle = e.geom_type == GeomType.CIRCLE and e.is_closed
        except Exception:
            is_full_circle = False
        if is_full_circle:
            out += _circle_arcs(e.radius, e.arc_center)
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
    faces: list[Any] = []
    for e in _split_circles(strokes):
        try:
            faces += trace([e], line_width=line_width).faces()
        except Exception:
            # A stroke that fails to trace is dropped (the rest still render),
            # but the output is meant to be complete geometry — surface it
            # rather than vanishing silently.
            _log.warning("dropped a stroke that failed to trace (%r)", e, exc_info=True)
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

    def __init__(
        self,
        sketch,
        *,
        label="",
        label_bbox=None,
        segments=None,
        rotation=0,
        align=None,
        mode=Mode.ADD,
    ):
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
            (_bake_pt(a, off, rotation), _bake_pt(b, off, rotation)) for a, b in (segments or [])
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


def draft_preset(
    font_size: float = 2.5,
    decimal_precision: int = 2,
    font_path: str | None = _UNSET,  # type: ignore[assignment]
    **overrides,
) -> Draft:
    """A ``Draft`` tuned for clean technical-drawing output.

    Scales the arrowhead to the font and uses a thin line, closer to ISO
    drawing weights::

        arrow_length = 0.9 * font_size
        line_width   = 0.1

    Any field can be overridden by keyword.

    Text is rendered with the bundled :data:`DEFAULT_FONT_PATH` (Liberation Sans)
    so glyph geometry is identical on every platform. Pass ``font_path`` to pin a
    different font *file*, or ``font_path=None`` to opt out and resolve the
    ``font`` *name* through the OS font stack instead. The choice travels on the
    returned ``Draft`` as a ``font_path`` attribute (see :func:`_font_path`).
    """
    params: dict[str, Any] = dict(
        font_size=font_size,
        decimal_precision=decimal_precision,
        arrow_length=0.9 * font_size,
        line_width=0.1,
    )
    params.update(overrides)
    draft = Draft(**params)
    if font_path is not _UNSET:
        draft.font_path = font_path  # type: ignore[attr-defined]  # Draft has no field; we pin it
    return draft


# ---------------------------------------------------------------------------
# dim_linear  ->  Dimension / SafeDimension
# ---------------------------------------------------------------------------

_SIDE_VECTORS: dict[str, tuple[float, float, float]] = {
    "above": (0.0, 1.0, 0.0),
    "below": (0.0, -1.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
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


# --- boolean-free dimension ink (#177) -------------------------------------
#
# build123d's DimensionLine/ExtensionLine assemble their output with boolean
# fuse/intersect (arrow fuse + a 3-candidate label-placement search scored by
# intersecting text faces) — ~150-230 ms per dimension, the dominant cost of a
# dimension-dense drawing. Every element of a straight dimension is analytically
# known, so the helpers below build the identical ink as a plain collection of
# faces: cached arrowhead copies, thin rectangles trimmed by arithmetic around
# the label gap, and one Text. Layout constants (shaft trim, gap width, outside
# arrows when the label + heads don't fit) follow DimensionLine's own formulas.

_ARROWHEAD_CACHE: dict[tuple[float, Any], Face] = {}


def _arrowhead_face(size: float, head_type) -> Face:
    """The filled arrowhead face for (size, head_type) — tip at the origin, body
    extending toward -X — built once via build123d's ``ArrowHead`` (so the curve
    is identical) and cached; callers place cheap transformed copies with
    ``.moved()``."""
    key = (round(size, 9), head_type)
    face = _ARROWHEAD_CACHE.get(key)
    if face is None:
        face = ArrowHead(size, head_type=head_type, mode=Mode.PRIVATE).faces()[0]
        _ARROWHEAD_CACHE[key] = face
    return face


def _rect_face(a, b, width: float) -> Face | None:
    """A thin filled rectangle of *width* centred on the segment a→b (2D points),
    or ``None`` for a degenerate segment."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    return Face.make_rect(length, width).moved(
        Location(
            Vector((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, 0.0),
            (0, 0, 1),
            math.degrees(math.atan2(dy, dx)),
        )
    )


def _spans_minus_gap(spans, lo: float, hi: float):
    """Subtract the interval [lo, hi] from a list of (t0, t1) spans."""
    out = []
    for t0, t1 in spans:
        if hi <= t0 or lo >= t1:
            out.append((t0, t1))
            continue
        if t0 < lo:
            out.append((t0, lo))
        if hi < t1:
            out.append((hi, t1))
    return [(t0, t1) for t0, t1 in out if t1 - t0 > 1e-9]


def _dim_line_ink(a: Vector, b: Vector, draft: Draft, label_str: str, label_t: float | None):
    """Dimension-line ink from *a* to *b* assembled without booleans (#177).

    Follows ``DimensionLine``'s layout: when the label and both heads fit within
    the span the arrows sit inside with the line broken around the label
    (gap = label extent + ``pad_around_text`` each side); otherwise the arrows
    sit outside pointing in, with no line drawn between the ends — exactly as
    build123d draws that case.

    ``label_t`` is the label centre along a→b in mm; ``None`` centres it when it
    fits and hangs it past *b* otherwise (DimensionLine's external-label spot).

    Returns ``(faces, spans, label_geo)``: the ink faces, the drawn line pieces
    as ((x0,y0),(x1,y1)) segment metadata, and ``(cx, cy, angle_deg, half_w,
    half_h)`` for the label or ``None`` when *label_str* is empty.
    """
    av = Vector(a.X, a.Y, 0.0)
    bv = Vector(b.X, b.Y, 0.0)
    span = bv - av
    length = span.length
    u = span * (1.0 / length)
    u_ang = math.degrees(math.atan2(u.Y, u.X))
    # keep the label upright — readable from the bottom/right of the sheet
    label_ang = u_ang if -90.0 < u_ang <= 90.0 else u_ang - math.copysign(180.0, u_ang)

    al = draft.arrow_length
    pad = draft.pad_around_text

    text_face = None
    half_w = half_h = 0.0
    if label_str:
        text_face = Text(
            txt=label_str,
            font_size=draft.font_size,
            font=draft.font,
            font_style=draft.font_style,
            font_path=_font_path(draft),
            align=(Align.CENTER, Align.CENTER),
            mode=Mode.PRIVATE,
        )
        tb = text_face.bounding_box()
        half_w, half_h = tb.size.X / 2.0, tb.size.Y / 2.0

    # Inside arrows need the label to fit (DimensionLine's own test) AND a
    # drawable shaft piece beside each head: where the reference formula
    # (length/2 - half_w - pad) leaves less than the al/2 head trim, build123d
    # raises "empty wire" — route that band to the outside-arrows layout
    # instead of rendering a head with no shaft behind it.
    fits = 2.0 * half_w + 2.0 * al < length and (
        text_face is None or length / 2.0 - half_w - pad > al / 2.0
    )
    if fits:
        # heads at the ends, tips outward, bodies inward; shafts trimmed by
        # half a head so the tip isn't overdrawn (Arrow's own trim)
        heads = [(0.0, u_ang + 180.0, (0.0, al)), (length, u_ang, (length - al, length))]
        ink = [(al / 2.0, length - al / 2.0)]
        if label_t is None:
            label_t = length / 2.0
    else:
        heads = [(0.0, u_ang, (-al, 0.0)), (length, u_ang + 180.0, (length, length + al))]
        ink = [(-2.0 * al, -al / 2.0), (length + al / 2.0, length + 2.0 * al)]
        if label_t is None:
            # a label that fits between the ends stays centred (DimensionLine's
            # scorer keeps it there); only one wider than the span hangs past the end
            label_t = length / 2.0 if 2.0 * half_w < length else length + 2.0 * al + pad + half_w
    if text_face is not None:
        ink = _spans_minus_gap(ink, label_t - half_w - pad, label_t + half_w + pad)

    faces: list[Any] = []
    spans = []
    for t0, t1 in ink:
        p, q = av + u * t0, av + u * t1
        rect = _rect_face((p.X, p.Y), (q.X, q.Y), draft.line_width)
        if rect is not None:
            faces.append(rect)
            spans.append(((p.X, p.Y), (q.X, q.Y)))
    head = _arrowhead_face(al, draft.head_type)
    for t, ang, (h_lo, h_hi) in heads:
        # nothing renders under the label: a head the text itself would cover is
        # dropped, as the old boolean gap-cut removed that ink (pad-zone contact
        # is fine — DimensionLine's scorer tested actual glyph overlap, not pads)
        if text_face is not None and label_t - half_w < h_hi and h_lo < label_t + half_w:
            continue
        tip = av + u * t
        faces.append(head.moved(Location(Vector(tip.X, tip.Y, 0.0), (0, 0, 1), ang)))

    label_geo = None
    if text_face is not None:
        c = av + u * label_t
        faces.append(text_face.moved(Location(Vector(c.X, c.Y, 0.0), (0, 0, 1), label_ang)))
        label_geo = (c.X, c.Y, label_ang, half_w, half_h)
    return faces, spans, label_geo


class Dimension(_Annotation):
    """ExtensionLine-style dimension with named placement side, as a native Sketch.

    Renders the same ink as build123d's ``ExtensionLine`` (witness lines, broken
    dimension line, arrowheads, label) but assembles it without boolean
    operations — see :func:`_dim_line_ink` (#177).

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
        if offset == 0:
            raise ValueError("A dimension line should be used if offset is 0")

        p1v = Vector(p1[0], p1[1], 0.0)
        p2v = Vector(p2[0], p2[1], 0.0)
        measured = (p2v - p1v).length
        if measured < 1e-9:
            raise ValueError("Start and end points of border must be different.")
        u = (p2v - p1v) * (1.0 / measured)  # path direction
        n = Vector(u.Y, -u.X, 0.0)  # right-hand normal (matches _offset_sign)
        d1 = p1v + n * offset  # dimension-line endpoints
        d2 = p2v + n * offset

        # rendered text: what ExtensionLine would draw inline (units and all);
        # .label metadata keeps _format_label's unit-less form for lint parity
        rendered = label if label is not None else draft._number_with_units(measured, tolerance)
        label_str = label if label is not None else _format_label(measured, draft, tolerance)

        label_t = measured / 2.0 + label_offset_x
        faces, spans, label_geo = _dim_line_ink(d1, d2, draft, rendered, label_t=label_t)

        # witness (extension) lines: from the part — offset by extension_gap
        # along their own direction, as ExtensionLine places them. The label
        # keep-clear (glyph extent + pad, the old boolean gap-cut's rect) is
        # cut out so a shifted label never sits on top of a witness stroke;
        # s runs along the witness line from the part point.
        w = n if offset >= 0 else -n
        g = draft.extension_gap
        pad = draft.pad_around_text
        for t_i, pv in ((0.0, p1v), (measured, p2v)):
            s_spans = [(g, abs(offset) + g)]
            if label_geo is not None and abs(label_t - t_i) < label_geo[3] + pad:
                half_h = label_geo[4]
                s_spans = _spans_minus_gap(
                    s_spans, abs(offset) - half_h - pad, abs(offset) + half_h + pad
                )
            for s0, s1 in s_spans:
                wa, wb = pv + w * s0, pv + w * s1
                rect = _rect_face((wa.X, wa.Y), (wb.X, wb.Y), draft.line_width)
                if rect is not None:
                    faces.append(rect)
                    spans.append(((wa.X, wa.Y), (wb.X, wb.Y)))

        boxes = [f.bounding_box() for f in faces]
        max_y = max(b.max.Y for b in boxes)
        min_y = min(b.min.Y for b in boxes)
        dim_level_y = max_y if abs(max_y) >= abs(min_y) else min_y

        if label_geo is not None:
            label_cx, label_cy, label_ang, half_w, half_h = label_geo
            label_bbox_tuple = _xf_bbox(
                (-half_w, -half_h, half_w, half_h), label_ang, (label_cx, label_cy)
            )
        else:
            mid = (d1 + d2) * 0.5 + u * label_offset_x
            label_bbox_tuple = (mid.X, mid.Y, mid.X, mid.Y)
        strokes: list[Edge] = []

        if basic:
            bpad = 0.4 * draft.font_size
            bx0, by0, bx1, by1 = label_bbox_tuple
            bx0 -= bpad
            by0 -= bpad
            bx1 += bpad
            by1 += bpad
            corners = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1), (bx0, by0)]
            strokes += [
                Edge.make_line(Vector(a[0], a[1], 0), Vector(b[0], b[1], 0))
                for a, b in zip(corners, corners[1:], strict=False)
            ]
            label_bbox_tuple = (bx0, by0, bx1, by1)

        # Combine the dimension/witness ink with any box strokes (as thin faces).
        if strokes:
            faces += trace(strokes, line_width=line_width).faces()
        sk = Sketch(children=faces)

        # segments are the drawn line pieces (witness lines, shafts) + box strokes
        seg = spans + _segments(strokes)

        super().__init__(
            sk,
            label=label_str,
            label_bbox=label_bbox_tuple,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.measured_length = measured
        self.dim_level_y = dim_level_y
        self.is_basic = basic


def _truncate(s: str, max_len: int = 6) -> str:
    return s[:max_len] + "…" if len(s) > max_len else s


class SafeDimension(_Annotation):
    """DimensionLine-style dimension that won't crash on labels longer than the path.

    A straight path renders through the boolean-free ink builder (#177), whose
    arithmetic layout handles any label: one too wide for the path moves the
    arrows outside and hangs the text past the end, as DimensionLine draws that
    case. Curved paths still go through build123d's ``DimensionLine`` with the
    old truncate-and-retry guard (build123d raises ValueError when the label is
    wider than the dimension path).

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

        try:
            is_line = edge.geom_type == GeomType.LINE
        except Exception:
            is_line = False

        faces = None
        seg: list = []
        if is_line and measured > 1e-9:
            try:
                faces, seg, _ = _dim_line_ink(
                    edge.position_at(0), edge.position_at(1), draft, label, label_t=None
                )
                chosen_label = label
            except Exception:
                faces = None  # fall through to the legacy path, then the bare line
        if faces is None:
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
            # Last resort — bare edge so the drawing isn't missing the line. No
            # text is rendered, so clear the label rather than report one the
            # geometry doesn't show (lint reads .label).
            sk, seg = _strokes_and_text([edge], [], line_width)
            chosen_label = ""
        else:
            sk = Sketch(children=faces)

        super().__init__(
            sk,
            label=chosen_label,
            label_bbox=None,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.measured_length = measured


# ---------------------------------------------------------------------------
# centerline  ->  Centerline
# ---------------------------------------------------------------------------


class Centerline(_Annotation):
    """A centreline between two points, drawn as an ISO 128 long-dash–short-dash
    chain line whose dash/gap lengths scale with *draft* (solid if too short).

    Metadata: ``.label`` (""), ``.segments``, ``.is_centerline`` (True).
    """

    def __init__(
        self,
        p1: tuple,
        p2: tuple,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        draft = draft or Draft(font_size=2.5)
        v1 = Vector(p1[0], p1[1], p1[2] if len(p1) > 2 else 0.0)
        v2 = Vector(p2[0], p2[1], p2[2] if len(p2) > 2 else 0.0)
        sk, seg = _strokes_and_text(_dash_line(v1, v2, draft), [], line_width)
        super().__init__(
            sk, label="", label_bbox=None, segments=seg, rotation=rotation, align=align, mode=mode
        )
        self.is_centerline = True


class CenterMark(_Annotation):
    """A centre mark — a crosshair at a hole centre (#95).

    *size* is the full length of each stroke; pick it a little larger than
    the hole's page-space diameter so the mark protrudes past the circle. Each
    arm is a chain line scaled from *draft*; a mark too small for a dash pattern
    stays a solid cross, as ISO draws it.

    Metadata: ``.label`` (""), ``.segments``, ``.is_centerline`` (True) —
    exempt from view-overlap lint like :class:`Centerline`.
    """

    def __init__(
        self,
        point: tuple,
        size: float,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        if size <= 0:
            raise ValueError(f"size must be positive, got {size!r}")
        draft = draft or Draft(font_size=2.5)
        cx, cy = point[0], point[1]
        h = size / 2
        edges = _dash_line(Vector(cx - h, cy, 0.0), Vector(cx + h, cy, 0.0), draft) + _dash_line(
            Vector(cx, cy - h, 0.0), Vector(cx, cy + h, 0.0), draft
        )
        sk, seg = _strokes_and_text(edges, [], line_width)
        super().__init__(
            sk, label="", label_bbox=None, segments=seg, rotation=rotation, align=align, mode=mode
        )
        self.is_centerline = True


class CenterlineCircle(_Annotation):
    """A circular centreline — e.g. the pitch circle of a bolt pattern (#92).

    Drawn as an ISO 128 long-dash–short-dash chain line around the circle (a
    line, not a flooded disc), with dash/gap lengths scaled from *draft*.
    Metadata: ``.label`` (""), ``.segments``, ``.is_centerline`` (True) —
    exempt from view-overlap lint like :class:`Centerline`.
    """

    def __init__(
        self,
        center: tuple,
        diameter: float,
        draft: Draft | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        if diameter <= 0:
            raise ValueError(f"diameter must be positive, got {diameter!r}")
        draft = draft or Draft(font_size=2.5)
        edges = _dash_arcs(Vector(center[0], center[1], 0.0), diameter / 2, draft)
        sk, seg = _strokes_and_text(edges, [], line_width)
        super().__init__(
            sk, label="", label_bbox=None, segments=seg, rotation=rotation, align=align, mode=mode
        )
        self.is_centerline = True


# ---------------------------------------------------------------------------
# Note — free-text annotation
# ---------------------------------------------------------------------------


class Note(_Annotation):
    """A free-text note at a page position — e.g. a view caption like "(NTS)".

    The text is rendered as faces (real geometry, like every other helper), so
    it round-trips to DXF/SVG. Use it for short sheet notes that belong to the
    drawing content; title-block fields belong in :class:`TitleBlock`.

    Args:
        text: the note text.
        position: ``(x, y)`` page position of the text anchor.
        draft: Draft config (font and size).
        rotation: degrees CCW about the page origin (``BaseSketchObject``
            semantics — unlike :class:`TextBlock`, which rotates about its
            anchor).
        align: which point of the text sits at ``position`` — default
            ``Align.CENTER`` (the text centre). E.g. ``(Align.MIN,
            Align.CENTER)`` anchors the left edge at ``position``.
        mode: standard ``BaseSketchObject`` option.

    Metadata: ``.label``, ``.label_bbox``.
    """

    def __init__(
        self,
        text: str,
        position: tuple,
        draft: Draft,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        shape = Text(
            txt=text,
            font_size=draft.font_size,
            font=draft.font,
            font_path=_font_path(draft),
            align=Align.CENTER if align is None else align,
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(position[0], position[1], 0.0)))
        tb = shape.bounding_box()
        sk = Sketch(children=list(shape.faces()))
        super().__init__(
            sk,
            label=text,
            label_bbox=(tb.min.X, tb.min.Y, tb.max.X, tb.max.Y),
            rotation=rotation,
            align=None,
            mode=mode,
        )


class TextBlock(_Annotation):
    """A multi-line, left-aligned text block — general notes and hole tables.

    Each line is rendered as text faces sharing a common left edge and a
    common baseline grid, spaced ``line_spacing × font_size`` apart. Interior
    spaces advance normally (column padding works); leading spaces are not
    preserved — the left edge is each line's first glyph. The whole block
    registers as a *single* annotation: ``.label`` is the joined text and
    ``.label_bbox`` covers every line, so lint treats it as one keep-clear
    region.

    Args:
        lines: text lines, top to bottom — a sequence of strings, or one
            string split on newlines. Empty strings leave a blank line.
        position: ``(x, y)`` page position of the block's anchor point.
        draft: Draft config (font and size).
        line_spacing: line pitch as a multiple of ``draft.font_size``.
        align: which point of the block sits at ``position`` — default
            ``(Align.MIN, Align.MAX)``, the top-left corner.
        rotation: degrees CCW about the anchor point.
        mode: standard ``BaseSketchObject`` option.

    Metadata: ``.label``, ``.label_bbox``.
    """

    def __init__(
        self,
        lines,
        position: tuple,
        draft: Draft,
        line_spacing: float = 1.6,
        align=(Align.MIN, Align.MAX),
        rotation: float = 0,
        mode: Mode = Mode.ADD,
    ):
        lines = lines.splitlines() if isinstance(lines, str) else list(lines)
        if not any(line.strip() for line in lines):
            raise ValueError("TextBlock needs at least one non-empty line")
        pitch = draft.font_size * line_spacing
        faces: list[Face] = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            faces.extend(
                Text(
                    txt=line,
                    font_size=draft.font_size,
                    font=draft.font,
                    font_path=_font_path(draft),
                    # Align.NONE keeps the font's raw vertical frame, so all
                    # lines sit on a shared baseline grid (bbox-MAX alignment
                    # would float lowercase-only lines up by the ascender gap)
                    align=(Align.MIN, Align.NONE),
                    mode=Mode.PRIVATE,
                )
                .moved(Location(Vector(0.0, -i * pitch, 0.0)))
                .faces()
            )
        block = Sketch(children=faces)
        gb = block.bounding_box()
        # The block's logical extent spans line 0's nominal em-box top to the
        # last line's nominal bottom (the em box is ~centred on each line's
        # placement in the raw frame), so leading/trailing blank lines count
        # even though they render no geometry, and descenders may dip past
        x0, x1 = gb.min.X, gb.max.X
        half_em = draft.font_size / 2
        y1 = max(gb.max.Y, half_em)
        y0 = min(gb.min.Y, -(len(lines) - 1) * pitch - half_em)

        def _anchor(lo, hi, a):
            if a == Align.MIN:
                return -lo
            if a == Align.MAX:
                return -hi
            return -(lo + hi) / 2  # CENTER

        al = align if isinstance(align, (tuple, list)) else (align, align)
        ox, oy = _anchor(x0, x1, al[0]), _anchor(y0, y1, al[1])
        # Anchor at the origin, then rotate about the anchor and place it
        shape = block.moved(
            Location((position[0], position[1], 0.0), (0.0, 0.0, rotation))
            * Location(Vector(ox, oy, 0.0))
        )
        super().__init__(
            shape,
            label="\n".join(lines),
            label_bbox=_xf_bbox((x0 + ox, y0 + oy, x1 + ox, y1 + oy), rotation, position),
            rotation=0,
            align=None,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# leader  ->  Leader
# ---------------------------------------------------------------------------


def _seg_intersects_rect(p, q, rect) -> bool:
    """True if segment p→q intersects the (min_x, min_y, max_x, max_y) rect.

    Thin wrapper over :func:`_seg_hits_box` (the same Liang–Barsky clip) with no
    padding, kept for call-site readability.
    """
    return _seg_hits_box(p, q, rect, pad=0.0)


class Leader(_Annotation):
    """Leader annotation with arrowhead at *tip* and label hanging from *elbow*.

    The horizontal shelf runs away from *tip* so the label text sits cleanly
    after the shelf end — the line never passes through the label bbox.

    **Label side**: by default the shelf and label continue in the horizontal
    direction of tip → elbow — to the **right** of the elbow when the elbow is
    right of the tip, to the **left** when it is left of the tip. A purely
    vertical leader (elbow directly above or below the tip) places the label
    to the right. Pass ``text_side="left"`` or ``"right"`` to force the side
    when the default would run the label off-page or across a neighbouring
    view.

    Args:
        tip:   arrow point on the part feature (x, y[, z]).
        elbow: where the shaft bends to become the horizontal shelf (x, y[, z]).
        label: annotation text.
        draft: Draft config.
        all_around: draw the ISO 1101 all-around circle at the kink.
        all_over: draw the all-over double circle at the kink.
        text_side: ``"auto"`` (default, the tip → elbow rule above), or
            ``"left"`` / ``"right"`` to force which side of the elbow the
            shelf and label extend to. A forced side that would run the
            shaft through the label text (e.g. the label forced back toward
            the tip of a near-horizontal leader) raises ``ValueError`` —
            the override is for steep or vertical leaders, where either side
            is clear.
        rotation, align, mode: standard ``BaseSketchObject`` placement options.
            Note ``align`` positions the *whole finished sketch* (line, arrow,
            and label together) relative to the origin — it does not control
            which side of the elbow the label is on; use ``text_side`` for
            that.
        callout: a sketch (e.g. :class:`HoleCallout`) hung at the shelf end
            in place of text — pass ``label=""`` with it. Its bbox becomes
            ``label_bbox`` and its ``covers_diameters`` (when present) is
            surfaced on the leader for the coverage lint.

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
        text_side: str = "auto",
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
        callout=None,
    ):
        if callout is not None and label:
            raise ValueError("Pass either label or callout, not both")
        if text_side not in ("auto", "left", "right"):
            raise ValueError(f"text_side must be 'auto', 'left', or 'right', got {text_side!r}")
        tip_v = Vector(tip[0], tip[1], 0.0)
        elbow_v = Vector(elbow[0], elbow[1], 0.0)
        gap = draft.pad_around_text

        if text_side == "auto":
            shelf_dir = 1.0 if elbow_v.X >= tip_v.X else -1.0
        else:
            shelf_dir = 1.0 if text_side == "right" else -1.0
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
                ring_strokes += _circle_arcs(rad, elbow_v)
        if ring_strokes:
            faces += trace(ring_strokes, line_width=line_width).faces()

        # Label content centred vertically at shelf height, inset by one gap:
        # either rendered text, or a caller-supplied sketch (e.g. HoleCallout)
        # hung where the text would go
        if shelf_dir > 0:
            text_align = (Align.MIN, Align.CENTER)
            text_x = elbow_v.X + gap
        else:
            text_align = (Align.MAX, Align.CENTER)
            text_x = elbow_v.X - gap

        if callout is not None:
            cb = callout.bounding_box()
            anchor_x = cb.min.X if shelf_dir > 0 else cb.max.X
            mid_y = (cb.min.Y + cb.max.Y) / 2
            text_shape = callout.moved(Location(Vector(text_x - anchor_x, elbow_v.Y - mid_y, 0.0)))
        else:
            text_shape = Text(
                txt=label,
                font_size=draft.font_size,
                font=draft.font,
                font_path=_font_path(draft),
                align=text_align,
                mode=Mode.PRIVATE,
            ).moved(Location(Vector(text_x, elbow_v.Y, 0.0)))
        _tb = text_shape.bounding_box()
        if text_side != "auto" and _seg_intersects_rect(
            (tip_v.X, tip_v.Y),
            (elbow_v.X, elbow_v.Y),
            (_tb.min.X, _tb.min.Y, _tb.max.X, _tb.max.Y),
        ):
            raise ValueError(
                f"text_side={text_side!r} runs the leader line through the label text "
                f"(tip ({tip_v.X:g}, {tip_v.Y:g}), elbow ({elbow_v.X:g}, {elbow_v.Y:g})) "
                f"— move the elbow further from the tip side or use the opposite side"
            )
        faces.append(text_shape)

        sk = Sketch(children=faces)
        seg = _segments([shaft_edge, shelf_edge])

        super().__init__(
            sk,
            label=label,
            label_bbox=(_tb.min.X, _tb.min.Y, _tb.max.X, _tb.max.Y),
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self._tip_local = self._bake_point((tip_v.X, tip_v.Y))
        self._elbow_local = self._bake_point((elbow_v.X, elbow_v.Y))
        # A carried HoleCallout dimensions diameters as glyphs; surface its
        # structured coverage on the leader for lint_feature_coverage (#80)
        covers = getattr(callout, "covers_diameters", ())
        if covers:
            self.covers_diameters = tuple(covers)
            self.covers_count = getattr(callout, "covers_count", 1)

    @property
    def tip(self):
        return self._live_point(self._tip_local)

    @property
    def elbow(self):
        return self._live_point(self._elbow_local)


_COMPASS_ANGLES = {
    "E": 0.0,
    "NE": 45.0,
    "N": 90.0,
    "NW": 135.0,
    "W": 180.0,
    "SW": 225.0,
    "S": 270.0,
    "SE": 315.0,
}


def leader_offset(
    tip: tuple,
    direction,
    length: float,
    label: str,
    draft: Draft,
    text_side: str = "auto",
) -> Leader:
    """Leader with the elbow placed by direction + distance instead of absolute coords.

    Computes ``elbow = tip + (cos θ, sin θ) * length`` and returns a ``Leader``.

    Args:
        direction: compass string ("N", "NE", … case-insensitive) or an angle in
                   degrees CCW from +X.
        text_side: which side of the elbow the label extends to — ``"auto"``
                   follows the leader direction (westward leaders place text
                   left, others right; see :class:`Leader`); ``"left"`` /
                   ``"right"`` force it.
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
    return Leader(tip=tip, elbow=elbow, label=label, draft=draft, text_side=text_side)


# ---------------------------------------------------------------------------
# view_axes — pure Python helpers (no OCC import)
# ---------------------------------------------------------------------------


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale3(s, v):
    return (s * v[0], s * v[1], s * v[2])


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm3(v):
    mag = math.sqrt(_dot3(v, v))
    return (v[0] / mag, v[1] / mag, v[2] / mag) if mag > 1e-10 else (0.0, 0.0, 0.0)


def _view_basis(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return the ``(page_x, page_y)`` world-space unit basis of the viewport.

    These are the camera's horizontal and vertical axes — build123d's
    ``project_to_viewport`` expresses projected coordinates in exactly this
    basis (its camera X axis is ``view_dir × page_y``, which equals ``page_x``
    below). A world point ``P`` projects to page offset
    ``(dot(P − C, page_x), dot(P − C, page_y))`` for a part centroid ``C``.
    """
    vo = tuple(float(x) for x in viewport_origin)
    la = tuple(float(x) for x in look_at)
    vu = tuple(float(x) for x in viewport_up)

    view_dir = _norm3(_sub3(la, vo))
    page_y = _norm3(_sub3(vu, _scale3(_dot3(vu, view_dir), view_dir)))
    page_x = _cross3(view_dir, page_y)
    return page_x, page_y


def view_axes(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> dict[str, tuple[str, float]]:
    """Return the world→page axis mapping for a project_to_viewport call.

    Each world axis is collapsed onto a single dominant page direction with a
    ±1 sign — exact for orthographic views, but **lossy for ISO/oblique views**
    (the foreshortening magnitude and the orthogonal component are discarded).
    For a projection that is correct on ISO/oblique views, build a
    :class:`ViewCoordinates` via :meth:`ViewCoordinates.from_viewport`, which
    keeps the full :func:`_view_basis` and uses it in ``pp()``.
    """
    page_x, page_y = _view_basis(viewport_origin, viewport_up, look_at)

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


class ViewCoordinates:
    """Page-coordinate helpers for a projected view.

    Wraps the ``view_axes()`` result with the view page-centre, part
    centroid, and scale.

    **General method** — projects any world point to the page::

        pp(world_x, world_y, world_z) -> (page_x, page_y)

    ``pp()`` is correct for **all** views — including ISO/oblique — *when the
    instance carries the full projection basis*. Build it with
    :meth:`from_viewport` (recommended) to get that basis, or pass
    ``page_basis=`` explicitly. When constructed from a bare ``view_axes()``
    mapping alone, the basis can be reconstructed only for orthographic views;
    on an ISO/oblique mapping ``pp()`` raises rather than return a silently
    wrong (un-foreshortened) point.

    **Orthographic-only scalar methods** — valid only when exactly one world
    axis projects to each page direction (front, plan, side views)::

        px(world_val)  — pass the coordinate on the world axis named by .px_axis
        py(world_val)  — pass the coordinate on the world axis named by .py_axis

    Attributes:
        px_axis: which world axis (``'world_X'``, ``'world_Y'``, or
                 ``'world_Z'``) feeds ``px()``. ``None`` for ISO/oblique views
                 where multiple world axes project to page_X.
        py_axis: same for ``py()``.

    Example (front view, orthographic)::

        axes = view_axes((cxs, cys - DIST, czs), (0, 0, 1), (cxs, cys, czs))
        vc = ViewCoordinates(axes, FV_X, FV_Y, cx, cy, cz, SCALE)
        # vc.px_axis == 'world_X', vc.py_axis == 'world_Z'
        page_pt = vc.pp(bb.max.X, bb.max.Y, bb.max.Z)
        page_x = vc.px(bb.max.X)  # orthographic shortcut

    Example (ISO, foreshortened correctly)::

        vc = ViewCoordinates.from_viewport(
            (1, -1, 1), (0, 0, 1), (cx, cy, cz), ISO_X, ISO_Y, cx, cy, cz, SCALE)
        page_pt = vc.pp(bb.max.X, bb.max.Y, bb.max.Z)  # true ISO projection
    """

    def __init__(
        self,
        axes: dict[str, tuple[str, float]],
        view_x: float,
        view_y: float,
        cx: float,
        cy: float,
        cz: float,
        scale: float,
        page_basis: tuple | None = None,
    ) -> None:
        self._axes = axes
        self._centers: dict[str, float] = {"world_X": cx, "world_Y": cy, "world_Z": cz}
        self._view_x = view_x
        self._view_y = view_y
        self._scale = scale

        px_axes = [(w, s) for w, (p, s) in axes.items() if p == "page_X"]
        py_axes = [(w, s) for w, (p, s) in axes.items() if p == "page_Y"]

        # Full projection basis for pp(). Use the explicit basis when given
        # (correct for ISO/oblique); otherwise reconstruct it from the collapsed
        # mapping, which is exact only when one world axis feeds each page
        # direction (orthographic). Left None for an ISO/oblique mapping with no
        # basis, so pp() can refuse rather than return an un-foreshortened point.
        self._page_x_vec: tuple[float, ...] | None
        self._page_y_vec: tuple[float, ...] | None
        if page_basis is not None:
            self._page_x_vec = tuple(float(c) for c in page_basis[0])
            self._page_y_vec = tuple(float(c) for c in page_basis[1])
        elif len(px_axes) == 1 and len(py_axes) == 1:
            unit = {
                "world_X": (1.0, 0.0, 0.0),
                "world_Y": (0.0, 1.0, 0.0),
                "world_Z": (0.0, 0.0, 1.0),
            }
            (wx, sx), (wy, sy) = px_axes[0], py_axes[0]
            self._page_x_vec = _scale3(sx, unit[wx])
            self._page_y_vec = _scale3(sy, unit[wy])
        else:
            self._page_x_vec = self._page_y_vec = None

        if len(px_axes) == 1:
            w, s = px_axes[0]
            self._px_center: float | None = self._centers[w]
            self._px_sign: float = s
            self.px_axis: str | None = w
        else:
            self._px_center = None
            self._px_sign = 1.0
            self.px_axis = None

        if len(py_axes) == 1:
            w, s = py_axes[0]
            self._py_center: float | None = self._centers[w]
            self._py_sign: float = s
            self.py_axis: str | None = w
        else:
            self._py_center = None
            self._py_sign = 1.0
            self.py_axis = None

    @classmethod
    def from_viewport(
        cls,
        viewport_origin: tuple,
        viewport_up: tuple,
        look_at: tuple,
        view_x: float,
        view_y: float,
        cx: float,
        cy: float,
        cz: float,
        scale: float,
    ) -> ViewCoordinates:
        """Build from the raw viewport, retaining the full projection basis.

        Unlike ``ViewCoordinates(view_axes(...), ...)``, this keeps the
        world-space ``(page_x, page_y)`` basis, so ``pp()`` is correct for
        ISO/oblique views as well as orthographic ones. ``viewport_origin``,
        ``viewport_up`` and ``look_at`` are the same arguments passed to
        build123d's ``project_to_viewport``.
        """
        basis = _view_basis(viewport_origin, viewport_up, look_at)
        axes = view_axes(viewport_origin, viewport_up, look_at)
        return cls(axes, view_x, view_y, cx, cy, cz, scale, page_basis=basis)

    def pp(self, world_x: float, world_y: float, world_z: float) -> tuple[float, float]:
        """Map a world point to ``(page_x, page_y)``.

        Correct for all views — including ISO/oblique — when the instance
        carries the projection basis (see :meth:`from_viewport`). For an
        ISO/oblique mapping built without a basis the projection is unknown, so
        this raises rather than return an un-foreshortened point.

        Raises:
            ValueError: ISO/oblique view constructed without a projection basis.
        """
        if self._page_x_vec is None:
            raise ValueError(
                "pp() needs the projection basis for ISO/oblique views, but this "
                "ViewCoordinates was built from a collapsed view_axes() mapping "
                "with no basis. Construct it via ViewCoordinates.from_viewport(...) "
                "(or pass page_basis=) so the foreshortening is known."
            )
        dx = (
            world_x - self._centers["world_X"],
            world_y - self._centers["world_Y"],
            world_z - self._centers["world_Z"],
        )
        page_x = self._view_x + _dot3(dx, self._page_x_vec) * self._scale
        page_y = self._view_y + _dot3(dx, self._page_y_vec) * self._scale
        return page_x, page_y

    def px(self, world_val: float) -> float:
        """Map a value on the world axis that projects to page X.

        Only valid for orthographic views where exactly one world axis maps to
        page_X. Check ``.px_axis`` to confirm which world coordinate to pass.
        For ISO/oblique views use ``pp()`` instead.

        Raises:
            ValueError: if the view has zero or multiple world axes projecting
                to page_X (e.g. ISO views have two).
        """
        if self._px_center is None:
            raise ValueError(
                "px() requires exactly one world axis to project to page_X, "
                "but this view has none or multiple (e.g. ISO views). "
                "Use pp(world_x, world_y, world_z) instead."
            )
        return self._view_x + (world_val - self._px_center) * self._px_sign * self._scale

    def py(self, world_val: float) -> float:
        """Map a value on the world axis that projects to page Y.

        Only valid for orthographic views. Check ``.py_axis`` for which world
        coordinate to pass. For ISO/oblique views use ``pp()`` instead.

        Raises:
            ValueError: if the view has zero or multiple world axes projecting
                to page_Y.
        """
        if self._py_center is None:
            raise ValueError(
                "py() requires exactly one world axis to project to page_Y, "
                "but this view has none or multiple (e.g. ISO views). "
                "Use pp(world_x, world_y, world_z) instead."
            )
        return self._view_y + (world_val - self._py_center) * self._py_sign * self._scale


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
            (min(p1[0], p2[0]), max(p1[0], p2[0]))
            if use_x
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
        results.append(Dimension(p1, p2, side, offset, draft, label=label, tolerance=tolerance))

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
            dim = Dimension(
                p1,
                p2,
                side,
                distance,
                draft,
                label=label,
                tolerance=tolerance,
                label_offset_x=offset_x,
            )
        results.append(dim)
    return results


# ---------------------------------------------------------------------------
# lint_drawing — generic / duck-typed
# ---------------------------------------------------------------------------


def _item_label(item) -> str:
    """The label lint should use for *item*: its ``.label``, or the explicit
    string attached via :func:`annotate` (``_annotate_label``) when it has none
    — e.g. a vanilla build123d ``ExtensionLine`` that does not retain its
    constructor label."""
    return getattr(item, "label", "") or getattr(item, "_annotate_label", "") or ""


def _label_value(label: str) -> float | None:
    """The dimensional value a dimension/callout label asserts, or ``None``.

    Handles the three label shapes lint compares against measured geometry::

        "12.5", "⌀8.5", "7.5 ±0.1"   -> the leading number   (12.5 / 8.5 / 7.5)
        "4× 20"                       -> a pitch span          (4·20 = 80)
        "4× ⌀8.5", "4× ⌀8.5 THRU"     -> a counted diameter    (8.5, not 4·8.5)

    The diameter/radius prefix on the repeated value is the discriminator: a
    bare ``N× v`` is a span of N pitches, but ``N× ⌀d`` counts d-diameter
    features, so the value is ``d`` itself.
    """
    body = label.split("±")[0].split("+")[0]
    nums = re.findall(r"\d+\.?\d*", body.lstrip("ø⌀Rr"))
    if not nums:
        return None
    rep = re.match(r"\s*(\d+)\s*[×x]\s*([ø⌀Rr]?)\s*(\d+\.?\d*)", label)
    try:
        if rep:
            count, prefix, value = rep.group(1), rep.group(2), rep.group(3)
            return float(value) if prefix else int(count) * float(value)
        return float(nums[0])
    except ValueError:
        return None


def lint_drawing(
    items,
    part_bbox=None,
    page_bbox=None,
    drawing_scale: float = 1.0,
    view_shapes: list | None = None,
    view_edge_cache: dict | None = None,
) -> list[LintIssue]:
    """Structural checks on a composed annotation list, duck-typed.

    .. deprecated::
        Linting has moved to the ``draftwright`` package (ADR 0007). This is a
        vendored-and-frozen copy; accessing it through ``build123d_drafting``
        warns and it will be removed in a future release.

    Dispatch is by attribute presence, not type:

    - leader-like  (``.elbow is not None``): elbow-through-label check.
    - dimension-like (``.measured_length is not None``): label-vs-measured and
      dim-inside-part checks.
    - centerline-like (``.is_centerline``): pairwise overlap against dims.

    Page-bounds checking is performed when *page_bbox* is provided as a
    ``(min_x, min_y, max_x, max_y)`` tuple, or when ``set_page()`` has been
    called and stored a module-level page context.  Any annotation whose full
    bounding box extends past the drawable area (page minus margin) is flagged
    as ``annotation_out_of_bounds`` (severity ``"error"``).  This includes a
    :class:`TitleBlock` passed as an item: its bounding box grows when a long
    string (e.g. a verbose subtitle) overflows the frame, so include the title
    block in *items* to catch text that spills past the page edge.

    Args:
        items: annotation objects exposing the relevant attrs (or SimpleNamespace
            stand-ins).
        part_bbox: optional BoundBox of the projected part outline.
        page_bbox: optional ``(min_x, min_y, max_x, max_y)`` drawable area.
            If ``None``, falls back to the module-level context set by
            ``set_page()``.  When neither is set, page-bounds are not checked.
        drawing_scale: the N:1 factor the geometry was scaled by before
            projecting (e.g. ``5.0`` for a 7.5 mm feature drawn at 5:1). The
            label-vs-measured check divides each measured path length by this
            before comparing to the label value, so labels carry the *real*
            dimension while the geometry is drawn enlarged. Defaults to ``1.0``
            (no scaling). See :func:`format_drawing_scale` to render the
            matching "5:1" indicator in the title block.
        view_shapes: optional list of build123d shapes representing projected
            view outlines.  When provided, annotations are checked against the
            view's actual projected edges (``view_annotation_overlap``,
            warning); an annotation inside the view's bounding box but over a
            blank region — a legitimate convention for callouts on large
            faces — is only an info-level ``view_annotation_inside_extents``
            notice.  View bounding boxes are also checked for overlap with
            each other (``view_overlap``, warning) and, when page bounds are
            known, against the drawable area (``view_out_of_bounds``, error).
            Annotations whose line-work must touch the view are not
            false-flagged: centrelines and datum targets are exempt, and
            annotations exposing a ``label_bbox`` (dimensions, leaders, datum
            features, surface-finish marks) are tested by the label-text
            extents only — witness lines, leader shafts, datum triangles, and
            finish marks may enter the view freely.  Shapes whose bounding box
            cannot be computed are silently skipped.
        view_edge_cache: optional dict, persisted by the caller across repeated
            ``lint_drawing`` calls on the *same* views, that memoises each view
            shape's per-edge bounding boxes — the dominant cost when a drawing
            is linted many times (e.g. a build→critique→fix loop) (#143). Pass
            the same dict to successive lints; discard it when the view shapes
            change. Omit it (the default) for a fresh per-call cache, which
            behaves exactly as before.

    Returns:
        list[LintIssue].

    Raises:
        ValueError: if ``drawing_scale`` is not positive (matches
            :func:`format_drawing_scale` / :class:`TitleBlock`).
    """
    if drawing_scale <= 0:
        raise ValueError(f"drawing_scale must be positive, got {drawing_scale}")

    issues: list[LintIssue] = []

    # Resolve page bounds: explicit arg beats module-level context.
    if page_bbox is None and _DRAWING_PAGE is not None:
        p = _DRAWING_PAGE
        page_bbox = (p["min_x"], p["min_y"], p["max_x"], p["max_y"])

    for item in items:
        if getattr(item, "elbow", None) is not None:
            _lint_leader(item, issues)
        elif getattr(item, "measured_length", None) is not None:
            _lint_dim(item, part_bbox, issues, drawing_scale)

    # Pairwise label-overlap check. The compare-box for a label-less item is an
    # *optimal* bounding_box() — expensive, and previously recomputed for both
    # items of every pair (O(n²): ~200 s on an 83-hole part). Compute each
    # item's box exactly once up front and index into it instead (#161); the
    # result is identical. Centre lines are compared via _centerline_extent, not
    # this box, so they don't need one.
    def _label_box(item):
        lb = getattr(item, "label_bbox", None)
        if lb is not None:
            return lb
        bb = item.bounding_box()
        return (bb.min.X, bb.min.Y, bb.max.X, bb.max.Y)

    boxes: list = []
    for item in items:
        if getattr(item, "is_centerline", False):
            boxes.append(None)
            continue
        try:
            boxes.append(_label_box(item))
        except Exception:
            boxes.append(None)

    for i, item_a in enumerate(items):
        for j in range(i + 1, len(items)):
            item_b = items[j]
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

                # Compare label text extents, NOT full bounding boxes.
                # Full bbox includes witness lines which legitimately overlap for
                # stacked dims (every inner bbox is a subset of the outer one).
                # label_bbox is the keep-clear region around the value text — the
                # thing that actually matters to a reader.
                la_box = boxes[i]
                lb_box = boxes[j]
                if la_box is None or lb_box is None:
                    continue
                ox = max(0.0, min(la_box[2], lb_box[2]) - max(la_box[0], lb_box[0]))
                oy = max(0.0, min(la_box[3], lb_box[3]) - max(la_box[1], lb_box[1]))
                if ox > 0.5 and oy > 0.5:
                    la = getattr(item_a, "label", "?")
                    lb = getattr(item_b, "label", "?")
                    issues.append(
                        LintIssue(
                            severity="warning",
                            message=(
                                f"labels '{la}' and '{lb}' overlap by "
                                f"{ox:.1f}×{oy:.1f} mm — use label_offset_x or "
                                f"increase dim offset to separate them"
                            ),
                            code="annotation_overlap",
                        )
                    )
            except Exception:
                pass

    # Page-bounds check — annotations must stay within the drawable area.
    if page_bbox is not None:
        for item in items:
            try:
                bb = item.bounding_box()
                for detail in _overshoots((bb.min.X, bb.min.Y, bb.max.X, bb.max.Y), page_bbox):
                    lbl = _item_label(item) or "?"
                    issues.append(
                        LintIssue(
                            severity="error",
                            message=(
                                f"annotation '{lbl}' extends past drawable area "
                                f"({detail}) — increase margin or reduce offset"
                            ),
                            code="annotation_out_of_bounds",
                        )
                    )
            except Exception:
                pass

    if view_shapes is not None:
        _lint_view_shapes(
            view_shapes, items, issues, page_bbox=page_bbox, edge_cache=view_edge_cache
        )

    # Principal envelope completeness check: verify each bbox extent appears
    # as a dimension label.  Only runs when part_bbox is supplied.
    if part_bbox is not None:
        covered: set[float] = set()
        for item in items:
            if getattr(item, "measured_length", None) is not None:
                val = _label_value(_item_label(item))
                if val is not None:
                    covered.add(val)

        def _check_extent(axis: str, page_extent: float) -> None:
            world_ext = page_extent / drawing_scale
            tol = max(0.5, world_ext * 0.001)
            if not any(abs(v - world_ext) <= tol for v in covered):
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f"no dimension found for {axis} extent ({world_ext:.4g} mm)"
                            " — add dim_width, dim_depth, or equivalent"
                        ),
                        code="missing_principal_dimension",
                    )
                )

        x_ext = part_bbox.max.X - part_bbox.min.X
        y_ext = part_bbox.max.Y - part_bbox.min.Y
        x_approx_y = max(x_ext, y_ext) > 1e-6 and abs(x_ext - y_ext) / max(x_ext, y_ext) < 0.05
        _check_extent("X", x_ext)
        if not x_approx_y:
            _check_extent("Y", y_ext)
        if hasattr(part_bbox.min, "Z") and hasattr(part_bbox.max, "Z"):
            _check_extent("Z", part_bbox.max.Z - part_bbox.min.Z)

    return issues


def _bbox2d(shape):
    """Return (minx, miny, maxx, maxy) for a build123d shape, or None on failure."""
    try:
        bb = shape.bounding_box()
        return bb.min.X, bb.min.Y, bb.max.X, bb.max.Y
    except Exception:
        return None


def _bboxes_overlap_2d(a, b) -> bool:
    """True if two (minx, miny, maxx, maxy) rectangles overlap in XY."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0


def _overshoots(bb, bounds) -> list[str]:
    """Sides where (min_x, min_y, max_x, max_y) *bb* spills past *bounds*, as text."""
    bx0, by0, bx1, by1 = bounds
    out = []
    if bb[0] < bx0:
        out.append(f"left by {bx0 - bb[0]:.1f} mm")
    if bb[2] > bx1:
        out.append(f"right by {bb[2] - bx1:.1f} mm")
    if bb[1] < by0:
        out.append(f"below by {by0 - bb[1]:.1f} mm")
    if bb[3] > by1:
        out.append(f"above by {bb[3] - by1:.1f} mm")
    return out


def _edges_intersect_rect(edge_entries, rect) -> bool:
    """True if any ``(edge, bbox2d)`` of *edge_entries* passes through the
    (min_x, min_y, max_x, max_y) rect.

    Straight edges use exact Liang–Barsky clipping; curved edges are sampled
    at roughly 1 mm spacing. An edge whose geometry cannot be analysed counts
    as a hit, so a real overlap is never silently missed.
    """
    for e, eb in edge_entries:
        try:
            if eb is None:
                return True  # unanalysable edge — count as a hit
            if not _bboxes_overlap_2d(eb, rect):
                continue
            if e.geom_type == GeomType.LINE:
                s, t = e.start_point(), e.end_point()
                if _seg_intersects_rect((s.X, s.Y), (t.X, t.Y), rect):
                    return True
                continue
            n = min(200, max(8, int(e.length) + 1))
            for i in range(n + 1):
                p = e.position_at(i / n)
                if rect[0] <= p.X <= rect[2] and rect[1] <= p.Y <= rect[3]:
                    return True
        except Exception:
            return True
    return False


def _view_edge_entries(vs, cache):
    """Per-edge ``(edge, bbox2d)`` list for view shape *vs*, memoised in *cache*.

    Building this list is the dominant lint cost (one optimal bounding box per
    projected edge); a caller-persisted *cache* lets repeated lints of the same
    views reuse it (#143). The shape is stored alongside its entries and checked
    by identity, so a reused cache can't return a stale list after ``id()``
    reuse. ``None`` marks a view whose edges can't be analysed (treated as a
    hit), matching the un-cached behaviour."""
    key = id(vs)
    hit = cache.get(key)
    if hit is not None and hit[0] is vs:
        return hit[1]
    try:
        entries: list | None = [(e, _bbox2d(e)) for e in vs.edges()]
    except Exception:
        entries = None
    cache[key] = (vs, entries)
    return entries


def _lint_view_shapes(view_shapes, ann_items, issues, page_bbox=None, edge_cache=None) -> None:
    """Check views against annotations (#159/#76), each other (#160), and the page (#75)."""
    # Build named bbox list; use the shape's id as fallback name.
    named_views = []
    view_shape_ids = set()
    for vs in view_shapes:
        bb = _bbox2d(vs)
        if bb is None:
            continue
        name = getattr(vs, "label", None) or getattr(vs, "name", None) or f"view@{id(vs)}"
        named_views.append((name, bb, vs))
        view_shape_ids.add(id(vs))

    # #159 — view shape vs annotation overlaps. Line-work (witness lines,
    # leader shafts, centrelines) legitimately enters the view, so test the
    # label-text bbox where the annotation exposes one and skip centrelines
    # entirely; only annotations without a label bbox fall back to their full
    # bounding box. Within the view bbox, only a label that crosses the view's
    # actual projected edges is a warning (#76) — on a large part the bbox is
    # mostly blank face, where placing callouts is a legitimate convention —
    # so a label over a blank region is reported as an info-level notice.
    cache = {} if edge_cache is None else edge_cache
    for vname, vbb, vs in named_views:
        vx0, vy0, vx1, vy1 = vbb
        for ann in ann_items:
            if id(ann) in view_shape_ids:
                continue
            if getattr(ann, "is_centerline", False):
                continue  # a centreline must cross the feature it marks
            if getattr(ann, "is_datum_target", False):
                continue  # a datum target sits on the part face by definition
            if getattr(ann, "is_section_hatch", False):
                continue  # hatching is intentionally inside the section view
            try:
                label_box = getattr(ann, "label_bbox", None)
                ab = label_box if label_box is not None else _bbox2d(ann)
                if ab is None:
                    continue
                if not _bboxes_overlap_2d(vbb, ab):
                    continue
                albl = (
                    getattr(ann, "label", None) or getattr(ann, "name", None) or type(ann).__name__
                )
                what = "label of annotation" if label_box is not None else "annotation"
                edges = _view_edge_entries(vs, cache)
                if edges is None or _edges_intersect_rect(edges, ab):
                    issues.append(
                        LintIssue(
                            severity="warning",
                            message=(
                                f"view '{vname}' line-work overlaps {what} '{albl}' "
                                f"— increase view spacing or move the annotation"
                            ),
                            code="view_annotation_overlap",
                        )
                    )
                else:
                    issues.append(
                        LintIssue(
                            severity="info",
                            message=(
                                f"{what} '{albl}' lies inside view '{vname}' extents "
                                f"[x={vx0:.1f}–{vx1:.1f}, y={vy0:.1f}–{vy1:.1f}] over a "
                                f"blank region — legitimate for callouts on large faces"
                            ),
                            code="view_annotation_inside_extents",
                        )
                    )
            except Exception:
                pass

    # #160 — view shape vs view shape bounding box overlaps
    for i, (aname, abb, _) in enumerate(named_views):
        ax0, ay0, ax1, ay1 = abb
        for bname, bbb, _ in named_views[i + 1 :]:
            bx0, by0, bx1, by1 = bbb
            if _bboxes_overlap_2d(abb, bbb):
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f"view '{aname}' bbox "
                            f"[x={ax0:.1f}–{ax1:.1f}, y={ay0:.1f}–{ay1:.1f}] "
                            f"overlaps view '{bname}' "
                            f"[x={bx0:.1f}–{bx1:.1f}, y={by0:.1f}–{by1:.1f}] "
                            f"— increase spacing between views"
                        ),
                        code="view_overlap",
                    )
                )

    # #75 — views must stay within the drawable area.
    if page_bbox is not None:
        for vname, vbb, _ in named_views:
            for detail in _overshoots(vbb, page_bbox):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=(
                            f"view '{vname}' extends past drawable area ({detail}) "
                            f"— reduce the view scale or move the view"
                        ),
                        code="view_out_of_bounds",
                    )
                )


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
            issues.append(
                LintIssue(
                    severity="warning",
                    message=(
                        f"label '{dim_label}' overlaps centerline by "
                        f"{ox:.1f}×{oy:.1f} mm — use label_offset_x to shift "
                        f"or increase dim offset to clear the centerline"
                    ),
                    code="label_centerline_overlap",
                )
            )
    except Exception:
        pass


def _lint_dim(item, part_bbox, issues, drawing_scale: float = 1.0) -> None:
    label = _item_label(item)
    measured = getattr(item, "measured_length", None)

    label_val = _label_value(label)
    if label_val is not None and measured is not None:
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
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f"Dim '{label}': label value {label_val:.3f} differs from "
                            f"measured path length {measured:.3f}"
                            + (
                                f" (÷{drawing_scale} = {effective_measured:.3f})"
                                if drawing_scale != 1.0
                                else ""
                            )
                            + f" by {ratio * 100:.1f}% "
                            f"— possible axis swap or wrong endpoint"
                        ),
                        code="label_vs_measured",
                    )
                )

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
            issues.append(
                LintIssue(
                    severity="warning",
                    message=(
                        f"Dim '{label}': annotation bbox overlaps part outline by "
                        f"{overlap / dim_area * 100:.0f}% — offset sign may place it inside the view"
                    ),
                    code="dim_inside_part",
                )
            )


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
            issues.append(
                LintIssue(
                    severity="error",
                    message=(
                        f"Leader '{getattr(item, 'label', '?')}': elbow point "
                        f"({ex:.2f}, {ey:.2f}) is inside the label bbox — leader "
                        f"line passes through the text"
                    ),
                    location=item.elbow,
                    code="leader_line_through_text",
                )
            )
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


def _bbox_dict(min_x, min_y, max_x, max_y):
    """A bbox dict in the same shape as ``TitleBlock.block_bbox``."""
    return {
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "width": max_x - min_x,
        "height": max_y - min_y,
    }


class TitleBlock(_Annotation):
    """ISO 7200:2004 title block built at the origin (bottom-left at (0, 0)).

    Standard 2-row layout::

        ┌──────────────────┬─────────┬──────┬──────┬──────┐
        │  part_name       │ dwg_no  │scale │ mat  │ rev  │
        ├──────────────────┼─────────┴──────┴──────┴──────┤
        │ general_tolerance│    designed_by               │
        └──────────────────┴─────────────────────────────-┘

    With ``legal_owner`` set, a full-width third row is added at the top::

        ┌──────────────────────────────────────────────────┐
        │  legal_owner (full width)                        │
        ├──────────────────┬─────────┬──────┬──────┬──────┤
        │  part_name       │ dwg_no  │scale │ mat  │ rev  │
        ├──────────────────┼─────────┴──────┴──────┴──────┤
        │ general_tolerance│    designed_by               │
        └──────────────────┴─────────────────────────────-┘

    Column proportions 40 / 20 / 15 / 15 / 10 %.

    ISO 7200:2004 mandatory field mapping:

    - Field 1 (legal owner)        → ``legal_owner``
    - Field 2 (document title)     → ``part_name``
    - Field 3 (document identifier) → ``drawing_number``
    - Field 4 (revision indicator) → ``revision``

    The ``revision`` parameter takes priority over ``date`` in the top-right
    cell.  Supply either ``revision`` (ISO 7200 preferred) or ``date`` (legacy).

    When ``show_labels`` is ``True`` (the default), each cell carries a small
    bottom-left field identifier: ``TITLE``, ``DWG NO.``, ``SCALE``, ``MAT.``,
    ``REV`` / ``DATE``, ``GEN. TOL.``, ``DRAWN BY``, and ``LEGAL OWNER``.
    Pass ``show_labels=False`` to suppress labels (legacy appearance).

    ``legal_owner_label`` controls just the full-width owner/origin row's
    identifier independently of ``show_labels``: pass ``None`` to omit it (e.g.
    when the row holds a self-attribution URL, where a ``LEGAL OWNER`` caption
    reads as a category error) or a string to rename it; the default keeps
    ``LEGAL OWNER``.

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
        revision: str = "",
        legal_owner: str = "",
        legal_owner_label: str | None = "LEGAL OWNER",
        show_labels: bool = True,
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
        legal_owner = legal_owner.strip()

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
        # When legal_owner is provided an extra full-width row sits above y2.
        y_top = (3.0 if legal_owner else 2.0) * cell_height

        strokes: list[Edge] = []
        # Outer border (right and left edges extend to y_top).
        strokes.append(Edge.make_line(Vector(x[0], y0, 0), Vector(x[-1], y0, 0)))
        strokes.append(Edge.make_line(Vector(x[-1], y0, 0), Vector(x[-1], y_top, 0)))
        strokes.append(Edge.make_line(Vector(x[-1], y_top, 0), Vector(x[0], y_top, 0)))
        strokes.append(Edge.make_line(Vector(x[0], y_top, 0), Vector(x[0], y0, 0)))
        # Row 0 / row 1 divider.
        strokes.append(Edge.make_line(Vector(x[0], y1, 0), Vector(x[-1], y1, 0)))
        # Row 1 / legal_owner divider (only when legal_owner row exists).
        if legal_owner:
            strokes.append(Edge.make_line(Vector(x[0], y2, 0), Vector(x[-1], y2, 0)))
        # Column verticals in the top content row.
        for xi in x[1:-1]:
            strokes.append(Edge.make_line(Vector(xi, y1, 0), Vector(xi, y2, 0)))
        # First column vertical in the bottom row.
        strokes.append(Edge.make_line(Vector(x[1], y0, 0), Vector(x[1], y1, 0)))

        fs = draft.font_size
        font = draft.font
        font_path = _font_path(draft)

        def _cell_txt(value, cx, cy):
            if not value:
                return None
            return Text(
                txt=value,
                font_size=fs,
                font=font,
                font_path=font_path,
                align=(Align.CENTER, Align.CENTER),
                mode=Mode.PRIVATE,
            ).moved(Location(Vector(cx, cy, 0.0)))

        # Small field-identifier labels anchored to bottom-left of each cell.
        lfs = fs * 0.5
        lpad = draft.pad_around_text * 0.4
        # Labels sit in the lower portion of a cell; suppress them if the label
        # text would overlap the (center-aligned) content text above it.
        _label_fits = (cell_height * 0.5 - fs * 0.5) > (lpad + lfs)

        def _label(text, x_left, y_bottom):
            if not show_labels or not _label_fits:
                return None
            return Text(
                txt=text,
                font_size=lfs,
                font=font,
                font_path=font_path,
                align=(Align.MIN, Align.MIN),
                mode=Mode.PRIVATE,
            ).moved(Location(Vector(x_left + lpad, y_bottom + lpad, 0.0)))

        top_y_mid = (y1 + y2) / 2.0
        # revision takes priority over date in the top-right cell (ISO 7200 field 4).
        col5_value, col5_label = (revision, "REV") if revision else (date, "DATE")
        top_cells = [
            (part_name, (x[0] + x[1]) / 2.0),
            (drawing_number, (x[1] + x[2]) / 2.0),
            (scale, (x[2] + x[3]) / 2.0),
            (material, (x[3] + x[4]) / 2.0),
            (col5_value, (x[4] + x[5]) / 2.0),
        ]
        top_label_specs = [
            ("TITLE", x[0], y1),
            ("DWG NO.", x[1], y1),
            ("SCALE", x[2], y1),
            ("MAT.", x[3], y1),
            (col5_label, x[4], y1),
        ]
        bot_y_mid = (y0 + y1) / 2.0
        bot_cells = [
            (general_tolerance, (x[0] + x[1]) / 2.0),
            (designed_by, (x[1] + x[-1]) / 2.0),
        ]
        bot_label_specs = [
            ("GEN. TOL.", x[0], y0),
            ("DRAWN BY", x[1], y0),
        ]

        text_faces = [_cell_txt(v, cx, top_y_mid) for v, cx in top_cells]
        text_faces += [_cell_txt(v, cx, bot_y_mid) for v, cx in bot_cells]
        text_faces += [_label(lbl, xl, yb) for lbl, xl, yb in top_label_specs]
        text_faces += [_label(lbl, xl, yb) for lbl, xl, yb in bot_label_specs]
        if legal_owner:
            lo_y_mid = (y2 + y_top) / 2.0
            text_faces.append(_cell_txt(legal_owner, (x[0] + x[-1]) / 2.0, lo_y_mid))
            if legal_owner_label:
                text_faces.append(_label(legal_owner_label, x[0], y2))
        text_faces = [t for t in text_faces if t is not None]

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(
            sk,
            label=part_name,
            label_bbox=None,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        # block_bbox: the frame extents in the BUILD frame (before any .moved()
        # or rotation=). Use .bounding_box() for the live, transform-accurate
        # extents when the title block has been repositioned.
        self.block_bbox = {
            "min_x": 0.0,
            "min_y": 0.0,
            "max_x": width,
            "max_y": y_top,
            "width": width,
            "height": y_top,
        }
        # Named cell rectangles in the BUILD frame, keyed by the constructor
        # field they hold (see cell_bbox()). The legal-owner cell exists only
        # when that full-width row was drawn.
        self._cells = {
            "title": _bbox_dict(x[0], y1, x[1], y2),
            "drawing_number": _bbox_dict(x[1], y1, x[2], y2),
            "scale": _bbox_dict(x[2], y1, x[3], y2),
            "material": _bbox_dict(x[3], y1, x[4], y2),
            "revision": _bbox_dict(x[4], y1, x[5], y2),
            "general_tolerance": _bbox_dict(x[0], y0, x[1], y1),
            "designed_by": _bbox_dict(x[1], y0, x[5], y1),
        }
        if legal_owner:
            self._cells["legal_owner"] = _bbox_dict(x[0], y2, x[-1], y_top)
        # Friendly aliases for the two cells whose constructor name and ISO 7200
        # label differ.
        self._cell_aliases = {"drawn_by": "designed_by", "date": "revision"}

    def cell_bbox(self, name: str) -> dict:
        """Bounding box of the named title-block cell, in the BUILD frame.

        *name* is the constructor field the cell holds: ``"title"``,
        ``"drawing_number"``, ``"scale"``, ``"material"``, ``"revision"``
        (alias ``"date"``), ``"general_tolerance"``, ``"designed_by"`` (alias
        ``"drawn_by"``), or ``"legal_owner"`` (only when a ``legal_owner`` row
        was drawn). Returns a dict with ``min_x``, ``min_y``, ``max_x``,
        ``max_y``, ``width``, ``height`` — same shape as ``block_bbox``.

        Coordinates are in the build frame (bottom-left of the block at the
        origin), like ``block_bbox``. When the title block has been
        repositioned with ``.moved()`` / ``rotation=``, apply the same location
        to these corners to get the live placement — a single cell cannot be
        recovered from ``.bounding_box()``. Intended for downstream consumers
        (e.g. ``draftwright`` placing an attribution link in the drawn-by cell).

        Raises:
            KeyError: if *name* is not a known cell (or ``"legal_owner"`` when
                no legal-owner row was drawn).
        """
        key = self._cell_aliases.get(name, name)
        if key not in self._cells:
            raise KeyError(f"unknown title-block cell: {name!r}")
        return dict(self._cells[key])

    def drawn_by_cell_bbox(self) -> dict:
        """Bounding box of the drawn-by (``designed_by``) cell, in the BUILD
        frame. Convenience for ``cell_bbox("designed_by")``."""
        return self.cell_bbox("designed_by")


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

    Metadata: ``.label``, ``.label_bbox`` (the Ra text extents), ``.mark_position``,
    ``.segments``.
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
            txt=ra_value,
            font_size=draft.font_size,
            font=draft.font,
            font_path=_font_path(draft),
            align=Align.CENTER,
            mode=Mode.PRIVATE,
        )
        text_w = probe.bounding_box().size.X
        gap = draft.pad_around_text
        shelf_end_v = Vector(elbow_x + gap + text_w + gap, elbow_y, 0.0)
        shelf = Edge.make_line(elbow_v, shelf_end_v)

        strokes = [leg1, leg2, shelf]

        v_gap = 0.3 * draft.font_size
        label_text = Text(
            txt=ra_value,
            font_size=draft.font_size,
            font=draft.font,
            font_path=_font_path(draft),
            align=(Align.MIN, Align.MIN),
            mode=Mode.PRIVATE,
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
        _tb = label_text.bounding_box()
        super().__init__(
            sk,
            label=ra_value,
            label_bbox=(_tb.min.X, _tb.min.Y, _tb.max.X, _tb.max.Y),
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.mark_position = (pos_v.X, pos_v.Y)


class ProjectionSymbol(_Annotation):
    """ISO 5456-2 projection-method symbol — the truncated-cone glyph that marks
    a drawing as **third-angle** or **first-angle**, for a title-block cell (#179).

    The symbol is a truncated cone shown in two views sharing a centreline: the
    cone in elevation (an isosceles trapezoid) beside its end view (two
    concentric circles, outer = large diameter, inner = small). The two
    projection methods differ in **which cone end faces the circles** — the
    orientation-independent rule that actually distinguishes them (ISO 5456-2;
    left/right placement is immaterial):

    * ``method="third"`` — the **small** (narrow) end of the cone is adjacent to
      the concentric circles;
    * ``method="first"`` — the **large** end is adjacent to the circles.

    They are therefore *not* mirror images: a left-right mirror preserves which
    end touches the circles, so only the taper direction relative to the end
    view changes between the two.

    ``method`` is required — the projection convention is region-dependent
    (ASME/US drawings are third-angle, many ISO/EU drawings first-angle) and a
    wrong symbol inverts every view, so there is deliberately no default.

    Pure geometry (thin filled faces on one ink layer, like every glyph here),
    no text; ``size`` (the large-end diameter's reach) defaults to
    ``draft.font_size`` so it drops into a title-block cell without extra
    scaling. Placed at the origin with align/rotation as usual.

    Metadata: ``.label`` (""), ``.method``, ``.segments``.
    """

    def __init__(
        self,
        method: Literal["third", "first"],
        draft: Draft | None = None,
        size: float | None = None,
        line_width: float = 0.15,
        rotation: float = 0,
        align=None,
        mode: Mode = Mode.ADD,
    ):
        if method not in ("third", "first"):
            raise ValueError(f"method must be 'third' or 'first', got {method!r}")
        draft = draft or Draft(font_size=2.5)
        h = size if size is not None else draft.font_size
        big_r = 0.5 * h  # large-end radius
        small_r = 0.3 * h  # small-end radius
        cone_len = 1.3 * h  # axial length of the cone elevation
        gap = 0.5 * h  # space between the elevation and the end view
        circ_cx = cone_len + gap + big_r  # centre of the concentric circles (right)

        # Cone elevation on the left (x=0..cone_len), end-view circles on the
        # right. The cone end nearer the circles (x=cone_len) carries the small
        # diameter for third-angle, the large diameter for first-angle — the ISO
        # rule that distinguishes the methods. The circles (outer=large,
        # inner=small) are the same for both; only the taper flips.
        near_r, far_r = (small_r, big_r) if method == "third" else (big_r, small_r)
        strokes: list[Edge] = []
        strokes.append(Edge.make_line(Vector(0.0, -far_r, 0.0), Vector(0.0, far_r, 0.0)))
        strokes.append(
            Edge.make_line(Vector(cone_len, -near_r, 0.0), Vector(cone_len, near_r, 0.0))
        )
        strokes.append(Edge.make_line(Vector(0.0, far_r, 0.0), Vector(cone_len, near_r, 0.0)))
        strokes.append(Edge.make_line(Vector(0.0, -far_r, 0.0), Vector(cone_len, -near_r, 0.0)))
        strokes += _circle_arcs(big_r, Vector(circ_cx, 0.0, 0.0))
        strokes += _circle_arcs(small_r, Vector(circ_cx, 0.0, 0.0))
        # centreline through both views, protruding a little past each end
        cl_lo, cl_hi = -0.25 * h, circ_cx + big_r + 0.25 * h
        strokes.append(Edge.make_line(Vector(cl_lo, 0.0, 0.0), Vector(cl_hi, 0.0, 0.0)))

        sk, seg = _strokes_and_text(strokes, [], line_width)
        super().__init__(
            sk, label="", label_bbox=None, segments=seg, rotation=rotation, align=align, mode=mode
        )
        self.method = method


# ---------------------------------------------------------------------------
# GD&T — feature control frame and datum symbols (ISO 1101 / ISO 5459)
# ---------------------------------------------------------------------------

GDTCharacteristic = Literal[
    "straightness",
    "flatness",
    "circularity",
    "cylindricity",
    "profile_line",
    "profile_surface",
    "angularity",
    "perpendicularity",
    "parallelism",
    "position",
    "concentricity",
    "symmetry",
    "circular_runout",
    "total_runout",
]

_GDT_GLYPHS: dict[str, str] = {
    "straightness": "—",
    "flatness": "▱",
    "circularity": "○",
    "cylindricity": "⌭",
    "profile_line": "⌒",
    "profile_surface": "⌓",
    "angularity": "∠",
    "perpendicularity": "⊥",
    "parallelism": "∥",
    "position": "⌖",
    "concentricity": "◎",
    "symmetry": "⌯",
    "circular_runout": "↗",
    "total_runout": "⏍",
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
        out.append(
            Edge.make_line(
                Vector(tx, ty, 0),
                Vector(tx + size * math.cos(a), ty + size * math.sin(a), 0),
            )
        )
    return out


def _characteristic_edges(name: str, h: float) -> list[Edge]:
    """Geometric-characteristic glyph as Edges, centred on the local origin."""
    s = 0.42 * h

    if name == "straightness":
        return [Edge.make_line(Vector(-s, 0, 0), Vector(s, 0, 0))]
    if name == "flatness":
        p1, p2 = Vector(-s, -0.5 * s, 0), Vector(0.3 * s, -0.5 * s, 0)
        p3, p4 = Vector(s, 0.5 * s, 0), Vector(-0.3 * s, 0.5 * s, 0)
        return [
            Edge.make_line(p1, p2),
            Edge.make_line(p2, p3),
            Edge.make_line(p3, p4),
            Edge.make_line(p4, p1),
        ]
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
        return [
            Edge.make_line(Vector(-s, -s, 0), Vector(s, s, 0)),
            Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0)),
        ]
    if name == "perpendicularity":
        return [
            Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0)),
            Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0)),
        ]
    if name == "parallelism":
        return [
            Edge.make_line(Vector(-0.7 * s, -s, 0), Vector(0.1 * s, s, 0)),
            Edge.make_line(Vector(-0.1 * s, -s, 0), Vector(0.7 * s, s, 0)),
        ]
    if name == "position":
        ring = list(Edge.make_circle(0.6 * s).edges())
        return ring + [
            Edge.make_line(Vector(-s, 0, 0), Vector(s, 0, 0)),
            Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0)),
        ]
    if name == "concentricity":
        return list(Edge.make_circle(s).edges()) + list(Edge.make_circle(0.5 * s).edges())
    if name == "symmetry":
        return [
            Edge.make_line(Vector(0, -s, 0), Vector(0, s, 0)),
            Edge.make_line(Vector(-0.7 * s, 0.45 * s, 0), Vector(0.7 * s, 0.45 * s, 0)),
            Edge.make_line(Vector(-0.7 * s, -0.45 * s, 0), Vector(0.7 * s, -0.45 * s, 0)),
        ]
    if name == "circular_runout":
        shaft = Edge.make_line(Vector(-s, -s, 0), Vector(s, s, 0))
        return [shaft] + _arrowhead((s, s), (-s, -s), 0.5 * s)
    if name == "total_runout":
        out = []
        for dx in (-0.25 * s, 0.35 * s):
            out.append(Edge.make_line(Vector(-s + dx, -s, 0), Vector(s - 0.6 * s + dx, s, 0)))
            out += _arrowhead((s - 0.6 * s + dx, s), (-s + dx, -s), 0.45 * s)
        return out

    raise ValueError(f"Unknown characteristic '{name}'. Supported: {', '.join(_GDT_GLYPHS)}")


def _gdt_text(draft: Draft, txt: str, fs: float):
    """Centred PRIVATE Text in the draft font — the GD&T frames' text builder."""
    return Text(
        txt=txt,
        font_size=fs,
        font=draft.font,
        font_path=_font_path(draft),
        align=(Align.CENTER, Align.CENTER),
        mode=Mode.PRIVATE,
    )


def _gdt_metrics(draft: Draft) -> tuple[float, float, float, float]:
    """Shared GD&T frame metrics: (font height, cell pad, ⌀-prefix radius, modifier radius)."""
    h = draft.font_size
    return h, 0.6 * h, 0.42 * h, 0.62 * h


def _gdt_tol_cell_width(draft: Draft, tol_str: str, diameter, modifier) -> float:
    """Width of a tolerance-value cell (value + optional ⌀ prefix + optional modifier)."""
    h, pad, r_pre, mr = _gdt_metrics(draft)
    w = pad + _gdt_text(draft, tol_str, h).bounding_box().size.X + pad
    if diameter:
        w += 2 * r_pre + pad
    if modifier:
        w += 2 * mr + pad
    return w


def _gdt_tol_cell(draft: Draft, tol_str: str, x_left: float, cy: float, diameter, modifier):
    """Strokes + text for one tolerance cell, laid out left→right from *x_left*."""
    h, pad, r_pre, mr = _gdt_metrics(draft)
    strokes: list[Edge] = []
    text_faces: list = []
    x = x_left + pad
    if diameter:
        dia_cx = x + r_pre
        strokes += [
            e.moved(Location(Vector(dia_cx, cy, 0))) for e in Edge.make_circle(r_pre).edges()
        ]
        strokes.append(
            Edge.make_line(
                Vector(dia_cx + 0.9 * r_pre, cy - 0.9 * r_pre, 0),
                Vector(dia_cx - 0.9 * r_pre, cy + 0.9 * r_pre, 0),
            )
        )
        x = dia_cx + r_pre + pad
    val_w = _gdt_text(draft, tol_str, h).bounding_box().size.X
    val_cx = x + val_w / 2
    text_faces.append(_gdt_text(draft, tol_str, h).moved(Location(Vector(val_cx, cy, 0))))
    x = val_cx + val_w / 2 + pad
    if modifier:
        m = modifier.upper()
        if m not in _MODIFIER_LETTER:
            raise ValueError(f"Unknown modifier '{modifier}'. Use M, L, or P.")
        mod_cx = x + mr
        strokes += [e.moved(Location(Vector(mod_cx, cy, 0))) for e in Edge.make_circle(mr).edges()]
        text_faces.append(
            _gdt_text(draft, _MODIFIER_LETTER[m], h * 0.8).moved(Location(Vector(mod_cx, cy, 0)))
        )
    return strokes, text_faces


def _gdt_datum_cell(draft: Draft, letter: str, cx: float, cy: float, dm):
    """Strokes + text for one datum-reference letter (+ optional material modifier)."""
    h = draft.font_size
    strokes: list[Edge] = []
    text_faces: list = []
    if dm:
        text_faces.append(_gdt_text(draft, letter, h).moved(Location(Vector(cx - 0.35 * h, cy, 0))))
        mod_cx = cx + 0.5 * h
        strokes += [
            e.moved(Location(Vector(mod_cx, cy, 0))) for e in Edge.make_circle(0.55 * h).edges()
        ]
        text_faces.append(
            _gdt_text(draft, dm.upper(), h * 0.7).moved(Location(Vector(mod_cx, cy, 0)))
        )
    else:
        text_faces.append(_gdt_text(draft, letter, h).moved(Location(Vector(cx, cy, 0))))
    return strokes, text_faces


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
                f"Unknown characteristic '{characteristic}'. Supported: {', '.join(_GDT_GLYPHS)}"
            )

        h = draft.font_size
        H = 2.0 * h
        datum_modifiers = datum_modifiers or {}

        if isinstance(tolerance, str):
            tol_str = tolerance
        else:
            prec = draft.decimal_precision
            tol_str = f"{round(tolerance, prec):.{prec}f}"

        w_sym = H
        w_tol = _gdt_tol_cell_width(draft, tol_str, diameter, modifier)
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

        ts, tf = _gdt_tol_cell(draft, tol_str, xs[1], cy, diameter, modifier)
        strokes += ts
        text_faces += tf

        for i, letter in enumerate(datums):
            cx = (xs[2 + i] + xs[3 + i]) / 2
            ds, dt = _gdt_datum_cell(draft, letter, cx, cy, datum_modifiers.get(letter))
            strokes += ds
            text_faces += dt

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(
            sk,
            label=tol_str,
            label_bbox=None,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.characteristic = name
        self.tolerance_str = tol_str
        self.datums = tuple(datums)


class DatumFeature(_Annotation):
    """ISO 5459 datum feature symbol: a (filled) triangle on a short leader to a
    framed datum letter. Triangle tip at the origin pointing down (-Y).

    Metadata: ``.label`` (letter), ``.label_bbox`` (the letter frame),
    ``.segments``, ``.letter``.
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
            strokes += [Edge.make_line(apex, bl), Edge.make_line(bl, br), Edge.make_line(br, apex)]

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

        glyph = Text(
            txt=letter,
            font_size=h,
            font=draft.font,
            font_path=_font_path(draft),
            align=(Align.CENTER, Align.CENTER),
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(0, (by0 + by1) / 2, 0)))

        sk, seg = _strokes_and_text(strokes, extra_faces + [glyph], line_width)
        super().__init__(
            sk,
            label=letter,
            label_bbox=(-box / 2, by0, box / 2, by1),
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.letter = letter


class DatumTarget(_Annotation):
    """ISO 5459 datum-target symbol: a circle split by a horizontal line into an
    upper compartment (target-area size) and a lower compartment (identifier).
    Centred at the origin.

    Metadata: ``.label`` (identifier), ``.label_bbox`` (None), ``.segments``,
    ``.identifier``, ``.area_label``, ``.is_datum_target`` (True — the symbol
    sits on the part face by definition, so the view-overlap lint exempts it).
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
        text_faces = [
            Text(
                txt=identifier,
                font_size=fs,
                font=draft.font,
                font_path=_font_path(draft),
                align=(Align.CENTER, Align.CENTER),
                mode=Mode.PRIVATE,
            ).moved(Location(Vector(0, -r / 2, 0)))
        ]
        if area_label:
            text_faces.append(
                Text(
                    txt=area_label,
                    font_size=fs,
                    font=draft.font,
                    font_path=_font_path(draft),
                    align=(Align.CENTER, Align.CENTER),
                    mode=Mode.PRIVATE,
                ).moved(Location(Vector(0, r / 2, 0)))
            )

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(
            sk,
            label=identifier,
            label_bbox=None,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.identifier = identifier
        self.area_label = area_label or ""
        self.is_datum_target = True


def _feature_symbol_edges(name: str, h: float) -> list[Edge]:
    """Hole-feature callout glyphs as Edges, centred on the local origin."""
    s = 0.42 * h
    if name == "diameter":
        ring = list(Edge.make_circle(s).edges())
        return ring + [Edge.make_line(Vector(0.9 * s, -0.9 * s, 0), Vector(-0.9 * s, 0.9 * s, 0))]
    if name == "counterbore":
        return [
            Edge.make_line(Vector(-s, s, 0), Vector(-s, -s, 0)),
            Edge.make_line(Vector(-s, -s, 0), Vector(s, -s, 0)),
            Edge.make_line(Vector(s, -s, 0), Vector(s, s, 0)),
        ]
    if name == "countersink":
        return [
            Edge.make_line(Vector(-s, s, 0), Vector(0, -s, 0)),
            Edge.make_line(Vector(s, s, 0), Vector(0, -s, 0)),
        ]
    if name == "depth":
        return [Edge.make_line(Vector(0, s, 0), Vector(0, -s, 0))] + _arrowhead(
            (0, -s), (0, s), 0.55 * s
        )
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
            raise ValueError(
                f"Unknown characteristic '{characteristic}'. Supported: {', '.join(_GDT_GLYPHS)}"
            )
        if not rows:
            raise ValueError("composite frame needs at least one row")

        h = draft.font_size
        H = 2.0 * h
        prec = draft.decimal_precision

        def _tol_str(t):
            return t if isinstance(t, str) else f"{round(t, prec):.{prec}f}"

        tol_strs = [_tol_str(r["tolerance"]) for r in rows]
        tol_w = max(
            _gdt_tol_cell_width(draft, ts, r.get("diameter"), r.get("modifier"))
            for r, ts in zip(rows, tol_strs, strict=True)
        )
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

            ts, tf = _gdt_tol_cell(
                draft, tol_strs[i], w_sym, cy, r.get("diameter"), r.get("modifier")
            )
            strokes += ts
            text_faces += tf

            datums = r.get("datums", ())
            dmods = r.get("datum_modifiers", {}) or {}
            for j, letter in enumerate(datums):
                cx = x2 + j * H + H / 2
                xr = x2 + (j + 1) * H
                if xr < total_w - 1e-6:
                    strokes.append(Edge.make_line(Vector(xr, bot, 0), Vector(xr, top, 0)))
                ds, dt = _gdt_datum_cell(draft, letter, cx, cy, dmods.get(letter))
                strokes += ds
                text_faces += dt

        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(
            sk,
            label=tol_strs[0],
            label_bbox=None,
            segments=seg,
            rotation=rotation,
            align=align,
            mode=mode,
        )
        self.characteristic = name
        self.tolerances = tuple(tol_strs)


class HoleCallout(_Annotation):
    """Single-line hole note built from geometry symbols, e.g. ``4× ⌀8.5 THRU``.
    Bottom-left at (0, 0).

    Metadata: ``.label`` (""), ``.label_bbox`` (None), ``.segments``,
    ``.callout_width``, ``.callout_height``, ``.covers_diameters``.
    """

    def __init__(
        self,
        diameter,
        *,
        count: int | None = None,
        through: bool = False,
        depth: float | str | None = None,
        cbore_dia=None,
        cbore_depth: float | str | None = None,
        csink_dia=None,
        csink_angle: float | None = None,
        suffix: str | None = None,
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
        if suffix:
            tokens.append(("text", suffix))

        strokes: list[Edge] = []
        text_faces = []
        x = 0.0
        for kind, val in tokens:
            if kind == "sym":
                cx = x + sym_w / 2
                strokes += [
                    e.moved(Location(Vector(cx, 0, 0))) for e in _feature_symbol_edges(val, h)
                ]
                x += sym_w + gap
            else:
                t = Text(
                    txt=val,
                    font_size=h,
                    font=draft.font,
                    font_path=_font_path(draft),
                    align=(Align.MIN, Align.CENTER),
                    mode=Mode.PRIVATE,
                ).moved(Location(Vector(x, 0, 0)))
                text_faces.append(t)
                x += t.bounding_box().size.X + gap

        width = max(x - gap, 0.0)
        sk, seg = _strokes_and_text(strokes, text_faces, line_width)
        super().__init__(
            sk, label="", label_bbox=None, segments=seg, rotation=rotation, align=align, mode=mode
        )
        self.callout_width = width
        self.callout_height = h
        # The ø values this callout dimensions — drawn as glyphs, so invisible
        # to label-text scans; lint_feature_coverage reads this instead.
        # Values may be strings carrying tolerance/fit text ("8.5 H7"): take
        # the leading number, and skip values with none.
        covers = []
        for v in (diameter, cbore_dia, csink_dia):
            if isinstance(v, str):
                m = re.match(r"\s*(\d+(?:\.\d+)?)", v)
                v = float(m.group(1)) if m else None
            if v:
                covers.append(float(v))
        self.covers_diameters = tuple(covers)
        # How many holes this single callout dimensions (the n× prefix) —
        # read by lint_feature_coverage's count check (#92)
        self.covers_count = count or 1


# ---------------------------------------------------------------------------
# find_overlaps — pure-geometry collision
# ---------------------------------------------------------------------------


def find_overlaps(sketches, *, min_area: float = 0.01) -> list[LintIssue]:
    """Pure-geometry collision check: pairs of sketches whose filled faces
    actually intersect with area > *min_area*.

    .. deprecated::
        Linting has moved to the ``draftwright`` package (ADR 0007). This is a
        vendored-and-frozen copy; accessing it through ``build123d_drafting``
        warns and it will be removed in a future release.

    Works on any build123d ``Sketch`` with zero metadata — uses the boolean
    intersect ``(a & b)`` and tests the resulting area.

    An AABB (bounding-box) pre-filter is applied before the expensive OCC
    boolean: pairs whose bboxes do not overlap are skipped, reducing the cost
    from O(n²) boolean ops to O(n²) cheap AABB tests + O(k) booleans where k
    is the number of actually-overlapping pairs (usually very small).

    When the boolean itself raises (non-manifold geometry, invalid sketch) a
    ``geometry_check_failed`` warning is emitted rather than silently
    continuing — so callers can distinguish "clean, no overlap" from "check
    never ran".

    Returns:
        list[LintIssue] with codes ``faces_overlap`` (warning) or
        ``geometry_check_failed`` (warning).
    """
    items = list(sketches)
    issues: list[LintIssue] = []
    # Pre-compute bboxes once; fall back to None on failure.
    bboxes: list[tuple[Any, Any, Any, Any] | None] = []
    for item in items:
        try:
            bb = item.bounding_box()
            bboxes.append((bb.min.X, bb.min.Y, bb.max.X, bb.max.Y))
        except Exception:
            bboxes.append(None)

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            # AABB pre-filter — skip if bboxes don't overlap at all.
            bi, bj = bboxes[i], bboxes[j]
            if bi is not None and bj is not None:
                if bi[2] <= bj[0] or bj[2] <= bi[0] or bi[3] <= bj[1] or bj[3] <= bi[1]:
                    continue
            la = getattr(items[i], "label", None) or f"#{i}"
            lb = getattr(items[j], "label", None) or f"#{j}"
            try:
                inter = items[i] & items[j]
                area = inter.area
            except Exception as exc:
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f"geometry check failed for '{la}' vs '{lb}': {exc} "
                            f"— overlap status unknown"
                        ),
                        code="geometry_check_failed",
                    )
                )
                continue
            if area > min_area:
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f"sketches '{la}' and '{lb}' overlap by {area:.2f} mm² of filled area"
                        ),
                        code="faces_overlap",
                    )
                )
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

    name = _item_label(item) or "?"
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
    return min(a[2], b[2]) - max(a[0], b[0]) > minov and min(a[3], b[3]) - max(a[1], b[1]) > minov


def _box_inside(inner, outer):
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


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
    if (
        abs((bx0 - ax0) * uy - (by0 - ay0) * ux) > tol
        or abs((bx1 - ax0) * uy - (by1 - ay0) * ux) > tol
    ):
        return 0.0
    pb0 = (bx0 - ax0) * ux + (by0 - ay0) * uy
    pb1 = (bx1 - ax0) * ux + (by1 - ay0) * uy
    lo = max(0.0, min(pb0, pb1))
    hi = min(la, max(pb0, pb1))
    return max(0.0, hi - lo)


def find_interferences(
    items, *, part_bbox=None, page_bbox=None, obstacles=None, min_overlap=0.5, pad=0.2, min_run=1.5
):
    """Geometry-precise interference detection between drafting annotations.

    .. deprecated::
        Linting has moved to the ``draftwright`` package (ADR 0007). This is a
        vendored-and-frozen copy; accessing it through ``build123d_drafting``
        warns and it will be removed in a future release.

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
        for box_j, _, name_j in geoms[i + 1 :]:
            if box_j is not None and _box_overlap(box_i, box_j, min_overlap):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=f'labels "{name_i}" and "{name_j}" overlap',
                        code="labels_overlap",
                    )
                )
        if page_bbox is not None:
            pb = (page_bbox.min.X, page_bbox.min.Y, page_bbox.max.X, page_bbox.max.Y)
            if not _box_inside(box_i, pb):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=f'label "{name_i}" extends outside the drawing frame',
                        code="label_out_of_frame",
                    )
                )
        if part_bbox is not None:
            pb = (part_bbox.min.X, part_bbox.min.Y, part_bbox.max.X, part_bbox.max.Y)
            if _box_overlap(box_i, pb, min_overlap):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=f'label "{name_i}" sits on the part outline',
                        code="label_on_part",
                    )
                )
        if obstacles:
            if any(_box_overlap(box_i, _as_box(o), min_overlap) for o in obstacles):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=f'label "{name_i}" lands over drawing geometry',
                        code="label_over_geometry",
                    )
                )

    for i, (_, segs_i, name_i) in enumerate(geoms):
        for j, (box_j, _, name_j) in enumerate(geoms):
            if i == j or box_j is None:
                continue
            if any(_seg_hits_box(p, q, box_j, pad) for (p, q) in segs_i):
                issues.append(
                    LintIssue(
                        severity="error",
                        message=f'a line from "{name_i}" pierces label "{name_j}"',
                        code="line_pierces_label",
                    )
                )

    for i, (_, segs_i, name_i) in enumerate(geoms):
        for j in range(i + 1, len(geoms)):
            _, segs_j, name_j = geoms[j]
            if any(_collinear_overlap(a, b) > min_run for a in segs_i for b in segs_j):
                issues.append(
                    LintIssue(
                        severity="warning",
                        message=(
                            f'redundant overlapping lines between "{name_i}" '
                            f'and "{name_j}" — shared witness/edge drawn twice'
                        ),
                        code="redundant_lines",
                    )
                )

    return issues


# ---------------------------------------------------------------------------
# Drawing context — set_page() and annotate() for standalone scripts
# ---------------------------------------------------------------------------

# Module-level drawing context singleton. Stores page extents set by
# set_page() so that lint_drawing() can check annotation bounds in standalone
# scripts (i.e. outside the MCP session).
_DRAWING_PAGE: dict | None = None


def set_page(width: float, height: float, margin: float = 5.0) -> dict:
    """Register the drawing page extent for lint_drawing() bounds checking.

    .. deprecated::
        This only feeds the standalone linting that has moved to the
        ``draftwright`` package (ADR 0007). Accessing it through
        ``build123d_drafting`` warns; it will be removed in a future release.

    In the MCP server ``set_page()`` is a session builtin that stores state in
    the session object; this module-level version stores state globally in the
    ``build123d_drafting`` module so that standalone drawing scripts can use it
    without an MCP session::

        from build123d_drafting import set_page, lint_drawing

        set_page(297, 210, margin=10)   # A4 landscape
        # ... build dims ...
        violations = lint_drawing(dims)  # checks page bounds automatically

    Args:
        width:  page width in mm (e.g. 297 for A4 landscape).
        height: page height in mm (e.g. 210 for A4 landscape).
        margin: clear border in mm (default 5). Annotations must stay within
            (margin, margin) to (width−margin, height−margin).

    Returns:
        The page dict ``{min_x, min_y, max_x, max_y, width, height, margin}``,
        also stored module-globally for ``lint_drawing()``.
    """
    global _DRAWING_PAGE
    _DRAWING_PAGE = {
        "width": width,
        "height": height,
        "margin": margin,
        "min_x": margin,
        "min_y": margin,
        "max_x": width - margin,
        "max_y": height - margin,
    }
    return _DRAWING_PAGE


def clear_page() -> None:
    """Clear the module-level drawing page context (e.g. between sheets in tests).

    .. deprecated::
        Standalone-lint helper; linting has moved to the ``draftwright`` package
        (ADR 0007). Accessing it through ``build123d_drafting`` warns; it will be
        removed in a future release.
    """
    global _DRAWING_PAGE
    _DRAWING_PAGE = None


def annotate(result, name: str | None = None, label: str | None = None) -> None:
    """Register a drawing annotation for lint_drawing() in standalone scripts.

    .. deprecated::
        Standalone-lint helper; linting has moved to the ``draftwright`` package
        (ADR 0007). Accessing it through ``build123d_drafting`` warns; it will be
        removed in a future release.

    In the MCP server ``annotate()`` is a session builtin that stores metadata
    in the session *and* calls ``show()``.  This module-level version is a
    lightweight companion for standalone drawing scripts: since
    ``build123d_drafting`` annotation objects (``Dimension``, ``Leader``, …)
    already carry their lint metadata as instance attributes (``label``,
    ``label_bbox``, ``segments``, ``measured_length``, …), calling
    ``annotate()`` is **optional** in standalone use.  Its main purpose is
    providing a drop-in API so the same drawing script runs both via the MCP
    and as a standalone Python script without changes.

    In standalone mode this is a no-op unless you need to attach an explicit
    label string to a vanilla build123d ``ExtensionLine`` / ``DimensionLine``
    (which does not carry its constructor label after construction)::

        from build123d import ExtensionLine, Draft
        from build123d_drafting import annotate, lint_drawing

        draft = Draft(font_size=2.5, decimal_precision=1)
        el = ExtensionLine(border=[...], offset=8, draft=draft, label="40")
        annotate(el, "width", label="40")   # attaches label for lint_vs_measured

    For ``Dimension`` / ``Leader`` objects, just pass them to ``lint_drawing()``
    directly — ``annotate()`` is not needed.

    Args:
        result: annotation object.
        name:   optional name (ignored in standalone mode).
        label:  explicit label string, stored as ``result._annotate_label``
                and read by ``lint_drawing()`` if the object has no ``.label``.
    """
    if label is not None:
        try:
            result._annotate_label = label
        except AttributeError:
            pass
