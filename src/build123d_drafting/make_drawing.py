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
    import_step,
)
from build123d_drafting.helpers import (
    Dimension,
    Leader,
    TitleBlock,
    annotate,
    draft_preset,
    lint_drawing,
    set_page,
)
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane

_log = logging.getLogger(__name__)

_TB_W = 150.0
_MARGIN = 10.0
_DIM_PAD = 18.0


# ---------------------------------------------------------------------------
# SVG post-processing
# ---------------------------------------------------------------------------


def _fix_svg_page_size(svg_path: str, page_w: float, page_h: float) -> None:
    """Rewrite the SVG width/height/viewBox to match the full ISO page size.

    ExportSVG crops to content bounding box; this expands it to the declared
    page so the rendering fills the correct A-series sheet.
    """
    data = Path(svg_path).read_text()
    data = re.sub(r'width="[^"]*"', f'width="{page_w:.3f}mm"', data, count=1)
    data = re.sub(r'height="[^"]*"', f'height="{page_h:.3f}mm"', data, count=1)
    data = re.sub(
        r'viewBox="[^"]*"',
        f'viewBox="0 -{page_h:.3f} {page_w:.3f} {page_h:.3f}"',
        data,
        count=1,
    )
    Path(svg_path).write_text(data)


# ---------------------------------------------------------------------------
# Geometry analysis
# ---------------------------------------------------------------------------


def _analyse_cylinders(part):
    """Return (z_cyls, cross_cyls) from OCP cylindrical face analysis.

    Each entry is a dict with keys: diameter, area, cx, cy, cz, axis.
    z_cyls: cylinders whose axis is approximately Z.
    cross_cyls: cylinders whose axis is approximately X or Y.
    """
    z_cyls, cross_cyls = [], []
    for face in part.faces():
        surf = BRepAdaptor_Surface(face.wrapped)
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        cyl = surf.Cylinder()
        r = cyl.Radius()
        d = cyl.Axis().Direction()
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
        )
        (z_cyls if ax == "z" else cross_cyls).append(rec)
    return z_cyls, cross_cyls


def _dedup_diams(cyls, tol: float = 0.15) -> list:
    """Return sorted-descending deduplicated diameter list from cylinder records."""
    raw = sorted({c["diameter"] for c in cyls}, reverse=True)
    merged = []
    for d in raw:
        if not merged or abs(d - merged[-1]) > tol:
            merged.append(round(d, 2))
    return merged


def _fmt(v: float) -> str:
    """Format a float as integer string if whole, otherwise 1 dp."""
    return str(int(v)) if v == int(v) else f"{v:.1f}"


def _analyse_face_levels(part, tol: float = 0.5) -> list:
    """Return sorted unique Z-coords of horizontal (normal≈±Z) planar faces."""
    zs = set()
    for face in part.faces():
        surf = BRepAdaptor_Surface(face.wrapped)
        if surf.GetType() == GeomAbs_Plane:
            ax = surf.Plane().Axis().Direction()
            if abs(ax.Z()) > 0.99:
                zs.add(round(surf.Plane().Location().Z() / tol) * tol)
    return sorted(zs)


def _choose_scale(x_size: float, y_size: float, z_size: float) -> tuple:
    """Return (SCALE, PAGE_W, PAGE_H, TB_W) for a 4-view layout.

    Layout columns: [front(x×z)] [side(y×z)] [iso(~0.7*max)] [title block].
    Rows: [plan(x×y)] above [front/side].
    Tries ISO A-series pages (A4→A3→A2→A1→A0) at preferred scales.
    A4 uses a 120 mm title block; A3+ use 150 mm.
    """
    bbox_max = max(x_size, y_size, z_size)
    for SCALE, PAGE_W, PAGE_H, TB_W in [
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
    ]:
        w = (
            _MARGIN
            + _DIM_PAD
            + x_size * SCALE
            + _DIM_PAD
            + y_size * SCALE
            + _DIM_PAD
            + bbox_max * SCALE * 0.7
            + _DIM_PAD
            + TB_W
            + _MARGIN
        )
        h = (
            _MARGIN
            + _DIM_PAD
            + y_size * SCALE
            + _DIM_PAD
            + z_size * SCALE
            + _DIM_PAD
            + _MARGIN
        )
        if w <= PAGE_W and h <= PAGE_H:
            return SCALE, PAGE_W, PAGE_H, TB_W
    return 0.2, 1189.0, 841.0, 150.0


