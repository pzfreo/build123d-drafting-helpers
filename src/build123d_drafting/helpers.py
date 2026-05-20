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

    shape = ExtensionLine(
        border=[p1, p2],
        offset=offset,
        draft=draft,
        label=measured_label,
        tolerance=tolerance,
        mode=Mode.PRIVATE,
    )
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

    if label_offset_x != 0.0:
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

    # Text: left-aligned, vertically centred at shelf height
    text_raw = Compound(children=[
        Text(
            txt=ra_value,
            font_size=draft.font_size,
            font=draft.font,
            align=(Align.MIN, Align.CENTER),
            mode=Mode.PRIVATE,
        ).moved(Location(Vector(elbow_x + gap, elbow_y, 0.0)))
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
