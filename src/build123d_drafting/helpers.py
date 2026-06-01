"""
build123d drawing helpers — pure build123d, no MCP dependency.

Import from your drawing scripts directly:

    from build123d_mcp.drawing_helpers import (
        dim_linear, safe_dim_line, leader, view_axes, lint_drawing,
        DimResult, LeaderResult, LintIssue,
    )

These helpers paper over the rough edges in build123d.drafting:

  dim_linear    ExtensionLine with named side ("above"/"below"/"left"/"right")
                instead of raw signed offset.  The sign is computed from the
                path direction's right-hand normal so you never have to guess.

  safe_dim_line DimensionLine that won't raise ValueError when the label is
                longer than the dim path — it truncates gracefully.

  leader        Leader annotation built from scratch: arrowhead shaft + horizontal
                shelf + text, returned as separate (lines, text) compounds so each
                can go on its own SVG layer with appropriate fill_color.

  view_axes     Analytic world→page axis mapping for a project_to_viewport call.
                Returns {"world_X": ("page_X", +1.0), "world_Z": ("depth", 0.0), …}
                so axis swaps in bottom/side views are visible before rendering.

  lint_drawing  Structural checks on a list of DimResult / LeaderResult objects:
                label-vs-measured-length divergence, leader-through-label, and
                optionally dim-inside-part-outline.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from build123d import (
    Align,
    Arrow,
    Compound,
    DimensionLine,
    Draft,
    Edge,
    ExtensionLine,
    GeomType,
    Location,
    Mode,
    Text,
    Vector,
)
from build123d.operations_generic import sweep


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LintIssue:
    severity: Literal["error", "warning"]
    message: str
    location: tuple[float, float] | None = None
    code: str = ""   # stable machine-readable check id, e.g. "label_vs_measured"


@dataclass
class DimResult:
    """Returned by dim_linear and safe_dim_line."""
    shape: Compound
    label_str: str
    measured_length: float
    dim_level_y: float | None = None  # Y coord of the dim line (for stacking checks)
    label_bbox: tuple[float, float, float, float] | None = None  # (min_x, min_y, max_x, max_y)

    def bbox(self):
        return self.shape.bounding_box()


@dataclass
class CenterlineResult:
    """Returned by centerline()."""
    shape: Compound
    label_str: str = ""

    def bbox(self):
        return self.shape.bounding_box()


@dataclass
class LeaderResult:
    """Returned by leader().

    Route *lines* and *text* to separate SVG layers, both with fill_color set
    (lines needs it for the arrowhead and shelf faces; text needs it so glyph
    faces render filled rather than outlined).
    """
    lines: Compound   # Arrow + shelf — route to a fill_color layer
    text: Compound    # label glyphs — route to a fill_color layer
    label_str: str
    tip: tuple[float, float]
    elbow: tuple[float, float]

    @property
    def shape(self) -> Compound:
        """Combined compound for single-layer export."""
        return Compound(children=[self.lines, self.text])

    def bbox(self):
        return self.shape.bounding_box()


@dataclass
class TitleBlockResult:
    """Returned by iso_title_block().

    Route *lines* to a line_color SVG layer and *text* to a fill_color layer.
    """
    lines: Compound   # border and grid edges
    text: Compound    # all text faces
    bbox: dict        # {"min_x", "min_y", "max_x", "max_y", "width", "height"}

    @property
    def shape(self) -> Compound:
        return Compound(children=[self.lines, self.text])


@dataclass
class SurfaceFinishResult:
    """Returned by surface_finish_mark().

    Route *lines* to a line_color SVG layer and *text* to a fill_color layer.
    """
    lines: Compound               # check-mark strokes and horizontal shelf
    text: Compound                # Ra label glyphs
    label_str: str
    position: tuple[float, float]

    @property
    def shape(self) -> Compound:
        return Compound(children=[self.lines, self.text])

    def bbox(self):
        return self.shape.bounding_box()


# ---------------------------------------------------------------------------
# Draft preset
# ---------------------------------------------------------------------------

def draft_preset(font_size: float = 2.5, decimal_precision: int = 2,
                 **overrides) -> Draft:
    """A ``Draft`` tuned for clean technical-drawing output.

    build123d's ``Draft`` defaults render heavy arrowheads
    (``arrow_length=3.0`` mm) and thick dimension lines (``line_width=0.5`` mm),
    which look clumsy at the small font sizes typical of multi-view sheets. This
    preset scales the arrowhead to the font and uses a thin line, closer to ISO
    drawing weights::

        arrow_length = 0.9 * font_size
        line_width   = 0.1

    Use it as the starting point for every helper that takes a ``draft``; any
    field can be overridden by keyword::

        draft = draft_preset(font_size=1.6)
        draft = draft_preset(font_size=3.0, line_width=0.15, arrow_length=2.0)

    Note that on-screen/SVG stroke thickness is a separate concern set by the
    exporter (``ExportSVG.add_layer(line_weight=...)``); this only controls the
    geometry build123d generates.
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
# dim_linear
# ---------------------------------------------------------------------------

_SIDE_VECTORS: dict[str, tuple[float, float, float]] = {
    "above": (0.0,  1.0, 0.0),
    "below": (0.0, -1.0, 0.0),
    "left":  (-1.0, 0.0, 0.0),
    "right": ( 1.0, 0.0, 0.0),
}


