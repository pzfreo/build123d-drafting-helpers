# build123d-drafting

Drawing annotation helpers for [build123d](https://github.com/gumyr/build123d) — pure Python, no MCP dependency.

```python
from build123d_drafting import dim_linear, leader, view_axes, lint_drawing
```

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

## Installation

Install directly from GitHub (no PyPI release yet):

```
pip install git+https://github.com/pzfreo/build123d-drafting-helpers.git
```

Or with uv:

```
uv add git+https://github.com/pzfreo/build123d-drafting-helpers.git
```

Requires `build123d >= 0.7.0` and Python ≥ 3.10.

## Status

Alpha. API may change. Developed alongside [build123d-mcp](https://github.com/pzfreo/build123d-mcp).
