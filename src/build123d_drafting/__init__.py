"""build123d-drafting — drawing annotation helpers for build123d.

Every annotation builder is a native build123d ``BaseSketchObject`` subclass —
the returned object *is* a ``Sketch`` (composable in ``BuildSketch``,
``.moved()``-able, exportable directly). Import directly in your drawing
scripts — no MCP server required::

    from build123d_drafting import (
        Dimension, SafeDimension, Leader, Centerline,
        view_axes, lint_drawing, find_interferences, find_overlaps, LintIssue,
    )
"""

from build123d_drafting.make_drawing import fix_svg_page_size, generate_script, make_drawing
from build123d_drafting.helpers import (
    Centerline,
    CompositeFeatureControlFrame,
    DatumFeature,
    DatumTarget,
    Dimension,
    FeatureControlFrame,
    HoleCallout,
    Leader,
    LintIssue,
    SafeDimension,
    SurfaceFinish,
    TitleBlock,
    annotate,
    clear_page,
    draft_preset,
    find_interferences,
    find_overlaps,
    format_drawing_scale,
    leader_offset,
    lint_drawing,
    place_dims,
    place_labels,
    set_page,
    view_axes,
)

__all__ = [
    "fix_svg_page_size",
    "generate_script",
    "make_drawing",
    "Centerline",
    "CompositeFeatureControlFrame",
    "DatumFeature",
    "DatumTarget",
    "Dimension",
    "FeatureControlFrame",
    "HoleCallout",
    "Leader",
    "LintIssue",
    "SafeDimension",
    "SurfaceFinish",
    "TitleBlock",
    "annotate",
    "clear_page",
    "draft_preset",
    "find_interferences",
    "find_overlaps",
    "format_drawing_scale",
    "leader_offset",
    "lint_drawing",
    "place_dims",
    "place_labels",
    "set_page",
    "view_axes",
]
