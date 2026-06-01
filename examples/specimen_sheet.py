"""Specimen sheet — an A3 technical drawing that catalogues the helpers.

The sheet is itself a real drawing: an A3 frame, an ``iso_title_block()`` title
block, and every specimen pointed at by a ``leader()`` carrying the helper's
name — i.e. it is laid out and annotated *with the helpers it documents*. Symbol
drafts come from ``draft_preset()``, and each cell is captioned with the exact
snippet that drew it. All geometry, so it exports to SVG *and* DXF and scales
like any technical drawing.

    python examples/specimen_sheet.py            # writes specimen_sheet.svg
    python examples/specimen_sheet.py out.svg
"""
from __future__ import annotations

from build123d import (
    Align, Color, Compound, Edge, ExportSVG, LineType, Location, Pos, Rectangle, Text,
)
from build123d_drafting import (
    centerline, datum_feature, dim_linear, draft_preset, feature_control_frame,
    iso_title_block, leader, place_dims, surface_finish_mark,
)

MONO = "Liberation Mono"
SYM_DRAFT = draft_preset(font_size=2.4, decimal_precision=1)
LBL_DRAFT = draft_preset(font_size=3.4, decimal_precision=1)
CODE_FS, LINE_DY, BLUE = 2.2, 3.0, Color(0.10, 0.16, 0.55)

# A3 landscape, 420 x 297, centred on the origin.
PW, PH, MARGIN = 420.0, 297.0, 6.0

# (name, snippet, var, routing-kind, (cell_x, cell_y))
SPECS = [
    ("dim_linear",
     'd = dim_linear(\n    (0,0,0), (30,0,0),\n    "below", 6, draft,\n    label="30.0")', "d", "shape_ink", (-135, 100)),
    ("leader",
     'ld = leader(\n    (0,0,0), (9,7,0),\n    "Ø7.93 H7", draft)', "ld", "lines_ink", (0, 100)),
    ("centerline",
     'cl = centerline(\n    (-12,0,0),\n    (12,0,0), draft)', "cl", "shape_line", (135, 100)),
    ("feature_control_frame",
     'fcf = feature_control_frame(\n    "position", 0.5,\n    ("A","B","C"), draft,\n    diameter=True, modifier="M")', "fcf", "lines_str", (-135, 34)),
    ("datum_feature",
     'dt = datum_feature("A", draft)', "dt", "lines_ink", (0, 34)),
    ("surface_finish_mark",
     'm = surface_finish_mark(\n    "1.6", (0,0,0),\n    draft=draft)', "m", "lines_str", (135, 34)),
]

PRE = ("from build123d import *\nfrom build123d_drafting import *\n"
       "draft = draft_preset(font_size=2.4, decimal_precision=1)\n")

border, stroke, ink, fill, code = [], [], [], [], []
BUCKET = {"stroke": stroke, "ink": ink, "fill": fill}


def run(snippet, var):
    ns: dict = {}
    exec(PRE + snippet, ns)  # noqa: S102 — trusted in-repo snippets
    return ns[var]


def route(res, kind):
    if kind == "shape_ink":
        return [(res.shape, "ink")]
    if kind == "shape_line":
        return [(res.shape, "stroke")]
    if kind == "lines_str":
        return [(res.lines, "stroke"), (res.text, "fill")]
    if kind == "lines_ink":
        return [(res.lines, "ink"), (res.text, "fill")]
    raise ValueError(kind)


def place_code(snippet, cx, top_y):
    lines = [ln for ln in snippet.split("\n") if ln.strip()]
    texts = [Text(ln, font_size=CODE_FS, font=MONO, align=(Align.MIN, Align.MAX)) for ln in lines]
    maxw = max((t.bounding_box().size.X for t in texts), default=0.0)
    for j, t in enumerate(texts):
        code.append(t.moved(Location((cx - maxw / 2.0, top_y - j * LINE_DY, 0))))