# ---------------------------------------------------------------------------
# Shared analysis step
# ---------------------------------------------------------------------------


def _analyse(step_file, title, number, tolerance, drawn_by, out):
    """Load STEP, analyse geometry, compute layout. Returns SimpleNamespace."""
    part = import_step(step_file)
    bb = part.bounding_box()
    x_size = bb.max.X - bb.min.X
    y_size = bb.max.Y - bb.min.Y
    z_size = bb.max.Z - bb.min.Z
    cx = (bb.min.X + bb.max.X) / 2
    cy = (bb.min.Y + bb.max.Y) / 2
    cz = (bb.min.Z + bb.max.Z) / 2
    bbox_max = max(x_size, y_size, z_size)

    _log.info("Loaded %s  bbox: %.2f × %.2f × %.2f mm", step_file, x_size, y_size, z_size)

    z_cyls, cross_cyls = _analyse_cylinders(part)
    z_diams = _dedup_diams(z_cyls)
    cross_diams = _dedup_diams(cross_cyls)

    _log.info("Z-axis diameters: %s", z_diams)
    if cross_diams:
        _log.info("Cross-hole diams: %s", cross_diams)

    SCALE, PAGE_W, PAGE_H, TB_W = _choose_scale(x_size, y_size, z_size)
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
    tb_top_y = margin + 35
    iso_above_tb = (PV_Y - pv_hh) > tb_top_y
    iso_right_limit = (PAGE_W - margin) if iso_above_tb else (PAGE_W - TB_W - margin)
    right_avail = max(0.0, iso_right_limit - sv_right)
    ISO_X = sv_right + right_avail / 2
    ISO_Y = PV_Y

    face_zs = _analyse_face_levels(part)
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
        step_zs=step_zs,
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
# Direct export (SVG + DXF)
# ---------------------------------------------------------------------------


