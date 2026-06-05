"""The examples/specimen_sheet.py A3 drawing builds and exports a valid SVG."""

import importlib.util
from pathlib import Path

import pytest

_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "specimen_sheet.py"


@pytest.fixture(scope="module")
def sheet_module():
    spec = importlib.util.spec_from_file_location("specimen_sheet", _EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # builds the drawing at import
    return mod


def test_all_layers_received_geometry(sheet_module):
    # frame, stroked geometry, filled ink, text, and code captions all present
    assert sheet_module.border and sheet_module.stroke
    assert sheet_module.ink and sheet_module.fill and sheet_module.code


def test_writes_nonempty_svg(sheet_module, tmp_path):
    out = tmp_path / "sheet.svg"
    sheet_module.write_specimen_sheet(str(out))
    text = out.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<?xml") and "<svg" in text
    assert len(text) > 2000  # real geometry, not an empty canvas


def test_all_layers_present(sheet_module, tmp_path):
    out = tmp_path / "sheet.svg"
    sheet_module.write_specimen_sheet(str(out))
    svg = out.read_text(encoding="utf-8")
    for layer in ("border", "stroke", "ink", "fill", "code"):
        assert f'id="{layer}"' in svg


def test_sheet_lints_clean(sheet_module):
    # The catalogue is itself linted with find_interferences — no leader may
    # strike through a label/code/note box (the bug this regression guards).
    errors = [i for i in sheet_module.lint() if i.severity == "error"]
    assert errors == [], "; ".join(f"{i.code}: {i.message}" for i in errors)
