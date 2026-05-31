"""build123d-drafting — drawing annotation helpers for build123d.

Import directly in your drawing scripts — no MCP server required:

    from build123d_drafting import (
        dim_linear, safe_dim_line, leader, view_axes, lint_drawing,
        DimResult, LeaderResult, LintIssue,
    )
"""
from build123d_drafting.helpers import (
    CenterlineResult,
    DatumFeatureResult,
    DimResult,
    FeatureControlFrameResult,
    LeaderResult,
    LintIssue,
    SurfaceFinishResult,
    TitleBlockResult,
    centerline,
    datum_feature,
    dim_linear,
    feature_control_frame,
    iso_title_block,
    leader,
    leader_offset,
    lint_drawing,
    place_dims,
    place_labels,
    safe_dim_line,
    surface_finish_mark,
    view_axes,
)

__all__ = [
    "CenterlineResult",
    "DatumFeatureResult",
    "DimResult",
    "FeatureControlFrameResult",
    "LeaderResult",
    "LintIssue",
    "SurfaceFinishResult",
    "TitleBlockResult",
    "centerline",
    "datum_feature",
    "dim_linear",
    "feature_control_frame",
    "iso_title_block",
    "leader",
    "leader_offset",
    "lint_drawing",
    "place_dims",
    "place_labels",
    "safe_dim_line",
    "surface_finish_mark",
    "view_axes",
]
