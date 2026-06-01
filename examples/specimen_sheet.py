"""Specimen sheet — a self-documenting legend of the drafting helpers.

Renders every annotation helper as a labelled cell whose caption is the *exact*
snippet that drew the symbol above it, so the picture can never drift from the
code. Everything is build123d geometry (text included), so the sheet exports to
SVG *and* DXF — it is itself a valid technical drawing, in line with the
project's CLAUDE.md invariant.

Run it::

    python examples/specimen_sheet.py            # writes specimen_sheet.svg
    python examples/specimen_sheet.py out.svg    # writes to out.svg

To rasterise the SVG to PNG, use any SVG renderer (resvg, Inkscape, a browser);
this script keeps to build123d only and emits vector output.
"""
from __future__ import annotations

import math

from build123d import (
    Align, Color, Compound, Draft, Edge, ExportSVG, LineType, Location,
    Rectangle, Text,
)
from build123d_drafting import (
    centerline, datum_feature, dim_linear, feature_control_frame, iso_title_block,
    leader, place_dims, surface_finish_mark,
)

# Geometry text falls back gracefully if these exact fonts are absent.
SANS, MONO = "Liberation Sans", "Liberation Mono"
GRID_PRE = (
    "from build123d import Draft\n"
    "from build123d_drafting import *\n"
    "draft = Draft(font_size=2.5, decimal_precision=2)\n"
)
BAND_PRE = (
    "from build123d import *\n"
    "from build123d_drafting import *\n"
    "draft = Draft(font_size=2.5, decimal_precision=2)\n"
)

# (name, snippet, result-var, routing-kind). routing-kind chooses SVG layers so
# closed loops stay hollow (FCF rings) while faces fill (leader arrow, datum
# triangle): shape_ink = .shape stroked+filled; shape_line = .shape stroked;
# lines_str = .lines stroked + .text filled; lines_ink = .lines stroked+filled.
GRID = [
    ("dim_linear", 'd = dim_linear(\n    (0, 0, 0), (30, 0, 0),\n    "below", 6, draft,\n    label="30.0")', "d", "shape_ink"),
    ("leader", 'ld = leader(\n    tip=(0, 0, 0),\n    elbow=(9, 7, 0),\n    label="Ø7.93 H7",\n    draft=draft)', "ld", "lines_ink"),
    ("centerline", 'cl = centerline(\n    (-12, 0, 0),\n    (12, 0, 0),\n    draft=draft)', "cl", "shape_line"),
    ("feature_control_frame", 'fcf = feature_control_frame(\n    "position", 0.5,\n    datums=("A", "B", "C"),\n    draft=draft,\n    diameter=True, modifier="M")', "fcf", "lines_str"),
    ("datum_feature", 'dt = datum_feature(\n    "A",\n    draft=draft)', "dt", "lines_ink"),
    ("surface_finish_mark", 'm = surface_finish_mark(\n    "1.6",\n    position=(0, 0, 0),\n    draft=draft)', "m", "lines_str"),
]

COLS = 3
W, H, GAP, VGAP = 88.0, 45.0, 10.0, 11.0
BAND_H, BAND_GAP = 66.0, 12.0
GCFG = dict(title_fs=3.2, code_fs=1.9, line_dy=2.7, spec_y=31.0, code_top=19.5)
BCFG = dict(title_fs=3.4, code_fs=1.9, line_dy=2.8, spec_y=44.0, code_top=24.0)


def _run(preamble: str, snippet: str, var: str):
    ns: dict = {}
    exec(preamble + snippet, ns)  # noqa: S102 — trusted, in-repo snippets
    return ns[var]


def _arrow(x0, y0, x1, y1):
    """Leader line with an arrowhead at (x1, y1) oriented along the line."""
    segs = [Edge.make_line((x0, y0, 0), (x1, y1, 0))]
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    ux, uy = -dx / n, -dy / n
    length, ang = 1.6, math.radians(22)
    for s in (+1, -1):
        th = s * ang
        rx = ux * math.cos(th) - uy * math.sin(th)
        ry = ux * math.sin(th) + uy * math.cos(th)
        segs.append(Edge.make_line((x1, y1, 0), (x1 + length * rx, y1 + length * ry, 0)))
    return segs


