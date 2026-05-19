"""build123d-drafting — drawing annotation helpers for build123d.

Import directly in your drawing scripts — no MCP server required:

    from build123d_drafting import (
        dim_linear, safe_dim_line, leader, view_axes, lint_drawing,
        DimResult, LeaderResult, LintIssue,
    )
"""
from build123d_drafting.helpers import (
    DimResult,
    LeaderResult,
    LintIssue,
    SurfaceFinishResult,
    TitleBlockResult,
    dim_linear,
    iso_title_block,
    leader,
    lint_drawing,
    safe_dim_line,
    surface_finish_mark,
    view_axes,
)

__all__ = [
    "DimResult",
    "LeaderResult",
    "LintIssue",
    "SurfaceFinishResult",
    "TitleBlockResult",
    "dim_linear",
    "iso_title_block",
    "leader",
    "lint_drawing",
    "safe_dim_line",
    "surface_finish_mark",
    "view_axes",
]
