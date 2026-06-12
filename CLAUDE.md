# build123d-drafting-helpers

Automated technical-drawing generation for [build123d](https://github.com/gumyr/build123d).
Import name is `build123d_drafting`; install name is `build123d-drafting-helpers`.

## Scope

This project has three distinct layers. Keep them separate:

1. **Annotation primitives** (`helpers.py`) — composable building blocks:
   `Dimension`, `Leader`, `TitleBlock`, `HoleCallout`, `CenterlineCircle`, etc.
   These are close to build123d's own drafting model and may eventually be
   contributed upstream.

2. **Feature recognition** (`features.py`) — geometric analysis of CAD solids:
   `find_holes`, `find_bosses`, `find_hole_patterns`, `BoltCircle`, `LinearArray`.
   No dependency on the drawing layer.

3. **Auto-drawing engine** (`make_drawing.py`) — the application-level system:
   `build_drawing`, `make_drawing`, `Drawing`, layout solver, sheet selection,
   PMI normalisation. Depends on both layers above.

Do not blur these boundaries. Feature recognition must not import drawing
primitives. The engine imports both.

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

## Architecture: strip/zone layout model

The auto-drawing engine places annotations in **strips** — directional bands
adjacent to each orthographic view. Each strip has an `anchor` (the view edge),
an `outer_limit` (page margin, neighbouring view, or title-block boundary), and a
cursor that advances as annotations are allocated. This replaces ad-hoc per-
annotation spacing arithmetic.

`_analyse()` constructs a `ViewZones` (front, plan, side) immediately after
computing view positions. Annotation functions call `strip.allocate(size)` to
reserve space; they skip the annotation and log a warning if the strip is full.
`_auto_annotate()` tightens iso-view limits at entry once the iso has been
projected.

New annotation types **must** declare which strip they belong to and register
via `strip.allocate()`. Do not add free-floating `distance=` constants.

## Conventions to match

- **Result objects** are dataclasses exposing `.lines` (stroke) and `.text` (fill)
  where the symbol mixes both, plus `.shape` and `.bbox()`.
- **Standards-faithful output**: follow the relevant ISO/ASME convention and draw
  geometric-characteristic glyphs geometrically.
- **Sizing** derives from the `Draft` config (`font_size`, `line_width`,
  `pad_around_text`).

## Dependencies

`kiwisolver>=1.4,<2` is an allowed runtime dependency for the layout solver
(the Cassowary constraint solver, < 200 KB wheel, no transitive deps). Add it
only when the constraint-optimisation layer is wired in. Other new runtime
dependencies require explicit discussion.

## Testing

`uv run pytest`. Tests are geometry-level (edge counts, bbox placement, face
counts, error paths) — match that style. Target is 100% passing.