def _offset_sign(
    p1: tuple,
    p2: tuple,
    toward: tuple[float, float, float],
) -> int:
    """Return +1 or -1 so ExtensionLine(offset=sign*d) places the dim toward *toward*.

    For a path from p1 to p2, the right-hand normal on the XY plane is
    (dy, -dx, 0).  offset>0 ↔ Side.RIGHT (right-hand side of path direction);
    offset<0 ↔ Side.LEFT.
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    right = Vector(dy, -dx, 0.0)
    if right.length < 1e-10:
        return 1
    right = right.normalized()
    tw = Vector(*toward).normalized()
    return 1 if right.dot(tw) >= 0 else -1


def dim_linear(
    p1: tuple,
    p2: tuple,
    side: Literal["above", "below", "left", "right"] | tuple[float, float, float],
    distance: float,
    draft: Draft,
    label: str | None = None,
    tolerance: float | tuple[float, float] | None = None,
    label_offset_x: float = 0.0,
) -> DimResult:
    """ExtensionLine wrapper with named placement side.

    Args:
        p1, p2: endpoints of the segment to dimension (3-tuple or 2-tuple).
        side:   where to place the dim line — "above", "below", "left", "right",
                or an explicit world-direction vector (e.g. (0, 1, 0) for above).
        distance: perpendicular distance from the segment to the dim line (>0).
        draft:  Draft config.
        label:  override label string; if None the measured length is formatted.
        tolerance: symmetric float or (lower, upper) pair appended to the label.
        label_offset_x: signed distance (mm) to shift the label along the dim line
            away from the midpoint. Positive shifts toward p2; negative toward p1.
            When 0.0 (default), behaviour is unchanged.

    Returns:
        DimResult with .shape (the Compound), .label_str, .measured_length,
        and .label_bbox (4-tuple min_x, min_y, max_x, max_y).
    """
    toward = _SIDE_VECTORS[side] if isinstance(side, str) else tuple(side)
    sign = _offset_sign(p1, p2, toward)
    offset = sign * abs(distance)

    measured_label = label  # what we pass to ExtensionLine for its built-in text
    if label_offset_x != 0.0:
        measured_label = ""  # suppress built-in label; we'll place our own Text

    _force_external = False
    try:
        shape = ExtensionLine(
            border=[p1, p2],
            offset=offset,
            draft=draft,
            label=measured_label,
            tolerance=tolerance,
            mode=Mode.PRIVATE,
        )
    except ValueError:
        # Path too short for inline label — ExtensionLine with label="" also crashes;
        # label=None suppresses the built-in text without triggering the empty-wire bug.
        # Place the label externally via the Text path below.
        shape = ExtensionLine(border=[p1, p2], offset=offset, draft=draft,
                              label=None, tolerance=None, mode=Mode.PRIVATE)
        label_offset_x = label_offset_x or 0.0  # ensure external text path runs below
        _force_external = True

    measured = shape.dimension  # set by ExtensionLine: length of the border path
    label_str = label if label is not None else _format_label(measured, draft, tolerance)

    bb = shape.bounding_box()
    dim_level_y = bb.max.Y if abs(bb.max.Y) >= abs(bb.min.Y) else bb.min.Y

    # Compute the label position
    midpoint_x = (p1[0] + p2[0]) / 2.0
    label_x = midpoint_x + label_offset_x

    # Probe text size for label_bbox
    probe = Text(
        txt=label_str,
        font_size=draft.font_size,
        font=draft.font,
        align=Align.CENTER,
        mode=Mode.PRIVATE,
    )
    text_bb = probe.bounding_box()
    half_w = text_bb.size.X / 2.0
    half_h = text_bb.size.Y / 2.0
    label_bbox_tuple = (
        label_x - half_w,
        dim_level_y - half_h,
        label_x + half_w,
        dim_level_y + half_h,
    )

    if label_offset_x != 0.0 or _force_external:
        # Place explicit Text at the shifted position
        text_shape = Text(
            txt=label_str,
            font_size=draft.font_size,
            font=draft.font,
            align=(Align.CENTER, Align.CENTER),
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(label_x, dim_level_y, 0.0)))
        final_shape = Compound(children=[shape, text_shape])
    else:
        final_shape = shape

    return DimResult(
        shape=final_shape,
        label_str=label_str,
        measured_length=measured,
        dim_level_y=dim_level_y,
        label_bbox=label_bbox_tuple,
    )


def _format_label(
    length: float,
    draft: Draft,
    tolerance: float | tuple[float, float] | None,
) -> str:
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


# ---------------------------------------------------------------------------
# centerline
# ---------------------------------------------------------------------------

def centerline(
    p1: tuple,
    p2: tuple,
    draft: Draft | None = None,  # noqa: ARG001 — reserved for future dash pattern config
) -> CenterlineResult:
    """Create a centerline between two points.

    Args:
        p1, p2: endpoints of the centerline (3-tuple or 2-tuple).
        draft:  Draft config (reserved; not yet used).

    Returns:
        CenterlineResult with .shape (Compound wrapping an Edge).
    """
    v1 = Vector(p1[0], p1[1], p1[2] if len(p1) > 2 else 0.0)
    v2 = Vector(p2[0], p2[1], p2[2] if len(p2) > 2 else 0.0)
    edge = Edge.make_line(v1, v2)
    return CenterlineResult(shape=Compound(children=[edge]))


# ---------------------------------------------------------------------------
# safe_dim_line
# ---------------------------------------------------------------------------

def safe_dim_line(
    path: list | Edge,
    label: str,
    draft: Draft,
    fallback_label: str | None = None,
) -> DimResult:
    """DimensionLine wrapper that won't crash on labels longer than the path.

    build123d raises ValueError("Can't get geom adaptor of empty wire") when
    the label is wider than the dimension path.  This wrapper catches that and
    retries with a truncated label.

    Args:
        path: two-point list or Edge.
        label: desired label string.
        draft: Draft config.
        fallback_label: label to use on retry; default truncates to 6 chars + "…".

    Returns:
        DimResult.  If both attempts fail, .shape is a bare Edge compound.
    """
    if isinstance(path, (list, tuple)):
        edge = Edge.make_line(Vector(*path[0][:3]), Vector(*path[1][:3]))
    else:
        edge = path
    measured = edge.length

    for lbl in [label, fallback_label or _truncate(label)]:
        try:
            shape = DimensionLine(path=path, draft=draft, label=lbl, mode=Mode.PRIVATE)
            return DimResult(shape=shape, label_str=lbl, measured_length=measured)
        except Exception:
            continue

    # Last resort — bare edge so the drawing isn't missing the line
    return DimResult(
        shape=Compound(children=[edge]),
        label_str=fallback_label or _truncate(label),
        measured_length=measured,
    )


def _truncate(s: str, max_len: int = 6) -> str:
    return s[:max_len] + "…" if len(s) > max_len else s


# ---------------------------------------------------------------------------
# leader
# ---------------------------------------------------------------------------

def leader(
    tip: tuple,
    elbow: tuple,
    label: str,
    draft: Draft,
) -> LeaderResult:
    """Leader annotation with arrowhead at *tip* and label hanging from *elbow*.

    The horizontal shelf runs away from *tip* so the label text sits cleanly
    after the shelf end — the line never passes through the label bbox.

    Args:
        tip:   arrow point on the part feature (x, y[, z]).
        elbow: where the shaft bends to become the horizontal shelf (x, y[, z]).
        label: annotation text (e.g. "⌀7.93 H7", "Ra 1.6").
        draft: Draft config.

    Returns:
        LeaderResult with .lines (Arrow + shelf Face) and .text (Text compound).
        Route both to SVG layers with fill_color set.
    """
    tip_v = Vector(tip[0], tip[1], 0.0)
    elbow_v = Vector(elbow[0], elbow[1], 0.0)

    # Measure text width so the shelf ends before the label
    probe = Text(
        txt=label,
        font_size=draft.font_size,
        font=draft.font,
        align=Align.CENTER,
        mode=Mode.PRIVATE,
    )
    text_w = probe.bounding_box().size.X
    gap = draft.pad_around_text

    # Shelf goes in the direction away from tip (left or right).
    # Shelf is a short stub (= gap) that ends where the text starts.
    # Using gap + text_w + gap would extend the shelf line through the label.
    shelf_dir = 1.0 if elbow_v.X >= tip_v.X else -1.0
    shelf_len = gap
    shelf_end_v = Vector(elbow_v.X + shelf_dir * shelf_len, elbow_v.Y, 0.0)

    # Arrow: shaft from tip to elbow with arrowhead at tip
    shaft_edge = Edge.make_line(tip_v, elbow_v)
    arrow_shape = Arrow(
        arrow_size=draft.arrow_length,
        shaft_path=shaft_edge,
        shaft_width=draft.line_width,
        head_at_start=True,
        mode=Mode.PRIVATE,
    )

    # Horizontal shelf as a swept filled rectangle (Face), same as ExtensionLine shafts
    shelf_edge = Edge.make_line(elbow_v, shelf_end_v)
    shelf_pen = shelf_edge.perpendicular_line(draft.line_width, 0)
    shelf_shape = sweep(shelf_pen, shelf_edge, mode=Mode.PRIVATE)

    lines = Compound(children=[arrow_shape, shelf_shape])

    # Text centred vertically at shelf height, horizontally inset by one gap
    if shelf_dir > 0:
        text_align = (Align.MIN, Align.CENTER)
        text_x = elbow_v.X + gap
    else:
        text_align = (Align.MAX, Align.CENTER)
        text_x = elbow_v.X - gap

    text_shape = Text(
        txt=label,
        font_size=draft.font_size,
        font=draft.font,
        align=text_align,
        mode=Mode.PRIVATE,
    ).moved(Location(Vector(text_x, elbow_v.Y, 0.0)))

    text = Compound(children=[text_shape])

    return LeaderResult(
        lines=lines,
        text=text,
        label_str=label,
        tip=(tip_v.X, tip_v.Y),
        elbow=(elbow_v.X, elbow_v.Y),
    )


_COMPASS_ANGLES = {
    "E":  0.0,
    "NE": 45.0,
    "N":  90.0,
    "NW": 135.0,
    "W":  180.0,
    "SW": 225.0,
    "S":  270.0,
    "SE": 315.0,
}


def leader_offset(
    tip: tuple,
    direction: str | float,
    length: float,
    label: str,
    draft: Draft,
) -> LeaderResult:
    """Leader with the elbow placed by direction + distance instead of absolute coords.

    Computes ``elbow = tip + (cos θ, sin θ) * length`` and delegates to ``leader()``.
    Useful when laying out callouts relative to a feature ("12 mm NW of this point")
    rather than in absolute page coordinates — especially handy when the drawing
    uses a non-1:1 scale.

    Args:
        tip:       arrow point on the part feature (x, y[, z]).
        direction: compass string ("N", "NE", "E", "SE", "S", "SW", "W", "NW",
                   case-insensitive) or an angle in degrees CCW from +X
                   (so 0 = +X, 90 = +Y, matches build123d conventions).
        length:    distance from tip to elbow, in page mm.
        label:     annotation text.
        draft:     Draft config.

    Returns:
        LeaderResult — same as ``leader()``.
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
    return leader(tip=tip, elbow=elbow, label=label, draft=draft)


