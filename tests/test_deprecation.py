"""ADR 0007 — recognition and linting are vendored-and-frozen here and emit a
DeprecationWarning when accessed through the package; the rendering substrate
does not. See build123d_drafting/__init__.py.
"""

import warnings

import pytest

import build123d_drafting as d

_DEPRECATED = sorted(d._DEPRECATED)

# Rendering substrate that must stay warning-free.
_KEPT = [
    "Dimension",
    "Leader",
    "Centerline",
    "TitleBlock",
    "draft_preset",
    "set_page",
    "view_axes",
    "ViewCoordinates",
    "place_dims",
    "place_labels",
]


@pytest.mark.parametrize("name", _DEPRECATED)
def test_deprecated_symbol_warns_and_resolves(name):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obj = getattr(d, name)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep, f"{name} did not emit a DeprecationWarning"
    assert "ADR 0007" in str(dep[0].message)
    assert "draftwright" in str(dep[0].message)
    # The warning still hands back the real, working object.
    assert obj is not None


@pytest.mark.parametrize("name", _KEPT)
def test_kept_symbol_does_not_warn(name):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        getattr(d, name)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not dep, f"{name} should not be deprecated but warned: {dep}"


def test_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        d.does_not_exist


def test_deprecated_names_are_exported():
    # Backward compatibility: deprecated names remain in __all__ so existing
    # star-imports keep working (with a warning).
    for name in _DEPRECATED:
        assert name in d.__all__
