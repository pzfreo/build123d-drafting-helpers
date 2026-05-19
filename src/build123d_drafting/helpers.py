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

    Returns:
        DimResult with .shape (the Compound), .label_str, and .measured_length.
    """
    toward = _SIDE_VECTORS[side] if isinstance(side, str) else tuple(side)
    sign = _offset_sign(p1, p2, toward)
    offset = sign * abs(distance)

    shape = ExtensionLine(
        border=[p1, p2],
        offset=offset,
        draft=draft,
        label=label,
        tolerance=tolerance,
        mode=Mode.PRIVATE,
    )
    measured = shape.dimension  # set by ExtensionLine: length of the border path
    label_str = label if label is not None else _format_label(measured, draft, tolerance)
    return DimResult(shape=shape, label_str=label_str, measured_length=measured)


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

    # Shelf goes in the direction away from tip (left or right)
    shelf_dir = 1.0 if elbow_v.X >= tip_v.X else -1.0
    shelf_len = gap + text_w + gap
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
# view_axes
# ---------------------------------------------------------------------------

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
    vo = Vector(*viewport_origin)
    la = Vector(*look_at)
    vu = Vector(*viewport_up)

    view_dir = (la - vo).normalized()
    # Gram-Schmidt: remove the view_dir component from viewport_up
    page_y = (vu - vu.dot(view_dir) * view_dir).normalized()
    page_x = view_dir.cross(page_y)

    result: dict[str, tuple[str, float]] = {}
    for name, world_v in [
        ("world_X", Vector(1, 0, 0)),
        ("world_Y", Vector(0, 1, 0)),
        ("world_Z", Vector(0, 0, 1)),
    ]:
        px = world_v.dot(page_x)
        py = world_v.dot(page_y)
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
    items: list[DimResult | LeaderResult],
    part_bbox=None,
) -> list[LintIssue]:
    """Structural checks on a composed drawing annotation list.

    Args:
        items:     list of DimResult / LeaderResult returned by this module's helpers.
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

    return issues


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
