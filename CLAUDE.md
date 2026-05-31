# build123d-drafting-helpers

Third-party drawing-annotation helpers for [build123d](https://github.com/gumyr/build123d).
Pure Python, no MCP dependency. Import name is `build123d_drafting`; install name is
`build123d-drafting-helpers`.

## Core principle: stay in line with build123d's technical-drawing model

These helpers **extend** build123d's drafting model — they do not replace or work
around it. Everything they produce must remain a first-class build123d *drawing*, and
the style of both the **code** and the **output** must stay consistent with
`build123d.drafting` and with the existing helpers.

**Output is geometry, always.** Every symbol, line, and label is real build123d
geometry (`Text` → faces, `Edge`/`Wire` strokes). This is non-negotiable because it is
what makes the output a technical drawing:

- it **round-trips to DXF and SVG** via `ExportDXF` / `ExportSVG` — a fabricator opens
  the DXF and the dimensions, GD&T frames, and notes are really there;
- it is **single-source-of-truth** — the SVG preview and the DXF handoff come from the
  same geometry;
- it **scales** with the drawing.

Do **not** introduce text or annotations that bypass the geometry model — e.g. native
SVG `<text>`, raster-only overlays, or anything that renders in a preview but vanishes
on DXF export. Crisper-looking text is not worth breaking the use case. If a *caller*
wants poster-style native text for sheet furniture (titles, captions), that belongs in
their presentation script, never in these helpers.

## Conventions to match

- **Result objects** are dataclasses exposing `.lines` (stroke) and `.text` (fill)
  where the symbol mixes both, plus `.shape` and `.bbox()`. Keep the `.lines`/`.text`
  split so closed loops (e.g. the GD&T ⌀ prefix, modifier rings) can be stroked rather
  than flooded solid. Route them with `add_to_layers()`.
- **Standards-faithful output**: follow the relevant ISO/ASME convention (e.g. ISO 1101
  feature control frames, ISO 5459 datum features, ISO 1302 surface finish) and draw
  geometric-characteristic glyphs geometrically — they are absent from CAD-safe fonts.
- **Sizing** derives from the `Draft` config (`font_size`, `line_width`,
  `pad_around_text`) so a helper composes cleanly at any scale.
- **No new runtime dependencies** beyond build123d.

## Testing

`uv run pytest`. Tests are geometry-level (edge counts, bbox placement, face counts,
error paths) — match that style. Target is 100% passing.
