"""Part drawing — the end-to-end workflow on a real part (M10 hex bolt + nut).

Unlike the specimen sheet (a catalogue of symbols), this shows how you actually
*make a drawing* with the helpers:

    build the 3D part  ->  project_to_viewport() views (front / top / side / iso)
    ->  dimension with the helpers  ->  lint with find_interferences()  ->  export

It's an A4 frame with an iso_title_block(); each part gets front/top/side views
plus an isometric, dimensioned with dim_linear()/leader()
(drafts from draft_preset()). The layout is verified collision-free by lint()
before export.

    python examples/part_drawing.py            # writes part_drawing.svg
"""
from __future__ import annotations

from types import SimpleNamespace

from build123d import (
    Align, BuildPart, BuildSketch, Circle, Color, Compound, Edge, ExportSVG,
    LineType, Location, Mode, Plane, RegularPolygon, Text, extrude,
)
from build123d_drafting import (
    dim_linear, draft_preset, find_interferences, iso_title_block, leader,
)

DRAFT = draft_preset(font_size=2.2, decimal_precision=1)
PW, PH, MARGIN = 297.0, 210.0, 6.0          # A4 landscape
AF, HEAD_H, DIA, LEN = 16.0, 6.4, 10.0, 40.0
NUT_AF, NUT_T, BORE = 16.0, 8.0, 8.5
SANS = "Liberation Sans"
D = 800.0                                    # camera distance

border, part_v, hidden_v, dims_l, text_l = [], [], [], [], []
lint_items: list = []
view_labels: list = []   # FRONT/TOP/SIDE/ISO labels — linted against the dim lines
view_boxes: list = []    # bbox of each projected view — labels must clear them


def _label_box(t, name="text"):
    bb = t.bounding_box()
    return SimpleNamespace(label_bbox=(bb.min.X, bb.min.Y, bb.max.X, bb.max.Y),
                           label_str=name, lines=None, shape=None)


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
        extrude(amount=NUT_T, mode=Mode.SUBTRACT)
    return p.part


def _project(part, origin, up, look):
    vis, hid = part.project_to_viewport(origin, up, look)
    return Compound(children=list(vis)), (Compound(children=list(hid)) if hid else None)


def _place(comp, cx, cy):
    ctr = comp.bounding_box().center()
    return comp.moved(Location((cx - ctr.X, cy - ctr.Y, 0)))


def _add_view(vis, hid, cx, cy, label):
    v = _place(vis, cx, cy)
    part_v.append(v)
    if hid is not None:
        hidden_v.append(_place(hid, cx, cy))
    bb = v.bounding_box()
    lbl = Text(label, font_size=2.6, font=SANS, align=(Align.CENTER, Align.MAX)
               ).moved(Location((cx, bb.min.Y - 3.0, 0)))
    text_l.append(lbl)
    view_labels.append(lbl)
    view_boxes.append((bb.min.X, bb.min.Y, bb.max.X, bb.max.Y))
    return bb


def _dim(p1, p2, side, dist, label):
    d = dim_linear(p1, p2, side, dist, DRAFT, label=label)
    dims_l.append(d.shape)
    lint_items.append(d)


def _leader(tip, elbow, label):
    ld = leader(tip, elbow, label, DRAFT)
    dims_l.append(ld.lines)
    text_l.append(ld.text)
    lint_items.append(ld)


def _draw_part(part, cz, gx, gy):
    """Third-angle front/top/side + isometric. Returns the placed FRONT bbox."""
    front = _project(part, (0, -D, cz), (0, 0, 1), (0, 0, cz))
    top = _project(part, (0, 0, D), (0, 1, 0), (0, 0, cz))
    side = _project(part, (D, 0, cz), (0, 0, 1), (0, 0, cz))
    iso = _project(part, (D, D, D), (0, 0, 1), (0, 0, cz))

    fb = front[0].bounding_box()
    fw, fh = fb.size.X, fb.size.Y
    sw = side[0].bounding_box().size.X
    th = top[0].bounding_box().size.Y

    iso_w = iso[0].bounding_box().size.X
    side_cx = gx + fw / 2 + 16 + sw / 2
    # FRONT | SIDE | ISO in a row, TOP above FRONT — keeps tall parts from
    # overlapping (an isometric stacked above the side view would collide).
    f_bb = _add_view(*front, gx, gy, "FRONT")
    _add_view(*top, gx, gy + fh / 2 + 16 + th / 2, "TOP")
    _add_view(*side, side_cx, gy, "SIDE")
    _add_view(*iso, side_cx + sw / 2 + 16 + iso_w / 2, gy, "ISO")
    return f_bb


