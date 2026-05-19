# Changelog

## v0.1.0 — 2026-05-19

First PyPI release.

Renamed from `build123d-drafting` to `build123d-drafting-helpers` to signal the third-party status of the package (it is not affiliated with the upstream build123d project). The Python import name is unchanged — existing `from build123d_drafting import ...` lines continue to work.

### Helpers

- `dim_linear(p1, p2, side, distance, draft, label=None, tolerance=None)` — named-side wrapper over `build123d.ExtensionLine`. Returns `DimResult(shape, label_str, measured_length)`.
- `safe_dim_line(path, label, draft, fallback_label=None)` — `DimensionLine` wrapper that won't raise when the label is wider than the dim path.
- `leader(tip, elbow, label, draft)` — leader annotation built from scratch; line stops cleanly before the label. Returns `LeaderResult(lines, text, label_str, tip, elbow)`.
- `view_axes(viewport_origin, viewport_up, look_at)` — analytic world-to-page axis mapping for `project_to_viewport`.
- `lint_drawing(items, part_bbox=None)` — structural checks (label-vs-length divergence, dim inside view, leader through label).
- `iso_title_block(...)` and `surface_finish_mark(...)` — title-block + Ra-value mark helpers.
