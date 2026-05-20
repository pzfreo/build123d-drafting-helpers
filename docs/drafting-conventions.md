# Drafting conventions and gotchas

Practical notes for annotating build123d drawings. Most of these encode hard-won lessons about where `build123d`'s own drawing primitives surprise you.

---

## 1. Offset sign convention for `ExtensionLine`

`build123d.ExtensionLine.offset` follows a right-hand-rule convention relative to the border direction (p1→p2). Getting the sign wrong is the single most common reason a dimension ends up on the wrong side of the part.

| Border direction (p1→p2) | offset sign | Dim lands on… |
|---|---|---|
| RIGHT (left→right) | − | above |
| RIGHT | + | below |
| LEFT (right→left) | − | below |
| LEFT | + | above |
| UP (bottom→top) | + | right |
| UP | − | left |
| DOWN (top→bottom) | − | right |
| DOWN | + | left |

**Recommendation:** skip this table entirely and use `dim_linear(side="above")` from `build123d_drafting`. It computes the correct sign from the path direction's right-hand normal — you name a side, it figures out the number.

```python
from build123d_drafting import dim_linear

draft = Draft(font_size=2.5, decimal_precision=1)
dim = dim_linear((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
```

---

## 2. Label-wider-than-path crash

`DimensionLine` raises `ValueError: Can't get geom adaptor of empty wire` when the label text is wider than the dimension path. This happens silently on short dimensions with long labels (tolerances, units).

**Workaround:** use `safe_dim_line(path, label, draft)` from `build123d_drafting`. It catches the error, truncates the label, and retries.

```python
from build123d_drafting import safe_dim_line

result = safe_dim_line(my_edge, "1.5 ±0.05", draft)
```

---

## 3. Text needs a fill layer

`build123d.Text` returns filled `Face` shapes (glyph outlines). On an SVG layer with no `fill_color` set, the exporter renders glyphs as thick closed strokes with no fill — illegible at typical drawing sizes (font 2–3 mm).

Always add a dedicated text layer with both `line_color` and `fill_color`:

```python
black = (0, 0, 0)
exporter.add_layer("text", line_color=black, fill_color=black, line_weight=0.05)
exporter.add_shape(dim.shape, layer="text")
```

The `line_weight=0.05` keeps the stroke hairline-thin so fill dominates.

---

## 4. Leader gap before label

A leader line drawn all the way to the label's centre point strikes through the text. The gap must be computed from the label bounding box.

`leader(tip, elbow, label, draft)` from `build123d_drafting` handles this automatically — the line terminates cleanly before the text begins.

```python
from build123d_drafting import leader

res = leader((5, 5, 0), (20, 12, 0), "⌀7.93 H7", draft)
exporter.add_shape(res.lines, layer="dims")   # arrowhead + shelf
exporter.add_shape(res.text,  layer="text")   # glyphs
```

Route `lines` and `text` to separate SVG layers, both with `fill_color` set.

---

## 5. Reach-for-what table

| If you want… | Use |
|---|---|
| A linear dim outside a view | `dim_linear(p1, p2, side="below", distance=6, draft, label="40")` |
| Stack dims without computing offsets | `place_dims(specs, draft)` |
| Stack dims and clear centreline crossings | `place_labels(specs, draft, centerlines=[cl])` |
| Shift a single label past a centreline | `dim_linear(..., label_offset_x=15)` |
| A diameter / hole callout | `leader(tip, elbow, label, draft)` |
| A dim where the label might overflow | `safe_dim_line(path, label, draft)` |
| A centreline to use with lint / place_labels | `centerline(p1, p2)` |
| ISO 1302 surface-finish symbol | `surface_finish_mark(ra_value, position, draft=draft)` |
| ISO title block | `iso_title_block(part_name, drawing_number, ...)` |
| Check all annotation quality issues | `lint_drawing([dim1, dim2, lea1, cl])` |
| Know which world axis maps where on the page | `view_axes(viewport_origin, viewport_up, look_at)` |

---

## 6. Recommended feedback loop

1. Build annotations with helpers and register them: `annotate(result, "name")`
2. Run `lint_drawing()` (or the MCP `lint_drawing` tool) to catch axis swaps and label mismatches before exporting.
3. Export SVG, then render with `render_drawing(svg_path)` to review visually.
4. Run `inspect_drawing()` (session mode, without `svg_path`) to verify bounding boxes and annotation metadata.

Catching problems at step 2 is cheaper than re-rendering and re-exporting.

---

## 7. Centreline-label collision

Diameter and bore dimensions placed inline often have the dim line crossing a centreline at the label midpoint — the label text ends up on top of the centreline.

**Detection:** pass `CenterlineResult` objects into `lint_drawing()`. The `label_centerline_overlap` check uses the precise label bbox (not the full annotation bbox) so false positives from extension lines are avoided.

**Fix options, in order of preference:**

1. `place_labels(specs, draft, centerlines=[cl])` — auto-computes and applies the minimum shift.
2. `dim_linear(..., label_offset_x=15)` — manual shift; positive = toward p2, negative = toward p1.
3. Use a `leader()` instead — a leader always places its text to the side of the tip, never across it.
4. Increase `distance` so the dim line clears the centreline region entirely.

```python
from build123d_drafting import centerline, place_labels, lint_drawing

bore_cl = centerline((0, -50, 0), (0, 50, 0))
dims = place_labels([
    ((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8"),
], draft, centerlines=[bore_cl])
issues = lint_drawing(dims + [bore_cl])   # should be empty
```

---

## 8. `annotate()` label limitation

Vanilla `build123d.ExtensionLine` does not expose the constructor label string after construction — `.label` is always `''`. As a result, `annotate(el, "width")` auto-derives a label from the measured dimension length rather than from whatever custom label you passed to the `ExtensionLine` constructor.

If the label matters (tolerances, units, custom text), use `dim_linear()` from `build123d_drafting` instead. It returns a `DimResult` that carries the exact label string through the whole pipeline.

```python
from build123d_drafting import dim_linear

dim = dim_linear(p1, p2, "above", 8, draft, label="40 ±0.1")
# dim.label_str == "40 ±0.1"   ← preserved
annotate(dim.shape, "width_dim")
```