class _Sheet:
    """Accumulates geometry into five logical layers."""

    def __init__(self):
        self.border, self.stroke, self.ink, self.fill, self.code = [], [], [], [], []

    def cell(self, cx, base_y, cw, ch, name, snippet, pieces, *,
             title_fs, code_fs, line_dy, spec_y, code_top):
        mid = cx + cw / 2
        self.border.append(Rectangle(cw, ch).moved(Location((mid, base_y + ch / 2, 0))))
        self.fill.append(
            Text(name, font_size=title_fs, font=SANS, align=(Align.CENTER, Align.MAX))
            .moved(Location((mid, base_y + ch - 4.5, 0)))
        )

        spec = Compound(children=[s for s, _ in pieces])
        ctr = spec.bounding_box().center()
        tx, ty = mid - ctr.X, base_y + spec_y - ctr.Y
        for s, bucket in pieces:
            getattr(self, bucket).append(s.moved(Location((tx, ty, 0))))
        spec_bottom = spec.moved(Location((tx, ty, 0))).bounding_box().min.Y

        lines = [ln for ln in snippet.split("\n") if ln.strip()]
        texts = [Text(ln, font_size=code_fs, font=MONO, align=(Align.MIN, Align.MAX))
                 for ln in lines]
        maxw = max((t.bounding_box().size.X for t in texts), default=0.0)
        left_x = mid - maxw / 2.0
        for j, t in enumerate(texts):
            self.code.append(t.moved(Location((left_x, base_y + code_top - j * line_dy, 0))))

        self.stroke.extend(_arrow(mid, base_y + code_top + 1.2, mid, spec_bottom - 0.5))

    def export(self, svg_path: str):
        exp = ExportSVG(margin=10)
        exp.add_layer("border", line_color=Color(0.62, 0.62, 0.62), line_weight=0.3, line_type=LineType.CONTINUOUS)
        exp.add_layer("stroke", line_color=Color(0, 0, 0), line_weight=0.22, line_type=LineType.CONTINUOUS)
        exp.add_layer("ink", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.22, line_type=LineType.CONTINUOUS)
        exp.add_layer("fill", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.0, line_type=LineType.CONTINUOUS)
        exp.add_layer("code", line_color=Color(0.10, 0.16, 0.55), fill_color=Color(0.10, 0.16, 0.55), line_weight=0.0, line_type=LineType.CONTINUOUS)
        for bucket, layer in [(self.border, "border"), (self.stroke, "stroke"),
                              (self.ink, "ink"), (self.fill, "fill"), (self.code, "code")]:
            exp.add_shape(Compound(children=bucket), layer=layer)
        exp.write(svg_path)


def _route(res, kind):
    if kind == "shape_ink":
        return [(res.shape, "ink")]
    if kind == "shape_line":
        return [(res.shape, "stroke")]
    if kind == "lines_str":
        return [(res.lines, "stroke"), (res.text, "fill")]
    if kind == "lines_ink":
        return [(res.lines, "ink"), (res.text, "fill")]
    raise ValueError(kind)


def build_sheet() -> _Sheet:
    sheet = _Sheet()

    # --- grid: the core annotation symbols (3 x 2) ---
    for i, (name, snippet, var, kind) in enumerate(GRID):
        col, row = i % COLS, i // COLS
        base_y = BAND_H + BAND_GAP + (1 - row) * (H + VGAP)
        res = _run(GRID_PRE, snippet, var)
        sheet.cell(col * (W + GAP), base_y, W, H, name, snippet, _route(res, kind), **GCFG)

    band_w = (COLS * W + (COLS - 1) * GAP - GAP) / 2.0

    # --- band cell 1: place_dims on a part outline ---
    pd_snip = ('dft = Draft(font_size=1.6, decimal_precision=1)\n'
               'part = Rectangle(36, 20)\n'
               'dims = place_dims([\n'
               '    ((-18, -10, 0), (18, -10, 0), "below", "36"),\n'
               '    ((-18, -10, 0), (0, -10, 0), "below", "18"),\n'
               '    ((18, -10, 0), (18, 10, 0), "right", "20"),\n'
               '], dft, base_distance=5)')
    ns: dict = {}
    exec(BAND_PRE + pd_snip, ns)  # noqa: S102
    pd_pieces = [
        (Compound(children=list(ns["part"].edges())), "stroke"),
        (Compound(children=[d.shape for d in ns["dims"]]), "ink"),
    ]
    sheet.cell(0.0, 0.0, band_w, BAND_H, "place_dims", pd_snip, pd_pieces, **BCFG)

    # --- band cell 2: iso_title_block ---
    tb_snip = ('tb = iso_title_block(\n'
               '    "BRACKET", "DWG-014",\n'
               '    scale="1:2", material="AL 6061",\n'
               '    designed_by="P. Fremantle",\n'
               '    date="2026-06-01", width=135,\n'
               '    draft=Draft(font_size=2.0,\n'
               '                decimal_precision=2))')
    tb = _run(BAND_PRE, tb_snip, "tb")
    sheet.cell(band_w + GAP, 0.0, band_w, BAND_H, "iso_title_block", tb_snip,
               [(tb.lines, "stroke"), (tb.text, "fill")], **BCFG)

    # --- header ---
    top = BAND_H + BAND_GAP + 2 * H + VGAP
    sheet.fill.append(
        Text("build123d drafting helpers — specimen sheet", font_size=4.6, font=SANS,
             align=(Align.MIN, Align.MAX)).moved(Location((0, top + 15.0, 0)))
    )
    for k, ln in enumerate(GRID_PRE.strip().split("\n")):
        sheet.code.append(
            Text(ln, font_size=1.8, font=MONO, align=(Align.MIN, Align.MAX))
            .moved(Location((2.0, top + 9.0 - k * 2.5, 0)))
        )
    return sheet


def write_specimen_sheet(svg_path: str = "specimen_sheet.svg") -> str:
    """Build the specimen sheet and write it to *svg_path*. Returns the path."""
    build_sheet().export(svg_path)
    return svg_path


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "specimen_sheet.svg"
    print("wrote", write_specimen_sheet(out))
