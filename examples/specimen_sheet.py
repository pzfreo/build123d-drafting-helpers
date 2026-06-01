"""Specimen sheet — an A3 technical drawing that catalogues the helpers.

The sheet is itself a real drawing: an A3 frame, a ``TitleBlock`` title block,
and every specimen pointed at by a ``Leader`` carrying the helper's name — i.e.
it is laid out and annotated *with the helpers it documents*. Symbol drafts come
from ``draft_preset()``, and each cell is captioned with the exact snippet that
drew it. All geometry, so it exports to SVG *and* DXF and scales like any
technical drawing.

    python examples/specimen_sheet.py            # writes specimen_sheet.svg
    python examples/specimen_sheet.py out.svg
"""
from __future__ import annotations

from types import SimpleNamespace

from build123d import (
    Align, Color, Compound, Edge, ExportSVG, LineType, Location, Pos, Rectangle, Text,
)
from build123d_drafting import (
    Centerline, DatumFeature, Dimension, draft_preset, FeatureControlFrame,
    find_interferences, Leader, place_dims, SurfaceFinish, TitleBlock,
)

MONO, SANS = "Liberation Mono", "Liberation Sans"
SYM_DRAFT = draft_preset(font_size=2.4, decimal_precision=1)
LBL_DRAFT = draft_preset(font_size=3.0, decimal_precision=1)
CODE_FS, LINE_DY, BLUE = 2.1, 2.9, Color(0.10, 0.16, 0.55)

# A3 landscape, 420 x 297, centred on the origin.
PW, PH, MARGIN = 420.0, 297.0, 6.0

# 4-column × 3-row grid (12 slots). Columns at x = -150 / -50 / 50 / 150; rows at
# y = 102 / 30 / -44. The rightmost column's name labels point *left* so they stay
# inside the frame. place_dims fills the col2/row3 slot; the rest are SPECS below.
# (name, snippet, var, (cell_x, cell_y))
SPECS = [
    ("Dimension",
     'd = Dimension(\n    (0,0,0), (30,0,0),\n    "below", 6, draft,\n    label="30.0")', "d", (-150, 102)),
    ("Leader",
     'ld = Leader(\n    (0,0,0), (9,7,0),\n    "Ø7.93 H7", draft)', "ld", (-50, 102)),
    ("Centerline",
     'cl = Centerline(\n    (-12,0,0),\n    (12,0,0), draft)', "cl", (50, 102)),
    ("DatumFeature",
     'dt = DatumFeature("A",\n    draft)', "dt", (150, 102)),
    ("FeatureControlFrame",
     'fcf = FeatureControlFrame(\n    "position", 0.5,\n    ("A","B","C"), draft,\n    diameter=True, modifier="M")', "fcf", (-150, 30)),
    ("SurfaceFinish",
     'm = SurfaceFinish(\n    "1.6", (0,0,0),\n    draft=draft)', "m", (-50, 30)),
    ("DatumTarget",
     'dt2 = DatumTarget(\n    "A1", area_label="Ø6",\n    draft=draft)', "dt2", (50, 30)),
    ("HoleCallout",
     'hc = HoleCallout(\n    8.5, count=4,\n    through=True,\n    draft=draft)', "hc", (150, 30)),
    ("CompositeFeatureControlFrame",
     'cfcf = CompositeFeatureControlFrame(\n    "position",\n    [{"tolerance":0.25,\n      "datums":("A","B","C"),\n      "diameter":True},\n     {"tolerance":0.1,\n      "datums":("A",), "diameter":True}],\n    draft)', "cfcf", (-150, -44)),
    ("Dimension basic",
     'db = Dimension(\n    (0,0,0), (24,0,0),\n    "below", 6, draft,\n    label="24", basic=True)', "db", (50, -44)),
    ("Leader all-around",
     'la = Leader(\n    (0,0,0), (9,7,0),\n    "0.2", draft,\n    all_around=True)', "la", (150, -44)),
]

PRE = ("from build123d import *\nfrom build123d_drafting import *\n"
       "draft = draft_preset(font_size=2.4, decimal_precision=1)\n")

border, stroke, ink, fill, code = [], [], [], [], []

# Page-level annotations to lint with find_interferences: the leader callouts
# (which carry lines + a label) and the code/note text blocks (label boxes).
lint_items: list = []


def _text_box_item(shapes, name):
    """A lint item carrying just a label box (no lines) for a block of text."""
    bb = Compound(children=shapes).bounding_box()
    lint_items.append(SimpleNamespace(
        label_bbox=(bb.min.X, bb.min.Y, bb.max.X, bb.max.Y),
        label=name, segments=[], elbow=None))


def run(snippet, var):
    ns: dict = {}
    exec(PRE + snippet, ns)  # noqa: S102 — trusted in-repo snippets
    return ns[var]


def place_code(snippet, cx, top_y):
    lines = [ln for ln in snippet.split("\n") if ln.strip()]
    texts = [Text(ln, font_size=CODE_FS, font=MONO, align=(Align.MIN, Align.MAX)) for ln in lines]
    maxw = max((t.bounding_box().size.X for t in texts), default=0.0)
    placed = [t.moved(Location((cx - maxw / 2.0, top_y - j * LINE_DY, 0)))
              for j, t in enumerate(texts)]
    code.extend(placed)
    _text_box_item(placed, "code")


