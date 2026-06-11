# build123d-drafting-helpers

[![CI](https://github.com/pzfreo/build123d-drafting-helpers/actions/workflows/ci.yml/badge.svg)](https://github.com/pzfreo/build123d-drafting-helpers/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/build123d-drafting-helpers.svg)](https://pypi.org/project/build123d-drafting-helpers/)
[![Python](https://img.shields.io/pypi/pyversions/build123d-drafting-helpers.svg)](https://pypi.org/project/build123d-drafting-helpers/)
[![License](https://img.shields.io/pypi/l/build123d-drafting-helpers.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Third-party drawing-annotation helpers for [build123d](https://github.com/gumyr/build123d) — pure Python, no MCP dependency. Not affiliated with the upstream build123d project.

Dimensions, leaders, centrelines, GD&T symbols, a title block, and a geometry-precise lint —
all as build123d geometry, so a drawing exports to SVG **and** DXF and scales like any
technical drawing. The sheet below is itself produced by the library, with every helper drawn
and labelled by the helpers it documents ([`examples/specimen_sheet.py`](examples/specimen_sheet.py)):

![Specimen sheet of the drafting helpers](docs/specimen_sheet.png)

```python
from build123d_drafting import (
    Dimension, place_dims, place_labels, Centerline,
    Leader, view_axes, lint_drawing, find_interferences, find_overlaps,
    format_drawing_scale,
)
```

Every annotation builder is a native build123d `BaseSketchObject` subclass — the
returned object **is** a `Sketch`. It composes inside a `BuildSketch`, combines with
`+` / `-`, can be `.moved()`, exports directly, and is queried with `.faces()` /
`.bounding_box()`. All geometry — frame boxes, witness lines, GD&T glyphs, and text —
is rendered as thin filled *faces* on a single ink layer (no `.lines` / `.text` split).

The install name is `build123d-drafting-helpers`; the import name is `build123d_drafting`.

> **Not to be confused with [`baverman/build123d_draft`](https://github.com/baverman/build123d_draft)** — that project is *modelling shortcuts* (slot helpers, rotation aliases, `build_line` wrappers), not drafting/annotation. Different scope despite the similar name.

## Installation

```
pip install build123d-drafting-helpers
```

Or with uv:

```
uv add build123d-drafting-helpers
```

Requires `build123d >= 0.9.0` and Python ≥ 3.10.

## Automated drawing generation

For a fully automatic STEP → SVG + DXF pipeline with no drawing code required, use
`make_drawing()` or the bundled `make-drawing` CLI. It analyses the part geometry,
chooses a scale and page size, projects four views, and annotates diameter callouts,
centrelines, and a title block automatically.

### CLI

```
make-drawing part.step
make-drawing part.step --title "BRACKET" --number DWG-042 --drawn-by "Paul"
make-drawing part.step --out drawings/bracket   # explicit output prefix
make-drawing part.step --tolerance "ISO 2768-f"
```

All options:

| Flag | Default | Description |
|------|---------|-------------|
| `--title` | stem of STEP file | Part title in the title block |
| `--number` | `DWG-001` | Drawing number |
| `--tolerance` | `ISO 2768-m` | General tolerance field |
| `--drawn-by` | _(empty)_ | Designer name |
| `--out` | STEP file stem | Output path prefix (`.svg` and `.dxf` appended) |
| `--scale` | auto | Drawing-scale override, e.g. `5` for 5:1 or `0.5` for 1:2 |
| `--page` | auto | Page-size override: `A4`…`A0` or `WIDTHxHEIGHT` in mm |
| `--script` | — | Write an editable `.py` script instead of SVG+DXF (see below) |

### Python API

`make_drawing()` accepts either a STEP file path or an in-memory build123d object
(`Part`, `Solid`, `Compound`, …) as its first argument:

```python
from build123d import Box
from build123d_drafting import make_drawing

# From a STEP file
svg_path, dxf_path = make_drawing(
    "part.step",
    title="BRACKET",
    number="DWG-042",
    drawn_by="Paul",
)

# Directly from a build123d object — no STEP file needed
part = Box(40, 30, 10)
svg_path, dxf_path = make_drawing(part, out="bracket", title="BRACKET")
```

Scale and page size are chosen automatically; pass `scale=` and/or `page=` to
override when the heuristic can't know the answer (workshop printer, company
sheet standard):

```python
svg_path, dxf_path = make_drawing(part, out="bracket", scale=5, page="A3")
```

`page` accepts an ISO name (`"A4"`…`"A0"`), a `"WIDTHxHEIGHT"` string in mm, or a
`(width, height)` tuple. Give only one of the two and the other is chosen to fit.
Both overrides also work on `build_drawing()` (below).

When a build123d object is passed without `out=`, the files default to
`drawing.svg` / `drawing.dxf` in the current directory. (The `--script` /
`generate_script()` workflow is STEP-only — the generated script reloads geometry
from disk, so it cannot embed a live object.)

### Customising a drawing — the `Drawing` builder

`make_drawing()` is a one-shot wrapper. To edit a drawing *before* it is written —
add or remove dimensions, add a section or auxiliary view — call `build_drawing()`,
which returns a live `Drawing` with the standard four views and automatic
annotations already in place but **not yet exported**:

```python
from build123d_drafting import build_drawing, Leader

dwg = build_drawing(part, out="bracket", title="BRACKET", number="DWG-042")

# dwg.views        {"front", "plan", "side", "iso"} → (visible, hidden) compounds
# dwg.annotations  mutable list of annotation objects
# dwg.draft / dwg.scale / dwg.page_w / dwg.page_h
# dwg.at(view, x, y, z)  → page point (px, py, 0) mapped from world coordinates

dwg.add(Leader(tip=dwg.at("front", 10, 0, 5), elbow=(8, 40, 0),
               label="ø4 BORE", draft=dwg.draft), "ldr_bore")
dwg.remove("dim_od")                       # drop an automatic dimension by name

svg_path, dxf_path = dwg.export("bracket")
```

`make_drawing(...)` is exactly `build_drawing(...).export()`.

Add a section or auxiliary view with `add_view()` — supply a (pre-cut) shape, a
camera in scaled space (compose it from `dwg.look_at` and `dwg.dist`), an up
vector, and a page position; it returns that view's coordinate helper:

```python
look = dwg.look_at
bottom = (look[0], look[1], look[2] - dwg.dist)
vc = dwg.add_view("bottom", part, bottom, (0, 1, 0), (260.0, 60.0))
```

### Editable script

`--script` (or `generate_script()`) writes a `.py` file that calls `build_drawing()`
with all parameters wired up, an editable customisation block, and an `export()` call
at the end. Run it as-is for the standard drawing, or open it and add annotations in
the customisation block — they run before export, so they land in the output:

```
make-drawing part.step --script
python part.py          # produces part.svg + part.dxf
```

### Scale selection

`choose_scale(x_size, y_size, z_size, scale=None, page=None)` returns
`(SCALE, PAGE_W, PAGE_H, TB_W)` — the same thresholds used by `make_drawing()`,
including ISO 5455 enlargement scales (10:1, 5:1) for small parts. Call it directly
when writing your own drawing script to stay consistent with the automated pipeline:

```python
from build123d_drafting import choose_scale

SCALE, PAGE_W, PAGE_H, TB_W = choose_scale(x_size, y_size, z_size)
```

`scale=` pins the scale (the page is chosen as the smallest sheet that fits);
`page=` pins the sheet (the scale is chosen as the largest that fits); with both
given they are returned as-is.

---

## Helpers

### `draft_preset(font_size=2.5, decimal_precision=2, **overrides)`

A `Draft` tuned for clean output. build123d's `Draft` defaults draw heavy arrowheads
(`arrow_length=3.0` mm) and thick dimension lines (`line_width=0.5` mm) that look clumsy at
small fonts; this scales the arrowhead to the font (`0.9 * font_size`) and thins the line
(`0.1` mm). Use it as the starting point for every helper that takes a `draft`.

```python
draft = draft_preset(font_size=1.6)                       # light, font-scaled arrows
draft = draft_preset(font_size=3.0, line_width=0.15)      # override any field
```

(On-screen/SVG stroke thickness is separate — set it on the exporter via
`ExportSVG.add_layer(line_weight=...)`.)

---

### `Dimension(p1, p2, side, distance, draft, label=None, tolerance=None, label_offset_x=0.0, basic=False)`

`ExtensionLine` wrapper with named placement side instead of raw signed offset.

```python
draft = Draft(font_size=2.5, decimal_precision=1)
dim = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
```

`side` accepts `"above"` / `"below"` / `"left"` / `"right"` or an explicit world-direction vector.
The correct `offset` sign is computed from the path direction's right-hand normal — no guessing.

`label_offset_x` shifts the label along the dim line (mm, signed). Use it to move the label away
from a crossing centreline without changing the dim position or geometry. Positive shifts toward p2.

```python
# Label crosses bore centreline at x=0 — shift it right by 15 mm
dim = Dimension((-10, 0, 0), (10, 0, 0), "above", 8, draft, label="Ø5.0 H8", label_offset_x=15)
```

`basic=True` draws a rectangle around the value, marking it a *basic* (theoretically-exact)
dimension per ISO 1101 / ASME Y14.5.

The object is a `Sketch` with metadata attributes `.label`, `.measured_length`,
`.dim_level_y`, `.is_basic`, `.segments`, and `.label_bbox` — the precise text extent
`(min_x, min_y, max_x, max_y)` used by `lint_drawing` / `find_interferences` and
`place_labels` for centreline-overlap detection.

---

### `place_dims(specs, draft, base_distance=8.0, tier_spacing=None)`

Build a stack of parallel dims with automatically assigned offsets. No need to compute `distance` manually.

```python
dims = place_dims([
    ((-30, 0, 0), (30, 0, 0), "above", "60"),   # full width  → tier 0 (innermost)
    ((-10, 0, 0), (10, 0, 0), "above", "20"),   # overlaps    → tier 1
    (( 15, 0, 0), (30, 0, 0), "above", "15"),   # non-overlap → tier 0 (shares with first)
], draft)
```

Specs are `(p1, p2, side, label)` or `(p1, p2, side, label, tolerance)` — no `distance`.
Dims whose X/Y spans overlap are placed on successive tiers; non-overlapping dims share a tier.
**Order matters:** specs listed first are placed on lower (inner) tiers.

`tier_spacing` defaults to `draft.font_size * 3 + draft.arrow_length`.

---

### `place_labels(specs, draft, centerlines, gap=1.0)`

Like `place_dims` but also auto-shifts each label to clear any crossing vertical centreline.

```python
bore_cl = Centerline((0, -30, 0), (0, 30, 0))   # vertical centreline at x=0

dims = place_labels([
    ((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8"),
    ((-20, 0, 0), (20, 0, 0), "above", 18, "40"),
], draft, centerlines=[bore_cl])
```

Specs are `(p1, p2, side, distance, label)` or `(p1, p2, side, distance, label, tolerance)`.
For each dim whose label would cross a centreline, the minimum left/right shift is computed
automatically. Multiple crossing centrelines are handled in one pass.

---

### `Centerline(p1, p2, draft=None)`

A centreline between two points — a single thin line rendered as a face, with
`.is_centerline = True` and a zero-width `.segments` rail for lint.

```python
bore_cl = Centerline((cx, -50, 0), (cx, 50, 0))   # vertical through bore axis
```

Pass `Centerline` objects to `place_labels(..., centerlines=[bore_cl])` for auto-avoidance,
or pass them to `lint_drawing([...] + [bore_cl])` to get `label_centerline_overlap` warnings.

---

### `SafeDimension(path, label, draft, fallback_label=None)`

`DimensionLine` wrapper that won't raise `ValueError` when the label is wider than the dim path.
Truncates gracefully and retries.

---

### `Leader(tip, elbow, label, draft, all_around=False, all_over=False, text_side="auto")`

Leader annotation built from scratch. The line stops cleanly before the label text.

```python
ld = Leader((5, 5, 0), (20, 12, 0), "⌀7.93 H7", draft)
exporter.add_shape(ld, layer="ink")   # arrowhead + shelf + glyphs — one ink layer
```

**Label side**: by default the label continues in the horizontal direction of tip → elbow —
right of the elbow when the elbow is right of the tip, left when it is left of it (a purely
vertical leader places it right). On a dense sheet, pass `text_side="left"` / `"right"` to
force the side so the label doesn't run off-page or across a neighbouring view. The override
is for steep or vertical leaders, where either side is clear — a forced side that would run
the line through the label text (e.g. forcing it back toward the tip of a near-horizontal
leader) raises `ValueError`.

The object is a `Sketch` with metadata `.label`, `.tip`, `.elbow`, `.label_bbox`, `.segments`.
`all_around=True` / `all_over=True` draw the ISO 1101 all-around / all-over circles at the kink.

`leader_offset(tip, direction, length, label, draft, text_side="auto")` is a thin wrapper
that places the elbow by compass direction (`"N"`, `"NE"`, …) or numeric angle (degrees CCW
from +X) and a distance, instead of absolute coords — handy when the drawing uses a non-1:1
scale.

```python
ld = leader_offset((x, y), "NW", 12, "⌀6 boss", draft)   # returns a Leader
```

---

### `view_axes(viewport_origin, viewport_up=(0,1,0), look_at=(0,0,0))`

Returns the world→page axis mapping for a `project_to_viewport` call, computed analytically.

```python
axes = view_axes((0, 0, -100), (0, 1, 0), (0, 0, 0))
# {"world_X": ("page_X", -1.0), "world_Y": ("page_Y", 1.0), "world_Z": ("depth", 0.0)}
# ↑ bottom view flips world-X on the page
```

---

### `lint_drawing(items, part_bbox=None, page_bbox=None, drawing_scale=1.0, view_shapes=None)`

Duck-typed structural checks on a list of annotation objects (`Dimension`, `Leader`,
`Centerline`, …). Dispatch is by attribute presence, not type:

| Check | Trigger |
|---|---|
| `label_vs_measured` | Label value differs from measured path length by >0.5% — likely axis swap |
| `annotation_overlap` | Two annotations overlap by >0.5 mm in both axes at the same Y level |
| `label_centerline_overlap` | Dim label bbox crosses a `Centerline` — use `label_offset_x` or `place_labels` |
| `dim_inside_part` | Dim bbox overlaps part outline by >10% — dim is inside the view |
| `leader_line_through_text` | Leader elbow point inside label bbox — line strikes through text |
| `annotation_out_of_bounds` | An item's bbox extends past the page (set `page_bbox` or call `set_page()`) |
| `view_annotation_overlap` | An annotation's label text overlaps a view's projected edges (full bbox for annotations without a label; centrelines and datum targets are exempt; witness lines, leader shafts, datum triangles, and finish marks may touch the view freely) |
| `view_annotation_inside_extents` | Info: a label sits inside a view's bbox but over a blank region — a legitimate convention for callouts on large faces |
| `view_overlap` | Two view shape bboxes overlap each other — views are too close |
| `view_out_of_bounds` | A view extends past the drawable area (set `page_bbox` or call `set_page()`) |

```python
bore_cl = Centerline((0, -30, 0), (0, 30, 0))
issues = lint_drawing([dim1, dim2, lea1, bore_cl])
for issue in issues:
    print(issue.severity, issue.message)
```

**Scaled drawings.** To draw a small part enlarged — e.g. a 7.5 mm feature at 5:1 —
scale the geometry up before projecting and pass the same factor as `drawing_scale`.
The `label_vs_measured` check then divides the measured path length by it, so labels
carry the *real* dimension while the geometry is drawn large:

```python
issues = lint_drawing([dim], drawing_scale=5.0)   # 37.5 mm measured, label "7.5" → OK
tb = TitleBlock("Gear", "DRW-007", drawing_scale=5.0)   # title block prints "5:1"
```

`format_drawing_scale(5.0)` → `"5:1"`, `format_drawing_scale(0.5)` → `"1:2"`. Pass the
numeric factor to both `lint_drawing` and `TitleBlock` so the linted and printed scales
can't drift.

**Page bounds.** Set a drawable area — pass `page_bbox=(min_x, min_y, max_x, max_y)`
or call `set_page(width, height, margin)` first — and any item whose bounding box spills
past it is flagged `annotation_out_of_bounds`. Include your `TitleBlock` in the items
list: its bounding box grows when a long string (e.g. a verbose subtitle) overflows the
frame, so a too-long title block is caught the same way as a stray dimension.

---

### `lint_feature_coverage(part, annotations, tol=0.15)`

Coarse **completeness** check (the checks above are hygiene checks): builds a feature
inventory from the part's hole/boss diameters — cylinder patches totalling at least
half a turn around their axis, so fillets don't count but a keyway-split bore does —
and diffs it against every `ø` value mentioned in the annotations' labels, plus the
structured `covers_diameters` metadata on `HoleCallout`. Radius callouts are not
counted (an "R5 TYP" fillet note must not mask an undimensioned ø10 bore). Each
uncovered diameter yields a `feature_not_dimensioned` warning. Title blocks are
skipped (a part name like "BRACKET R8" is not a callout). Size coverage only — location
coverage needs feature recognition and is future work.

`Drawing.lint()` (and therefore `export()`) runs this automatically when the drawing
knows its source part, which `build_drawing` / `make_drawing` always provide.

---

### `find_interferences(items, *, part_bbox=None, page_bbox=None, obstacles=None)`

Geometry-precise interference detection between annotation objects. Each item is
decomposed into a **label box** (`.label_bbox` or its own face bbox) and **structural
line segments** (`.segments` or straight edges), then checked for `labels_overlap`,
`label_out_of_frame`, `label_on_part`, `label_over_geometry`, `line_pierces_label`, and
`redundant_lines`. Works on the native objects and on lightweight `SimpleNamespace`
stand-ins exposing `label_bbox` / `label` / `segments` (e.g. a raw view-label `Text`).

```python
issues = find_interferences([dim1, dim2, leader1], obstacles=view_boxes)
```

---

### `find_overlaps(sketches, min_area=0.01)`

A generic, zero-metadata geometric collision check: returns the pairs of `Sketch`es whose
filled faces actually intersect (boolean `a & b`) with area above `min_area`. Useful for a
final once-over of *any* drawing, not just helper objects.

```python
issues = find_overlaps([sketch_a, sketch_b, sketch_c])   # code: "faces_overlap"
```

---

### `FeatureControlFrame(characteristic, tolerance, datums=(), draft=None, diameter=False, modifier=None, datum_modifiers=None)`

ISO 1101 feature control frame — the boxed GD&T callout. build123d ships no GD&T primitives, and the
geometric-characteristic symbols (⌖ ⊥ ∥ ◎ …) are absent from CAD-safe fonts, so each is drawn
geometrically rather than as a glyph.

```python
draft = Draft(font_size=2.5, decimal_precision=1)

# | ⌖ | ⌀0.5 Ⓜ | A | B | C |
fcf = FeatureControlFrame(
    "position", 0.5, ("A", "B", "C"), draft,
    diameter=True,     # prepend ⌀ (cylindrical tolerance zone)
    modifier="M",      # circled M = MMC ("L" = LMC, "P" = projected; None = RFS)
)
exporter.add_shape(fcf, layer="ink")   # frame + symbols + values — one ink layer
```

All 14 characteristics are supported: `straightness`, `flatness`, `circularity`, `cylindricity`,
`profile_line`, `profile_surface`, `angularity`, `perpendicularity`, `parallelism`, `position`,
`concentricity`, `symmetry`, `circular_runout`, `total_runout`.

Per-datum material-condition modifiers are available via `datum_modifiers`, e.g.
`datum_modifiers={"A": "M"}` draws a circled M after datum A's letter.

The frame is built with its bottom-left corner at the origin (height = 2 × font size, per ISO 3098).
The object is a `Sketch` with metadata `.characteristic`, `.tolerance_str`, `.datums`, `.segments`.
A `CompositeFeatureControlFrame(characteristic, rows, draft=None)` stacks two or more tolerance
rows sharing one characteristic cell — each `row` is a dict with `tolerance` (required) plus
optional `datums` / `diameter` / `modifier` / `datum_modifiers`.

> **Why drawn, not typed:** a frequent failure mode (see build123d-mcp Discord) is building these
> symbols ad-hoc from circles + lines and positioning each by `someRect.center()` — if a referenced
> rectangle resolves to the wrong centre, a symbol silently lands off-frame and "disappears". This
> helper lays out every compartment by explicit arithmetic so nothing depends on fragile lookups.

---

### `DatumFeature(letter, draft=None, filled=True)`

ISO 5459 datum feature symbol: a filled triangle on a short leader to a framed datum letter.
Built with the triangle tip at the origin (pointing −Y); move it onto the feature with `.moved(loc)`.

```python
dat = DatumFeature("A", draft)
exporter.add_shape(dat, layer="ink")   # triangle + box + letter — one ink layer
```

A `DatumTarget(identifier, area_label=None, draft=None)` draws the companion ISO 5459
datum-target circle (upper compartment = target-area size, lower = identifier).

---

### `TitleBlock(...)`, `SurfaceFinish(...)` and `HoleCallout(...)`

`TitleBlock` is a **standalone title box** (170 × 16 mm by default), positioned by the caller. It is *not* a substitute for `build123d.TechnicalDrawing`, which is a whole-page chrome — page-sized border + grid ticks + embedded title box. Use `TechnicalDrawing` when you want the full drawing-sheet frame; reach for `TitleBlock` when you want just the title box, positionable anywhere, with `material` / `general_tolerance` fields that `TechnicalDrawing` does not carry. It is a `Sketch` — for its overall height use `tb.bounding_box().size.Y`.

`SurfaceFinish(ra_value, position, angle=0.0, draft=None, size=None)` produces an ISO 1302 Ra-value
check-mark symbol (build123d does not ship one); its tip is exposed as `.mark_position`.
`HoleCallout(diameter, *, count=None, through=False, depth=None, cbore_dia=None, …)` builds a
single-line hole note, e.g. `4× ⌀8.5 THRU`.

### `Note(...)` and `TextBlock(...)`

`Note(text, position, draft, rotation=0, align=None)` is a one-line free-text note;
`position` is the text centre by default, and `align` picks a different anchor — e.g.
`align=(Align.MIN, Align.CENTER)` anchors the left edge at `position`.

`TextBlock(lines, position, draft, line_spacing=1.6, align=(Align.MIN, Align.MAX))`
renders a multi-line, left-aligned block — general notes lists and hole tables —
anchored by default at its top-left corner. `lines` is a list of strings (empty
strings leave blank lines) or one string split on newlines. The whole block is a
single annotation for lint purposes.

## Status against upstream

- `lint_drawing` is a prototype of rule-based drawing checks that build123d's roadmap mentions as future work. If upstream ships its own linter later, this one can be deprecated.
- `Dimension` is a thin convenience wrapper over `ExtensionLine` — it does not replace the underlying class, it just lets you write `side="above"` instead of computing the right-hand-normal signed offset by hand. If upstream adds a named-side parameter, this helper becomes redundant.

## Examples

[`examples/specimen_sheet.py`](examples/specimen_sheet.py) — the catalogue **shown at the top
of this page** — is an **A3 technical drawing that catalogues the helpers using the helpers
themselves**: a real drawing frame, a `TitleBlock` title block, every specimen called
out by a `Leader` carrying the helper's name, all drafted with `draft_preset()`, and each
cell captioned with the exact snippet that produced it.

Run `python examples/specimen_sheet.py` to write `specimen_sheet.svg`, then rasterise it
(resvg, Inkscape, or a browser) to refresh `docs/specimen_sheet.png`. Because the whole sheet
is build123d geometry — text included — it also exports to DXF and scales like any drawing.

### A worked drawing — [`examples/part_drawing.py`](examples/part_drawing.py)

Where the specimen sheet is a *catalogue*, this shows the **end-to-end workflow on a real
part**: take a [`bd_warehouse`](https://github.com/gumyr/bd_warehouse) `HexHeadScrew` +
`HexNut` → `project_to_viewport()` views → dimension with `Dimension` / `Leader` →
**lint with `find_interferences()`** → export. An A4 frame, a `TitleBlock`, **front / top /
side views plus an isometric** for each part (with dashed hidden lines), and the layout is
verified collision-free by `lint()` before it's written.

**Every dimension and callout is pulled from the bd_warehouse object** — change
`BOLT_SIZE` / `BOLT_LENGTH` at the top of the script (e.g. `"M8-1.25"`, `"M12-1.75"`) and the
views, length / across-flats dimensions, thread designation and title block all reflow
automatically; the lint stays clean because the label values come from the same source as
the geometry. (`bd_warehouse` is an example-only dev dependency, not a runtime dependency.)

![hex bolt and nut drawing](docs/part_drawing.png)

## Development

```
git clone https://github.com/pzfreo/build123d-drafting-helpers.git
cd build123d-drafting-helpers
uv run pytest tests/
```

## Status

Alpha. API may change. Developed alongside [build123d-mcp](https://github.com/pzfreo/build123d-mcp), which integrates these helpers into its LLM-facing drawing workflow.

## Documentation

- [Drafting conventions and gotchas](docs/drafting-conventions.md) — offset sign table, crash modes, recommended feedback loop, and when to reach for which helper.
