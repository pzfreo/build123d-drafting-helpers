"""Part drawing — the end-to-end workflow on a real part (M10 hex bolt + nut).

Unlike the specimen sheet (a catalogue of symbols), this shows how you actually
*make a drawing* with the helpers:

    build the 3D part  ->  project_to_viewport() views  ->  dimension with the
    helpers  ->  lint with find_interferences()  ->  export SVG/DXF

It's an A4 frame with an iso_title_block(), the bolt and nut as projected front
views, dimensioned with dim_linear()/leader() (drafts from draft_preset()), and
the layout is verified collision-free by lint() before export.

    python examples/part_drawing.py            # writes part_drawing.svg
"""
from __future__ import annotations

from types import SimpleNamespace

from build123d import (
    Align, BuildPart, BuildSketch, Circle, Color, Compound, Edge, ExportSVG,
    LineType, Location, Plane, RegularPolygon, Text, extrude,
)
from build123d_drafting import (
    dim_linear, draft_preset, find_interferences, iso_title_block, leader,
)

DRAFT = draft_preset(font_size=2.5, decimal_precision=1)
PW, PH, MARGIN = 297.0, 210.0, 6.0          # A4 landscape
AF, HEAD_H, DIA, LEN = 16.0, 6.4, 10.0, 40.0
NUT_AF, NUT_T, BORE = 16.0, 8.0, 8.5

part_v, hidden_v, dims_l, text_l, border = [], [], [], [], []
lint_items: list = []


# --- 3D parts (simplified — ISO 6410 style, no modelled threads) ------------
def _bolt():
    with BuildPart() as p:
        with BuildSketch(Plane.XY.offset(LEN)):
            RegularPolygon(radius=AF / 2, side_count=6, major_radius=False, rotation=90)
        extrude(amount=HEAD_H)
        with BuildSketch():
            Circle(DIA / 2)
        extrude(amount=LEN)
    return p.part


def _nut():
    with BuildPart() as p:
        with BuildSketch():
            RegularPolygon(radius=NUT_AF / 2, side_count=6, major_radius=False, rotation=90)
        extrude(amount=NUT_T)
        with BuildSketch():
            Circle(BORE / 2)
        extrude(amount=NUT_T, mode=__import__("build123d").Mode.SUBTRACT)
    return p.part


def _front_view(part, look_z, page_xy):
    """Project a front view (look along -Y) and move its centre to page_xy.
    Returns (translation_dx, translation_dy) so feature coords can be placed."""
    vis, hid = part.project_to_viewport((0, -200, look_z), (0, 0, 1), (0, 0, look_z))
    v = Compound(children=list(vis))
    ctr = v.bounding_box().center()
    dx, dy = page_xy[0] - ctr.X, page_xy[1] - ctr.Y
    loc = Location((dx, dy, 0))
    part_v.append(v.moved(loc))
    if hid:
        hidden_v.append(Compound(children=list(hid)).moved(loc))
    return dx, dy, look_z


def _dim(p1, p2, side, dist, label):
    d = dim_linear(p1, p2, side, dist, DRAFT, label=label)
    dims_l.append(d.shape)
    lint_items.append(d)


def _leader(tip, elbow, label):
    ld = leader(tip, elbow, label, DRAFT)
    dims_l.append(ld.lines)
    text_l.append(ld.text)
    lint_items.append(ld)


def _note_item(shapes, name):
    bb = Compound(children=shapes).bounding_box()
    lint_items.append(SimpleNamespace(
        label_bbox=(bb.min.X, bb.min.Y, bb.max.X, bb.max.Y),
        label_str=name, lines=None, shape=None))


# --- page frame + title block -----------------------------------------------
def _rect_edges(w, h, cx=0.0, cy=0.0):
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [Edge.make_line((a[0], a[1], 0), (b[0], b[1], 0)) for a, b in zip(pts, pts[1:])]


border.extend(_rect_edges(PW - 2 * MARGIN, PH - 2 * MARGIN))
border.extend(_rect_edges(PW, PH))