def label_with_leader(bb, name, left=False):
    """Dogfood Leader(): point at the specimen and label it with the helper name.

    For the rightmost column pass ``left=True`` so the label hangs to the left of
    the cell and stays inside the sheet frame.
    """
    if left:
        lab = Leader((bb.min.X, bb.max.Y, 0), (bb.min.X - 9, bb.max.Y + 8, 0), name, LBL_DRAFT)
    else:
        lab = Leader((bb.max.X, bb.max.Y, 0), (bb.max.X + 9, bb.max.Y + 8, 0), name, LBL_DRAFT)
    ink.append(lab)   # arrow + shelf + text are all faces on one sketch
    lint_items.append(lab)


def add_cell(name, snippet, var, cx, cy):
    obj = run(snippet, var)
    ctr = obj.bounding_box().center()
    tx, ty = cx - ctr.X, cy - ctr.Y
    placed = obj.moved(Location((tx, ty, 0)))
    ink.append(placed)
    bb = placed.bounding_box()
    label_with_leader(bb, name, left=cx > 90)   # rightmost column labels point left
    place_code(snippet, cx, bb.min.Y - 5)


# --- page frame ---
def _rect_edges(w, h, cx=0.0, cy=0.0):
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [Edge.make_line((a[0], a[1], 0), (b[0], b[1], 0)) for a, b in zip(pts, pts[1:])]


border.extend(_rect_edges(PW - 2 * MARGIN, PH - 2 * MARGIN))           # inner frame
border.extend(_rect_edges(PW, PH))                                     # sheet edge

# --- TitleBlock (our helper) in the bottom-right corner ---
TB_W = 180.0
tb = TitleBlock(
    "SPECIMEN SHEET", "B3D-DH-1", scale="1:1", material="—",
    designed_by="build123d-drafting-helpers", date="2026-06-01",
    width=TB_W, draft=draft_preset(font_size=2.6, decimal_precision=1),
)
TB_X0, TB_Y0 = PW / 2 - MARGIN - TB_W, -PH / 2 + MARGIN
tb_loc = Location((TB_X0, TB_Y0, 0))
ink.append(tb.moved(tb_loc))

# the title block is itself a specimen — call it out with a leader too, and
# note *why* it exists (the nuance vs build123d's TechnicalDrawing title box).
# The leader runs up the left; the name + note sit to its right so the leader
# line never crosses the text (verified by lint() below).
tb_top = TB_Y0 + tb.bounding_box().size.Y
tb_lab = Leader((TB_X0 + 44, tb_top, 0), (TB_X0 + 46, tb_top + 15, 0),
                "TitleBlock", LBL_DRAFT)
ink.append(tb_lab)
lint_items.append(tb_lab)
note = []
for k, ln in enumerate((
        "standalone alternative to TechnicalDrawing's title box —",
        "positionable, layer-routable, with ISO 7200 fields",
        "(material + general tolerance)")):
    t = Text(ln, font_size=2.3, font=SANS, align=(Align.MIN, Align.MAX)
             ).moved(Location((TB_X0 + 92, tb_top + 18 - k * 3.2, 0)))
    fill.append(t)
    note.append(t)
_text_box_item(note, "title-block note")

# --- specimens ---
for name, snippet, var, (cx, cy) in SPECS:
    add_cell(name, snippet, var, cx, cy)

# --- place_dims composite, grid col2 / row3 ---
pd = ("part = Rectangle(36,20) - Pos(0,-10)*Rectangle(16,8)\n"
      "dims = place_dims([\n"
      '    ((-8,-10,0),(8,-10,0),"below","16"),\n'
      '    ((-18,-10,0),(18,-10,0),"below","36"),\n'
      '    ((18,-10,0),(18,10,0),"right","20"),\n'
      "], draft, base_distance=5)")
ns: dict = {}
exec(PRE + pd, ns)  # noqa: S102
outline = Compound(children=list(ns["part"].edges()))
dims = Compound(children=list(ns["dims"]))
whole = Compound(children=[outline, dims])
ctr = whole.bounding_box().center()
loc = Location((-50 - ctr.X, -44 - ctr.Y, 0))
stroke.append(outline.moved(loc))
ink.append(dims.moved(loc))
bb = whole.moved(loc).bounding_box()
label_with_leader(bb, "place_dims")
place_code(pd, -50, bb.min.Y - 5)


def lint():
    """Lint the sheet's own annotations — dogfood find_interferences().

    Checks every leader callout's line against every label / code / note box, so
    a leader that strikes through text is caught. Returns a list[LintIssue].
    """
    return find_interferences(lint_items)


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
    issues = lint()
    for i in issues:
        print(f"[{i.severity}] {i.code}: {i.message}")
    print(f"lint: {sum(i.severity == 'error' for i in issues)} error(s), "
          f"{sum(i.severity == 'warning' for i in issues)} warning(s)")
    print("wrote", write_specimen_sheet(sys.argv[1] if len(sys.argv) > 1 else "specimen_sheet.svg"))
