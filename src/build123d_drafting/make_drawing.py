"""make_drawing — Zero-AI STEP-to-technical-drawing pipeline.

Produces a 4-view third-angle technical drawing (front, plan, side, isometric)
with automatic dimension selection from face-geometry analysis.

Typical usage::

    from build123d_drafting.make_drawing import make_drawing
    svg_path, dxf_path = make_drawing("part.step", title="BRACKET", number="DWG-042")

CLI (registered as ``make-drawing``)::

    make-drawing part.step
    make-drawing part.step --title "BRACKET" --number DWG-042
    make-drawing part.step --script   # write editable .py instead
    make-drawing part.step --out /tmp/output
"""

import argparse
import logging
import math
import re
from pathlib import Path
from types import SimpleNamespace

from build123d import (
    Color,
    Compound,
    ExportDXF,
    ExportSVG,
    LineType,
    Location,
    Shape,
    import_step,
)
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
from OCP.TopAbs import TopAbs_Orientation

from build123d_drafting.helpers import (
    Centerline,
    Dimension,
    Leader,
    LintIssue,
    Note,
    TitleBlock,
    ViewCoordinates,
    annotate,
    draft_preset,
    format_drawing_scale,
    lint_drawing,
    place_dims,
    set_page,
    view_axes,
)

_log = logging.getLogger(__name__)

_TB_W = 150.0
_MARGIN = 10.0
_DIM_PAD = 18.0
_TB_H = 35.0

_PAGE_SIZES = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}


# ---------------------------------------------------------------------------
# SVG post-processing
# ---------------------------------------------------------------------------


def fix_svg_page_size(svg_path: str, page_w: float, page_h: float) -> None:
    """Rewrite the SVG width/height/viewBox to match the full ISO page size.

    ExportSVG crops to content bounding box; this expands it to the declared
    page so the rendering fills the correct A-series sheet.
    """
    data = Path(svg_path).read_text(encoding="utf-8")
    data = re.sub(r'width="[^"]*"', f'width="{page_w:.3f}mm"', data, count=1)
    data = re.sub(r'height="[^"]*"', f'height="{page_h:.3f}mm"', data, count=1)
    data = re.sub(
        r'viewBox="[^"]*"',
        f'viewBox="0 -{page_h:.3f} {page_w:.3f} {page_h:.3f}"',
        data,
        count=1,
    )
    Path(svg_path).write_text(data, encoding="utf-8")


# ---------------------------------------------------------------------------
# Geometry analysis
# ---------------------------------------------------------------------------


