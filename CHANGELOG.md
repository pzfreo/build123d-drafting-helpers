# Changelog

## Unreleased

### Features

- **`LintIssue.code`**: every issue from `lint_drawing()` and `find_interferences()` now
  carries a stable machine-readable check id (`label_vs_measured`, `annotation_overlap`,
  `label_centerline_overlap`, `dim_inside_part`, `leader_line_through_text`,
  `labels_overlap`, `label_out_of_frame`, `label_on_part`, `line_pierces_label`,
  `redundant_lines`). Lets consumers (e.g. the MCP server) route issues by code instead of
  string-matching the message. Backward-compatible — new optional field.

## v0.1.9 — 2026-06-01

### Features

- **`draft_preset(font_size=2.5, decimal_precision=2, **overrides)`**: returns a
  `Draft` tuned for clean drawing output — `arrow_length = 0.9 * font_size` and
  `line_width = 0.1`, instead of build123d's heavy defaults (`arrow_length=3.0`,
  `line_width=0.5`) which look clumsy at small font sizes. Any field can be
  overridden by keyword.
- **`find_interferences(items, *, part_bbox=None, page_bbox=None)`**: geometry-precise
  interference detection between drafting annotations. Decomposes each annotation into
  its label box and its structural line segments (witness lines, dim lines, leader
  shafts) and tests actual crossings — catching cases `lint_drawing`'s whole-bbox checks
  miss, e.g. a stacked dim's extension line spearing a neighbouring dim's value. Reports
  `line↔label`, `label↔label`, **`line↔line` redundant collinear overlap** (two stacked
  dims sharing an endpoint each draw the shared witness line), and (when
  `page_bbox`/`part_bbox` are supplied) `label↔frame` and `label↔part`. Generic line↔line
  *crossings* are intentionally not flagged — only collinear overlap. Real collisions are
  severity `"error"`; redundant collinear overlaps are `"warning"` (chain dimensioning
  legitimately shares witness lines, so they are advisory). Returns `list[LintIssue]`.

## v0.1.8 — 2026-06-01

### Bug fixes

- **`surface_finish_mark`**: the Ra value was vertically centred on the horizontal
  extension line, so the line struck through the digits. It now rests just above
  the line per ISO 1302.

### Features

- **`add_to_layers(exporter, result, *, line_layer="lines", text_layer="text")`**:
  routes a result's `.lines` (stroke) and `.text` (fill) onto the correct
  ExportSVG layers. Prevents the common mistake of adding the combined `.shape`
  to a single `fill_color` layer, which floods closed loops solid (the GD&T ⌀
  prefix and modifier ring turn into black discs). Raises `TypeError` for
  `.shape`-only results (e.g. `DimResult`), which carry no flood-prone loops.

## v0.1.7 — 2026-05-31

### Features

- **`feature_control_frame(characteristic, tolerance, datums, draft, diameter=False, modifier=None)`**:
  ISO 1101 feature control frame. Supports all 14 geometric characteristics (drawn geometrically — the
  GD&T symbols are absent from CAD-safe fonts), optional ⌀ tolerance-zone prefix, MMC/LMC/projected
  modifiers, and per-datum modifiers. Returns `FeatureControlFrameResult(lines, text, characteristic,
  tolerance_str, datums, width, height)` with the usual lines/text layer split. Frame height = 2 ×
  font size (ISO 3098); bottom-left corner at the origin. Compartments are laid out by explicit
  arithmetic so symbols never depend on fragile `.center()` lookups.
- **`datum_feature(letter, draft, filled=True)`**: ISO 5459 datum feature symbol (filled triangle +
  leader + framed letter). Triangle tip at the origin. Returns `DatumFeatureResult(lines, text, letter)`.

---

## v0.1.6 — 2026-05-25

### Features

- **`leader_offset(tip, direction, length, label, draft)` (#6)**: place a leader's elbow by compass direction (`"N"`, `"NE"`, …, case-insensitive) or numeric angle (degrees CCW from +X) and a distance, instead of absolute page coords. Thin wrapper over `leader()` — useful when the drawing uses a non-1:1 scale and elbow arithmetic gets noisy.

---

## v0.1.5 — 2026-05-25

### Bug fixes

- **`dim_linear` short-path fallback corrected (#8)**: v0.1.4's fallback retried `ExtensionLine(label="")` which still crashes. Fixed to use `label=None`, which suppresses the built-in text without triggering the empty-wire code path. Paths shorter than ~5 mm now render correctly with an externally-placed label.

---

## v0.1.4 — 2026-05-21

### Bug fixes

- **`dim_linear` short-path crash (#124)**: no longer raises `ValueError` when the label is wider than the dimension path — falls back to external `Text` placement automatically.

### Features

- **`label_offset_x` on `dim_linear`**: shifts the label along the dim line (signed mm) to clear centreline crossings without changing the dimension geometry.
- **`CenterlineResult` / `centerline(p1, p2)`**: thin Edge compound for centrelines; accepted by `lint_drawing` and `place_labels` for collision detection.
- **`place_dims(specs, draft, base_distance, tier_spacing)`**: greedy tier assignment — stack parallel dims without manually computing offsets. Non-overlapping dims share a tier; overlapping dims are pushed to successive tiers.
- **`place_labels(specs, draft, centerlines, gap)`**: like `place_dims` but also auto-shifts each label the minimum distance to clear crossing vertical centrelines in one pass.

---

## v0.1.3 — 2026-05-20

### Features

- **`place_dims(specs, draft, base_distance=8.0, tier_spacing=None)`**: build a stack of parallel dims with automatically assigned offsets. Specs are `(p1, p2, side, label[, tolerance])` — no `distance` needed. Dims whose spans overlap are placed on successive tiers; non-overlapping dims share a tier. First spec in each group is innermost.
- **`place_labels(specs, draft, centerlines, gap=1.0)`**: like `place_dims` but also shifts each label the minimum distance left/right to clear any crossing vertical centreline. Handles multiple centrelines in one pass.
- **`centerline(p1, p2)` / `CenterlineResult`**: thin Edge compound representing a centreline. Pass to `place_labels` for auto-avoidance or to `lint_drawing` for `label_centerline_overlap` detection.
- **`label_offset_x` on `dim_linear`**: shifts the label along the dim line (mm, signed) without changing the dim geometry. Used by `place_labels` internally; also available directly.
- **`label_bbox` on `DimResult`**: precise text bounding box `(min_x, min_y, max_x, max_y)` computed from a Text probe. Used by `lint_drawing` for centreline-overlap detection.
- **Annotation overlap detection** in `lint_drawing`: flags same-level annotation pairs overlapping by >0.5 mm in both axes. Uses `dim_level_y` to skip stacked dims at different Y levels.
- **Centreline-label overlap detection** in `lint_drawing`: flags `(DimResult, CenterlineResult)` pairs where the label bbox crosses the centreline (`label_centerline_overlap` check).

### Documentation

- README updated with sections for all new helpers.
- `docs/drafting-conventions.md` updated reach-for-what table; new section on centreline-label collision avoidance.

---

## v0.1.2 — 2026-05-20

### Bug fixes

- **`leader()` shelf no longer strikes through label text** (#4): the horizontal shelf was sized `gap + text_w + gap`, making the line extend through the full width of the label bounding box. Fixed to `shelf_len = gap` — a short stub ending exactly where the text starts. Regression tests added for both right-going and left-going leaders.

---

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
