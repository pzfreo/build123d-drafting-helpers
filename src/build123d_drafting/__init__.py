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
    draft_preset,
    find_interferences,
    find_overlaps,
    leader_offset,
    lint_drawing,
    place_dims,
    place_labels,
    view_axes,
)

__all__ = [
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
    "draft_preset",
    "find_interferences",
    "find_overlaps",
    "leader_offset",
    "lint_drawing",
    "place_dims",
    "place_labels",
    "view_axes",
]