def analyse_cylinders(part):
    """Return (z_cyls, cross_cyls) from OCP cylindrical face analysis.

    Each entry is a dict with keys: diameter, area, cx, cy, cz, axis,
    u_extent (the face's angular span in radians — partial spans are fillets),
    axis_xyz (a point on the cylinder axis), and external (True when the face
    is outward-facing — a boss/OD; False for a bore).
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
        rec = dict(
            diameter=round(r * 2, 2),
            area=face.area,
            cx=fc.X,
            cy=fc.Y,
            cz=fc.Z,
            axis=ax,
            u_extent=surf.LastUParameter() - surf.FirstUParameter(),
            axis_xyz=(ap.X(), ap.Y(), ap.Z()),
            # Outward material (boss/OD) vs bore: a right-handed cylinder's
            # natural normal points away from the axis, so FORWARD means
            # external — but mirroring makes the frame left-handed and flips
            # both, so compare against the frame handedness
            external=(face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_FORWARD)
            == cyl.Position().Direct(),
        )
        (z_cyls if ax == "z" else cross_cyls).append(rec)
    return z_cyls, cross_cyls


def dedup_diams(cyls, tol: float = 0.15) -> list:
    """Return sorted-descending deduplicated diameter list from cylinder records."""
    raw = sorted({c["diameter"] for c in cyls}, reverse=True)
    merged: list[float] = []
    for d in raw:
        if not merged or abs(d - merged[-1]) > tol:
            merged.append(round(d, 2))
    return merged


def _fmt(v: float) -> str:
    """Format a float as integer string if whole, otherwise 1 dp."""
    return str(int(v)) if v == int(v) else f"{v:.1f}"


_DIAM_RE = re.compile(r"[øØ⌀]\s*(\d+(?:\.\d+)?)")

# Cylinder patches around one axis spanning less than ~half a turn in total
# are edge blends (fillets/rounds), not holes or bosses — exclude them from
# the feature inventory.
_FULL_CYL_MIN_EXTENT = math.pi * 0.9

# Turned-part classification (#81): a rotational part's bounding box is
# square in XY to within _SQUARENESS_TOL, and its OD — the largest full
# *external* Z cylinder — fills at least _OD_FILL_MIN of that envelope, with
# its axis within _OD_AXIS_TOL of the envelope centre. Anything else is
# prismatic — its Z cylinders are holes or local bosses, not an OD.
_SQUARENESS_TOL = 0.05
_OD_FILL_MIN = 0.8
_OD_AXIS_TOL = 0.05


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


def _is_rotational(x_size, y_size, od_diam, od_axis_offset) -> bool:
    """True for turned parts: an outward-facing Z cylinder, concentric with
    the bounding box, filling a square envelope.

    ``od_diam`` is the largest full *external* Z-cylinder diameter (``None``
    when there is none — bores never qualify as an OD) and
    ``od_axis_offset`` that cylinder's axis distance from the bbox centre.
    """
    if od_diam is None:
        return False
    envelope = max(x_size, y_size)
    return (
        abs(x_size - y_size) <= _SQUARENESS_TOL * envelope
        and od_diam >= _OD_FILL_MIN * envelope
        and od_axis_offset <= _OD_AXIS_TOL * envelope
    )


def lint_feature_coverage(part, annotations, tol: float = 0.15, cyls=None) -> list:
    """Coarse completeness check: report part diameters with no callout (#80).

    Builds a feature inventory from *part*'s hole/boss diameters (cylinder
    patches spanning at least ~half a turn around their axis in total, so
    fillets are ignored) and diffs it against every ø value mentioned in the
    annotations' labels, plus the structured ``covers_diameters`` metadata on
    annotations that draw their values geometrically (e.g. ``HoleCallout``).
    Radius callouts are *not* counted — "R5 TYP" fillet notes would otherwise
    mask an undimensioned ø10 bore. Title blocks are skipped — part numbers
    like "BRACKET R8" are not callouts. Each uncovered diameter yields one
    ``feature_not_dimensioned`` warning.

    ``cyls`` accepts a precomputed ``analyse_cylinders(part)`` result so
    repeated lint runs need not re-scan the solid.

    This checks *size* coverage only; location coverage needs feature
    recognition and is out of scope.
    """
    z_cyls, cross_cyls = cyls if cyls is not None else analyse_cylinders(part)
    inventory = dedup_diams(_full_cyls(z_cyls + cross_cyls), tol=tol)

    mentioned: set[float] = set()
    for ann in annotations:
        if isinstance(ann, TitleBlock):
            continue
        label = getattr(ann, "label", None) or ""
        for m in _DIAM_RE.finditer(label):
            mentioned.add(float(m.group(1)))
        for v in getattr(ann, "covers_diameters", ()):
            mentioned.add(float(v))

    return [
        LintIssue(
            severity="warning",
            code="feature_not_dimensioned",
            message=f"cylindrical feature ø{_fmt(d)} has no diameter callout on the sheet",
        )
        for d in inventory
        if not any(abs(d - v) <= tol for v in mentioned)
    ]


def analyse_face_levels(part, tol: float = 0.5) -> list:
    """Return sorted unique Z-coords of horizontal (normal≈±Z) planar faces.

    Uses tol-bucket deduplication but returns the actual face Z, not the rounded
    bucket centre, so dimension labels match the true geometry.
    """
    buckets = {}
    for face in part.faces():
        surf = BRepAdaptor_Surface(face.wrapped)
        if surf.GetType() == GeomAbs_Plane:
            ax = surf.Plane().Axis().Direction()
            if abs(ax.Z()) > 0.99:
                z = surf.Plane().Location().Z()
                key = round(z / tol) * tol
                if key not in buckets:
                    buckets[key] = z
    return sorted(buckets.values())


_LADDER = [
    (10.0, 297.0, 210.0, 120.0),  # A4 10:1
    (5.0, 297.0, 210.0, 120.0),  # A4 5:1
    (5.0, 420.0, 297.0, 150.0),  # A3 5:1
    (2.0, 297.0, 210.0, 120.0),  # A4 2:1
    (1.0, 297.0, 210.0, 120.0),  # A4 1:1
    (2.0, 420.0, 297.0, 150.0),  # A3 2:1
    (1.0, 420.0, 297.0, 150.0),  # A3 1:1
    (2.0, 594.0, 420.0, 150.0),  # A2 2:1
    (1.0, 594.0, 420.0, 150.0),  # A2 1:1
    (0.5, 594.0, 420.0, 150.0),  # A2 1:2
    (1.0, 841.0, 594.0, 150.0),  # A1 1:1
    (0.5, 841.0, 594.0, 150.0),  # A1 1:2
    (0.2, 841.0, 594.0, 150.0),  # A1 1:5
    (0.5, 1189.0, 841.0, 150.0),  # A0 1:2
    (0.2, 1189.0, 841.0, 150.0),  # A0 1:5
]

_SCALES = [10.0, 5.0, 2.0, 1.0, 0.5, 0.2]


def _tb_width(page_w: float) -> float:
    """Title-block width for a page: 120 mm on A4, 150 mm on A3 and larger."""
    return 120.0 if page_w <= 297.0 else 150.0


def _parse_page(page) -> tuple:
    """Resolve a page spec to ``(PAGE_W, PAGE_H, TB_W)``.

    Accepts an ISO name (``"A4"``…``"A0"``, case-insensitive), a
    ``"WIDTHxHEIGHT"`` string in mm (e.g. ``"420x297"``), or a
    ``(width, height)`` tuple in mm.
    """
    if isinstance(page, str):
        name = page.strip().upper()
        if name in _PAGE_SIZES:
            pw, ph = _PAGE_SIZES[name]
        else:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)", page.strip())
            if not m:
                raise ValueError(
                    f"unknown page size {page!r} — expected one of "
                    f"{', '.join(_PAGE_SIZES)} or WIDTHxHEIGHT in mm (e.g. '420x297')"
                )
            pw, ph = float(m.group(1)), float(m.group(2))
    else:
        try:
            pw, ph = float(page[0]), float(page[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError(
                f"invalid page size {page!r} — expected an ISO name, "
                f"'WIDTHxHEIGHT', or a (width, height) tuple in mm"
            ) from None
    if pw <= 0 or ph <= 0:
        raise ValueError(f"page dimensions must be positive, got {page!r}")
    return pw, ph, _tb_width(pw)


def _fits(x_size, y_size, z_size, scale, page_w, page_h, tb_w) -> bool:
    """True if the 4-view layout fits the page at this scale.

    The title block occupies only the bottom ``_TB_H`` mm of the sheet, so when
    the vertically-centred view rows clear its top edge, the row width does not
    need to reserve title-block space (the iso view may extend over it).
    """
    bbox_max = max(x_size, y_size, z_size)
    w = (
        _MARGIN
        + _DIM_PAD
        + x_size * scale
        + _DIM_PAD
        + y_size * scale
        + _DIM_PAD
        + bbox_max * scale * 0.7
        + _DIM_PAD
        + tb_w
        + _MARGIN
    )
    h = _MARGIN + _DIM_PAD + y_size * scale + _DIM_PAD + z_size * scale + _DIM_PAD + _MARGIN
    if h > page_h:
        return False
    if w <= page_w:
        return True
    views_bottom = max(0.0, (page_h - h) / 2) + _MARGIN + _DIM_PAD
    return w - _DIM_PAD - tb_w <= page_w and views_bottom >= _MARGIN + _TB_H


def choose_scale(x_size: float, y_size: float, z_size: float, scale=None, page=None) -> tuple:
    """Return (SCALE, PAGE_W, PAGE_H, TB_W) for a 4-view layout.

    Layout columns: [front(x×z)] [side(y×z)] [iso(~0.7*max)] [title block].
    Rows: [plan(x×y)] above [front/side].
    Tries ISO A-series pages (A4→A3→A2→A1→A0) at preferred scales, including
    ISO 5455 enlargement scales (10:1, 5:1) so small parts get legible views.
    A4 uses a 120 mm title block; A3+ use 150 mm. The title block only
    constrains row width when the view rows would overlap it vertically.

    Args:
        scale: optional fixed scale factor (e.g. ``5`` for 5:1, ``0.5`` for
            1:2); the page is then chosen as the smallest A-series sheet that
            fits.
        page: optional fixed page — an ISO name (``"A3"``), ``"WIDTHxHEIGHT"``
            in mm, or a ``(width, height)`` tuple; the scale is then chosen as
            the largest standard scale that fits. When both ``scale`` and
            ``page`` are given they are used as-is (a warning is logged if the
            layout does not fit).
    """
    if scale is not None and float(scale) <= 0:
        raise ValueError(f"scale must be positive, got {scale!r}")
    if scale is not None and page is not None:
        pw, ph, tb = _parse_page(page)
        if not _fits(x_size, y_size, z_size, float(scale), pw, ph, tb):
            _log.warning("Requested scale %s on %s page may not fit the 4-view layout", scale, page)
        return float(scale), pw, ph, tb
    if page is not None:
        pw, ph, tb = _parse_page(page)
        candidates = [(s, pw, ph, tb) for s in _SCALES]
    elif scale is not None:
        candidates = [(float(scale), pw, ph, _tb_width(pw)) for pw, ph in _PAGE_SIZES.values()]
    else:
        candidates = _LADDER
    for cand in candidates:
        if _fits(x_size, y_size, z_size, *cand):
            return cand
    _log.warning(
        "No layout fits %.0f × %.0f × %.0f mm; falling back to %s",
        x_size,
        y_size,
        z_size,
        candidates[-1],
    )
    return candidates[-1]


# ---------------------------------------------------------------------------
# Shared analysis step
# ---------------------------------------------------------------------------


def _analyse(step_file, title, number, tolerance, drawn_by, out, scale=None, page=None):
    """Load STEP or use a build123d Shape, analyse geometry, compute layout.

    Returns SimpleNamespace.
    """
    if isinstance(step_file, Shape):
        part = step_file
        src = "build123d object"
    else:
        part = import_step(step_file)
        src = str(step_file)
    bb = part.bounding_box()
    x_size = bb.max.X - bb.min.X
    y_size = bb.max.Y - bb.min.Y
    z_size = bb.max.Z - bb.min.Z
    cx = (bb.min.X + bb.max.X) / 2
    cy = (bb.min.Y + bb.max.Y) / 2
    cz = (bb.min.Z + bb.max.Z) / 2
    bbox_max = max(x_size, y_size, z_size)

    _log.info("Loaded %s  bbox: %.2f × %.2f × %.2f mm", src, x_size, y_size, z_size)

    z_cyls, cross_cyls = analyse_cylinders(part)
    # Partial (fillet) faces are not features: they would pollute the OD,
    # the bore leaders, and the rotational classification alike (#81)
    full_z = _full_cyls(z_cyls)
    z_diams = dedup_diams(full_z)
    cross_diams = dedup_diams(_full_cyls(cross_cyls))

    _log.info("Z-axis diameters: %s", z_diams)
    if cross_diams:
        _log.info("Cross-hole diams: %s", cross_diams)

    od_cyl = max((c for c in full_z if c["external"]), key=lambda c: c["diameter"], default=None)
    od_diam = od_cyl["diameter"] if od_cyl else None
    od_axis_offset = (
        math.hypot(od_cyl["axis_xyz"][0] - cx, od_cyl["axis_xyz"][1] - cy) if od_cyl else 0.0
    )
    is_rotational = _is_rotational(x_size, y_size, od_diam, od_axis_offset)
    if z_diams and not is_rotational:
        _log.info("Part classified prismatic; skipping OD/centreline/bore annotations")

    SCALE, PAGE_W, PAGE_H, TB_W = choose_scale(x_size, y_size, z_size, scale=scale, page=page)
    DIM_PAD = _DIM_PAD
    margin = _MARGIN

    fv_hw = x_size * SCALE / 2
    fv_hh = z_size * SCALE / 2
    pv_hh = y_size * SCALE / 2
    sv_hw = y_size * SCALE / 2

    total_h = 2 * margin + 3 * DIM_PAD + z_size * SCALE + y_size * SCALE
    y_offset = max(0.0, (PAGE_H - total_h) / 2)

    total_content_w = 4 * DIM_PAD + x_size * SCALE + y_size * SCALE + bbox_max * SCALE * 0.7
    x_offset = max(0.0, (PAGE_W - 2 * margin - TB_W - total_content_w) / 2)

    FV_X = margin + x_offset + DIM_PAD + fv_hw
    FV_Y = y_offset + margin + DIM_PAD + fv_hh
    PV_X = FV_X
    PV_Y = FV_Y + fv_hh + DIM_PAD + pv_hh
    SV_X = FV_X + fv_hw + DIM_PAD + sv_hw
    SV_Y = FV_Y
    sv_right = SV_X + sv_hw + DIM_PAD
    tb_top_y = margin + _TB_H
    iso_above_tb = (PV_Y - pv_hh) > tb_top_y
    iso_right_limit = (PAGE_W - margin) if iso_above_tb else (PAGE_W - TB_W - margin)
    right_avail = max(0.0, iso_right_limit - sv_right)
    ISO_X = sv_right + right_avail / 2
    ISO_Y = PV_Y

    face_zs = analyse_face_levels(part)
    step_zs = [z for z in face_zs if z > bb.min.Z + 0.6 and z < bb.max.Z - 0.6]

    page_label = {297: "A4", 420: "A3", 594: "A2", 841: "A1", 1189: "A0"}.get(
        int(PAGE_W), f"{PAGE_W:.0f}mm"
    )
    _log.info(
        "Scale %s:1  page %s  FV(%.0f,%.0f) PV(%.0f,%.0f) SV(%.0f,%.0f) ISO(%.0f,%.0f)",
        SCALE,
        page_label,
        FV_X,
        FV_Y,
        PV_X,
        PV_Y,
        SV_X,
        SV_Y,
        ISO_X,
        ISO_Y,
    )

    return SimpleNamespace(
        part=part,
        bb=bb,
        x_size=x_size,
        y_size=y_size,
        z_size=z_size,
        cx=cx,
        cy=cy,
        cz=cz,
        bbox_max=bbox_max,
        z_diams=z_diams,
        cross_diams=cross_diams,
        cyls=(z_cyls, cross_cyls),
        od_diam=od_diam,
        is_rotational=is_rotational,
        step_zs=step_zs,
        sv_right=sv_right,
        iso_right_limit=iso_right_limit,
        SCALE=SCALE,
        PAGE_W=PAGE_W,
        PAGE_H=PAGE_H,
        TB_W=TB_W,
        DIM_PAD=DIM_PAD,
        margin=margin,
        x_offset=x_offset,
        FV_X=FV_X,
        FV_Y=FV_Y,
        PV_X=PV_X,
        PV_Y=PV_Y,
        SV_X=SV_X,
        SV_Y=SV_Y,
        ISO_X=ISO_X,
        ISO_Y=ISO_Y,
        step_file=step_file,
        title=title,
        number=number,
        tolerance=tolerance,
        drawn_by=drawn_by,
        out=out,
    )


# ---------------------------------------------------------------------------
# Drawing builder (composable; make_drawing == build_drawing + export)
# ---------------------------------------------------------------------------


class Drawing:
    """A composable technical drawing — the editable form of :func:`make_drawing`.

    A ``Drawing`` holds the projected views, the annotation list, and per-view
    coordinate helpers. :func:`build_drawing` returns one pre-populated with the
    standard 4-view layout and automatic dimensions; you then add or remove
    annotations, add section/auxiliary views, and finally :meth:`export`.

    Attributes:
        scale: drawing scale factor (e.g. ``2.0`` for 2:1).
        page_w, page_h: sheet size in mm.
        tb_w: title-block width in mm.
        draft: the shared ``Draft`` preset used by the automatic annotations.
        look_at: scaled centroid ``(x, y, z)`` — the default ``look_at`` and a
            building block for custom view cameras (see :meth:`add_view`).
        dist: orthographic camera distance in scaled space.
        centroid: unscaled centroid ``(x, y, z)``.
        views: ``{name: (visible_compound, hidden_compound_or_None)}``.
        annotations: ordered list of annotation objects (mutable).
        part: the source solid, when known — enables the feature-coverage lint.

    The constructor also accepts ``cyls``, a precomputed
    ``analyse_cylinders(part)`` result (cached privately; computed lazily on
    first :meth:`lint` otherwise).
    """

    def __init__(
        self, *, scale, page_w, page_h, tb_w, draft, look_at, dist, centroid, out, part=None, cyls=None
    ):
        self.scale = scale
        self.part = part
        self._cyl_cache = cyls
        self.page_w = page_w
        self.page_h = page_h
        self.tb_w = tb_w
        self.draft = draft
        self.look_at = look_at
        self.dist = dist
        self.centroid = centroid
        self.out = out
        self.views: dict = {}
        self.annotations: list = []
        self._coords: dict = {}
        self._named: dict = {}
        self.svg_path: str | None = None
        self.dxf_path: str | None = None

    # -- views ----------------------------------------------------------------
    def add_view(self, name, shape, camera, up, position, *, look_at=None, scaled=False):
        """Project ``shape`` from ``camera`` and place it at ``position``.

        Args:
            name: view name (key in :attr:`views`); also used for coordinate lookups.
            shape: a build123d ``Shape`` to project. Given in world (unscaled)
                coordinates and scaled internally unless ``scaled=True``.
            camera, up, look_at: viewport parameters in **scaled** space (the same
                convention the standard views use). ``look_at`` defaults to
                :attr:`look_at` (the scaled centroid). Compose custom cameras from
                :attr:`look_at` and :attr:`dist`.
            position: ``(x, y)`` page position for the view centre, in mm.
            scaled: set ``True`` if ``shape`` is already scaled by :attr:`scale`.

        Returns:
            The :class:`ViewCoordinates` for this view (also via :meth:`coords`),
            for mapping world points to page coordinates.
        """
        la = self.look_at if look_at is None else look_at
        shape_s = shape if scaled else shape.scale(self.scale)
        vis, hid = shape_s.project_to_viewport(camera, up, la)
        vl, hl = list(vis), list(hid)
        if not vl and not hl:
            raise ValueError(
                f"project_to_viewport returned empty geometry for view {name!r} "
                f"(camera {camera}) — check the camera position and look_at."
            )
        loc = Location((position[0], position[1], 0))
        placed = Compound(children=vl).locate(loc)
        placed_hid = Compound(children=hl).locate(loc) if hl else None
        self.views[name] = (placed, placed_hid)
        axes = view_axes(camera, up, la)
        cx, cy, cz = la[0] / self.scale, la[1] / self.scale, la[2] / self.scale
        self._coords[name] = ViewCoordinates(axes, position[0], position[1], cx, cy, cz, self.scale)
        _log.info("  %s: %d visible / %d hidden", name, len(vl), len(hl))
        return self._coords[name]

    def coords(self, view):
        """Return the :class:`ViewCoordinates` for a named view."""
        return self._coords[view]

    def at(self, view, x, y, z):
        """Map a world point to a page point ``(px, py, 0)`` in ``view``."""
        px, py = self._coords[view].pp(x, y, z)
        return (px, py, 0.0)

    # -- annotations ----------------------------------------------------------
    def add(self, obj, name=None):
        """Register an annotation so lint and export include it; returns ``obj``.

        Re-using an existing ``name`` replaces the previously added object (it is
        dropped from :attr:`annotations`), so a name always maps to one object.
        """
        if name is not None and name in self._named:
            self.annotations.remove(self._named[name])
        annotate(obj, name)
        self.annotations.append(obj)
        if name is not None:
            self._named[name] = obj
        return obj

    def remove(self, name):
        """Remove a previously named annotation. Raises ``KeyError`` if absent."""
        obj = self._named.pop(name, None)
        if obj is None:
            raise KeyError(f"no annotation named {name!r}")
        self.annotations.remove(obj)
        return obj

    def clear_annotations(self, keep=("title_block",)):
        """Remove all annotations except those named in *keep* (#74).

        Wholesale removal that does not depend on the automatic naming
        scheme — ``dwg.clear_annotations()`` strips every automatic dimension,
        leader, and centreline but keeps the title block.

        Returns:
            The list of removed annotation objects.
        """
        keep_set = set(keep)
        kept_named = {n: o for n, o in self._named.items() if n in keep_set}
        kept_ids = {id(o) for o in kept_named.values()}
        removed = [o for o in self.annotations if id(o) not in kept_ids]
        self.annotations = [o for o in self.annotations if id(o) in kept_ids]
        self._named = kept_named
        return removed

    # -- output ---------------------------------------------------------------
    def lint(self):
        """Lint all annotations against all views; returns the list of issues.

        When :attr:`part` is set, also runs :func:`lint_feature_coverage`.
        """
        set_page(self.page_w, self.page_h, margin=10)
        view_shapes = [vis for vis, _ in self.views.values()]
        issues = lint_drawing(self.annotations, drawing_scale=self.scale, view_shapes=view_shapes)
        if self.part is not None:
            if self._cyl_cache is None:
                self._cyl_cache = analyse_cylinders(self.part)
            issues += lint_feature_coverage(self.part, self.annotations, cyls=self._cyl_cache)
        return issues

    def export(self, out=None):
        """Lint, then write SVG and DXF. Returns ``(svg_path, dxf_path)``."""
        out = out if out is not None else self.out
        for _ext in (".svg", ".dxf"):
            if out.endswith(_ext):
                out = out[: -len(_ext)]
                break

        issues = self.lint()
        if issues:
            _log.warning("Lint issues:")
            for iss in issues:
                _log.warning("  [%s] %s: %s", iss.severity, iss.code, iss.message)
        else:
            _log.info("Lint: OK")

        blk = Color(0, 0, 0)
        grey = Color(0.5, 0.5, 0.5)
        blue = Color(0, 0.2, 0.7)

        svg = ExportSVG(margin=10)
        svg.add_layer("part", line_color=blk, line_weight=0.5)
        svg.add_layer("hidden", line_color=grey, line_weight=0.25, line_type=LineType.HIDDEN)
        svg.add_layer("dims", line_color=blue, fill_color=blue, line_weight=0.05)
        self._add_shapes(svg)
        svg_path = out + ".svg"
        svg.write(svg_path)
        fix_svg_page_size(svg_path, self.page_w, self.page_h)
        _log.info("SVG → %s", svg_path)

        dxf = ExportDXF()
        dxf.add_layer("part", line_weight=0.5)
        dxf.add_layer("hidden", line_weight=0.25)
        dxf.add_layer("dims", line_weight=0.05)
        self._add_shapes(dxf)
        dxf_path = out + ".dxf"
        dxf.write(dxf_path)
        _log.info("DXF → %s", dxf_path)

        self.svg_path = svg_path
        self.dxf_path = dxf_path
        return svg_path, dxf_path

    def _add_shapes(self, exporter):
        """Add every view layer and annotation to *exporter* with error context."""
        for name, (vis, hid) in self.views.items():
            _export_shape(exporter, vis, "part", f"view {name!r}")
            if hid:
                _export_shape(exporter, hid, "hidden", f"view {name!r}")
        for ann in self.annotations:
            label = getattr(ann, "label", "") or type(ann).__name__
            _export_shape(exporter, ann, "dims", f"annotation {label!r}")


def _elements(shape):
    """Decompose *shape* for export retry: faces plus any loose edges."""
    faces = list(shape.faces())
    if not faces:
        return list(shape.edges())
    owned = {e for f in faces for e in f.edges()}
    return faces + [e for e in shape.edges() if e not in owned]


def _export_shape(exporter, shape, layer, ctx):
    """Add *shape* to *exporter*, degrading element-by-element on failure.

    build123d's exporters abort the whole export on the first edge whose
    curve cannot be approximated (a bare ``AssertionError`` from OCCT, #83).
    Instead, drop only the offending elements with a warning naming the
    view/layer, and raise (with that context) only if nothing exported.

    ``ExportSVG.add_shape`` is atomic — it appends converted elements only
    after the whole shape succeeds — so the shape is tried in one call first.
    ``ExportDXF`` writes edge-by-edge as it converts, so a mid-shape failure
    would leave partial output that a blind retry duplicates; for it (and any
    unknown exporter) every element is added individually from the start.
    """
    first_err = None
    if isinstance(exporter, ExportSVG):
        try:
            exporter.add_shape(shape, layer=layer)
            return
        except Exception as exc:
            first_err = exc
            _log.warning(
                "%s (layer %r) failed to export as one shape: %s — retrying element-wise",
                ctx,
                layer,
                exc,
            )
    elements = _elements(shape)
    skipped = 0
    for element in elements:
        try:
            exporter.add_shape(element, layer=layer)
        except Exception as exc:
            first_err = first_err or exc
            skipped += 1
            _log.debug("%s (layer %r): element failed to convert: %s", ctx, layer, exc)
    if skipped == len(elements) and first_err is not None:
        raise RuntimeError(
            f"{ctx} (layer {layer!r}): nothing could be exported"
        ) from first_err
    if skipped:
        _log.warning(
            "%s (layer %r): skipped %d of %d elements that failed to convert",
            ctx,
            layer,
            skipped,
            len(elements),
        )


def _auto_annotate(dwg, a):
    """Add the standard automatic dimensions, centrelines, and title block."""
    draft = dwg.draft

    def FX(x):
        return a.FV_X + (x - a.cx) * a.SCALE

    def FZ(z):
        return a.FV_Y + (z - a.cz) * a.SCALE

    def SX(y):
        return a.SV_X + (y - a.cy) * a.SCALE

    def SZ(z):
        return a.SV_Y + (z - a.cz) * a.SCALE

    # Overall height
    dwg.add(
        Dimension(
            (FX(a.bb.max.X) + 2, FZ(a.bb.min.Z), 0),
            (FX(a.bb.max.X) + 2, FZ(a.bb.max.Z), 0),
            "right",
            8,
            draft,
            label=_fmt(a.z_size),
        ),
        "dim_height",
    )

    # Outer diameter — only for rotational (turned) parts, and from the
    # classified external OD cylinder, never a bore that happens to be the
    # largest diameter (#81)
    if a.is_rotational:
        od = a.od_diam
        dwg.add(
            Dimension(
                (FX(a.cx - od / 2), FZ(a.bb.max.Z) + 2, 0),
                (FX(a.cx + od / 2), FZ(a.bb.max.Z) + 2, 0),
                "above",
                8,
                draft,
                label=f"ø{_fmt(od)}",
            ),
            "dim_od",
        )
        # Centreline through the rotation axis — front and side views
        dwg.add(
            Centerline(
                (FX(a.cx), FZ(a.bb.min.Z) - 5, 0),
                (FX(a.cx), FZ(a.bb.max.Z) + 5, 0),
            ),
            "centerline_front",
        )
        dwg.add(
            Centerline(
                (SX(a.cy), SZ(a.bb.min.Z) - 5, 0),
                (SX(a.cy), SZ(a.bb.max.Z) + 5, 0),
            ),
            "centerline_side",
        )

    # Z-axis bore leaders to the left of the front view — these assume bores
    # concentric with the rotation axis, so rotational only (#81)
    bores = [d for d in a.z_diams if d != a.od_diam]
    if a.is_rotational and bores:
        left_edge = FX(a.bb.min.X)
        left_space = left_edge - a.margin
        if left_space >= a.DIM_PAD:
            ldr_length = a.DIM_PAD * 0.6
            elbow_x = left_edge - ldr_length
            for i, d in enumerate(bores[:3]):
                tip_z = FZ(a.cz) + (i - 1) * 10
                dwg.add(
                    Leader(
                        tip=(FX(a.cx - d / 2), tip_z, 0),
                        elbow=(elbow_x, tip_z, 0),
                        label=f"ø{_fmt(d)}",
                        draft=draft,
                    ),
                    f"ldr_z{i}",
                )
        else:
            _log.info("Additional diameters %s not annotated (insufficient left margin)", bores)

    if a.cross_diams:
        _log.info(
            "Cross-hole ø%s detected but not annotated (requires section view)",
            _fmt(a.cross_diams[0]),
        )

    # Step heights — only where the step is tall enough to fit a label
    if a.step_zs:
        right_x0 = FX(a.bb.max.X) + 2 + a.DIM_PAD + 10
        step_specs = [
            (
                (right_x0 + col * 14, FZ(a.bb.min.Z), 0),
                (right_x0 + col * 14, FZ(z), 0),
                "right",
                _fmt(z - a.bb.min.Z),
            )
            for col, z in enumerate([z for z in a.step_zs[:3] if (z - a.bb.min.Z) * a.SCALE >= 20])
        ]
        for col, dim in enumerate(place_dims(step_specs, draft)):
            dwg.add(dim, f"dim_step_{col}")

    # Width (non-round / non-square parts only)
    if abs(a.x_size - a.y_size) > max(a.x_size, a.y_size) * 0.05:

        def PX(x):
            return a.PV_X + (x - a.cx) * a.SCALE

        def PY(y):
            return a.PV_Y + (y - a.cy) * a.SCALE

        dwg.add(
            Dimension(
                (PX(a.bb.min.X), PY(a.bb.min.Y) - 2, 0),
                (PX(a.bb.max.X), PY(a.bb.min.Y) - 2, 0),
                "below",
                8,
                draft,
                label=_fmt(a.x_size),
            ),
            "dim_width",
        )

    _add_title_block(dwg, a)


def _add_title_block(dwg, a):
    """Add the title block annotation."""
    tb = TitleBlock(
        a.title,
        a.number,
        scale=format_drawing_scale(a.SCALE),
        general_tolerance=a.tolerance,
        designed_by=a.drawn_by,
        revision="A",
        legal_owner="",
        width=a.TB_W,
        draft=dwg.draft,
    ).locate(Location((a.PAGE_W - a.TB_W - 11, 11, 0)))
    dwg.add(tb, "title_block")


_ISO_SHRINK_FACTORS = (0.5, 0.2, 0.1)


def _iso_bbox(dwg):
    """(min_x, min_y, max_x, max_y) of the placed iso view, hidden lines included."""
    vis, hid = dwg.views["iso"]
    bb = vis.bounding_box()
    x0, y0, x1, y1 = bb.min.X, bb.min.Y, bb.max.X, bb.max.Y
    if hid:
        hb = hid.bounding_box()
        x0, y0 = min(x0, hb.min.X), min(y0, hb.min.Y)
        x1, y1 = max(x1, hb.max.X), max(y1, hb.max.Y)
    return x0, y0, x1, y1


def _bbox_within(bb, region, tol: float = 0.5) -> bool:
    """True if (min_x, min_y, max_x, max_y) *bb* fits inside *region* within *tol*."""
    return (
        bb[0] >= region[0] - tol
        and bb[1] >= region[1] - tol
        and bb[2] <= region[2] + tol
        and bb[3] <= region[3] + tol
    )


def _project_iso(dwg, a, scale, shape_s=None):
    """(Re-)project the iso view at *scale* (an absolute factor, not a fraction).

    Pass *shape_s* when the part is already scaled by *scale* to skip the copy.
    """
    la = (a.cx * scale, a.cy * scale, a.cz * scale)
    off = (a.bbox_max * scale + 100) / math.sqrt(3)
    camera = (la[0] + off, la[1] + off, la[2] + off)
    dwg.add_view(
        "iso",
        shape_s if shape_s is not None else a.part.scale(scale),
        camera,
        (0, 0, 1),
        (a.ISO_X, a.ISO_Y),
        look_at=la,
        scaled=True,
    )
    if scale != dwg.scale:
        # add_view derives ViewCoordinates from the drawing scale; an iso
        # projected at a different scale needs them rebuilt so
        # dwg.at("iso", ...) keeps mapping world points correctly.
        axes = view_axes(camera, (0, 0, 1), la)
        dwg._coords["iso"] = ViewCoordinates(axes, a.ISO_X, a.ISO_Y, a.cx, a.cy, a.cz, scale)


def _fit_iso_view(dwg, a):
    """Shrink the iso view to fit its page region, captioning it NTS (#75).

    The layout reserves ~0.7 × bbox_max for the iso column, but the true
    projected extent can be wider (long prismatic parts), pushing the iso past
    the page edge or into the side view's dimension space. When the projected
    iso bbox overflows the region, re-project at a clean fraction of sheet
    scale and add an "ISO VIEW (NTS)" caption below it.
    """
    region = (a.sv_right, a.margin, a.iso_right_limit, a.PAGE_H - a.margin)
    bb = _iso_bbox(dwg)
    # Exact check (no tolerance): the lint's view_out_of_bounds is exact, so
    # accepting a sub-tolerance overflow here would pass the fit yet fail lint.
    if _bbox_within(bb, region, tol=0.0):
        return
    # Orthographic projection is linear and the view centre maps to
    # (ISO_X, ISO_Y), so each bbox side's offset from the centre scales
    # exactly with the shape scale — the factor needed to fit can be computed
    # from the measured extents, costing a single re-projection.
    ratios = [
        avail / extent
        for extent, avail in (
            (a.ISO_X - bb[0], a.ISO_X - region[0]),
            (bb[2] - a.ISO_X, region[2] - a.ISO_X),
            (a.ISO_Y - bb[1], a.ISO_Y - region[1]),
            (bb[3] - a.ISO_Y, region[3] - a.ISO_Y),
        )
        if extent > 0
    ]
    needed = min(ratios, default=1.0)
    factor = next((f for f in _ISO_SHRINK_FACTORS if f <= needed), _ISO_SHRINK_FACTORS[-1])
    _project_iso(dwg, a, a.SCALE * factor)
    bb = _iso_bbox(dwg)
    if not _bbox_within(bb, region):
        _log.warning("Iso view still overflows its page region at %g× sheet scale", factor)
    font = dwg.draft.font_size
    dwg.add(
        Note(
            "ISO VIEW (NTS)",
            (a.ISO_X, max(bb[1] - 2 * font, a.margin + font)),
            dwg.draft,
        ),
        "note_iso_nts",
    )
    _log.info("Iso view shrunk to %g× sheet scale (NTS)", factor)


def build_drawing(
    step_file: str | Path | Shape,
    out: str | None = None,
    title: str | None = None,
    number: str = "DWG-001",
    tolerance: str = "ISO 2768-m",
    drawn_by: str = "",
    scale: float | None = None,
    page: str | tuple | None = None,
    auto_dims: bool = True,
) -> Drawing:
    """Build a customisable 4-view :class:`Drawing` without exporting it.

    Same arguments as :func:`make_drawing`, but returns the live :class:`Drawing`
    so you can add or remove annotations and add section/auxiliary views before
    calling :meth:`Drawing.export`. ``make_drawing(...)`` is exactly
    ``build_drawing(...).export()``.

    Args:
        auto_dims: pass ``False`` to skip the automatic dimensions,
            centrelines, and leaders (#74) — the automatic set assumes a
            turned part and is wrong for prismatic geometry. Views, scale,
            page, and title block are still produced; add your own
            annotations before export. (Annotations added by the default can
            also be removed wholesale with :meth:`Drawing.clear_annotations`.)

    Returns:
        A :class:`Drawing` with the standard front/plan/side/iso views projected
        and the automatic dimensions + title block already added.
    """
    stem = "drawing" if isinstance(step_file, Shape) else Path(step_file).stem
    out = out or stem
    for _ext in (".svg", ".dxf"):
        if out.endswith(_ext):
            out = out[: -len(_ext)]
            break
    title = title or stem.replace("_", " ").upper()

    a = _analyse(step_file, title, number, tolerance, drawn_by, out, scale=scale, page=page)

    cxs, cys, czs = a.cx * a.SCALE, a.cy * a.SCALE, a.cz * a.SCALE
    look_at = (cxs, cys, czs)
    dist = a.bbox_max * a.SCALE + 100

    dwg = Drawing(
        scale=a.SCALE,
        page_w=a.PAGE_W,
        page_h=a.PAGE_H,
        tb_w=a.TB_W,
        draft=draft_preset(font_size=3.0, decimal_precision=1),
        look_at=look_at,
        dist=dist,
        centroid=(a.cx, a.cy, a.cz),
        out=out,
        part=a.part,
        cyls=a.cyls,
    )

    part_s = a.part.scale(a.SCALE)
    dwg.add_view("front", part_s, (cxs, cys - dist, czs), (0, 0, 1), (a.FV_X, a.FV_Y), scaled=True)
    dwg.add_view("plan", part_s, (cxs, cys, czs + dist), (0, 1, 0), (a.PV_X, a.PV_Y), scaled=True)
    dwg.add_view("side", part_s, (cxs + dist, cys, czs), (0, 0, 1), (a.SV_X, a.SV_Y), scaled=True)
    _project_iso(dwg, a, a.SCALE, shape_s=part_s)
    _fit_iso_view(dwg, a)

    if auto_dims:
        _auto_annotate(dwg, a)
    else:
        _add_title_block(dwg, a)
    return dwg


# ---------------------------------------------------------------------------
# Direct export (SVG + DXF)
# ---------------------------------------------------------------------------


def make_drawing(
    step_file: str | Path | Shape,
    out: str | None = None,
    title: str | None = None,
    number: str = "DWG-001",
    tolerance: str = "ISO 2768-m",
    drawn_by: str = "",
    scale: float | None = None,
    page: str | tuple | None = None,
    auto_dims: bool = True,
) -> tuple[str, str]:
    """Generate a 4-view technical drawing from a STEP file or build123d object.

    Args:
        step_file: Path to a STEP/STP file, or a build123d ``Shape`` (e.g. a
            ``Part``, ``Solid``, or ``Compound``) to draw directly.
        out: Output path stem (default: input filename stem, or ``"drawing"``
            when a build123d object is passed).
        title: Part title for the title block (default: stem uppercased).
        number: Drawing number (e.g. ``"DWG-042"``).
        tolerance: General tolerance string (e.g. ``"ISO 2768-m"``).
        drawn_by: Designer name for the title block.
        scale: Drawing-scale override (e.g. ``5`` for 5:1, ``0.5`` for 1:2).
            Default: chosen automatically by :func:`choose_scale`.
        page: Page-size override — an ISO name (``"A3"``), ``"WIDTHxHEIGHT"``
            in mm, or a ``(width, height)`` tuple. Default: chosen
            automatically by :func:`choose_scale`.
        auto_dims: pass ``False`` to skip the automatic dimensions,
            centrelines, and leaders (#74) — views, scale, page, and title
            block only.

    Returns:
        Tuple of ``(svg_path, dxf_path)`` for the generated files.

    This is a thin wrapper: ``make_drawing(...)`` is ``build_drawing(...).export()``.
    To add or remove annotations or add section/auxiliary views before export,
    call :func:`build_drawing` and use the returned :class:`Drawing`.
    """
    return build_drawing(
        step_file,
        out=out,
        title=title,
        number=number,
        tolerance=tolerance,
        drawn_by=drawn_by,
        scale=scale,
        page=page,
        auto_dims=auto_dims,
    ).export()


# ---------------------------------------------------------------------------
# Script generation (Cog-enabled .py output)
# ---------------------------------------------------------------------------


def _write_script(a) -> str:
    """Write an editable script at ``a.out + '.py'`` that calls make_drawing()."""
    py_path = a.out + ".py"
    py_name = Path(py_path).name

    cog_output = "\n".join(
        [
            f"STEP_FILE = {a.step_file!r}",
            f"TITLE = {a.title!r}",
            f"NUMBER = {a.number!r}",
            f"TOLERANCE = {a.tolerance!r}",
            f"DRAWN_BY = {a.drawn_by!r}",
        ]
    )

    cog_block = (
        "# [[[cog\n"
        "# ── Config: edit these, then run `cog -r <script>.py` to update ────────────\n"
        f"_STEP_FILE = {a.step_file!r}\n"
        f"_TITLE     = {a.title!r}\n"
        f"_NUMBER    = {a.number!r}\n"
        f"_TOLERANCE = {a.tolerance!r}\n"
        f"_DRAWN_BY  = {a.drawn_by!r}\n"
        "try:\n"
        "    cog  # NameError → not under cog\n"
        "    for _k, _v in [\n"
        "        ('STEP_FILE', repr(_STEP_FILE)), ('TITLE', repr(_TITLE)),\n"
        "        ('NUMBER', repr(_NUMBER)), ('TOLERANCE', repr(_TOLERANCE)),\n"
        "        ('DRAWN_BY', repr(_DRAWN_BY)),\n"
        "    ]:\n"
        "        cog.outl(f'{_k} = {_v}')\n"
        "except NameError:\n"
        "    pass\n"
        "# ]]]\n"
        f"{cog_output}\n"
        "# [[[end]]]"
    )

    _tq = '"""'
    _safe_doc_title = a.title.replace(_tq, "'''")
    _safe_doc_number = a.number.replace(_tq, "'''")
    header = (
        f"#!/usr/bin/env python3\n"
        f'"""\n'
        f"{_safe_doc_title} — Technical drawing ({_safe_doc_number}).\n"
        f"\n"
        f"Auto-generated by make-drawing. Edit freely.\n"
        f"To update metadata: edit _STEP_FILE / _TITLE / etc. in the cog block, then run:\n"
        f"  cog -r {py_name}   (pip install cogapp)\n"
        f"\n"
        f"Run:  uv run python {py_name}\n"
        f'"""\n'
        f"import os as _os\n"
        f"from build123d_drafting import build_drawing\n"
        f"\n"
        f"# ── Config (auto-updated by cog) ──────────────────────────────────────────────\n"
    )

    run_section = (
        "\n"
        "# ── Build drawing (standard 4-view layout + automatic dimensions) ─────────────\n"
        "_stem = _os.path.splitext(__file__)[0]\n"
        "dwg = build_drawing(\n"
        "    STEP_FILE,\n"
        "    out=_stem,\n"
        "    title=TITLE,\n"
        "    number=NUMBER,\n"
        "    tolerance=TOLERANCE,\n"
        "    drawn_by=DRAWN_BY,\n"
        ")\n"
        "\n"
        "# ── Customise here — runs BEFORE export, so edits land in the output ───────────\n"
        "# dwg.views        'front' 'plan' 'side' 'iso'  → (visible, hidden) compounds\n"
        "# dwg.annotations  mutable list of annotation objects\n"
        "# dwg.at(view, x, y, z)  → page point (px, py, 0) mapped from world coordinates\n"
        "# dwg.add(obj, name) / dwg.remove(name)\n"
        "# dwg.add_view(name, shape, camera, up, position)  → section / auxiliary view\n"
        "# Example:\n"
        "#   from build123d_drafting import Leader\n"
        "#   dwg.add(Leader(tip=dwg.at('front', 10, 0, 5), elbow=(8, 40, 0),\n"
        "#                  label='ø4 BORE', draft=dwg.draft), 'ldr_bore')\n"
        "#   dwg.remove('dim_height')\n"
        "\n"
        "# ── Export ────────────────────────────────────────────────────────────────────\n"
        "svg_path, dxf_path = dwg.export(_stem)\n"
        'print(f"SVG \\u2192 {svg_path}")\n'
        'print(f"DXF \\u2192 {dxf_path}")\n'
    )

    content = header + cog_block + run_section
    Path(py_path).write_text(content, encoding="utf-8")
    _log.info("Script → %s", py_path)
    return py_path


def generate_script(
    step_file: str,
    out: str | None = None,
    title: str | None = None,
    number: str = "DWG-001",
    tolerance: str = "ISO 2768-m",
    drawn_by: str = "",
) -> str:
    """Generate an editable Cog-enabled drawing script from a STEP file.

    Returns:
        Path to the generated ``.py`` file.
    """
    if isinstance(step_file, Shape):
        raise TypeError(
            "generate_script() requires a STEP file path — the generated script "
            "reloads geometry from disk and cannot embed a live build123d object. "
            "Use make_drawing() directly to draw an in-memory object."
        )
    stem = Path(step_file).stem
    out = out or stem
    for _ext in (".py", ".svg", ".dxf"):
        if out.endswith(_ext):
            out = out[: -len(_ext)]
            break
    title = title or stem.replace("_", " ").upper()
    a = _analyse(step_file, title, number, tolerance, drawn_by, out)
    return _write_script(a)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        description="Zero-AI STEP → technical drawing (SVG + DXF, or editable .py script)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("step_file", help="Input STEP file (.step / .stp)")
    ap.add_argument("--out", default=None, help="Output prefix (default: input stem)")
    ap.add_argument("--title", default=None, help="Part title for title block")
    ap.add_argument("--number", default="DWG-001", help="Drawing number")
    ap.add_argument("--tolerance", default="ISO 2768-m", help="General tolerance")
    ap.add_argument("--drawn-by", default="", help="Designer name")
    ap.add_argument(
        "--scale",
        type=float,
        default=None,
        help="Drawing-scale override, e.g. 5 for 5:1 or 0.5 for 1:2 (default: auto)",
    )
    ap.add_argument(
        "--page",
        default=None,
        help="Page-size override: A4..A0 or WIDTHxHEIGHT in mm, e.g. 420x297 (default: auto)",
    )
    ap.add_argument(
        "--script",
        action="store_true",
        help="Write an editable .py drawing script instead of SVG+DXF",
    )
    args = ap.parse_args()

    if args.script and (args.scale is not None or args.page is not None):
        ap.error("--scale/--page only apply to direct output; edit the generated script instead")

    if args.script:
        generate_script(
            step_file=args.step_file,
            out=args.out,
            title=args.title,
            number=args.number,
            tolerance=args.tolerance,
            drawn_by=args.drawn_by,
        )
    else:
        make_drawing(
            step_file=args.step_file,
            out=args.out,
            title=args.title,
            number=args.number,
            tolerance=args.tolerance,
            drawn_by=args.drawn_by,
            scale=args.scale,
            page=args.page,
        )


if __name__ == "__main__":
    _cli()
