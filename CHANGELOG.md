# Changelog

## Unreleased

### Deprecated

- **Feature recognition and linting** — these subsystems have moved to the
  `draftwright` package (its ADR 0007, "helpers renders; draftwright reasons").
  The recognition API (`find_holes`, `find_bosses`, `find_hole_patterns`,
  `analyse_cylinders`, `feature_diameters`, `full_cylinders`, and the
  `HoleFeature`/`BossFeature`/`CounterBore`/`HoleSpec`/`BoltCircle`/
  `LinearArray`/`RectGrid` types) and the lint API (`lint_drawing`,
  `find_overlaps`, `find_interferences`, `LintIssue`) are now **vendored-and-
  frozen** here: importing any of them through `build123d_drafting` emits a
  `DeprecationWarning`. Nothing is removed — the copies still work — but they no
  longer change and will be deleted in a future major version. Use `draftwright`
  for recognition and linting. The rendering substrate (annotation objects,
  `draft_preset`/`set_page`, `view_axes`/`ViewCoordinates`, `place_dims`/
  `place_labels`) is unaffected.

## v0.13.0 — 2026-06-27

### Added

- **Deterministic, platform-independent text** (#172) — every label is now
  rendered from a bundled font *file* instead of a font *name*. build123d's
  `Draft.font` defaults to the name `"Arial"`, which the OS font stack silently
  substitutes when absent (Liberation/DejaVu on Linux, real Arial on macOS),
  giving different glyph outlines and metrics per platform and drifting a whole
  sheet by ~1 mm across operating systems. The helpers now default to a bundled
  **Liberation Sans** (SIL OFL 1.1), exposed as `DEFAULT_FONT_PATH`, so glyph
  geometry is identical everywhere.
  - **`draft_preset(font_path=...)`** pins any font file; `font_path=None` opts
    out and resolves the `font` name through the OS font stack as before.
  - Any `Draft` honours a `font_path` attribute (`draft.font_path = "/f.ttf"`);
    a path always takes precedence over the `font` name.

### Changed

- Default rendered typeface is now bundled Liberation Sans rather than the
  OS-resolved `Draft.font` name. Liberation Sans is metric-compatible with Arial,
  so layout shifts are negligible; pass `font_path=None` for the previous
  name-based behaviour.

## v0.12.1 — 2026-06-26

### Added

- **Public contracts for downstream consumers** (#169) — stable equivalents of
  three previously private names that `draftwright` depended on:
  - **`full_cylinders(cyls)`** (was `_full_cyls`) — the feature-relevant ("full")
    cylinder records within one `analyse_cylinders` list.
  - **`HoleSpec`** + **`HoleSpec.from_hole(hole)`** (was `_spec_key`) — a frozen,
    hashable machining-spec value usable as a dict/set key for grouping holes.
  - **`TitleBlock.cell_bbox(name)`** and **`TitleBlock.drawn_by_cell_bbox()`**
    (replacing the need for the private `_TB_COL_FRACTIONS`) — build-frame
    bounding box of a named title-block cell.

## v0.12.0 — 2026-06-21

### Added

- **`RectGrid`** pattern type — `find_hole_patterns` now recognises a
  fully-populated N×M rectangular grid of identical holes as one pattern, with
  `rows`, `cols`, `row_pitch`, `col_pitch`, `angle`, and `center` (#126). A 2×2
  set is deliberately excluded (four lattice corners are a rectangle, not a
  grid). Tests cover 3×2 / 4×3 / 4×2 and a square grid.

### Changed

- **`find_hole_patterns` now sub-clusters within each machining-spec group**
  instead of fitting the whole group as a single bolt circle or line (#144).
  All candidate sub-patterns are enumerated and allocated greedily
  largest-first, so one spec can contribute several patterns and each hole
  belongs to at most one: two separate bolt circles of the same screw yield two
  `BoltCircle`s, and a rectangular perimeter / ring is reported as its edge
  `LinearArray` rows. Validated on the NIST CTC-02 model: `find_hole_patterns`
  goes from **0 → 14 patterns** (the ⌀10.1 outer perimeter and ⌀8.38 inner ring
  are now recognised, where previously nothing was).

### Fixed

- **`_as_linear_array` no longer drops near-axis rows that carry float noise.**
  Endpoints were chosen by a lexicographic `(x, y)` sort, which the sub-micron
  perpendicular jitter of real STEP geometry could reorder — picking an interior
  point as an endpoint, halving the measured span and pitch, and rejecting a
  valid array. Endpoints are now the farthest-apart pair, correct for any
  orientation. (Pre-existing; surfaced by the heavier use of this path in the
  pattern sub-clustering.)

## v0.11.0 — 2026-06-20

### Added

- **`feature_diameters(part, cyls=None)`** — the sorted unique diameters of the
  *recognised* dimensionable cylindrical features (hole bores, their
  counterbore/spotface steps, and bosses). Use this for coverage checks instead
  of the raw `_full_cyls` patch inventory: it is built from `find_holes` /
  `find_bosses`, so partial cylinders that never become a real feature — slot
  ends and interrupted recesses — are excluded, while genuine counterbore/
  spotface steps are kept (#158). Validated on NIST CTC-02: drops the phantom
  ø60/ø75/ø115 slot-ends, keeps the real ø100/ø120 spotfaces and all bores.
- **`TitleBlock(legal_owner_label=...)`** — per-row control of the full-width
  owner/origin caption, independent of `show_labels` (#163). Pass `None` to omit
  it (e.g. when the row holds a self-attribution URL) or a string to rename it;
  the default keeps `LEGAL OWNER`. Backwards-compatible.
- **`ViewCoordinates.from_viewport(...)`** (and a `page_basis=` kwarg) — carries
  the full projection basis so `pp()` is correct on ISO/oblique views (#145).

### Changed

- **`Centerline`, `CenterMark`, and `CenterlineCircle` now draw ISO 128
  long-dash–short-dash chain lines** instead of solid lines (#154). The `draft`
  argument is finally honoured: dash and gap lengths scale from
  `draft.font_size`, and the pattern is symmetric about the centre with the
  outermost dash reaching each end so the centreline's extent (and the
  lint/`place_labels` clearance geometry) is unchanged. A path too short for a
  pattern stays solid — small centre marks remain solid crosses, as ISO draws
  them.
- Internal cleanup (#150): deduplicated the label parser, the Liang–Barsky
  segment clip, and the half-circle arc split (`_circle_arcs`); shared the GD&T
  frame cell geometry between `FeatureControlFrame` and
  `CompositeFeatureControlFrame`; memoised hole/boss end classification within a
  call; removed dead code and tidied `math.hypot`/projection usage. Identical
  output.
- A stroke that fails to `trace()` in `_strokes_and_text` is now logged at
  warning level (with the offending edge) instead of being silently dropped
  (#150).

### Fixed

- **`ViewCoordinates.pp()` now projects ISO/oblique views correctly (#145).**
  `view_axes()` collapses each world axis onto a single page direction with a
  ±1 sign — exact for orthographic views but lossy for ISO/oblique. `pp()` now
  uses the true world-space `(page_x, page_y)` projection basis (the same camera
  axes build123d's `project_to_viewport` uses); the orthographic path is
  unchanged, and an ISO mapping built from a bare `view_axes()` result now raises
  rather than return a wrong point.
- **`lint_drawing` no longer recomputes optimal bounding boxes O(n²) times
  (#161).** The pairwise label-overlap loop computed each label-less item's
  `bounding_box(optimal=True)` once per *pair*; each item's compare-box is now
  computed once before the loop. Output is byte-identical, ~60× faster on
  complex parts.
- **`lint_drawing` can reuse projected-view edge boxes across repeated lints
  (#143).** A new optional `view_edge_cache` dict lets a caller that lints the
  same view objects repeatedly persist each view's per-edge bounding boxes;
  omitting it keeps the previous per-call behaviour exactly.
- **`annotate(label=...)` is now actually read by `lint_drawing()` (#146).** The
  stored `_annotate_label` was never read; lint now resolves an item's label as
  its `.label` falling back to `_annotate_label`.
- **Count-prefixed diameter labels no longer misparse in lint (#148).** A
  shared `_label_value()` helper reads `N× ⌀d` as the diameter `d` (not the
  count) while a bare `N× v` pitch label stays a span of `N·v`.
- **`SurfaceFinish` metadata docstring corrected (#147)** — the attribute is
  `.mark_position` (avoids shadowing build123d's inherited `.position`).
- **`SafeDimension.label` is cleared when it falls back to a bare edge** —
  previously it reported a label the geometry didn't render.

## v0.10.1 — 2026-06-18

### Fixed

- **Coaxial bores in separate solids are kept distinct (#68).** `find_holes`
  grouped cylinder patches purely by axis line, so two collinear bores in
  different bodies of a multi-solid assembly merged into one hole — measuring a
  depth across the gap between the bodies (e.g. ⌀9.8 ↓111.4 where each bore is
  only ~12 deep). `analyse_cylinders` now tags each cylinder record with its
  owning solid and `_line_key` includes it, so cross-body coaxial bores no
  longer combine. Single-solid parts are unchanged; a bore split into arcs
  *within* one solid (a keyway) still recombines.

## v0.10.0 — 2026-06-14

Automated drawing engine moved to the [draftwright](https://github.com/pzfreo/draftwright) package.

### Removed

- **`make_drawing`, `build_drawing`, `Drawing`, `choose_scale`, `generate_script`,
  `lint_feature_coverage`, `dedup_diams`, `fix_svg_page_size`, `analyse_face_levels`**
  — the full automated drawing engine is now in the `draftwright` package (AGPL-3.0).
  These names are no longer exported from `build123d_drafting`.
- **`kiwisolver`** runtime dependency removed (used only by the engine).
- **`make-drawing` CLI** entry point removed (use `draftwright` CLI instead).

### Migration

```python
# Before (0.9.x)
from build123d_drafting import make_drawing, build_drawing, choose_scale

# After (0.10+)
from draftwright import make_drawing, build_drawing, choose_scale
```

Install: `pip install draftwright`

## v0.9.1 — 2026-06-14

ISO 128-50 section hatching and ISO 128-44 solid filled arrows (#100, #125, PR #139):

### Added

- **ISO 128-50 section hatching** (#125): 45° hatch lines are drawn on the
  exposed cut face of Section A–A views using the even-odd fill rule.  Outer
  and inner wires (holes) are both included so holes correctly appear un-hatched.
  Hatching is excluded from the `view_annotation_overlap` lint check via the
  `is_section_hatch` marker.
- **ISO 128-44 solid filled arrows** (#100): cutting-plane end indicators now
  use build123d `Arrow` with `HeadType.STRAIGHT` (solid filled head) instead of
  open barbs, matching the ISO 128-44 standard.

### Fixed

- **Even-odd hatch vertex bug**: hatch lines passing exactly through a polygon
  corner vertex previously produced an odd-length intersection list, silently
  dropping the final interior span.  Switched to a half-open intersection
  interval `[0, 1)` so each shared vertex is counted once.

## v0.9.0 — 2026-06-13

Two-pass layout sizing (#131) and ISO 128-44 section arrows (#134):
bore callout widths are now estimated from hole geometry before view
positions are fixed, eliminating overflow of callout labels into adjacent
views and extension lines. Section A–A cutting-plane indicators now carry
proper ISO 128-44 wing stubs and open arrowheads.

### Added

- **Two-pass layout** (#131, PR #131): `_measure_strips()` estimates bore
  callout widths and step-dimension depth before `choose_scale()` fixes
  the inter-view corridors. Introduces `StripDepths` dataclass and
  `_est_bore_callout_width()`. Eliminates the hard-coded 18 mm floor that
  caused labels to overflow on wide callouts (e.g. "4× ⌀15.9 THRU" = 23.4 mm
  on CTC-03). Also fixes the razor-fit pv_below strip (#130) and supersedes
  the old ad-hoc corridor widening (#103).
- **ISO 128-44 section end arrows** (#134, PR #134): perpendicular wing stubs
  and open 2-barb arrowheads at both ends of the SECTION A–A cutting-plane
  indicator on the plan view, pointing in the direction of view.

### Fixed

- **Bore callout shafts** (#127): leaders no longer cross the view boundary
  when no section line is present; elbow offset is zero in that case.
- **Plan-view callout / dim_locy collision**: bore callout labels that would
  overlap with dim_locy extension lines are reassigned to the left strip.
- **BoltCircle suffix in width estimate**: "EQ SP ON øD BC" suffix (~34 mm)
  is now included in `_est_bore_callout_width`, preventing underestimated
  gaps for bolt-circle parts.
- **`_fmt()` float noise** (#133, PR #133): near-integer floats from
  STEP-imported bounding boxes (e.g. `800.0000000000001`) now round to
  `"800"` instead of `"800.0"`.
- **PMI presentation geometry**: non-solid annotation curves from AP242 STEP
  files are stripped at load time, eliminating phantom lines in all drawing
  views. Closes #135 (initial drop; full PMI annotation support is future work).

## v0.8.0 — 2026-06-13

Two-pass Cassowary bore-callout layout (#123, PR #124): replaces the old
greedy single-pass ladder with a boundary-assignment pass (nearest left/right
strip per hole group) followed by a Cassowary constraint-solver Y-placement
pass. Leaders stay within the view's projected Y extent, are at least
`min_gap` apart, and land near their natural (hole-centre) Y position.
Adds `kiwisolver>=1.4,<2` as a runtime dependency.

### Changed

- **Bore-callout elbow offset** reduced from `0.6 × DIM_PAD` (~10.8 mm) to
  `draft.arrow_length` (~2.7 mm). Elbows sit just past the view boundary to
  clear section-line extension lines; shafts no longer cut deeply across the
  view outline.
- **Overflow handling**: when the solver cannot fit all callouts in the
  available Y range it now places the longest feasible prefix (greedy
  fallback) rather than dropping the entire strip.
- **Side-view right limit** correctly uses `PAGE_W − margin` for callouts
  below the iso view, restoring the pre-refactor behaviour on drawings where
  `iso_above_tb=False`.

### Added

- `kiwisolver>=1.4,<2` runtime dependency (Cassowary constraint solver).
- Internal helpers `_solve_strip_ys` and `_greedy_strip_ys` (with
  `prefix=False` / `prefix=True` modes).

## v0.7.0 — 2026-06-12

The zero-LLM prismatic-drawing roadmap (#91–#95; PRs #97, #98, #99): a
CTC-02-class machined plate now produces a lint-clean, shop-usable sheet —
grouped hole callouts, patterns, locations, sections, centre marks — with no
LLM input. Known seams are tracked in #100.

### Added

- **Baseline hole-location dimensions** (#93): X dims above the plan view
  and Y dims above the side view (which maps world Y horizontally — the
  plan's left margin fits barely one tier), measured from a default datum
  corner (minimum-X/Y), one reference per pattern (bolt-circle centre,
  array first hole) plus each unpatterned hole, deduped, capped at 4
  (largest first) and skipped when a tier would leave the page.
- **Automatic section views** (#94, conservative core): when a Z-axis hole
  has a counterbore/spotface or a non-through bottom, a full SECTION A–A is
  cut through the densest row of qualifying hole axes and placed right of
  the side view (skipped with a log when there is no room), with a caption
  and a cutting-plane centreline + 'A' letters on the plan view. Hatching
  and proper section arrows are staged for a follow-up.

- **Hole-pattern recognition** (#92): `find_hole_patterns(holes)` returns
  `BoltCircle` / `LinearArray` records for ≥3 identical-spec holes equally
  spaced on a circle or collinear at constant pitch. `make_drawing` uses
  them: bolt circles get an `EQ SP ON øD BC` callout suffix plus a
  `CenterlineCircle` pitch-circle centreline, linear arrays a `(n-1)× pitch`
  dimension placed outside the view (the `label_vs_measured` lint now
  understands repetition labels). A pattern annotates only when it accounts
  for the whole spec group.
- **Count-aware coverage lint** (#92, the #80 rewiring deferred from #87):
  structured callouts declare how many holes they dimension
  (`covers_count`, from the `n×` prefix) and `lint_feature_coverage`
  compares against the part's per-diameter hole counts — a shortfall yields
  `feature_count_mismatch`. Free-text ø-labels stay exempt.

- **Automatic hole callouts on prismatic parts** (#91, the grouping half of
  #92): `make_drawing()` / `build_drawing()` now annotate every recognised
  hole from the #87 feature records — leader-attached `HoleCallout`s with
  count grouping (`4× ø10 THRU`), blind depths, and counterbore symbols, one
  per distinct hole spec, in the view the hole's axis is normal to. Callouts
  are width-checked against the page layout (right of plan/side, below
  front, with left-side fallback) and skipped with a log message rather than
  force-placed. Their structured coverage feeds `lint_feature_coverage`:
  when the layout has room the auto-generated prismatic sheet passes the #80
  lint out of the box, and anything skipped (more than 4 specs per view —
  the largest diameters win — or no clear strip) is logged and surfaces as
  `feature_not_dimensioned`, marking exactly what needs a manual callout.
- **`CenterMark(point, size)`** (#95): crosshair centre marks, added
  automatically for every hole in its normal view (all part classes); exempt
  from view-overlap lint like `Centerline`.
- **`Leader(..., callout=...)`**: a leader can carry a symbol-built sketch
  (e.g. `HoleCallout`) in place of its text label; the callout's bbox becomes
  `label_bbox` and its `covers_diameters` is surfaced on the leader.
- `find_holes()` gains a `cyls=` parameter (precomputed `analyse_cylinders`
  result) mirroring `lint_feature_coverage`, so the drawing pipeline reuses
  one face scan.

## v0.6.0 — 2026-06-12

### Added

- **`find_holes(part)` / `find_bosses(part)`** (#87): public feature
  recognition. Coaxial internal cylinders group into one `HoleFeature` per
  drilled hole (drill + counterbore + spotface), with the bottom classified
  as through / flat / drill-point by probing adjacent faces; entry chamfers
  and countersinks are recognised as openings; bores interrupted by crossing
  holes are recombined; slot end caps are excluded. `find_bosses` reports
  external segments (including a turned part's OD). The cylinder analysis
  moved from `make_drawing` into a new `features` module (old import paths
  still work).

### Fixed

- **OD/bore-leader exclusion is exact by construction** (#86): `od_diam` is
  snapped to its dedup representative, so the bore-leader filter cannot
  duplicate the OD callout if cylinder records ever carry unrounded OCCT
  diameters.

## v0.5.0 — 2026-06-11

All four fixes come from issues found by a one-shot engineering-drawing
benchmark run (NIST CTC-02); see #80–#83. Merged as #84.

### Added

- **`TextBlock(lines, position, draft, ...)`** (#82): multi-line note
  primitive with a shared baseline grid, corner anchoring (default top-left),
  configurable `line_spacing` and `align`, and rotation about its own anchor.
  Registers as a single annotation for layout/lint purposes.
- **`lint_feature_coverage(part, annotations, tol=0.15)`** (#80): coarse
  check that every full cylindrical feature (hole or boss) on the part has a
  matching ø callout on the sheet — via ø-labels or structured `HoleCallout`
  diameters (`covers_diameters`). Partial patches (fillets) are excluded, but
  slot- or keyway-split bores are recombined and still counted. Runs
  automatically from `Drawing.lint()` / `export()` when the drawing knows its
  part, so `lint()` can now return `feature_not_dimensioned` warnings.

### Fixed

- **Prismatic parts no longer get turned-part auto-annotations** (#81).
  `make_drawing()` now classifies the part: OD dimension, centrelines, and
  bore leaders are only emitted when an external, full, concentric Z cylinder
  fills a square envelope (a turned part). Plates with bores or corner
  fillets previously picked up a bogus `dim_od` from the largest internal
  radius. Trade-off: a shaft with a flat exceeding ~5% of the envelope now
  classifies prismatic and loses the auto-OD — the coverage lint flags the
  undimensioned diameter as the safety net.
- **Export no longer dies with a bare `AssertionError`** (#83). Shapes are
  exported element-wise with per-element error recovery: a failing face or
  edge is skipped with a warning naming the view and layer, and the export
  completes. Only when *nothing* in a shape can be converted does it raise,
  now with view/layer context.
- **`Note(align=...)` anchors at `position`** (#82). The align argument was
  re-anchoring the whole sketch to the page origin, discarding the requested
  position.

## v0.4.3 — 2026-06-11

### Added

- **`auto_dims=False`** (#74) on `make_drawing()` / `build_drawing()` skips the
  automatic dimensions, centrelines, and leaders — which assume a turned part
  and are wrong for prismatic geometry — while keeping views, scale, page, and
  title block. `Drawing.clear_annotations(keep=("title_block",))` removes the
  automatic set wholesale without knowing the auto-name scheme.
- **Iso view fit-check with auto-shrink** (#75). When the projected iso view
  overflows its page region (the layout reserves ~0.7 × bbox_max, but long
  prismatic parts project wider), it is re-projected at a clean fraction of
  sheet scale (1/2, 1/5, 1/10) and captioned with an "ISO VIEW (NTS)" note.
- **`Note`** — a free-text annotation (text rendered as faces, like every other
  helper) for view captions and sheet notes, with `.label` / `.label_bbox`
  metadata.
- **`view_out_of_bounds` lint** (#75) — when page bounds are known
  (`page_bbox` or `set_page()`), any view extending past the drawable area is
  flagged as an error.

### Changed

- **`view_annotation_overlap` lint now tests against the view's projected
  edges, not its bounding box** (#76). On a large part the view bbox is mostly
  blank face, where placing callouts is a legitimate convention — those no
  longer warn, so the check can be used as a pass/fail gate. A label inside
  the view extents but over a blank region is reported as a new info-level
  `view_annotation_inside_extents` notice instead. Straight edges are tested
  exactly; curved edges are sampled at ~1 mm spacing. `LintIssue.severity`
  gains `"info"`.

## v0.4.2 — 2026-06-10

### Added

- **Enlargement scales for small parts** (#62). `choose_scale()` now tries
  ISO 5455 enlargement scales — 10:1 (A4) and 5:1 (A4, A3) — before the
  existing 2:1 entry, so small precision parts get legible drawings instead
  of a 1:1 sheet with tiny views. The fit check also no longer reserves
  title-block width when the view rows clear the title block vertically,
  which lets borderline parts stay on smaller sheets (e.g. an 80 mm cube now
  lands on A3 1:1 instead of A2).
- **`scale=` and `page=` overrides** (#63) on `make_drawing()`,
  `build_drawing()`, and `choose_scale()`, plus `--scale` / `--page` CLI
  flags. `page` accepts an ISO name (`"A3"`), `"WIDTHxHEIGHT"` in mm, or a
  `(width, height)` tuple. Give one and the other is chosen to fit; give both
  and they are used as-is.
- **`Leader` / `leader_offset` gain `text_side="auto"|"left"|"right"`** (#64)
  to force which side of the elbow the label extends to, and the default
  placement rule is now documented: the label follows the horizontal
  direction of tip → elbow (right when the elbow is right of the tip, left
  when left of it; vertical leaders place it right). A forced side that
  would run the shaft through the label text raises `ValueError` instead of
  silently producing a struck-through label. The `align` parameter is also
  documented — it positions the whole sketch, not the label side.

### Fixed

- **`view_annotation_overlap` lint no longer fires on line-work that must
  touch the view** (#65). Centrelines are exempt from the check, and
  annotations exposing a `label_bbox` (dimensions and leaders) are
  tested by their label-text extents only, so witness lines and leader
  shafts can enter the view without producing warnings. A label actually
  sitting on the part still fires.
- **`DatumFeature` and `SurfaceFinish` expose a real `label_bbox`** (#69) —
  the letter frame and the Ra text extents respectively — so the
  label-text-only lint logic from #65 covers them too: the datum triangle
  and the finish check-mark sit on the part by design and no longer trip
  `view_annotation_overlap` (or inflate `annotation_overlap` /
  centerline-overlap checks, which also prefer `label_bbox`).
- **`DatumTarget` is exempt from `view_annotation_overlap`** (#71). The
  circled identifier sits on the part face by definition (ISO 5459), so the
  check warned on every correct placement — and unlike #69 a `label_bbox`
  cannot help, since the symbol's full bbox *is* the circle + text. It now
  carries an `is_datum_target` marker (mirroring `is_centerline`) that the
  view-overlap check skips; page-bounds and pairwise overlap checks still
  apply.

## v0.4.1 — 2026-06-07

### Added

- **`build_drawing()` and the `Drawing` builder** — the composable form of
  `make_drawing()`. `build_drawing(...)` returns a live `Drawing` with the
  standard four views projected and the automatic dimensions + title block
  already added, but not yet exported. Customise it before writing files:
  `dwg.add(obj, name)` / `dwg.remove(name)` to edit annotations,
  `dwg.at(view, x, y, z)` to map a world point to page coordinates in any view,
  `dwg.add_view(name, shape, camera, up, position)` for section/auxiliary views,
  then `dwg.export(out)`. `make_drawing(...)` is now exactly
  `build_drawing(...).export()` — fully backward compatible.
- **`generate_script()` / `make-drawing --script` emit a `build_drawing()`
  script** with the customisation block placed *before* export, so hand- or
  LLM-edited annotations actually land in the output (previously the seam sat
  after `make_drawing()` had already written the files, making it a no-op).

### Fixed

- **Generated drawing scripts are now written as UTF-8.** `generate_script()` /
  `make-drawing --script` previously used the platform default codec and crashed
  on Windows (cp1252) on the non-ASCII characters in the script's comment
  separators.

## v0.4.0 — 2026-06-07

### Added

- **`make_drawing()` — automated STEP → SVG + DXF drawing pipeline** (#50, #53, #56).
  Analyses part geometry, chooses scale and ISO page size, projects four views,
  and annotates diameter callouts, centrelines, and an ISO 7200 title block with
  no drawing code required.
- **`make-drawing` CLI** entry point wrapping `make_drawing()`, with `--title`,
  `--number`, `--tolerance`, `--drawn-by`, `--out`, and `--script` flags.
- **`make_drawing()` accepts a build123d object** (`Part`, `Solid`, `Compound`)
  as well as a STEP file path — draw in-memory geometry with no STEP round-trip
  (#58). Object input defaults `out` to `"drawing"`; `generate_script()` remains
  STEP-only.
- **New public API** promoted from pipeline internals (#52): `choose_scale`,
  `fix_svg_page_size`, `analyse_cylinders`, `analyse_face_levels`, `dedup_diams`,
  `generate_script`, and `ViewCoordinates`.

### Documentation

- README now documents the automated pipeline (CLI + Python API).

## v0.3.2 — 2026-06-05

### Added

- **`TitleBlock` gains `revision`, `legal_owner`, and `show_labels` parameters**
  for ISO 7200:2004 compliance (#45).
  - `revision` (field 4) takes priority over `date` in the top-right cell.
  - `legal_owner` (field 1) adds a full-width third row above the content rows;
    `block_bbox["height"]` grows by one `cell_height`.
  - `show_labels=True` (default) renders small field-identifier labels
    ("TITLE", "DWG NO.", "SCALE", "MAT.", "REV"/"DATE", "GEN. TOL.", "DRAWN BY",
    "LEGAL OWNER") in the bottom-left corner of each cell.

### Fixed

- **`find_overlaps()` adds an AABB pre-filter** (#38) to skip expensive OCC
  boolean operations for obviously non-overlapping pairs, improving performance
  on drawings with many annotations. Boolean failures now surface as
  `geometry_check_failed` warnings instead of raising.

## v0.3.1 — 2026-06-04

### Added

- **`lint_drawing()` gains a `view_shapes` parameter** (#159, #160) for
  checking projected view outlines against the rest of the drawing.
  Pass a list of build123d shapes representing the view bounding regions;
  `lint_drawing()` emits `view_annotation_overlap` (warning) when an
  annotation's bbox overlaps a view outline, and `view_overlap` (warning)
  when two view outlines overlap each other.

### Fixed

- **`pyproject.toml` version corrected to `0.3.1`** — the v0.3.0 release
  tag and GitHub release were created but `pyproject.toml` was never bumped
  from `0.2.0`.

## v0.3.0 — 2026-06-02

### Added

- **`set_page()` and `annotate()` are exported from the package** (#148) so
  standalone scripts (no MCP) can enable page-bounds checking and attach lint
  metadata: `from build123d_drafting import set_page, annotate, clear_page`.
  `lint_drawing()` gains an optional `page_bbox=(min_x, min_y, max_x, max_y)`
  (or reads the module-level context set by `set_page()`); any annotation whose
  bounding box extends past the drawable area is flagged `annotation_out_of_bounds`.
- **Native drawing-scale support (`drawing_scale`)** so small parts can be drawn
  enlarged (e.g. a 7.5 mm feature at 5:1) without `lint_drawing()` raising false
  `label_vs_measured` errors (#147). `lint_drawing(items, drawing_scale=5.0)`
  divides each measured path length by the scale before comparing it to the
  label, so labels carry the *real* dimension while the geometry is drawn scaled.
  `TitleBlock(..., drawing_scale=5.0)` derives the printed "5:1" indicator from
  the same number — one source of truth for both the lint and the title block.
  New `format_drawing_scale(scale)` helper formats the ISO string ("5:1", "1:2",
  "1:1"); `drawing_scale` defaults to `1.0`, so existing 1:1 drawings are
  unchanged.

### Fixed

- **`annotation_overlap` no longer false-positives on stacked dimensions** (#149).
  The overlap check now compares each annotation's `label_bbox` (the keep-clear
  region around the value text) instead of its full bounding box, which included
  witness lines that legitimately overlap for baseline-stacked dims — making a
  zero-violation lint unreachable for almost any real drawing. Falls back to the
  full bounding box when an item exposes no `label_bbox`.

### Documentation

- **Title-block overflow is catchable via the page-bounds check** (#151). A
  `TitleBlock` whose long text (e.g. a verbose subtitle) spills past its frame
  grows its bounding box, so passing it to `lint_drawing()` as an item with a
  `page_bbox` (or after `set_page()`) flags it as `annotation_out_of_bounds`.
  Documented in the README and `lint_drawing` docstring, with a regression test.
  Also corrected the README `lint_drawing` signature to include `page_bbox`.

### Examples

- **`examples/part_drawing.py` now draws a real [`bd_warehouse`](https://github.com/gumyr/bd_warehouse)
  fastener** (`HexHeadScrew` + `HexNut`) instead of geometry built from scratch. Every
  dimension and callout is pulled from the bd_warehouse object — change `BOLT_SIZE` /
  `BOLT_LENGTH` and the views, length / across-flats dims, thread designation and title block
  all reflow (the lint stays clean because label values share the part's source of truth).
  `bd_warehouse` is an example-only dev dependency; the library's runtime deps are unchanged
  (build123d only).

## v0.2.0 — 2026-06-01

### Changed (breaking)

- **Builders are now native build123d `BaseSketchObject` subclasses.** Every
  annotation builder returns a real `Sketch`: it composes inside a
  `BuildSketch`, can be `.moved()` / `.rotate()`d, exported directly, and
  queried with `.faces()` / `.bounding_box()`. The old functions-returning-
  `@dataclass`-`*Result` model is gone (clean break, no aliases).
- **`*Result` dataclasses removed.** `DimResult`, `CenterlineResult`,
  `LeaderResult`, `TitleBlockResult`, `SurfaceFinishResult`,
  `FeatureControlFrameResult`, `CompositeFeatureControlFrameResult`,
  `DatumFeatureResult`, `DatumTargetResult`, `HoleCalloutResult` are all
  deleted.
- **`add_to_layers()` removed.** Lines now render as thin filled *faces*, so
  there is a single ink layer — no `.lines` / `.text` split, no flooding of
  closed loops (the `⌀` prefix, modifier rings, GD&T glyphs all stroke
  cleanly). Render/export is one call: `exporter.add_shape(obj, layer="ink")`.
- **Function → class renames** (PascalCase, native build123d style):
  - `dim_linear()` → `Dimension`
  - `safe_dim_line()` → `SafeDimension`
  - `centerline()` → `Centerline`
  - `leader()` → `Leader`
  - `iso_title_block()` → `TitleBlock`
  - `surface_finish_mark()` → `SurfaceFinish`
  - `feature_control_frame()` → `FeatureControlFrame`
  - `composite_feature_control_frame()` → `CompositeFeatureControlFrame`
  - `datum_feature()` → `DatumFeature`
  - `datum_target()` → `DatumTarget`
  - `hole_callout()` → `HoleCallout`
- **Metadata moved onto the objects.** Each object carries `.label`,
  `.label_bbox`, `.segments`, plus type-specific attrs (`.measured_length`,
  `.dim_level_y`, `.is_basic`, `.elbow`, `.tip`, `.is_centerline`,
  `.characteristic`, `.tolerance_str`, `.datums`, `.identifier`, `.letter`,
  `.mark_position`, `.block_bbox`, …). `SurfaceFinish` exposes its tip via
  `.mark_position` (not `.position`, which is a read-only `Shape` property).
  The lint metadata (`.label_bbox` / `.segments`, and a leader's `.tip` /
  `.elbow`) is **transform-aware**: cached in the build frame and exposed
  through the object's current `.location`, so `.moved()` / `.located()` /
  `.rotate()` and the construction-time `rotation` / `align` all keep it
  consistent with the geometry — lint a moved/rotated object and it still
  checks the right region. A shared `_Annotation` base centralises this.
- **Kept as functions** (orchestrators / pure math): `place_dims()` and
  `place_labels()` now return `list[Dimension]`; `leader_offset()` returns a
  `Leader`; `view_axes()` and `draft_preset()` are unchanged.

### Changed

- **`lint_drawing()` is now generic and duck-typed.** Dispatch is by attribute
  presence (`.elbow`, `.measured_length`, `.is_centerline`), not `isinstance`,
  so it works on the new objects and on lightweight `SimpleNamespace`
  stand-ins. All existing `LintIssue` codes are preserved.
- **`find_interferences()` is now duck-typed.** Each item is decomposed via
  `getattr(item, "label_bbox", …)` and `getattr(item, "segments", …)`, falling
  back to the item's own face bbox and straight LINE edges. Same checks and
  codes as before.

### Added

- **`find_overlaps(sketches, *, min_area=0.01)`** — pure-geometry collision
  detection: flags pairs of sketches whose *filled faces* actually intersect
  (boolean `a & b`, area threshold). Works on any build123d `Sketch` with zero
  metadata. Code `faces_overlap` (warning).

## v0.1.13 — 2026-06-01

### Features

- **Composite feature control frames** — `composite_feature_control_frame(characteristic,
  rows)` draws a multi-row ISO 1101 frame sharing one full-height characteristic cell (e.g. a
  composite position tolerance for a hole pattern: `| ⌖ | ⌀0.25 | A | B | C |` over
  `| ⌀0.1 | A |`). Each `rows` entry takes `tolerance`/`datums`/`diameter`/`modifier`/
  `datum_modifiers`. New `CompositeFeatureControlFrameResult`.
- **Hole callouts** — `hole_callout(diameter, count=…, through=…, depth=…, cbore_dia=…,
  cbore_depth=…, csink_dia=…, csink_angle=…)` builds a single-line note from geometry symbols
  (⌀ counterbore ⌴ countersink ⌵ depth ↧), e.g. `4× ⌀8.5 THRU` or `⌀8.5 ↧20 ⌴ ⌀15 ↧6`.
  New `HoleCalloutResult`. Replaces hand-typed hole notes like the `⌀8.5 thru` in
  `part_drawing.py`.
- **All-around / all-over leaders** — `leader(..., all_around=True)` draws the ISO 1101
  circle at the kink (profile applies all around the section); `all_over=True` draws the
  double circle. Rings are open half-arcs so they stroke without flooding on a fill layer.
- **Basic (theoretically-exact) dimensions** — `dim_linear(..., basic=True)` boxes the value
  in a rectangle per ISO 1101 / ASME Y14.5, the framed dimension that GD&T position and
  profile tolerances are located from. The box is four separate Edges, so it strokes
  cleanly even on a `fill_color` layer (no flood). `DimResult.is_basic` records the flag.
- **Datum targets** — `datum_target(identifier, area_label=None)` draws the ISO 5459
  divided-circle symbol: upper compartment = target-area size (e.g. `⌀6`, blank for a
  point/line), lower = identifier (e.g. `A1`). Returns a `DatumTargetResult` with the usual
  `.lines`/`.text` split for `add_to_layers()`. Connect to the target with `leader()`.
- **`find_interferences(..., obstacles=[...])`**: labels are tested against arbitrary
  obstacle boxes (`BoundBox` or `(min_x, min_y, max_x, max_y)`), e.g. the projected views
  of a multi-view drawing — flags `label_over_geometry`. Label-only by design (leader lines
  are meant to point *into* geometry). `examples/part_drawing.py` now uses it instead of a
  hand-rolled loop.

### Examples

- **Specimen sheet** now catalogues all twelve helpers (added `datum_target`,
  `composite_feature_control_frame`, `hole_callout`, `dim_linear(basic=True)` and
  `leader(all_around=True)`). Re-laid as a 4-column × 3-row grid on the same A3 sheet — the
  rightmost column's name leaders point left so labels stay inside the frame — and it still
  lints clean (`find_interferences`, 0 errors). `docs/specimen_sheet.png` (the README hero)
  regenerated.

### Bug fixes

- **`dim_linear` label_bbox wrong for vertical dims**: for a left/right (vertical) dim the
  label box was computed at the path X and the dim-line Y, but the label actually sits at
  the offset dim-line X and the path-midpoint Y (and is rotated 90°). Fixed — the box now
  matches the rendered label for any orientation. Improves every lint check that reads
  `label_bbox` (centreline overlap, interference) on vertical dims. Surfaced by the new
  obstacle check.

## v0.1.12 — 2026-06-01

### Examples

- **`examples/part_drawing.py`**: a worked M10 hex bolt + nut drawing demonstrating the
  end-to-end workflow — build the 3D part, `project_to_viewport()` into a third-angle set of
  front/top/side views plus an isometric per part, dimension with the helpers, `lint()` with
  `find_interferences()`, then export. Complements the specimen-sheet catalogue.

## v0.1.11 — 2026-06-01

### Features

- **`LeaderResult.label_bbox`**: `leader()` now records the label text bbox, and
  `lint_drawing`'s leader-through-text check prefers it (falling back to measuring
  `.text`). Lets a reconstructed leader without live text geometry still be linted
  (e.g. the MCP server delegating its leader check). Backward-compatible.

## v0.1.10 — 2026-06-01

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
