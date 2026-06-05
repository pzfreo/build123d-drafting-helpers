"""examples/part_drawing.py builds a real part drawing, lints clean, exports SVG."""

import importlib.util
from pathlib import Path

import pytest

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "part_drawing.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("part_drawing", _EXAMPLE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # builds the drawing at import
    return m


def test_builds_and_has_geometry(mod):
    assert mod.border and mod.part_v and mod.dims_l and mod.text_l


def test_lints_clean(mod):
    errors = [i for i in mod.lint() if i.severity == "error"]
    assert errors == [], "; ".join(f"{i.code}: {i.message}" for i in errors)


def test_writes_nonempty_svg(mod, tmp_path):
    out = tmp_path / "part.svg"
    mod.write_part_drawing(str(out))
    svg = out.read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg and len(svg) > 2000
    for layer in ("border", "part", "dims", "text"):
        assert f'id="{layer}"' in svg