# ---------------------------------------------------------------------------
# view_axes — pure Python helpers (no OCC import)
# ---------------------------------------------------------------------------

def _dot3(a: tuple, b: tuple) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def _sub3(a: tuple, b: tuple) -> tuple:
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

def _scale3(s: float, v: tuple) -> tuple:
    return (s*v[0], s*v[1], s*v[2])

def _cross3(a: tuple, b: tuple) -> tuple:
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def _norm3(v: tuple) -> tuple:
    mag = math.sqrt(_dot3(v, v))
    return (v[0]/mag, v[1]/mag, v[2]/mag) if mag > 1e-10 else (0.0, 0.0, 0.0)


def view_axes(
    viewport_origin: tuple,
    viewport_up: tuple = (0.0, 1.0, 0.0),
    look_at: tuple = (0.0, 0.0, 0.0),
) -> dict[str, tuple[str, float]]:
    """Return the world→page axis mapping for a project_to_viewport call.

    Computes the projection plane axes analytically (Gram-Schmidt + cross
    product) so you know before rendering whether world-X maps to page-X or
    page-Y, and with what sign.

    Args:
        viewport_origin: camera position (same arg as project_to_viewport).
        viewport_up:     up vector for the page (same arg as project_to_viewport).
        look_at:         target point (same arg as project_to_viewport).

    Returns:
        dict mapping "world_X" / "world_Y" / "world_Z" to
        ("page_X"|"page_Y"|"depth", sign) where sign is +1.0 or -1.0
        (0.0 for "depth", meaning that axis points toward/away the camera).

    Example — top view (camera above, viewport_up = +Y):
        {"world_X": ("page_X", 1.0), "world_Y": ("page_Y", 1.0), "world_Z": ("depth", 0.0)}

    Example — bottom view (camera below, viewport_up = +Y):
        {"world_X": ("page_X", -1.0), "world_Y": ("page_Y", 1.0), "world_Z": ("depth", 0.0)}
        ↑ world-X is flipped on the page — the classic bottom-view axis swap.
    """
    # Pure Python arithmetic — no build123d/OCC import so this call is fast
    # even when the OCC kernel hasn't been loaded yet (avoids _SHORT_TIMEOUT).
    vo = tuple(float(x) for x in viewport_origin)
    la = tuple(float(x) for x in look_at)
    vu = tuple(float(x) for x in viewport_up)

    view_dir = _norm3(_sub3(la, vo))
    # Gram-Schmidt: remove the view_dir component from viewport_up
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
    draft: "Draft",
    base_distance: float = 8.0,
    tier_spacing: float | None = None,
) -> list["DimResult"]:
    """Build a stack of dims with automatically assigned offsets.

    Takes specs *without* a distance argument and assigns offsets so that dims
    spanning overlapping ranges are placed on successive tiers. Dims with
    non-overlapping ranges share a tier at the same offset.

    Ordering matters: specs listed first get lower tiers (closer to the part).

    Args:
        specs:         list of (p1, p2, side, label) or
                       (p1, p2, side, label, tolerance) tuples.
        draft:         Draft config.
        base_distance: offset for the innermost tier (mm, default 8.0).
        tier_spacing:  distance between successive tiers (mm).
                       Defaults to draft.font_size * 3 + draft.arrow_length.

    Returns:
        list of DimResult, same length and order as specs.
    """
    if tier_spacing is None:
        tier_spacing = draft.font_size * 3.0 + draft.arrow_length

    # tier_occupancy[i] = list of (range_min, range_max) placed on tier i
    tier_occupancy: list[list[tuple[float, float]]] = []

    results = []
    for spec in specs:
        p1, p2, side = spec[0], spec[1], spec[2]
        label = spec[3]
        tolerance = spec[4] if len(spec) > 4 else None

        # Primary span: X for above/below dims, Y for left/right dims.
        toward = _SIDE_VECTORS[side] if isinstance(side, str) else tuple(side)
        use_x = abs(toward[1]) >= abs(toward[0])  # above/below stacks in Y, spans in X
        span = (
            (min(p1[0], p2[0]), max(p1[0], p2[0])) if use_x
            else (min(p1[1], p2[1]), max(p1[1], p2[1]))
        )

        # First tier whose existing spans don't overlap this one.
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
            dim_linear(p1, p2, side, offset, draft, label=label, tolerance=tolerance)
        )

    return results


# ---------------------------------------------------------------------------
# place_labels
# ---------------------------------------------------------------------------

