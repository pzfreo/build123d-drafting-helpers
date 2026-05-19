# build123d-drafting-helpers

[![PyPI](https://img.shields.io/pypi/v/build123d-drafting-helpers.svg)](https://pypi.org/project/build123d-drafting-helpers/)
[![Python](https://img.shields.io/pypi/pyversions/build123d-drafting-helpers.svg)](https://pypi.org/project/build123d-drafting-helpers/)
[![License](https://img.shields.io/pypi/l/build123d-drafting-helpers.svg)](LICENSE)

Third-party drawing-annotation helpers for [build123d](https://github.com/gumyr/build123d) — pure Python, no MCP dependency. Not affiliated with the upstream build123d project.

```python
from build123d_drafting import dim_linear, leader, view_axes, lint_drawing
```

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

Requires `build123d >= 0.7.0` and Python ≥ 3.10.

## Helpers

### `dim_linear(p1, p2, side, distance, draft, label=None, tolerance=None)`

`ExtensionLine` wrapper with named placement side instead of raw signed offset.

```python
draft = Draft(font_size=2.5, decimal_precision=1)
dim = dim_linear((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
```

`side` accepts `"above"` / `"below"` / `"left"` / `"right"` or an explicit world-direction vector.
The correct `offset` sign is computed from the path direction's right-hand normal — no guessing.

Returns a `DimResult(shape, label_str, measured_length)`.

---

### `safe_dim_line(path, label, draft, fallback_label=None)`

`DimensionLine` wrapper that won't raise `ValueError` when the label is wider than the dim path.
Truncates gracefully and retries.

---

### `leader(tip, elbow, label, draft)`

Leader annotation built from scratch. The line stops cleanly before the label text.

```python
res = leader((5, 5, 0), (20, 12, 0), "⌀7.93 H7", draft)
exporter.add_shape(res.lines, layer="dims")   # arrowhead + shelf — fill_color layer
exporter.add_shape(res.text,  layer="text")   # glyphs — fill_color layer
```

Returns `LeaderResult(lines, text, label_str, tip, elbow)`. Route `lines` and `text` to
separate SVG layers, both with `fill_color` set.

---

### `view_axes(viewport_origin, viewport_up=(0,1,0), look_at=(0,0,0))`

Returns the world→page axis mapping for a `project_to_viewport` call, computed analytically.

```python
axes = view_axes((0, 0, -100), (0, 1, 0), (0, 0, 0))
# {"world_X": ("page_X", -1.0), "world_Y": ("page_Y", 1.0), "world_Z": ("depth", 0.0)}
# ↑ bottom view flips world-X on the page
```

---

### `lint_drawing(items, part_bbox=None)`

Structural checks on a list of `DimResult` / `LeaderResult` objects:

- Label numeric value differs from measured path length by >0.5% (likely axis swap)
- Dim bbox overlaps part outline by >10% (dim placed inside the view)
- Leader elbow point inside label bbox (line strikes through text)

```python
issues = lint_drawing([dim1, dim2, lea1])
for issue in issues:
    print(issue.severity, issue.message)
```

---

### `iso_title_block(...)` and `surface_finish_mark(...)`

`iso_title_block` is a **standalone title box** (170 × 16 mm by default), positioned by the caller. It is *not* a substitute for `build123d.TechnicalDrawing`, which is a whole-page chrome — page-sized border + grid ticks + embedded title box, returned as a single `Sketch`. Use `TechnicalDrawing` when you want the full drawing-sheet frame; reach for `iso_title_block` when you want just the title box, positionable anywhere, with separate `lines`/`text` `Compound`s for SVG layer routing, and with `material` / `general_tolerance` fields that `TechnicalDrawing` does not carry.

`surface_finish_mark` produces an ISO 1302 Ra-value check-mark symbol — build123d does not ship one.

## Status against upstream

- `lint_drawing` is a prototype of rule-based drawing checks that build123d's roadmap mentions as future work. If upstream ships its own linter later, this one can be deprecated.
- `dim_linear` is a thin convenience wrapper over `ExtensionLine` — it does not replace the underlying class, it just lets you write `side="above"` instead of computing the right-hand-normal signed offset by hand. If upstream adds a named-side parameter, this helper becomes redundant.

## Development

```
git clone https://github.com/pzfreo/build123d-drafting-helpers.git
cd build123d-drafting-helpers
uv run pytest tests/
```

## Status

Alpha. API may change. Developed alongside [build123d-mcp](https://github.com/pzfreo/build123d-mcp), which integrates these helpers into its LLM-facing drawing workflow.
