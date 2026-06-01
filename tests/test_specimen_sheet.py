"""The examples/specimen_sheet.py legend builds and exports a valid SVG."""
import importlib.util
from pathlib import Path

import pytest

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "specimen_sheet.py"


@pytest.fixture(scope="module")
def sheet_module():
    spec = importlib.util.spec_from_file_location("specimen_sheet", _EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builds_without_error(sheet_module):
    sheet = sheet_module.build_sheet()
    # every logical layer received geometry
    assert sheet.border and sheet.stroke and sheet.ink and sheet.fill and sheet.code


def test_writes_nonempty_svg(sheet_module, tmp_path):
    out = tmp_path / "sheet.svg"
    sheet_module.write_specimen_sheet(str(out))
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<?xml") and "<svg" in text
    assert len(text) > 2000  # real geometry, not an empty canvas


def test_all_helper_layers_present(sheet_module, tmp_path):
    out = tmp_path / "sheet.svg"
    sheet_module.write_specimen_sheet(str(out))
    svg = out.read_text(encoding="utf-8")
    for layer in ("border", "stroke", "ink", "fill", "code"):
        assert f'id="{layer}"' in svg
