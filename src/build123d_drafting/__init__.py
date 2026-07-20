"""build123d-drafting — drawing annotation helpers for build123d.

Every annotation builder is a native build123d ``BaseSketchObject`` subclass —
the returned object *is* a ``Sketch`` (composable in ``BuildSketch``,
``.moved()``-able, exportable directly). Import directly in your drawing
scripts — no MCP server required::

    from build123d_drafting import (
        Dimension, SafeDimension, Leader, Centerline,
        TitleBlock, draft_preset, view_axes,
    )

This package is the *rendering* substrate: annotation objects plus the styling
and view-coordinate frames they need. **Feature recognition and linting now
live in the** ``draftwright`` **package** (ADR 0007 — "helpers renders;
draftwright reasons"). The recognition symbols (``find_holes``, ``find_bosses``,
``find_hole_patterns``, ``analyse_cylinders``, ``feature_diameters``,
``full_cylinders`` and their feature/pattern types), the lint symbols
(``lint_drawing``, ``find_overlaps``, ``find_interferences``, ``LintIssue``) and
the standalone-lint registration helpers (``set_page``, ``annotate``,
``clear_page``) are still importable here but are **deprecated**: they emit a
``DeprecationWarning``, are frozen, and will be removed in a future release. Use
``draftwright`` for recognition and linting.
"""

import importlib
import warnings
from typing import TYPE_CHECKING

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
    ProjectionSymbol,
    SafeDimension,
    SurfaceFinish,
    TextBlock,
    TitleBlock,
    ViewCoordinates,
    draft_preset,
    format_drawing_scale,
    leader_offset,
    place_dims,
    place_labels,
    view_axes,
)

if TYPE_CHECKING:
    # Re-export the deprecated names for static type checkers only. At runtime
    # these resolve through __getattr__ below (which warns); this block keeps
    # mypy/IDEs seeing their real signatures while still routing through the
    # deprecation hook at runtime.
    from build123d_drafting.features import (
        BoltCircle,
        BossFeature,
        CounterBore,
        HoleFeature,
        HoleSpec,
        LinearArray,
        RectGrid,
        analyse_cylinders,
        feature_diameters,
        find_bosses,
        find_hole_patterns,
        find_holes,
        full_cylinders,
    )
    from build123d_drafting.helpers import (
        LintIssue,
        annotate,
        clear_page,
        find_interferences,
        find_overlaps,
        lint_drawing,
        set_page,
    )

# ADR 0007: feature recognition and linting moved to draftwright. These symbols
# are vendored-and-frozen here for backward compatibility; accessing one through
# the package emits a DeprecationWarning and lazily loads the frozen copy. Maps
# symbol -> (submodule, kind) where kind selects the deprecation reason below.
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
    "set_page": ("build123d_drafting.helpers", "lint-support"),
    "annotate": ("build123d_drafting.helpers", "lint-support"),
    "clear_page": ("build123d_drafting.helpers", "lint-support"),
}

_REASON = {
    "recognition": (
        "feature recognition has moved to the draftwright package (ADR 0007); "
        "import it from draftwright instead."
    ),
    "linting": (
        "linting has moved to the draftwright package (ADR 0007); import it "
        "from draftwright instead."
    ),
    "lint-support": (
        "it feeds the standalone linting that has moved to the draftwright package (ADR 0007)."
    ),
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
        f"build123d_drafting.{name} is deprecated: {_REASON[kind]} This frozen "
        f"copy will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module), name)


def __dir__():
    """Expose the kept *and* deprecated public names (restores tab-completion).

    Without this, the lazily-served deprecated names are absent from the module
    ``__dict__`` and would not show up in ``dir()`` / REPL completion.
    """
    return sorted(__all__)


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
    "ProjectionSymbol",
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
