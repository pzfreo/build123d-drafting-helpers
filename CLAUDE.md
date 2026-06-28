# build123d-drafting-helpers

Drawing-annotation primitives for [build123d](https://github.com/gumyr/build123d).
Import name is `build123d_drafting`; install name is `build123d-drafting-helpers`.

This package is the **rendering substrate** for technical drawings: the
composable annotation building blocks. The application-level systems that *use*
it — the auto-drawing engine, feature recognition, and the drawing lint — live
in the separate [`draftwright`](https://github.com/pzfreo/draftwright) package.
The dividing line (draftwright ADR 0007): **helpers renders; draftwright
reasons.**

## Scope

1. **Annotation primitives** (`helpers.py`) — the rendering substrate, and the
   only actively-developed layer here: `Dimension`, `Leader`, `TitleBlock`,
   `HoleCallout`, `Centerline`, `CenterlineCircle`, the GD&T family
   (`FeatureControlFrame`, `DatumFeature`, …), plus the styling/coordinate
   helpers they need (`draft_preset`, `view_axes`, `ViewCoordinates`,
   `place_dims`/`place_labels`). Close to build123d's own drafting model; may
   eventually be contributed upstream.

2. **Frozen, deprecated layers** — feature recognition (`features.py`:
   `find_holes`, `find_bosses`, `find_hole_patterns`, …) and the drawing lint
   (`lint_drawing`, `find_overlaps`, `find_interferences`, `LintIssue`, and the
   `set_page`/`annotate`/`clear_page` registration helpers) were moved to
   draftwright (ADR 0007). The copies here are **vendored-and-frozen**: importing
   them through `build123d_drafting` emits a `DeprecationWarning`. Do **not**
   extend them or add new recognition/lint here — that work goes in draftwright.
   They will be deleted in a future major version.

Do not blur these boundaries. New annotation work belongs in `helpers.py`; new
recognition or lint work belongs in draftwright, not here.

## Core principle: stay in line with build123d's technical-drawing model

The helpers **extend** build123d's drafting model — they do not replace or work
around it. Everything they produce must remain a first-class build123d *drawing*,
and the style of both the **code** and the **output** must stay consistent with
`build123d.drafting` and with the existing helpers.

**Output is geometry, always.** Every symbol, line, and label is real build123d
geometry (`Text` → faces, `Edge`/`Wire` strokes). This is non-negotiable because it
is what makes the output a technical drawing:

- it **round-trips to DXF and SVG** via `ExportDXF` / `ExportSVG` — a fabricator
  opens the DXF and the dimensions, GD&T frames, and notes are really there;
- it is **single-source-of-truth** — the SVG preview and the DXF handoff come from
  the same geometry;
- it **scales** with the drawing.

Do **not** introduce text or annotations that bypass the geometry model — e.g.
native SVG `<text>`, raster-only overlays, or anything that renders in a preview
but vanishes on DXF export.

## Conventions to match

- **Annotation objects are native build123d `BaseSketchObject` subclasses** — the
  returned object *is* a `Sketch`. It composes in a `BuildSketch`, combines with
  `+`/`-`, can be `.moved()`, and exports directly. All geometry (frame boxes,
  witness lines, GD&T glyphs, and text) is rendered as thin filled *faces* on a
  single ink layer — there is no `.lines`/`.text` split. Carry placement/lint
  metadata as instance attributes (`.label`, `.label_bbox`, `.segments`,
  `.measured_length`, …).
- **Standards-faithful output**: follow the relevant ISO/ASME convention and draw
  geometric-characteristic glyphs geometrically (the symbols are absent from
  CAD-safe fonts).
- **Sizing** derives from the `Draft` config (`font_size`, `line_width`,
  `pad_around_text`).
- **Deterministic text**: labels render from a bundled font *file*
  (`DEFAULT_FONT_PATH` → Liberation Sans) so glyph geometry is identical across
  platforms; a `font_path` always wins over the `font` name.

## Dependencies

The runtime dependency is build123d only. New runtime dependencies require
explicit discussion.

## Testing

`uv run pytest`. Tests are geometry-level (edge counts, bbox placement, face
counts, error paths) — match that style. Target is 100% passing. (CI lints with
`ruff check` / `ruff format --check` and type-checks with `mypy` over
`src/build123d_drafting/`; `examples/` is not linted in CI.)