def _compute_label_offset_x(
    dim: "DimResult",
    centerlines: list,
    gap: float,
) -> float:
    """Return the cumulative label_offset_x needed to clear all crossing vertical centerlines."""
    if dim.label_bbox is None:
        return 0.0
    lmin_x, _lmin_y, lmax_x, _lmax_y = dim.label_bbox
    label_cx = (lmin_x + lmax_x) / 2.0
    half_w = (lmax_x - lmin_x) / 2.0
    total = 0.0
    for cl in centerlines:
        if not isinstance(cl, CenterlineResult):
            continue
        try:
            cl_bb = cl.bbox()
        except Exception:
            continue
        if cl_bb.max.X - cl_bb.min.X >= 0.1:
            continue  # not a vertical centerline
        cl_x = (cl_bb.min.X + cl_bb.max.X) / 2.0
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
    draft: "Draft",
    centerlines: list,
    gap: float = 1.0,
) -> list["DimResult"]:
    """Build dims from specs, auto-shifting labels to clear vertical centerlines.

    For each spec, builds the dim at label_offset_x=0, checks whether any
    vertical centerline crosses the label bbox, and if so shifts the label by
    the minimum amount left or right to clear it (choosing the shorter direction).
    Multiple crossing centerlines are handled by accumulating offsets in order.

    Only handles vertical centerlines (zero width). Horizontal centerlines and
    non-DimResult specs are passed through unchanged.

    Args:
        specs: list of (p1, p2, side, distance, label) or
               (p1, p2, side, distance, label, tolerance) tuples.
        draft: Draft config.
        centerlines: list of CenterlineResult objects to avoid.
        gap: clearance between label edge and centerline (mm, default 1.0).

    Returns:
        list of DimResult, same length as specs.
    """
    results = []
    for spec in specs:
        p1, p2, side, distance, label = spec[:5]
        tolerance = spec[5] if len(spec) > 5 else None
        dim = dim_linear(p1, p2, side, distance, draft, label=label,
                         tolerance=tolerance)
        offset_x = _compute_label_offset_x(dim, centerlines, gap)
        if offset_x != 0.0:
            dim = dim_linear(p1, p2, side, distance, draft, label=label,
                             tolerance=tolerance, label_offset_x=offset_x)
        results.append(dim)
    return results


# ---------------------------------------------------------------------------
# lint_drawing
# ---------------------------------------------------------------------------

def lint_drawing(
    items: list[DimResult | LeaderResult | CenterlineResult],
    part_bbox=None,
) -> list[LintIssue]:
    """Structural checks on a composed drawing annotation list.

    Args:
        items:     list of DimResult / LeaderResult / CenterlineResult returned by
                   this module's helpers.
        part_bbox: optional BoundBox of the projected part outline; if provided,
                   dims whose bbox overlaps the part outline by >10% are flagged.

    Returns:
        list of LintIssue, each with .severity ("error"|"warning") and .message.
    """
    issues: list[LintIssue] = []

    for item in items:
        if isinstance(item, DimResult):
            _lint_dim(item, part_bbox, issues)
        elif isinstance(item, LeaderResult):
            _lint_leader(item, issues)

    # Pairwise overlap check: any two annotations whose bboxes intersect by
    # more than 0.5 mm in both axes are likely visually colliding.
    # Use dim_level_y to skip stacked dims that share an X range but are
    # at different Y levels (their extension lines overlap in bbox but not visually).
    for i, item_a in enumerate(items):
        for item_b in items[i + 1:]:
            try:
                is_cl_a = isinstance(item_a, CenterlineResult)
                is_cl_b = isinstance(item_b, CenterlineResult)

                # Skip centerline-vs-centerline pairs
                if is_cl_a and is_cl_b:
                    continue

                # Centerline-vs-dim: use dim's label_bbox for precision
                if is_cl_a or is_cl_b:
                    dim_item = item_b if is_cl_a else item_a
                    cl_item = item_a if is_cl_a else item_b
                    _lint_centerline_dim_overlap(dim_item, cl_item, issues)
                    continue

                # Normal pairwise overlap (dim/leader vs dim/leader)
                level_a = getattr(item_a, "dim_level_y", None)
                level_b = getattr(item_b, "dim_level_y", None)
                if (level_a is not None and level_b is not None
                        and abs(level_a - level_b) > 3.0):
                    continue  # different Y levels → stacked, not colliding
                ba = item_a.bbox()
                bb = item_b.bbox()
                ox = max(0.0, min(ba.max.X, bb.max.X) - max(ba.min.X, bb.min.X))
                oy = max(0.0, min(ba.max.Y, bb.max.Y) - max(ba.min.Y, bb.min.Y))
                if ox > 0.5 and oy > 0.5:
                    la = getattr(item_a, "label_str", "?")
                    lb = getattr(item_b, "label_str", "?")
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


def _lint_centerline_dim_overlap(
    dim_item: DimResult | LeaderResult,
    cl_item: CenterlineResult,
    issues: list[LintIssue],
) -> None:
    """Flag label-vs-centerline overlap for a (dim, centerline) pair.

    A centerline is typically a zero-width (vertical) or zero-height (horizontal)
    edge, so we use point-in-range checks rather than standard bbox overlap.
    The label is considered to overlap the centerline if the centerline passes
    through the label's bounding box in both axes with > 0.5 mm penetration.
    """
    try:
        cl_bb = cl_item.bbox()

        # Use label_bbox when available (DimResult); fall back to full bbox
        label_bbox = getattr(dim_item, "label_bbox", None)
        if label_bbox is not None:
            lmin_x, lmin_y, lmax_x, lmax_y = label_bbox
        else:
            db = dim_item.bbox()
            lmin_x, lmin_y = db.min.X, db.min.Y
            lmax_x, lmax_y = db.max.X, db.max.Y

        # For a zero-width vertical centerline (max.X == min.X), the "overlap"
        # in X is whether the centerline's X coordinate falls inside the label
        # range.  We synthesise an overlap measure by comparing how far inside
        # the label the centerline sits.
        cl_w = cl_bb.max.X - cl_bb.min.X
        cl_h = cl_bb.max.Y - cl_bb.min.Y

        if cl_w < 0.1:
            # Vertical (or near-vertical) centerline — use X penetration depth
            cl_x = (cl_bb.min.X + cl_bb.max.X) / 2.0
            if lmin_x < cl_x < lmax_x:
                ox = min(cl_x - lmin_x, lmax_x - cl_x)
            else:
                ox = 0.0
        else:
            ox = max(0.0, min(lmax_x, cl_bb.max.X) - max(lmin_x, cl_bb.min.X))

        if cl_h < 0.1:
            # Horizontal (or near-horizontal) centerline — use Y penetration depth
            cl_y = (cl_bb.min.Y + cl_bb.max.Y) / 2.0
            if lmin_y < cl_y < lmax_y:
                oy = min(cl_y - lmin_y, lmax_y - cl_y)
            else:
                oy = 0.0
        else:
            oy = max(0.0, min(lmax_y, cl_bb.max.Y) - max(lmin_y, cl_bb.min.Y))

        if ox > 0.5 and oy > 0.5:
            dim_label = getattr(dim_item, "label_str", "?")
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