# --- page frame + title block -----------------------------------------------
def _rect_edges(w, h, cx=0.0, cy=0.0):
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [Edge.make_line((a[0], a[1], 0), (b[0], b[1], 0)) for a, b in zip(pts, pts[1:])]


border.extend(_rect_edges(PW - 2 * MARGIN, PH - 2 * MARGIN))
border.extend(_rect_edges(PW, PH))

TB_W = 118.0
tb = iso_title_block(
    "M10 HEX BOLT & NUT", "B3D-DH-2", scale="1:1", material="Steel 8.8",
    general_tolerance="ISO 2768-m", designed_by="build123d-drafting-helpers",
    date="2026-06-01", width=TB_W, draft=draft_preset(font_size=2.4, decimal_precision=1),
)
tb_loc = Location((PW / 2 - MARGIN - TB_W, -PH / 2 + MARGIN, 0))
part_v.append(tb.lines.moved(tb_loc))
text_l.append(tb.text.moved(tb_loc))

# --- bolt: views + dimensions (front view) ----------------------------------
fb = _draw_part(_bolt(), LEN / 2, -104.0, 6.0)
y0, y_head, y_top = fb.min.Y, fb.max.Y - HEAD_H, fb.max.Y
xL, xR = fb.center().X - AF / 2, fb.center().X + AF / 2
_dim((xL - DIA / 2, y0, 0), (xL - DIA / 2, y_head, 0), "left", 12, str(LEN))   # thread length
_dim((xL, y_top, 0), (xR, y_top, 0), "above", 8, str(AF))                      # head A/F
# thread designation: leader from the shaft down into the clear area below the
# views (its label must not land over the SIDE view, which the annotation lint
# can't see — part geometry isn't a label).
_leader((fb.center().X + DIA / 2, y0 + 8, 0),
        (fb.center().X + 30, y0 - 20, 0), f"M{int(DIA)} × 1.5")

# --- nut: views + dimensions (front view) -----------------------------------
nb = _draw_part(_nut(), NUT_T / 2, 34.0, 6.0)
_dim((nb.min.X, nb.max.Y, 0), (nb.max.X, nb.max.Y, 0), "above", 8, str(NUT_AF))   # A/F
_dim((nb.max.X, nb.min.Y, 0), (nb.max.X, nb.max.Y, 0), "right", 8, str(NUT_T))   # thickness
_leader((nb.center().X, nb.min.Y, 0), (nb.center().X - 24, nb.min.Y - 14, 0),
        f"⌀{BORE} thru (M{int(DIA)})")

# the title block is a labelled feature too
_leader((tb_loc.position.X + 40, -PH / 2 + MARGIN + tb.bbox["height"], 0),
        (tb_loc.position.X + 30, -PH / 2 + MARGIN + tb.bbox["height"] + 16, 0),
        "iso_title_block")


def lint():
    """Lint the drawing's annotations with find_interferences() before export.

    Checks the dim/leader lines + labels against each other and against the
    FRONT/TOP/SIDE/ISO view labels, and — via ``obstacles`` — that no label lands
    over a projected view (the title block text is intentionally not a forbidden
    zone, since the iso_title_block leader is *meant* to point at it).
    """
    return find_interferences(
        lint_items + [_label_box(v) for v in view_labels],
        obstacles=view_boxes,
    )


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
    for i in lint():
        print(f"[{i.severity}] {i.code}: {i.message}")
    print(f"lint: {sum(i.severity == 'error' for i in lint())} error(s)")
    print("wrote", write_part_drawing(sys.argv[1] if len(sys.argv) > 1 else "part_drawing.svg"))
