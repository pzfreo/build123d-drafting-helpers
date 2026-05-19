# Changelog

## v0.1.1 — 2026-05-19

Bug fix, two new helpers, and a drafting-conventions reference doc.

### Bug fixes

- **`view_axes` rewritten with pure Python arithmetic** — no `build123d.Vector` / OCC import on this path. Eliminates the cold-start timeout that caused the MCP `view_axes` tool to fail when the worker process hadn't yet loaded the OCC kernel (#114 build123d-mcp).

### New helpers

- **`iso_title_block(part_name, drawing_number, ...)`** — ISO-style 2-row title block (170 × 16 mm default), returns `TitleBlockResult(lines, text, bbox)` for SVG layer routing.
- **`surface_finish_mark(ra_value, position, ...)`** — ISO 1302 check-mark symbol with Ra annotation, returns `SurfaceFinishResult(lines, text, label_str, position)`.

### Documentation

- **`docs/drafting-conventions.md`** — offset-sign table, label-overflow crash rule, text fill-layer rule, leader gap rule, reach-for-what table, and the recommended build→lint→render→inspect feedback loop.

---

## v0.1.0 — 2026-05-19

First PyPI release.

Renamed from `build123d-drafting` to `build123d-drafting-helpers` to signal the third-party status of the package (it is not affiliated with the upstream build123d project). The Python import name is unchanged — existing `from build123d_drafting import ...` lines continue to work.

### Helpers

- `dim_linear(p1, p2, side, distance, draft, label=None, tolerance=None)` — named-side wrapper over `build123d.ExtensionLine`. Returns `DimResult(shape, label_str, measured_length)`.
- `safe_dim_line(path, label, draft, fallback_label=None)` — `DimensionLine` wrapper that won't raise when the label is wider than the dim path.
- `leader(tip, elbow, label, draft)` — leader annotation built from scratch; line stops cleanly before the label. Returns `LeaderResult(lines, text, label_str, tip, elbow)`.
- `view_axes(viewport_origin, viewport_up, look_at)` — analytic world-to-page axis mapping for `project_to_viewport`.
- `lint_drawing(items, part_bbox=None)` — structural checks (label-vs-length divergence, dim inside view, leader through label).
- `iso_title_block(...)` — standalone title box (not a substitute for `build123d.TechnicalDrawing`, which is a whole-page chrome; this is the title box alone, positionable anywhere, with separate `lines`/`text` `Compound`s for SVG layer routing and additional `material` / `general_tolerance` fields).
- `surface_finish_mark(ra_value, position, ...)` — ISO 1302 Ra-value check-mark symbol; build123d does not ship one.