def _lint_dim(item: DimResult, part_bbox, issues: list[LintIssue]) -> None:
    label = item.label_str

    # Check: numeric label value vs measured length (>0.5% divergence = likely axis swap)
    nums = re.findall(r"\d+\.?\d*", label.split("±")[0].split("+")[0].lstrip("ø⌀Rr"))
    if nums:
        try:
            label_val = float(nums[0])
            if item.measured_length > 1e-6:
                ratio = abs(label_val - item.measured_length) / item.measured_length
                if ratio > 0.005:
                    issues.append(LintIssue(
                        severity="warning",
                        message=(
                            f"Dim '{label}': label value {label_val:.3f} differs from "
                            f"measured path length {item.measured_length:.3f} by "
                            f"{ratio*100:.1f}% — possible axis swap or wrong endpoint"
                        ),
                        code="label_vs_measured",
                    ))
        except ValueError:
            pass

    # Check: dim bbox overlaps part outline
    if part_bbox is not None:
        db = item.bbox()
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


def _lint_leader(item: LeaderResult, issues: list[LintIssue]) -> None:
    # Check: does the elbow point fall inside the text bbox?
    # If so, the leader line strikes through the label.
    try:
        tb = item.text.bounding_box()
        ex, ey = item.elbow
        if tb.min.X <= ex <= tb.max.X and tb.min.Y <= ey <= tb.max.Y:
            issues.append(LintIssue(
                severity="error",
                message=(
                    f"Leader '{item.label_str}': elbow point ({ex:.2f}, {ey:.2f}) "
                    f"is inside the label bbox — leader line passes through the text"
                ),
                location=item.elbow,
                code="leader_line_through_text",
            ))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# iso_title_block
# ---------------------------------------------------------------------------

# Column fractions: description, drawing_number, scale, material, date
_TB_COL_FRACTIONS = [0.40, 0.20, 0.15, 0.15, 0.10]


def iso_title_block(
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
) -> TitleBlockResult:
    """ISO-style 2-row title block built at the origin — standalone title box only.

    Differs from `build123d.TechnicalDrawing`:

    - `TechnicalDrawing` is a whole-page chrome (page-sized border + grid ticks +
      embedded title box) returning a single `Sketch`. Use it when you want the
      complete drawing-sheet frame.
    - `iso_title_block` is the title box alone — smaller, positionable anywhere,
      with separate `lines` and `text` `Compound`s so each can be routed to its
      own SVG layer (text needs `fill_color`, lines don't). Adds `material` and
      `general_tolerance` fields that `TechnicalDrawing` does not carry.

    The block is 170 × 16 mm by default (width × 2 rows) and is placed with
    its bottom-left corner at (0, 0).  Move it with `.lines.moved(loc)` and
    `.text.moved(loc)` after construction.

    Layout::

        ┌──────────────────┬─────────┬──────┬──────┬──────┐
        │  part_name       │ dwg_no  │scale │ mat  │ date │  ← top row
        ├──────────────────┼─────────┴──────┴──────┴──────┤
        │ general_tolerance│    designed_by               │  ← bottom row
        └──────────────────┴─────────────────────────────-┘

    Column proportions: 40 / 20 / 15 / 15 / 10 %.  The bottom row merges the
    right four columns into one "designed_by" cell.

    Args:
        part_name:         part description / title.
        drawing_number:    drawing identifier.
        scale:             e.g. "1:1", "2:1".
        material:          material specification.
        general_tolerance: tolerance note (e.g. "ISO 2768-m").
        designed_by:       drafter / designer name.
        date:              issue date string.
        cell_height:       height of each row in mm (default 8).
        width:             total block width in mm (default 170).
        draft:             Draft config for font settings; defaults to 2.5 mm.

    Returns:
        TitleBlockResult with .lines, .text, and .bbox dict.
    """
    if draft is None:
        draft = Draft(font_size=2.5, decimal_precision=1)

    # Column x-positions
    col_widths = [f * width for f in _TB_COL_FRACTIONS]
    x: list[float] = [0.0]
    for w in col_widths:
        x.append(x[-1] + w)

    y0, y1, y2 = 0.0, cell_height, 2.0 * cell_height

    # --- Grid edges ---
    edge_list: list[Edge] = []

    # Outer border
    edge_list.append(Edge.make_line(Vector(x[0], y0, 0), Vector(x[-1], y0, 0)))
    edge_list.append(Edge.make_line(Vector(x[-1], y0, 0), Vector(x[-1], y2, 0)))
    edge_list.append(Edge.make_line(Vector(x[-1], y2, 0), Vector(x[0], y2, 0)))
    edge_list.append(Edge.make_line(Vector(x[0], y2, 0), Vector(x[0], y0, 0)))

    # Horizontal divider between rows
    edge_list.append(Edge.make_line(Vector(x[0], y1, 0), Vector(x[-1], y1, 0)))

    # Top-row vertical dividers (between all 5 cells)
    for xi in x[1:-1]:
        edge_list.append(Edge.make_line(Vector(xi, y1, 0), Vector(xi, y2, 0)))

    # Bottom-row: single divider after the first column
    edge_list.append(Edge.make_line(Vector(x[1], y0, 0), Vector(x[1], y1, 0)))

    lines = Compound(children=edge_list)

    # --- Text ---
    fs = draft.font_size
    font = draft.font

    def _cell_txt(value: str, cx: float, cy: float) -> Text | None:
        if not value:
            return None
        return Text(
            txt=value,
            font_size=fs,
            font=font,
            align=(Align.CENTER, Align.CENTER),
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(cx, cy, 0.0)))

    # Top row cell centres
    top_y_mid = (y1 + y2) / 2.0
    top_cells = [
        (part_name,      (x[0] + x[1]) / 2.0),
        (drawing_number, (x[1] + x[2]) / 2.0),
        (scale,          (x[2] + x[3]) / 2.0),
        (material,       (x[3] + x[4]) / 2.0),
        (date,           (x[4] + x[5]) / 2.0),
    ]

    # Bottom row cell centres
    bot_y_mid = (y0 + y1) / 2.0
    bot_cells = [
        (general_tolerance, (x[0] + x[1]) / 2.0),
        (designed_by,       (x[1] + x[-1]) / 2.0),
    ]

    text_shapes = [
        _cell_txt(v, cx, top_y_mid) for v, cx in top_cells
    ] + [
        _cell_txt(v, cx, bot_y_mid) for v, cx in bot_cells
    ]
    text_shapes = [t for t in text_shapes if t is not None]

    text = Compound(children=text_shapes) if text_shapes else Compound(children=[])

    bbox_dict = {
        "min_x": 0.0, "min_y": 0.0,
        "max_x": width, "max_y": y2,
        "width": width, "height": y2,
    }
    return TitleBlockResult(lines=lines, text=text, bbox=bbox_dict)


# ---------------------------------------------------------------------------
# surface_finish_mark
# ---------------------------------------------------------------------------