def label_with_leader(bb, name):
    """Dogfood leader(): point at the specimen and label it with the helper name."""
    lab = leader((bb.max.X, bb.max.Y, 0), (bb.max.X + 9, bb.max.Y + 8, 0), name, LBL_DRAFT)
    ink.append(lab.lines)   # arrow + shelf are faces
    fill.append(lab.text)


def add_cell(name, snippet, var, kind, cx, cy):
    pieces = route(run(snippet, var), kind)
    spec = Compound(children=[s for s, _ in pieces])
    ctr = spec.bounding_box().center()
    tx, ty = cx - ctr.X, cy - ctr.Y
    for s, b in pieces:
        BUCKET[b].append(s.moved(Location((tx, ty, 0))))
    bb = spec.moved(Location((tx, ty, 0))).bounding_box()
    label_with_leader(bb, name)
    place_code(snippet, cx, bb.min.Y - 5)


# --- page frame ---
def _rect_edges(w, h, cx=0.0, cy=0.0):
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [Edge.make_line((a[0], a[1], 0), (b[0], b[1], 0)) for a, b in zip(pts, pts[1:])]


border.extend(_rect_edges(PW - 2 * MARGIN, PH - 2 * MARGIN))           # inner frame
border.extend(_rect_edges(PW, PH))                                     # sheet edge

# --- iso_title_block (our helper) in the bottom-right corner ---
TB_W = 180.0
tb = iso_title_block(
    "SPECIMEN SHEET", "B3D-DH-1", scale="1:1", material="—",
    designed_by="build123d-drafting-helpers", date="2026-06-01",
    width=TB_W, draft=draft_preset(font_size=2.6, decimal_precision=1),
)
tb_loc = Location((PW / 2 - MARGIN - TB_W, -PH / 2 + MARGIN, 0))
stroke.append(tb.lines.moved(tb_loc))
fill.append(tb.text.moved(tb_loc))

# --- specimens ---
for name, snippet, var, kind, (cx, cy) in SPECS:
    add_cell(name, snippet, var, kind, cx, cy)

# --- place_dims composite, lower-left (clear of the title block) ---
pd = ("part = Rectangle(36,20) - Pos(0,-10)*Rectangle(16,8)\n"
      "dims = place_dims([\n"
      '    ((-8,-10,0),(8,-10,0),"below","16"),\n'
      '    ((-18,-10,0),(18,-10,0),"below","36"),\n'
      '    ((18,-10,0),(18,10,0),"right","20"),\n'
      "], draft, base_distance=5)")
ns: dict = {}
exec(PRE + pd, ns)  # noqa: S102
outline = Compound(children=list(ns["part"].edges()))
dims = Compound(children=[d.shape for d in ns["dims"]])
whole = Compound(children=[outline, dims])
ctr = whole.bounding_box().center()
loc = Location((-128 - ctr.X, -58 - ctr.Y, 0))
stroke.append(outline.moved(loc))
ink.append(dims.moved(loc))
bb = whole.moved(loc).bounding_box()
label_with_leader(bb, "place_dims")
place_code(pd, -128, bb.min.Y - 5)


def write_specimen_sheet(svg_path: str = "specimen_sheet.svg") -> str:
    exp = ExportSVG(margin=4)
    exp.add_layer("border", line_color=Color(0, 0, 0), line_weight=0.3, line_type=LineType.CONTINUOUS)
    exp.add_layer("stroke", line_color=Color(0, 0, 0), line_weight=0.2, line_type=LineType.CONTINUOUS)
    exp.add_layer("ink", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.2, line_type=LineType.CONTINUOUS)
    exp.add_layer("fill", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.0, line_type=LineType.CONTINUOUS)
    exp.add_layer("code", line_color=BLUE, fill_color=BLUE, line_weight=0.0, line_type=LineType.CONTINUOUS)
    for bucket, layer in [(border, "border"), (stroke, "stroke"), (ink, "ink"), (fill, "fill"), (code, "code")]:
        exp.add_shape(Compound(children=bucket), layer=layer)
    exp.write(svg_path)
    return svg_path


if __name__ == "__main__":
    import sys
    print("wrote", write_specimen_sheet(sys.argv[1] if len(sys.argv) > 1 else "specimen_sheet.svg"))
