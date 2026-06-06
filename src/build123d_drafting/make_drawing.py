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
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane

from build123d_drafting.helpers import (
    Centerline,
    Dimension,
    Leader,
    TitleBlock,
    annotate,
    draft_preset,
    format_drawing_scale,
    lint_drawing,
    place_dims,
    set_page,
)

_log = logging.getLogger(__name__)

_TB_W = 150.0
_MARGIN = 10.0
_DIM_PAD = 18.0


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

    Each entry is a dict with keys: diameter, area, cx, cy, cz, axis.
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


def choose_scale(x_size: float, y_size: float, z_size: float) -> tuple:
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
        h = _MARGIN + _DIM_PAD + y_size * SCALE + _DIM_PAD + z_size * SCALE + _DIM_PAD + _MARGIN
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

    z_cyls, cross_cyls = analyse_cylinders(part)
    z_diams = dedup_diams(z_cyls)
    cross_diams = dedup_diams(cross_cyls)

    _log.info("Z-axis diameters: %s", z_diams)
    if cross_diams:
        _log.info("Cross-hole diams: %s", cross_diams)

    SCALE, PAGE_W, PAGE_H, TB_W = choose_scale(x_size, y_size, z_size)
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

    def SX(y):
        return a.SV_X + (y - a.cy) * a.SCALE

    def SZ(z):
        return a.SV_Y + (z - a.cz) * a.SCALE

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
        # Centreline through the rotation axis — front and side views
        _ann(
            Centerline(
                (FX(a.cx), FZ(a.bb.min.Z) - 5, 0),
                (FX(a.cx), FZ(a.bb.max.Z) + 5, 0),
            ),
            "centerline_front",
        )
        _ann(
            Centerline(
                (SX(a.cy), SZ(a.bb.min.Z) - 5, 0),
                (SX(a.cy), SZ(a.bb.max.Z) + 5, 0),
            ),
            "centerline_side",
        )

    # Additional Z-axis bore leaders to the left of the front view
    left_edge = FX(a.bb.min.X)
    left_space = left_edge - a.margin
    if left_space >= a.DIM_PAD and len(a.z_diams) > 1:
        ldr_length = a.DIM_PAD * 0.6
        elbow_x = left_edge - ldr_length
        for i, d in enumerate(a.z_diams[1:4]):
            tip_z = FZ(a.cz) + (i - 1) * 10
            _ann(
                Leader(
                    tip=(FX(a.cx - d / 2), tip_z, 0),
                    elbow=(elbow_x, tip_z, 0),
                    label=f"ø{_fmt(d)}",
                    draft=draft,
                ),
                f"ldr_z{i}",
            )
    elif len(a.z_diams) > 1:
        _log.info("Additional diameters %s not annotated (insufficient left margin)", a.z_diams[1:])

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
            _ann(dim, f"dim_step_{col}")

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
        scale=format_drawing_scale(a.SCALE),
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
    fix_svg_page_size(svg_path, a.PAGE_W, a.PAGE_H)
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
    """Write an editable script at ``a.out + '.py'`` that calls make_drawing()."""
    py_path = a.out + ".py"
    py_name = Path(py_path).name

    cog_output = "\n".join([
        f"STEP_FILE = {a.step_file!r}",
        f"TITLE = {a.title!r}",
        f"NUMBER = {a.number!r}",
        f"TOLERANCE = {a.tolerance!r}",
        f"DRAWN_BY = {a.drawn_by!r}",
    ])

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
        f"from build123d_drafting import make_drawing\n"
        f"\n"
        f"# ── Config (auto-updated by cog) ──────────────────────────────────────────────\n"
    )

    run_section = (
        "\n"
        "# ── Generate drawing ──────────────────────────────────────────────────────────\n"
        "_stem = _os.path.splitext(__file__)[0]\n"
        "svg_path, dxf_path = make_drawing(\n"
        "    STEP_FILE,\n"
        "    out=_stem,\n"
        "    title=TITLE,\n"
        "    number=NUMBER,\n"
        "    tolerance=TOLERANCE,\n"
        "    drawn_by=DRAWN_BY,\n"
        ")\n"
        'print(f"SVG \\u2192 {svg_path}")\n'
        'print(f"DXF \\u2192 {dxf_path}")\n'
        "\n"
        "# ── Add custom annotations here ───────────────────────────────────────────────\n"
        "# Example:\n"
        "#   from build123d_drafting import Leader, Dimension, annotate, draft_preset\n"
        "#   draft = draft_preset(font_size=3.0)\n"
    )

    content = header + cog_block + run_section
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