def surface_finish_mark(
    ra_value: str,
    position: tuple,
    angle: float = 0.0,
    draft: Draft | None = None,
    size: float | None = None,
) -> SurfaceFinishResult:
    """ISO 1302 surface finish check-mark symbol with Ra annotation.

    The symbol has three strokes::

          _________
          |
          |          ← vertical right leg (1.5 × left-leg height)
         /
        /            ← left diagonal leg at 60° from horizontal
       tip

    The Ra label sits on the horizontal shelf extending from the elbow.

    Args:
        ra_value: surface finish annotation (e.g. "Ra 1.6", "Ra 3.2").
        position: world XY position of the symbol tip (x, y[, z]).
        angle:    rotation in degrees, CCW around the tip (default 0).
        draft:    Draft config; defaults to 2.5 mm font.
        size:     diagonal leg length in mm; defaults to 2 × font_size.

    Returns:
        SurfaceFinishResult with .lines (strokes + shelf) and .text (label).
        Route .lines to a line_color layer and .text to a fill_color layer.
    """
    if draft is None:
        draft = Draft(font_size=2.5, decimal_precision=1)

    leg_len = size if size is not None else 2.0 * draft.font_size

    # Diagonal left leg rises at 60° from horizontal
    leg_angle = math.radians(60.0)
    elbow_x = leg_len * math.cos(leg_angle)
    elbow_y = leg_len * math.sin(leg_angle)
    elbow_v = Vector(elbow_x, elbow_y, 0.0)

    # Vertical right leg: 1.5× the elbow height
    top_v = Vector(elbow_x, 1.5 * elbow_y, 0.0)

    leg1 = Edge.make_line(Vector(0.0, 0.0, 0.0), elbow_v)
    leg2 = Edge.make_line(elbow_v, top_v)

    # Horizontal shelf from elbow, sized to hold the label
    probe = Text(
        txt=ra_value,
        font_size=draft.font_size,
        font=draft.font,
        align=Align.CENTER,
        mode=Mode.PRIVATE,
    )
    text_w = probe.bounding_box().size.X
    gap = draft.pad_around_text
    shelf_end_v = Vector(elbow_x + gap + text_w + gap, elbow_y, 0.0)
    shelf = Edge.make_line(elbow_v, shelf_end_v)

    lines_raw = Compound(children=[leg1, leg2, shelf])

    # Text: left-aligned, resting just ABOVE the shelf line (ISO 1302 places
    # the Ra value over the extension line, not on it — otherwise the shelf
    # strikes through the digits).
    v_gap = 0.3 * draft.font_size
    text_raw = Compound(children=[
        Text(
            txt=ra_value,
            font_size=draft.font_size,
            font=draft.font,
            align=(Align.MIN, Align.MIN),
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(elbow_x + gap, elbow_y + v_gap, 0.0)))
    ])

    # Rotate around origin then translate to position
    if angle != 0.0:
        rot_loc = Location((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), angle)
        lines_raw = lines_raw.moved(rot_loc)
        text_raw = text_raw.moved(rot_loc)

    pos_v = Vector(position[0], position[1], 0.0)
    trans_loc = Location(pos_v)
    lines_out = lines_raw.moved(trans_loc)
    text_out = text_raw.moved(trans_loc)

    return SurfaceFinishResult(
        lines=lines_out,
        text=text_out,
        label_str=ra_value,
        position=(pos_v.X, pos_v.Y),
    )


# ---------------------------------------------------------------------------
# GD&T — feature control frame and datum feature symbol (ISO 1101 / ISO 5459)
# ---------------------------------------------------------------------------

GDTCharacteristic = Literal[
    "straightness", "flatness", "circularity", "cylindricity",
    "profile_line", "profile_surface",
    "angularity", "perpendicularity", "parallelism",
    "position", "concentricity", "symmetry",
    "circular_runout", "total_runout",
]

# Unicode glyphs for the 14 geometric characteristics — used in repr/labels and
# as the fallback when a symbol cannot be drawn. Drawing is done geometrically
# (below) because these glyphs are absent from most CAD-safe fonts.
_GDT_GLYPHS: dict[str, str] = {
    "straightness": "—", "flatness": "▱", "circularity": "○",
    "cylindricity": "⌭", "profile_line": "⌒", "profile_surface": "⌓",
    "angularity": "∠", "perpendicularity": "⊥", "parallelism": "∥",
    "position": "⌖", "concentricity": "◎", "symmetry": "⌯",
    "circular_runout": "↗", "total_runout": "⏍",
}

_MODIFIER_LETTER = {"M": "M", "L": "L", "P": "P"}  # circled material-condition modifiers


@dataclass
class FeatureControlFrameResult:
    """Returned by feature_control_frame().

    Route *lines* to a line_color SVG layer and *text* to a fill_color layer
    (the same split as leader()/surface_finish_mark()).
    """
    lines: Compound          # frame box, dividers, characteristic symbol, modifier rings
    text: Compound           # tolerance value, modifier letters, datum letters, ⌀ prefix
    characteristic: str
    tolerance_str: str
    datums: tuple[str, ...]
    width: float
    height: float

    @property
    def shape(self) -> Compound:
        return Compound(children=[self.lines, self.text])

    def bbox(self):
        return self.shape.bounding_box()


@dataclass
class DatumFeatureResult:
    """Returned by datum_feature() — the datum triangle + framed letter (ISO 5459)."""
    lines: Compound          # filled triangle + connector + letter box
    text: Compound           # datum letter glyph
    letter: str

    @property
    def shape(self) -> Compound:
        return Compound(children=[self.lines, self.text])

    def bbox(self):
        return self.shape.bounding_box()


def _arrowhead(tip: tuple[float, float], back: tuple[float, float], size: float) -> list[Edge]:
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
    """Geometric-characteristic glyph as Edges, centred on the local origin,
    spanning roughly a square of side ~h. Drawn this way because the Unicode
    GD&T glyphs are missing from typical engineering fonts.
    """
    s = 0.42 * h  # half-extent of the glyph box

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