def make_drawing(
    step_file: str,
    out: str | None = None,
    title: str | None = None,
    number: str = "DWG-001",
    tolerance: str = "ISO 2768-m",
    drawn_by: str = "",
) -> tuple[str, str]:
    """Generate a 4-view technical drawing from a STEP file.

    Args:
        step_file: Path to the input STEP/STP file.
        out: Output path stem (default: input filename stem).
        title: Part title for the title block (default: stem uppercased).
        number: Drawing number (e.g. ``"DWG-042"``).
        tolerance: General tolerance string (e.g. ``"ISO 2768-m"``).
        drawn_by: Designer name for the title block.

    Returns:
        Tuple of ``(svg_path, dxf_path)`` for the generated files.
    """
    stem = Path(step_file).stem
    out = out or stem
    for _ext in (".svg", ".dxf"):
        if out.endswith(_ext):
            out = out[: -len(_ext)]
            break
    title = title or stem.replace("_", " ").upper()

    a = _analyse(step_file, title, number, tolerance, drawn_by, out)

    cxs, cys, czs = a.cx * a.SCALE, a.cy * a.SCALE, a.cz * a.SCALE
    look_at = (cxs, cys, czs)
    DIST = a.bbox_max * a.SCALE + 100
    ID = DIST / math.sqrt(3)

    part_s = a.part.scale(a.SCALE)

    view_cfg = [
        ("front", (cxs, cys - DIST, czs), (0, 0, 1), (a.FV_X, a.FV_Y)),
        ("plan", (cxs, cys, czs + DIST), (0, 1, 0), (a.PV_X, a.PV_Y)),
        ("side", (cxs + DIST, cys, czs), (0, 0, 1), (a.SV_X, a.SV_Y)),
    ]
    view_proj = {}
    for vname, cam, up, pos in view_cfg:
        vis, hid = part_s.project_to_viewport(cam, up, look_at)
        vl, hl = list(vis), list(hid)
        placed = Compound(children=vl).locate(Location((pos[0], pos[1], 0)))
        placed_hid = Compound(children=hl).locate(Location((pos[0], pos[1], 0))) if hl else None
        view_proj[vname] = (placed, placed_hid)
        _log.info("  %s: %d visible / %d hidden", vname, len(vl), len(hl))

    iso_vis, iso_hid = part_s.project_to_viewport(
        (cxs + ID, cys + ID, czs + ID), (0, 0, 1), look_at
    )
    iso = Compound(children=list(iso_vis)).locate(Location((a.ISO_X, a.ISO_Y, 0)))
    iso_h = (
        Compound(children=list(iso_hid)).locate(Location((a.ISO_X, a.ISO_Y, 0)))
        if list(iso_hid)
        else None
    )

    def FX(x):
        return a.FV_X + (x - a.cx) * a.SCALE

    def FZ(z):
        return a.FV_Y + (z - a.cz) * a.SCALE

    draft = draft_preset(font_size=3.0, decimal_precision=1)
    all_anns = []

    def _ann(obj, name):
        annotate(obj, name)
        all_anns.append(obj)
        return obj

    # Overall height
    _ann(
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

    # Outer diameter — only for parts with cylindrical faces
    if a.z_diams:
        od = a.z_diams[0]
        _ann(
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

    # Additional Z-axis diameter leaders to the left of the front view
    left_edge = FX(a.bb.min.X)
    left_space = left_edge - a.margin
    if left_space >= a.DIM_PAD and len(a.z_diams) > 1:
        elbow_x = left_edge - a.DIM_PAD * 0.6
        for i, d in enumerate(a.z_diams[1:4]):
            tip_z = FZ(a.cz) + (i - 1) * 10
            _ann(
                Leader(
                    tip=(FX(a.cx - d / 2), tip_z, 0),
                    elbow=(elbow_x, tip_z + 4, 0),
                    label=f"ø{_fmt(d)}",
                    draft=draft,
                ),
                f"ldr_z{i}",
            )
    elif len(a.z_diams) > 1:
        _log.info(
            "Additional diameters %s not annotated (insufficient left margin)", a.z_diams[1:]
        )

    if a.cross_diams:
        _log.info(
            "Cross-hole ø%s detected but not annotated (requires section view)",
            _fmt(a.cross_diams[0]),
        )

    # Step heights — only where the step is tall enough to fit a label
    if a.step_zs:
        right_x0 = FX(a.bb.max.X + 2) + a.DIM_PAD + 10
        step_col = 0
        for z in a.step_zs[:3]:
            step_h = z - a.bb.min.Z
            if step_h * a.SCALE < 20:
                continue
            _ann(
                Dimension(
                    (right_x0 + step_col * 14, FZ(a.bb.min.Z), 0),
                    (right_x0 + step_col * 14, FZ(z), 0),
                    "right",
                    8,
                    draft,
                    label=_fmt(step_h),
                ),
                f"dim_step_{step_col}",
            )
            step_col += 1

    # Width (non-round / non-square parts only)
    if abs(a.x_size - a.y_size) > max(a.x_size, a.y_size) * 0.05:

        def PX(x):
            return a.PV_X + (x - a.cx) * a.SCALE

        def PY(y):
            return a.PV_Y + (y - a.cy) * a.SCALE

        _ann(
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

    # Title block
    tb = TitleBlock(
        title,
        number,
        drawing_scale=a.SCALE,
        general_tolerance=tolerance,
        designed_by=drawn_by,
        revision="A",
        legal_owner="",
        width=a.TB_W,
        draft=draft,
    ).locate(Location((a.PAGE_W - a.TB_W - 11, 11, 0)))
    _ann(tb, "title_block")

    set_page(a.PAGE_W, a.PAGE_H, margin=10)
    placed_views = [view_proj[v][0] for v in ("front", "plan", "side") if view_proj.get(v)]
    placed_views.append(iso)
    issues = lint_drawing(all_anns, drawing_scale=a.SCALE, view_shapes=placed_views)
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
    for vname in ("front", "plan", "side"):
        p, ph = view_proj[vname]
        svg.add_shape(p, layer="part")
        if ph:
            svg.add_shape(ph, layer="hidden")
    svg.add_shape(iso, layer="part")
    if iso_h:
        svg.add_shape(iso_h, layer="hidden")
    for ann in all_anns:
        svg.add_shape(ann, layer="dims")
    svg_path = out + ".svg"
    svg.write(svg_path)
    _fix_svg_page_size(svg_path, a.PAGE_W, a.PAGE_H)
    _log.info("SVG → %s", svg_path)

    dxf = ExportDXF()
    dxf.add_layer("part", line_weight=0.5)
    dxf.add_layer("hidden", line_weight=0.25)
    dxf.add_layer("dims", line_weight=0.05)
    for vname in ("front", "plan", "side"):
        p, ph = view_proj[vname]
        dxf.add_shape(p, layer="part")
        if ph:
            dxf.add_shape(ph, layer="hidden")
    dxf.add_shape(iso, layer="part")
    if iso_h:
        dxf.add_shape(iso_h, layer="hidden")
    for ann in all_anns:
        dxf.add_shape(ann, layer="dims")
    dxf_path = out + ".dxf"
    dxf.write(dxf_path)
    _log.info("DXF → %s", dxf_path)

    return svg_path, dxf_path


# ---------------------------------------------------------------------------
# Script generation (Cog-enabled .py output)
# ---------------------------------------------------------------------------


def _write_script(a) -> str:
    """Write a Cog-enabled drawing script at ``a.out + '.py'``."""
    py_path = a.out + ".py"
    py_name = Path(py_path).name

    unannotated = []
    if len(a.z_diams) > 1:
        unannotated.append(f"inner diameters {a.z_diams[1:]}")
    if a.cross_diams:
        unannotated.append(f"cross-hole ø{_fmt(a.cross_diams[0])}")
    unannotated_note = (
        "\n".join(f"# Unannotated (add below manually): {u}" for u in unannotated)
        if unannotated
        else "# All detected features annotated."
    )

    cog_output = "\n".join(
        [
            f"STEP_FILE = {a.step_file!r}",
            f"TITLE = {a.title!r}",
            f"NUMBER = {a.number!r}",
            f"TOLERANCE = {a.tolerance!r}",
            f"DRAWN_BY = {a.drawn_by!r}",
            f"SCALE = {a.SCALE}",
            f"PAGE_W, PAGE_H = {a.PAGE_W}, {a.PAGE_H}",
            f"TB_W = {a.TB_W}",
            f"FV_X, FV_Y = {a.FV_X:.2f}, {a.FV_Y:.2f}",
            f"PV_X, PV_Y = {a.PV_X:.2f}, {a.PV_Y:.2f}",
            f"SV_X, SV_Y = {a.SV_X:.2f}, {a.SV_Y:.2f}",
            f"ISO_X, ISO_Y = {a.ISO_X:.2f}, {a.ISO_Y:.2f}",
            f"cx, cy, cz = {a.cx:.4f}, {a.cy:.4f}, {a.cz:.4f}",
            f"x_size, y_size, z_size = {a.x_size:.4f}, {a.y_size:.4f}, {a.z_size:.4f}",
            f"z_diams = {a.z_diams!r}",
            f"cross_diams = {a.cross_diams!r}",
        ]
    )

    # The cog block imports from build123d_drafting.make_drawing (the installed package).
    # Plain strings for the for-loop body so { } are literal Python f-string code for cogapp.
    cog_block = (
        "# [[[cog\n"
        "# ── Config: edit these when STEP file or metadata changes ───────────────────\n"
        f"_STEP_FILE = {a.step_file!r}\n"
        f"_TITLE     = {a.title!r}\n"
        f"_NUMBER    = {a.number!r}\n"
        f"_TOLERANCE = {a.tolerance!r}\n"
        f"_DRAWN_BY  = {a.drawn_by!r}\n"
        "# ── Regeneration (runs analysis only when updating with `cog -r`) ──────────\n"
        "try:\n"
        "    cog  # NameError → not under cog; use output section below\n"
        "    from build123d_drafting.make_drawing import (\n"
        "        _analyse_cylinders, _choose_scale, _dedup_diams,\n"
        "    )\n"
        "    from build123d import import_step as _imp\n"
        "    _part = _imp(_STEP_FILE)\n"
        "    _bb   = _part.bounding_box()\n"
        "    x_size = _bb.max.X - _bb.min.X\n"
        "    y_size = _bb.max.Y - _bb.min.Y\n"
        "    z_size = _bb.max.Z - _bb.min.Z\n"
        "    cx = (_bb.min.X + _bb.max.X) / 2\n"
        "    cy = (_bb.min.Y + _bb.max.Y) / 2\n"
        "    cz = (_bb.min.Z + _bb.max.Z) / 2\n"
        "    bbox_max = max(x_size, y_size, z_size)\n"
        "    _zc, _xc = _analyse_cylinders(_part)\n"
        "    z_diams     = _dedup_diams(_zc)\n"
        "    cross_diams = _dedup_diams(_xc)\n"
        "    SCALE, PAGE_W, PAGE_H, TB_W = _choose_scale(x_size, y_size, z_size)\n"
        "    _DIM_PAD = 18.0; _margin = 10.0\n"
        "    _fv_hw = x_size * SCALE / 2; _fv_hh = z_size * SCALE / 2\n"
        "    _sv_hw = y_size * SCALE / 2; _pv_hh = y_size * SCALE / 2\n"
        "    _total_h = 2*_margin + 3*_DIM_PAD + z_size*SCALE + y_size*SCALE\n"
        "    _y_off = max(0.0, (PAGE_H - _total_h) / 2)\n"
        "    _total_w = 4*_DIM_PAD + x_size*SCALE + y_size*SCALE + bbox_max*SCALE*0.7\n"
        "    _x_off = max(0.0, (PAGE_W - 2*_margin - TB_W - _total_w) / 2)\n"
        "    FV_X = _margin + _x_off + _DIM_PAD + _fv_hw\n"
        "    FV_Y = _y_off + _margin + _DIM_PAD + _fv_hh\n"
        "    PV_X = FV_X; PV_Y = FV_Y + _fv_hh + _DIM_PAD + _pv_hh\n"
        "    SV_X = FV_X + _fv_hw + _DIM_PAD + _sv_hw; SV_Y = FV_Y\n"
        "    _sv_right = SV_X + _sv_hw + _DIM_PAD\n"
        "    _iso_right = (PAGE_W - _margin) if (PV_Y - _pv_hh) > (_margin + 35) else (PAGE_W - TB_W - _margin)\n"
        "    ISO_X = _sv_right + max(0.0, _iso_right - _sv_right) / 2\n"
        "    ISO_Y = PV_Y\n"
        "    for _k, _v in [\n"
        "        ('STEP_FILE', repr(_STEP_FILE)), ('TITLE', repr(_TITLE)),\n"
        "        ('NUMBER', repr(_NUMBER)), ('TOLERANCE', repr(_TOLERANCE)),\n"
        "        ('DRAWN_BY', repr(_DRAWN_BY)),\n"
        "        ('SCALE', SCALE), ('PAGE_W', PAGE_W), ('PAGE_H', PAGE_H), ('TB_W', TB_W),\n"
        "        ('FV_X', f'{FV_X:.2f}'), ('FV_Y', f'{FV_Y:.2f}'),\n"
        "        ('PV_X', f'{PV_X:.2f}'), ('PV_Y', f'{PV_Y:.2f}'),\n"
        "        ('SV_X', f'{SV_X:.2f}'), ('SV_Y', f'{SV_Y:.2f}'),\n"
        "        ('ISO_X', f'{ISO_X:.2f}'), ('ISO_Y', f'{ISO_Y:.2f}'),\n"
        "        ('cx', f'{cx:.4f}'), ('cy', f'{cy:.4f}'), ('cz', f'{cz:.4f}'),\n"
        "        ('x_size', f'{x_size:.4f}'), ('y_size', f'{y_size:.4f}'), ('z_size', f'{z_size:.4f}'),\n"
        "        ('z_diams', repr(z_diams)), ('cross_diams', repr(cross_diams)),\n"
        "    ]:\n"
        "        cog.outl(f'{_k} = {_v}')\n"
        "except NameError:\n"
        "    pass\n"
        "# ]]]\n"
        f"{cog_output}\n"
        "# [[[end]]]"
    )

    projection = (
        "# ── Load and project ──────────────────────────────────────────────────────────\n"
        "part = import_step(STEP_FILE)\n"
        "part_s = part.scale(SCALE)\n"
        "cxs, cys, czs = cx * SCALE, cy * SCALE, cz * SCALE\n"
        "look_at = (cxs, cys, czs)\n"
        "DIST = max(x_size, y_size, z_size) * SCALE + 100\n"
        "ID   = DIST / math.sqrt(3)\n"
        "\n"
        "vis_f, hid_f = part_s.project_to_viewport((cxs, cys - DIST, czs), (0, 0, 1), look_at)\n"
        "front   = Compound(children=list(vis_f)).locate(Location((FV_X, FV_Y, 0)))\n"
        "front_h = Compound(children=list(hid_f)).locate(Location((FV_X, FV_Y, 0))) if list(hid_f) else None\n"
        "\n"
        "vis_p, hid_p = part_s.project_to_viewport((cxs, cys, czs + DIST), (0, 1, 0), look_at)\n"
        "plan   = Compound(children=list(vis_p)).locate(Location((PV_X, PV_Y, 0)))\n"
        "plan_h = Compound(children=list(hid_p)).locate(Location((PV_X, PV_Y, 0))) if list(hid_p) else None\n"
        "\n"
        "vis_s, hid_s = part_s.project_to_viewport((cxs + DIST, cys, czs), (0, 0, 1), look_at)\n"
        "side   = Compound(children=list(vis_s)).locate(Location((SV_X, SV_Y, 0)))\n"
        "side_h = Compound(children=list(hid_s)).locate(Location((SV_X, SV_Y, 0))) if list(hid_s) else None\n"
        "\n"
        "vis_i, hid_i = part_s.project_to_viewport((cxs + ID, cys + ID, czs + ID), (0, 0, 1), look_at)\n"
        "iso   = Compound(children=list(vis_i)).locate(Location((ISO_X, ISO_Y, 0)))\n"
        "iso_h = Compound(children=list(hid_i)).locate(Location((ISO_X, ISO_Y, 0))) if list(hid_i) else None\n"
        "\n"
        "# Front view coordinate helpers: world_X → page_X (+1), world_Z → page_Y (+1)\n"
        "def FX(x): return FV_X + (x - cx) * SCALE\n"
        "def FZ(z): return FV_Y + (z - cz) * SCALE\n"
        "# Side view coordinate helpers: world_Y → page_X (+1), world_Z → page_Y (+1)\n"
        "def SX(y): return SV_X + (y - cy) * SCALE\n"
        "def SZ(z): return SV_Y + (z - cz) * SCALE\n"
    )

    ann_section = (
        "# ── Annotations — edit freely; these survive `cog -r` ───────────────────────\n"
        + unannotated_note
        + "\n"
        "draft = draft_preset(font_size=3.0, decimal_precision=1)\n"
        "all_anns = []\n"
        "\n"
        "def _ann(obj, name): annotate(obj, name); all_anns.append(obj); return obj\n"
        "_fmt = lambda v: str(int(v)) if v == int(v) else f'{v:.1f}'\n"
        "\n"
        "# Overall height\n"
        "_ann(Dimension(\n"
        "    (FX(cx + x_size / 2) + 2, FZ(cz - z_size / 2), 0),\n"
        "    (FX(cx + x_size / 2) + 2, FZ(cz + z_size / 2), 0),\n"
        '    "right", 8, draft, label=f"{z_size:.0f}",\n'
        '), "dim_height")\n'
        "\n"
        "# Outer diameter (only for parts with cylindrical faces)\n"
        "if z_diams:\n"
        "    _ann(Dimension(\n"
        "        (FX(cx - z_diams[0] / 2), FZ(cz + z_size / 2) + 2, 0),\n"
        "        (FX(cx + z_diams[0] / 2), FZ(cz + z_size / 2) + 2, 0),\n"
        '        "above", 8, draft, label=f"ø{_fmt(z_diams[0])}",\n'
        '    ), "dim_od")\n'
        "\n"
        "# Title block\n"
        "tb = TitleBlock(\n"
        '    TITLE, NUMBER, drawing_scale=SCALE, general_tolerance=TOLERANCE,\n'
        '    designed_by=DRAWN_BY, revision="A", legal_owner="", width=TB_W, draft=draft,\n'
        ").locate(Location((PAGE_W - TB_W - 11, 11, 0)))\n"
        '_ann(tb, "title_block")\n'
    )

    lint_export = (
        "# ── Lint + Export ────────────────────────────────────────────────────────────\n"
        "set_page(PAGE_W, PAGE_H, margin=10)\n"
        "issues = lint_drawing(all_anns, drawing_scale=SCALE)\n"
        "if issues:\n"
        "    for iss in issues:\n"
        '        print(f"  [{iss.severity}] {iss.code}: {iss.message}")\n'
        "else:\n"
        '    print("Lint: OK")\n'
        "\n"
        "import os as _os\n"
        "_stem = _os.path.splitext(__file__)[0]\n"
        "_blk  = Color(0, 0, 0)\n"
        "_grey = Color(0.5, 0.5, 0.5)\n"
        "_blue = Color(0, 0.2, 0.7)\n"
        "\n"
        "svg = ExportSVG(margin=10)\n"
        'svg.add_layer("part",   line_color=_blk,  line_weight=0.5)\n'
        'svg.add_layer("hidden", line_color=_grey, line_weight=0.25, line_type=LineType.HIDDEN)\n'
        'svg.add_layer("dims",   line_color=_blue, fill_color=_blue, line_weight=0.05)\n'
        "for _v in (front, plan, side, iso):\n"
        '    if _v: svg.add_shape(_v, layer="part")\n'
        "for _v in (front_h, plan_h, side_h, iso_h):\n"
        '    if _v: svg.add_shape(_v, layer="hidden")\n'
        "for _ann_obj in all_anns:\n"
        '    svg.add_shape(_ann_obj, layer="dims")\n'
        'svg.write(_stem + ".svg")\n'
        "# Fix SVG to full page size (ExportSVG crops to content by default)\n"
        "import re as _re\n"
        "from pathlib import Path as _Path\n"
        '_svgdata = _Path(_stem + ".svg").read_text()\n'
        "_svgdata = _re.sub(r'width=\"[^\"]*\"', f'width=\"{PAGE_W:.3f}mm\"', _svgdata, count=1)\n"
        "_svgdata = _re.sub(r'height=\"[^\"]*\"', f'height=\"{PAGE_H:.3f}mm\"', _svgdata, count=1)\n"
        "_svgdata = _re.sub(r'viewBox=\"[^\"]*\"', f'viewBox=\"0 -{PAGE_H:.3f} {PAGE_W:.3f} {PAGE_H:.3f}\"', _svgdata, count=1)\n"
        '_Path(_stem + ".svg").write_text(_svgdata)\n'
        'print(f"SVG → {_stem}.svg")\n'
        "\n"
        "dxf = ExportDXF()\n"
        'dxf.add_layer("part",   line_weight=0.5)\n'
        'dxf.add_layer("hidden", line_weight=0.25)\n'
        'dxf.add_layer("dims",   line_weight=0.05)\n'
        "for _v in (front, plan, side, iso):\n"
        '    if _v: dxf.add_shape(_v, layer="part")\n'
        "for _v in (front_h, plan_h, side_h, iso_h):\n"
        '    if _v: dxf.add_shape(_v, layer="hidden")\n'
        "for _ann_obj in all_anns:\n"
        '    dxf.add_shape(_ann_obj, layer="dims")\n'
        'dxf.write(_stem + ".dxf")\n'
        'print(f"DXF → {_stem}.dxf")\n'
    )

    _tq = '"""'
    _safe_doc_title = a.title.replace(_tq, "'''")
    _safe_doc_number = a.number.replace(_tq, "'''")
    header = (
        f"#!/usr/bin/env python3\n"
        f'"""\n'
        f"{_safe_doc_title} — Technical drawing ({_safe_doc_number}).\n"
        f"\n"
        f"Auto-generated by make-drawing. Edit freely — the cog block at the top\n"
        f"re-computes layout when the STEP file changes. Annotations are yours to keep.\n"
        f"\n"
        f"Run drawing:    uv run python {py_name}\n"
        f"Update layout:  cog -r {py_name}   (pip install cogapp)\n"
        f'"""\n'
        f"import math\n"
        f"from build123d import import_step, Compound, Location, Color, LineType, ExportSVG, ExportDXF\n"
        f"from build123d_drafting import Dimension, Leader, TitleBlock, annotate, draft_preset, set_page, lint_drawing\n"
        f"\n"
        f"# ── Layout (auto-updated by cog) ──────────────────────────────────────────────\n"
        f"# Edit _STEP_FILE etc. in the cog block below, then run:\n"
        f"#   cog -r {py_name}\n"
    )

    content = "\n".join([header, cog_block, "", projection, ann_section, lint_export])
    Path(py_path).write_text(content)
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
        "--script",
        action="store_true",
        help="Write an editable .py drawing script instead of SVG+DXF",
    )
    args = ap.parse_args()

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
        )


if __name__ == "__main__":
    _cli()
