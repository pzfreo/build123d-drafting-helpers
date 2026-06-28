# Drafting conventions and gotchas

Practical notes for annotating build123d drawings. Most of these encode hard-won
lessons about where `build123d`'s own drawing primitives surprise you, and which
helper to reach for instead.

> Automated feature recognition and drawing-quality linting now live in the
> separate [`draftwright`](https://github.com/pzfreo/draftwright) package — this
> library is the rendering substrate. The notes below cover building the
> annotation geometry by hand.

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

**Recommendation:** skip this table entirely and use `Dimension(side="above")` from `build123d_drafting`. It computes the correct sign from the path direction's right-hand normal — you name a side, it figures out the number.

```python
from build123d import Draft
from build123d_drafting import Dimension

draft = Draft(font_size=2.5, decimal_precision=1)
dim = Dimension((-20, -10, 0), (20, -10, 0), "below", 8, draft, label="40")
```

---

## 2. Label-wider-than-path crash

`DimensionLine` raises `ValueError: Can't get geom adaptor of empty wire` when the label text is wider than the dimension path. This happens silently on short dimensions with long labels (tolerances, units).

**Workaround:** use `SafeDimension(path, label, draft)` from `build123d_drafting`. It catches the error, truncates the label, and retries.

```python
from build123d_drafting import SafeDimension

dim = SafeDimension(my_edge, "1.5 ±0.05", draft)
```

---

## 3. Text needs a fill layer

`build123d.Text` returns filled `Face` shapes (glyph outlines). On an SVG layer with no `fill_color` set, the exporter renders glyphs as thick closed strokes with no fill — illegible at typical drawing sizes (font 2–3 mm).

Every helper object *is* a `Sketch` whose strokes and text are filled faces on a
single layer, so route it to one ink layer that sets **both** `line_color` and
`fill_color`:

```python
black = Color(0, 0, 0)
exporter.add_layer("ink", line_color=black, fill_color=black, line_weight=0.05)
exporter.add_shape(dim, layer="ink")
```

The `line_weight=0.05` keeps the stroke hairline-thin so the fill dominates.

---

## 4. Leader gap before label

A leader line drawn all the way to the label's centre point strikes through the text. The gap must be computed from the label bounding box.

`Leader(tip, elbow, label, draft)` from `build123d_drafting` handles this automatically — the line terminates cleanly before the text begins, and the arrowhead, shelf, and glyphs are all faces on one sketch.

```python
from build123d_drafting import Leader

ld = Leader((5, 5, 0), (20, 12, 0), "⌀7.93 H7", draft)
exporter.add_shape(ld, layer="ink")   # arrowhead + shelf + glyphs, one ink layer
```

---

## 5. Reach-for-what table

| If you want… | Use |
|---|---|
| A linear dim outside a view | `Dimension(p1, p2, side="below", distance=6, draft, label="40")` |
| Stack dims without computing offsets | `place_dims(specs, draft)` |
| Stack dims and clear centreline crossings | `place_labels(specs, draft, centerlines=[cl])` |
| Shift a single label past a centreline | `Dimension(..., label_offset_x=15)` |
| A diameter / hole callout | `Leader(tip, elbow, label, draft)` |
| A dim where the label might overflow | `SafeDimension(path, label, draft)` |
| A centreline to use with `place_labels` | `Centerline(p1, p2)` |
| ISO 1302 surface-finish symbol | `SurfaceFinish(ra_value, position, draft=draft)` |
| ISO title block | `TitleBlock(part_name, drawing_number, ...)` |
| Know which world axis maps where on the page | `view_axes(viewport_origin, viewport_up, look_at)` |

---

## 6. Centreline-label collision

Diameter and bore dimensions placed inline often have the dim line crossing a centreline at the label midpoint — the label text ends up on top of the centreline.

**Fix options, in order of preference:**

1. `place_labels(specs, draft, centerlines=[cl])` — auto-computes and applies the minimum shift.
2. `Dimension(..., label_offset_x=15)` — manual shift; positive = toward p2, negative = toward p1.
3. Use a `Leader()` instead — a leader always places its text to the side of the tip, never across it.
4. Increase `distance` so the dim line clears the centreline region entirely.

```python
from build123d_drafting import Centerline, place_labels

bore_cl = Centerline((0, -50, 0), (0, 50, 0))
dims = place_labels([
    ((-10, 0, 0), (10, 0, 0), "above", 8, "Ø5.0 H8"),
], draft, centerlines=[bore_cl])
```

`place_labels` uses each dim's precise `.label_bbox` (not the full annotation
bbox), so extension lines don't trigger false shifts.

---

## 7. Custom label strings survive — use `Dimension`, not vanilla `ExtensionLine`

Vanilla `build123d.ExtensionLine` does not expose the constructor label string after construction — `.label` is always `''`. If the label matters (tolerances, units, custom text), use `Dimension()` from `build123d_drafting`: it carries the exact label string through as `.label`.

```python
from build123d_drafting import Dimension

dim = Dimension(p1, p2, "above", 8, draft, label="40 ±0.1")
# dim.label == "40 ±0.1"   ← preserved
exporter.add_shape(dim, layer="ink")
```