def feature_control_frame(
    characteristic: GDTCharacteristic,
    tolerance: float | str,
    datums: tuple[str, ...] | list[str] = (),
    draft: Draft | None = None,
    diameter: bool = False,
    modifier: str | None = None,
    datum_modifiers: dict[str, str] | None = None,
) -> FeatureControlFrameResult:
    """ISO 1101 feature control frame, e.g. ``| ⌖ | ⌀0.5 Ⓜ | A | B | C |``.

    Built at the origin with its bottom-left corner at (0, 0). Move it with
    ``.lines.moved(loc)`` and ``.text.moved(loc)`` after construction, or via
    ``.shape``.

    Args:
        characteristic: one of the 14 geometric characteristics — "position",
            "flatness", "perpendicularity", "parallelism", "concentricity",
            "circularity", "cylindricity", "straightness", "angularity",
            "symmetry", "profile_line", "profile_surface", "circular_runout",
            "total_runout".
        tolerance: tolerance value (float formatted to draft precision, or a
            ready-made string like "0.05").
        datums: ordered datum references, e.g. ("A", "B", "C"). Each becomes a
            compartment after the tolerance.
        draft: Draft config for font size / line width. Defaults to 2.5 mm.
        diameter: prepend a ⌀ symbol to the tolerance (cylindrical zone).
        modifier: material-condition modifier on the tolerance — "M" (MMC),
            "L" (LMC), or "P" (projected). None = RFS (no modifier).
        datum_modifiers: optional {datum_letter: "M"|"L"} modifiers drawn after
            the datum letter in its compartment.

    Returns:
        FeatureControlFrameResult with .lines (frame + symbols) and .text
        (values + letters). Route .lines to a line_color layer and .text to a
        fill_color layer.
    """
    if draft is None:
        draft = Draft(font_size=2.5, decimal_precision=1)
    name = characteristic.lower()
    if name not in _GDT_GLYPHS:
        raise ValueError(
            f"Unknown characteristic '{characteristic}'. "
            f"Supported: {', '.join(_GDT_GLYPHS)}"
        )

    h = draft.font_size
    H = 2.0 * h                       # ISO: frame height = 2 × char height
    pad = 0.6 * h
    datum_modifiers = datum_modifiers or {}

    # tolerance string
    if isinstance(tolerance, str):
        tol_str = tolerance
    else:
        prec = draft.decimal_precision
        tol_str = f"{round(tolerance, prec):.{prec}f}"

    def _text(txt: str, fs: float) -> Text:
        return Text(txt=txt, font_size=fs, font=draft.font,
                    align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE)

    # --- compartment widths ---
    w_sym = H                          # leading characteristic symbol: square
    # tolerance compartment: ⌀? + value + modifier-ring?
    r_pre = 0.42 * h                   # ⌀ prefix glyph radius
    mr = 0.62 * h                      # modifier ring radius
    val_w = _text(tol_str, h).bounding_box().size.X
    w_tol = pad + val_w + pad
    if diameter:
        w_tol += 2 * r_pre + pad
    if modifier:
        w_tol += 2 * mr + pad
    w_dat = H                          # each datum compartment: square

    widths = [w_sym, w_tol] + [w_dat] * len(datums)
    xs = [0.0]
    for w in widths:
        xs.append(xs[-1] + w)
    total_w = xs[-1]
    cy = H / 2.0

    # --- frame: outer box + interior verticals ---
    line_edges: list[Edge] = [
        Edge.make_line(Vector(0, 0, 0), Vector(total_w, 0, 0)),
        Edge.make_line(Vector(total_w, 0, 0), Vector(total_w, H, 0)),
        Edge.make_line(Vector(total_w, H, 0), Vector(0, H, 0)),
        Edge.make_line(Vector(0, H, 0), Vector(0, 0, 0)),
    ]
    for x in xs[1:-1]:
        line_edges.append(Edge.make_line(Vector(x, 0, 0), Vector(x, H, 0)))

    text_faces = []

    # --- characteristic symbol in compartment 0 ---
    sym_loc = Location(Vector(xs[0] + w_sym / 2, cy, 0))
    for e in _characteristic_edges(name, h):
        line_edges.append(e.moved(sym_loc))

    # --- tolerance compartment contents, laid out left → right ---
    x_cursor = xs[1] + pad
    if diameter:
        dia_cx = x_cursor + r_pre
        line_edges += [e.moved(Location(Vector(dia_cx, cy, 0)))
                       for e in Edge.make_circle(r_pre).edges()]
        line_edges.append(Edge.make_line(
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
        line_edges += [e.moved(Location(Vector(mod_cx, cy, 0)))
                       for e in Edge.make_circle(mr).edges()]
        text_faces.append(_text(_MODIFIER_LETTER[m], h * 0.8)
                          .moved(Location(Vector(mod_cx, cy, 0))))

    # --- datum compartments ---
    for i, letter in enumerate(datums):
        cx = (xs[2 + i] + xs[3 + i]) / 2
        dm = datum_modifiers.get(letter)
        if dm:
            # letter + small circled modifier side by side
            text_faces.append(_text(letter, h).moved(Location(Vector(cx - 0.35 * h, cy, 0))))
            mod_cx = cx + 0.5 * h
            line_edges += [e.moved(Location(Vector(mod_cx, cy, 0)))
                           for e in Edge.make_circle(0.55 * h).edges()]
            text_faces.append(_text(dm.upper(), h * 0.7).moved(Location(Vector(mod_cx, cy, 0))))
        else:
            text_faces.append(_text(letter, h).moved(Location(Vector(cx, cy, 0))))

    return FeatureControlFrameResult(
        lines=Compound(children=line_edges),
        text=Compound(children=text_faces),
        characteristic=name,
        tolerance_str=tol_str,
        datums=tuple(datums),
        width=total_w,
        height=H,
    )


def datum_feature(
    letter: str,
    draft: Draft | None = None,
    filled: bool = True,
) -> DatumFeatureResult:
    """ISO 5459 datum feature symbol: a (filled) triangle on a short leader to
    a framed datum letter. Built with the triangle tip at the origin pointing
    down (-Y); move it onto the feature with ``.shape.moved(loc)``.

    Args:
        letter: the datum identifier, e.g. "A".
        draft: Draft config for font size / line width. Defaults to 2.5 mm.
        filled: draw the triangle as a filled face (True) or outline (False).
            ISO datum triangles are solid-filled; outline is offered for media
            where fill is awkward.

    Returns:
        DatumFeatureResult with .lines (triangle + connector + letter box) and
        .text (the letter). Route .lines to a line_color layer (the filled
        triangle also needs fill_color) and .text to a fill_color layer.
    """
    if draft is None:
        draft = Draft(font_size=2.5, decimal_precision=1)
    h = draft.font_size
    tri = 1.4 * h                      # triangle base width ≈ 1.4 × char height
    box = 2.0 * h                      # framed-letter box side

    lines: list = []

    # Triangle: tip at origin (0,0), base above it
    apex = Vector(0, 0, 0)
    bl = Vector(-tri / 2, tri * 0.9, 0)
    br = Vector(tri / 2, tri * 0.9, 0)
    if filled:
        from build123d import Face, Wire
        lines.append(Face(Wire.make_polygon([apex, bl, br, apex])))
    else:
        lines += [Edge.make_line(apex, bl), Edge.make_line(bl, br), Edge.make_line(br, apex)]

    # Connector from triangle base up to the letter box
    conn_y0 = tri * 0.9
    conn_y1 = conn_y0 + 0.8 * h
    lines.append(Edge.make_line(Vector(0, conn_y0, 0), Vector(0, conn_y1, 0)))

    # Letter box centred on x=0, sitting on the connector
    by0 = conn_y1
    by1 = conn_y1 + box
    lines += [
        Edge.make_line(Vector(-box / 2, by0, 0), Vector(box / 2, by0, 0)),
        Edge.make_line(Vector(box / 2, by0, 0), Vector(box / 2, by1, 0)),
        Edge.make_line(Vector(box / 2, by1, 0), Vector(-box / 2, by1, 0)),
        Edge.make_line(Vector(-box / 2, by1, 0), Vector(-box / 2, by0, 0)),
    ]

    glyph = Text(txt=letter, font_size=h, font=draft.font,
                 align=(Align.CENTER, Align.CENTER), mode=Mode.PRIVATE
                 ).moved(Location(Vector(0, (by0 + by1) / 2, 0)))

    return DatumFeatureResult(
        lines=Compound(children=lines),
        text=Compound(children=[glyph]),
        letter=letter,
    )


# ---------------------------------------------------------------------------
# SVG export helper
# ---------------------------------------------------------------------------

def add_to_layers(exporter, result, *, line_layer="lines", text_layer="text"):
    """Route a drafting-helper result onto two ExportSVG layers correctly.

    GD&T and surface-finish results split their geometry into ``.lines`` (frame
    box, dividers, characteristic symbol, modifier rings — must be *stroked*)
    and ``.text`` (tolerance value, datum letters, ⌀ prefix — must be
    *filled*). Adding the combined ``.shape`` to a single ``fill_color`` layer
    floods every closed loop solid — the ⌀ prefix and the modifier ring turn
    into black discs. This helper enforces the correct split so callers don't
    have to remember it::

        exp.add_layer("lines", line_color=Color(0, 0, 0))                       # stroke only
        exp.add_layer("text", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0))
        add_to_layers(exp, fcf, line_layer="lines", text_layer="text")

    Results without a ``.lines``/``.text`` split (e.g. ``DimResult``) carry no
    flood-prone loops; add their ``.shape`` to a single stroked-and-filled
    layer directly instead of using this helper.

    Raises:
        TypeError: if *result* has no ``.lines``/``.text`` split.
    """
    lines = getattr(result, "lines", None)
    text = getattr(result, "text", None)
    if lines is None or text is None:
        raise TypeError(
            f"{type(result).__name__} has no .lines/.text split; add its "
            ".shape to a single stroked-and-filled layer directly."
        )
    exporter.add_shape(lines, layer=line_layer)
    exporter.add_shape(text, layer=text_layer)


# ---------------------------------------------------------------------------
# Interference detection
# ---------------------------------------------------------------------------

_MIN_STRUCT_LEN = 3.5  # mm — ignore glyph strokes / arrowhead edges below this


def _annotation_geom(item):
    """Decompose an annotation result into (label_box, [segments], name).

    label_box is (min_x, min_y, max_x, max_y) for the text, or None when the
    item carries no label. segments are the *structural* straight lines
    (witness lines, dim lines, leader shafts) — glyph strokes and short
    arrowhead edges are filtered out, as are edges sitting inside the item's
    own label box.
    """
    label_box = getattr(item, "label_bbox", None)
    if label_box is None:
        text = getattr(item, "text", None)
        if text is not None and text.faces():
            tb = text.bounding_box()
            label_box = (tb.min.X, tb.min.Y, tb.max.X, tb.max.Y)

    src = getattr(item, "lines", None)
    if src is None:
        src = getattr(item, "shape", None)

    segs = []
    if src is not None:
        for e in src.edges():
            try:
                if e.geom_type != GeomType.LINE or e.length <= _MIN_STRUCT_LEN:
                    continue
                a, b = e.position_at(0), e.position_at(1)
                if label_box is not None:
                    mx, my = (a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0
                    bx0, by0, bx1, by1 = label_box
                    if bx0 - 0.3 <= mx <= bx1 + 0.3 and by0 - 0.3 <= my <= by1 + 0.3:
                        continue  # this item's own glyph/label-region edge
                segs.append(((a.X, a.Y), (b.X, b.Y)))
            except Exception:
                continue

    name = getattr(item, "label_str", None) or "?"
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


def _collinear_overlap(seg_a, seg_b, tol=0.15):
    """Length (mm) over which two segments lie on the same line and overlap.

    Returns 0 when the segments are not collinear or merely touch at a point.
    """
    (ax0, ay0), (ax1, ay1) = seg_a
    (bx0, by0), (bx1, by1) = seg_b
    dax, day = ax1 - ax0, ay1 - ay0
    la = math.hypot(dax, day)
    if la < 1e-9:
        return 0.0
    ux, uy = dax / la, day / la
    # both endpoints of b must lie on the infinite line through a
    if (abs((bx0 - ax0) * uy - (by0 - ay0) * ux) > tol
            or abs((bx1 - ax0) * uy - (by1 - ay0) * ux) > tol):
        return 0.0
    pb0 = (bx0 - ax0) * ux + (by0 - ay0) * uy
    pb1 = (bx1 - ax0) * ux + (by1 - ay0) * uy
    lo = max(0.0, min(pb0, pb1))
    hi = min(la, max(pb0, pb1))
    return max(0.0, hi - lo)


def find_interferences(items, *, part_bbox=None, page_bbox=None,
                       min_overlap=0.5, pad=0.2, min_run=1.5):
    """Geometry-precise interference detection between drafting annotations.

    Where ``lint_drawing`` compares whole-annotation bounding boxes (and so
    needs level-aware skips and misses sub-component clashes), this decomposes
    each annotation into its **label box** and its **structural line segments**
    (witness lines, dim lines, leader shafts) and tests the actual geometry:

    - **line↔label** — a structural line of one annotation passes through
      another annotation's text (e.g. a stacked dim's extension line spearing a
      neighbouring dim's value). This is the class ``lint_drawing`` misses.
    - **label↔label** — two labels physically overlap.
    - **label↔frame** (when ``page_bbox`` is given) — a label extends outside
      the drawing frame.
    - **label↔part** (when ``part_bbox`` is given) — a label sits on top of the
      part outline.
    - **line↔line (redundant)** — structural lines from two annotations that lie
      on the same line and overlap, e.g. two stacked dims that share an endpoint
      each drawing the shared witness line (the duplicate could be avoided by
      choosing dims that do not share a corner, or sharing one witness line).

    Generic line↔line *crossings* are still not reported — witness lines
    legitimately cross dim lines and centerlines; only collinear overlap is
    flagged. An annotation's own (gapped) label is never flagged against itself.

    Args:
        items: result objects from this module (``DimResult``, ``LeaderResult``,
            ``CenterlineResult``, ``FeatureControlFrameResult`` …). Anything
            exposing ``.label_bbox`` or ``.text``, plus ``.lines`` or ``.shape``,
            is handled.
        part_bbox, page_bbox: optional ``BoundBox`` for the part outline / page
            frame checks.
        min_overlap: mm of bbox overlap (both axes) before two labels collide.
        pad: mm tolerance when testing a line against a label box.
        min_run: mm of collinear overlap before two lines are flagged as
            redundant (shorter overlaps are treated as lines merely touching).

    Returns:
        list[LintIssue]. Real collisions (label↔label, line↔label, label↔frame,
        label↔part) are severity ``"error"``. Redundant collinear overlaps are
        severity ``"warning"`` — chain/baseline dimensioning legitimately shares
        witness lines, so these are advisory and an author/LLM can choose to
        reduce them without it being a hard failure.
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
