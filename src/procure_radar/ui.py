from __future__ import annotations

from typing import NoReturn

UI_REQUIREMENT = "PyQt6>=6.7,<7"


def _missing_pyqt6(exc: ModuleNotFoundError) -> NoReturn:
    raise RuntimeError(
        "Для интерфейса Procure Radar нужен PyQt6. "
        f"Установи его в активное окружение: python -m pip install \"{UI_REQUIREMENT}\""
    ) from exc


def launch_ui(db_path: str, *, region_code: int = 2) -> None:
    try:
        from .ui_qt import launch_qt_ui
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "PyQt6" or exc.name.startswith("PyQt6.")):
            _missing_pyqt6(exc)
        raise
    launch_qt_ui(db_path, region_code=region_code)
