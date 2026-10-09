from __future__ import annotations

import pytest

import procure_radar.ui as ui


def test_ui_requirement_is_pyqt6():
    assert ui.UI_REQUIREMENT.startswith("PyQt6")


def test_missing_pyqt6_error_is_actionable():
    exc = ModuleNotFoundError("No module named 'PyQt6'")
    exc.name = "PyQt6"
    with pytest.raises(RuntimeError, match="python -m pip install"):
        ui._missing_pyqt6(exc)