TB_W = 150.0
tb = iso_title_block(
    "M10 HEX BOLT & NUT", "B3D-DH-2", scale="1:1", material="Steel 8.8",
    general_tolerance="ISO 2768-m", designed_by="build123d-drafting-helpers",
    date="2026-06-01", width=TB_W, draft=draft_preset(font_size=2.4, decimal_precision=1),
)
tb_loc = Location((PW / 2 - MARGIN - TB_W, -PH / 2 + MARGIN, 0))
part_v.append(tb.lines.moved(tb_loc))
text_l.append(tb.text.moved(tb_loc))

# --- bolt: front view + dimensions ------------------------------------------
bx, by = -62.0, 10.0
bdx, bdy, _ = _front_view(_bolt(), LEN / 2, (bx, by))
# placed page_y = world_z - LEN/2 + bdy ; world z: shaft 0..LEN, head LEN..LEN+HEAD_H
y0 = -LEN / 2 + bdy                 # shaft start (world z = 0)
y_head = LEN / 2 + bdy             # head start (world z = LEN)
y_top = y_head + HEAD_H
xL, xR = bx - AF / 2, bx + AF / 2   # head spans across-flats
_dim((bx - DIA / 2, y0, 0), (bx - DIA / 2, y_head, 0), "left", 14, str(LEN))      # thread length
_dim((xL, y_top, 0), (xR, y_top, 0), "above", 8, str(AF))                         # head A/F
_leader((bx + DIA / 2, (y0 + y_head) / 2, 0), (bx + 34, (y0 + y_head) / 2 + 10, 0),
        f"M{int(DIA)} × 1.5")
_leader((xR, y_top - HEAD_H / 2, 0), (xR + 30, y_top + 6, 0), f"head {HEAD_H} thk")

# --- nut: front view + dimensions -------------------------------------------
nx, ny = 52.0, 10.0
ndx, ndy, _ = _front_view(_nut(), NUT_T / 2, (nx, ny))
ny0 = -NUT_T / 2 + ndy
ny1 = NUT_T / 2 + ndy
nxL, nxR = nx - NUT_AF / 2, nx + NUT_AF / 2
_dim((nxL, ny1, 0), (nxR, ny1, 0), "above", 8, str(NUT_AF))                       # A/F
_dim((nxR, ny0, 0), (nxR, ny1, 0), "right", 12, str(NUT_T))                       # thickness
_leader((nx, ny0, 0), (nx - 30, ny0 - 14, 0), f"⌀{BORE} thru (M{int(DIA)})")

# title block is a labelled feature too
_leader((tb_loc.position.X + 40, -PH / 2 + MARGIN + tb.bbox["height"], 0),
        (tb_loc.position.X + 30, -PH / 2 + MARGIN + tb.bbox["height"] + 14, 0),
        "iso_title_block")


def lint():
    """Lint the drawing's annotations with find_interferences() before export."""
    return find_interferences(lint_items)


def write_part_drawing(svg_path: str = "part_drawing.svg") -> str:
    exp = ExportSVG(margin=4)
    exp.add_layer("border", line_color=Color(0, 0, 0), line_weight=0.3, line_type=LineType.CONTINUOUS)
    exp.add_layer("part", line_color=Color(0, 0, 0), line_weight=0.35, line_type=LineType.CONTINUOUS)
    exp.add_layer("hidden", line_color=Color(0.5, 0.5, 0.5), line_weight=0.18, line_type=LineType.DASHED)
    exp.add_layer("dims", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.18, line_type=LineType.CONTINUOUS)
    exp.add_layer("text", line_color=Color(0, 0, 0), fill_color=Color(0, 0, 0), line_weight=0.0, line_type=LineType.CONTINUOUS)
    for bucket, layer in [(border, "border"), (part_v, "part"), (hidden_v, "hidden"),
                          (dims_l, "dims"), (text_l, "text")]:
        exp.add_shape(Compound(children=bucket), layer=layer)
    exp.write(svg_path)
    return svg_path


if __name__ == "__main__":
    import sys
    issues = lint()
    for i in issues:
        print(f"[{i.severity}] {i.code}: {i.message}")
    print(f"lint: {sum(i.severity == 'error' for i in issues)} error(s)")
    print("wrote", write_part_drawing(sys.argv[1] if len(sys.argv) > 1 else "part_drawing.svg"))
