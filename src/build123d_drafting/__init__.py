"""build123d-drafting — drawing annotation helpers for build123d.

Every annotation builder is a native build123d ``BaseSketchObject`` subclass —
the returned object *is* a ``Sketch`` (composable in ``BuildSketch``,
``.moved()``-able, exportable directly). Import directly in your drawing
scripts — no MCP server required::

    from build123d_drafting import (
        Dimension, SafeDimension, Leader, Centerline,
        TitleBlock, draft_preset, set_page, view_axes,
    )

This package is the *rendering* substrate: annotation objects plus the styling
and view-coordinate frames they need. **Feature recognition and linting now
live in the** ``draftwright`` **package** (ADR 0007 — "helpers renders;
draftwright reasons"). The recognition symbols (``find_holes``, ``find_bosses``,
``find_hole_patterns``, ``analyse_cylinders``, ``feature_diameters``,
``full_cylinders`` and their feature/pattern types) and the lint symbols
(``lint_drawing``, ``find_overlaps``, ``find_interferences``, ``LintIssue``) are
still importable here but are **deprecated**: they emit a ``DeprecationWarning``,
are frozen, and will be removed in a future release. Use ``draftwright`` for
recognition and linting.
"""

import importlib
import warnings

from build123d_drafting.helpers import (
    DEFAULT_FONT_PATH,
    Centerline,
    CenterlineCircle,
    CenterMark,
    CompositeFeatureControlFrame,
    DatumFeature,
    DatumTarget,
    Dimension,
    FeatureControlFrame,
    HoleCallout,
    Leader,
    Note,
    SafeDimension,
    SurfaceFinish,
    TextBlock,
    TitleBlock,
    ViewCoordinates,
    annotate,
    clear_page,
    draft_preset,
    format_drawing_scale,
    leader_offset,
    place_dims,
    place_labels,
    set_page,
    view_axes,
)

# ADR 0007: feature recognition and linting moved to draftwright. These symbols
# are vendored-and-frozen here for backward compatibility; accessing one through
# the package emits a DeprecationWarning and lazily loads the frozen copy. Maps
# symbol -> (submodule, "recognition" | "linting").
_DEPRECATED = {
    "BoltCircle": ("build123d_drafting.features", "recognition"),
    "BossFeature": ("build123d_drafting.features", "recognition"),
    "CounterBore": ("build123d_drafting.features", "recognition"),
    "HoleFeature": ("build123d_drafting.features", "recognition"),
    "HoleSpec": ("build123d_drafting.features", "recognition"),
    "LinearArray": ("build123d_drafting.features", "recognition"),
    "RectGrid": ("build123d_drafting.features", "recognition"),
    "analyse_cylinders": ("build123d_drafting.features", "recognition"),
    "feature_diameters": ("build123d_drafting.features", "recognition"),
    "find_bosses": ("build123d_drafting.features", "recognition"),
    "find_hole_patterns": ("build123d_drafting.features", "recognition"),
    "find_holes": ("build123d_drafting.features", "recognition"),
    "full_cylinders": ("build123d_drafting.features", "recognition"),
    "LintIssue": ("build123d_drafting.helpers", "linting"),
    "find_interferences": ("build123d_drafting.helpers", "linting"),
    "find_overlaps": ("build123d_drafting.helpers", "linting"),
    "lint_drawing": ("build123d_drafting.helpers", "linting"),
}


def __getattr__(name):
    """Lazily serve the deprecated recognition/lint symbols (PEP 562).

    Kept symbols are imported eagerly above and never reach here; only the
    ADR-0007 vendored-and-frozen names fall through to this hook, where they warn
    before returning the real object from the submodule.
    """
    try:
        module, kind = _DEPRECATED[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    warnings.warn(
        f"build123d_drafting.{name} is deprecated: {kind} has moved to the "
        f"draftwright package (ADR 0007). This frozen copy will be removed in a "
        f"future release; import it from draftwright instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module), name)


__all__ = [
    "DEFAULT_FONT_PATH",
    "BoltCircle",
    "BossFeature",
    "CenterMark",
    "CenterlineCircle",
    "Centerline",
    "CompositeFeatureControlFrame",
    "CounterBore",
    "DatumFeature",
    "DatumTarget",
    "Dimension",
    "FeatureControlFrame",
    "HoleCallout",
    "HoleFeature",
    "HoleSpec",
    "Leader",
    "LinearArray",
    "LintIssue",
    "Note",
    "RectGrid",
    "SafeDimension",
    "SurfaceFinish",
    "TextBlock",
    "TitleBlock",
    "ViewCoordinates",
    "analyse_cylinders",
    "annotate",
    "clear_page",
    "draft_preset",
    "feature_diameters",
    "find_bosses",
    "find_hole_patterns",
    "find_holes",
    "find_interferences",
    "find_overlaps",
    "format_drawing_scale",
    "full_cylinders",
    "leader_offset",
    "lint_drawing",
    "place_dims",
    "place_labels",
    "set_page",
    "view_axes",
]
