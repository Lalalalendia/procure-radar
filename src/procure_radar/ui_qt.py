from __future__ import annotations

from datetime import date
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Callable

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QBrush, QCloseEvent, QFont, QPainter, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .db import connect_readonly
from .ui_collection import is_repeatable_action, parse_collection_chunk, should_chain_collection
from .ui_data import (
    dashboard_metrics,
    opportunity_rows,
    organization_card,
    organization_names,
    organization_rows,
    recent_purchases,
    relationship_rows,
    source_statuses,
)


# Restrained neutral palette: data carries the color, chrome mostly does not.
BG = "#0d0f12"
SIDEBAR = "#101216"
SURFACE = "#15181d"
SURFACE_ALT = "#191d23"
SURFACE_HOVER = "#1e232b"
TEXT = "#f2f4f7"
MUTED = "#8b95a5"
DIM = "#667085"
BORDER = "#272c35"
BORDER_SOFT = "#20242b"
ACCENT = "#6c8cff"
ACCENT_SOFT = "#202943"
GREEN = "#51c99a"
YELLOW = "#e6b95c"
RED = "#ed6a7f"
BLUE = "#6ca9ff"


APP_STYLESHEET = f"""
QMainWindow {{ background: {BG}; }}
QWidget {{ color: {TEXT}; background: transparent; font-family: "Segoe UI", sans-serif; font-size: 10pt; }}
QWidget#appRoot {{ background: {BG}; }}
QFrame#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER_SOFT}; }}
QFrame#detailPanel {{ background: {SURFACE}; border-left: 1px solid {BORDER}; }}
QFrame#card, QFrame#sourceCard, QFrame#panel {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}
QFrame#metricCard {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 10px;
}}
QLabel#muted {{ color: {MUTED}; }}
QLabel#dim {{ color: {DIM}; }}
QLabel#eyebrow {{ color: {MUTED}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#sectionTitle {{ font-size: 13pt; font-weight: 650; }}
QLabel#pageTitle {{ font-size: 20pt; font-weight: 650; }}
QLabel#metricValue {{ font-size: 21pt; font-weight: 650; }}
QLabel#metricCaption {{ color: {MUTED}; font-size: 9pt; }}
QLabel#brand {{ font-size: 17pt; font-weight: 750; letter-spacing: 1px; }}
QLabel#brandSub {{ color: {MUTED}; font-size: 8pt; }}
QLabel#statusOk {{ color: {GREEN}; font-weight: 600; }}
QLabel#statusBad {{ color: {RED}; font-weight: 600; }}
QLabel#statusWarn {{ color: {YELLOW}; font-weight: 600; }}
QLabel#statusInfo {{ color: {BLUE}; font-weight: 600; }}
QLabel#chip {{
    background: {SURFACE_ALT};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px 8px;
}}
QPushButton {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 11px;
    min-height: 20px;
}}
QPushButton:hover {{ background: {SURFACE_HOVER}; border-color: #343b47; }}
QPushButton:pressed {{ background: #242a34; }}
QPushButton:disabled {{ color: #596270; background: #121419; border-color: #20242b; }}
QPushButton#primary {{ background: {ACCENT}; color: #0c1020; border-color: {ACCENT}; font-weight: 650; }}
QPushButton#primary:hover {{ background: #829cff; border-color: #829cff; }}
QPushButton#success {{ background: {GREEN}; color: #08120e; border-color: {GREEN}; font-weight: 650; }}
QPushButton#danger {{ background: #21151a; color: {RED}; border-color: #3c222a; }}
QPushButton#ghost {{ background: transparent; border-color: transparent; color: {MUTED}; }}
QPushButton#ghost:hover {{ background: {SURFACE_ALT}; color: {TEXT}; }}
QPushButton#nav {{
    text-align: left;
    color: {MUTED};
    background: transparent;
    border: 0;
    border-left: 3px solid transparent;
    border-radius: 6px;
    padding: 9px 12px;
    min-height: 24px;
}}
QPushButton#nav:hover {{ background: {SURFACE}; color: {TEXT}; }}
QPushButton#nav[active="true"] {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border-left: 3px solid {ACCENT};
    font-weight: 650;
}}
QLineEdit, QSpinBox, QComboBox {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 6px 8px;
    min-height: 22px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QTableView {{
    background: {SURFACE};
    alternate-background-color: #171a20;
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 9px;
    gridline-color: transparent;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QTableView::item {{ padding: 5px 8px; border-bottom: 1px solid #1d2128; }}
QTableView::item:selected {{ border-bottom: 1px solid #2f3d67; }}
QHeaderView::section {{
    background: #12151a;
    color: {MUTED};
    border: 0;
    border-bottom: 1px solid {BORDER};
    padding: 8px;
    font-size: 9pt;
    font-weight: 650;
}}
QProgressBar {{
    background: #11141a;
    border: 1px solid {BORDER_SOFT};
    border-radius: 4px;
    min-height: 6px;
    max-height: 6px;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QTextEdit {{
    background: #111419;
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    padding: 6px;
    selection-background-color: {ACCENT};
}}
QScrollArea {{ border: 0; background: transparent; }}
QSplitter::handle {{ background: {BORDER_SOFT}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QStatusBar {{ background: {SIDEBAR}; color: {MUTED}; border-top: 1px solid {BORDER_SOFT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 1px; }}
QScrollBar::handle:vertical {{ background: #343a45; min-height: 28px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: #48505e; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px; }}
QScrollBar::handle:horizontal {{ background: #343a45; min-width: 28px; border-radius: 4px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolTip {{ background: {SURFACE_ALT}; color: {TEXT}; border: 1px solid {BORDER}; padding: 5px; }}
"""


class RowsTableModel(QAbstractTableModel):
    """Cheap resettable table model; avoids thousands of QTableWidgetItem objects."""

    def __init__(self, headers: list[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.headers = list(headers)
        self.rows: list[dict[str, Any]] = []
        self.display_rows: list[list[str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self.headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or index.row() >= len(self.display_rows):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            row = self.display_rows[index.row()]
            return row[index.column()] if index.column() < len(row) else ""
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 0:
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return None

    def headerData(  # type: ignore[override]
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section] if 0 <= section < len(self.headers) else ""
        return None

    def set_rows(
        self,
        rows: list[dict[str, Any]],
        formatter: Callable[[dict[str, Any]], list[str]],
    ) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.display_rows = [formatter(row) for row in self.rows]
        self.endResetModel()

    def payload(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None


class WorkerSignals(QObject):
    result = pyqtSignal(str, int, object)
    error = pyqtSignal(str, int, str)


class DataWorker(QRunnable):
    def __init__(self, key: str, generation: int, loader: Callable[[], Any]) -> None:
        super().__init__()
        self.key = key
        self.generation = generation
        self.loader = loader
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:  # type: ignore[override]
        try:
            result = self.loader()
        except Exception as exc:  # worker boundary: surface any DB/analytics failure to UI
            self.signals.error.emit(self.key, self.generation, f"{type(exc).__name__}: {exc}")
            return
        self.signals.result.emit(self.key, self.generation, result)


class GraphView(QGraphicsView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor("#111419"))
        self.setFrameShape(QFrame.Shape.NoFrame)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
        self.scale(factor, factor)


class MetricCard(QFrame):
    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setMinimumHeight(92)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(3)
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("metricCaption")
        self.caption_label.setWordWrap(True)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("metricValue")
        layout.addWidget(self.caption_label)
        layout.addWidget(self.value_label)
        layout.addStretch(1)

    def set_metric(self, value: str, caption: str | None = None) -> None:
        self.value_label.setText(value)
        if caption is not None:
            self.caption_label.setText(caption)


class SourceCard(QFrame):
    def __init__(
        self,
        source: dict[str, Any],
        *,
        on_detail: Callable[[dict[str, Any]], None],
        on_action: Callable[[str], None],
        active_action: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sourceCard")
        self.setMinimumHeight(180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel(str(source.get("title") or source.get("key") or "Источник"))
        title.setStyleSheet("font-weight: 650; font-size: 11pt;")
        count = QLabel(ProcureRadarWindow.fmt_int(source.get("count")))
        count.setStyleSheet("font-size: 15pt; font-weight: 650;")
        top.addWidget(title, 1)
        top.addWidget(count)
        layout.addLayout(top)

        status_row = QHBoxLayout()
        action = str(source.get("action") or "") or None
        collecting = bool(action and active_action == action)
        state = "collecting" if collecting else str(source.get("state") or ("connected" if source.get("connected") else "empty"))
        state_text, state_object = {
            "collecting": ("● собирается", "statusInfo"),
            "up_to_date": ("✓ актуально", "statusOk"),
            "needs_resume": ("◷ требуется продолжение", "statusWarn"),
            "connected": ("● подключён", "statusOk"),
            "empty": ("● нет данных", "statusBad"),
        }.get(state, ("● подключён", "statusOk"))
        status = QLabel(state_text)
        status.setObjectName(state_object)
        range_label = QLabel(ProcureRadarWindow.source_range(source))
        range_label.setObjectName("muted")
        status_row.addWidget(status)
        status_row.addWidget(range_label)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        details = QLabel(str(source.get("details") or "—"))
        details.setObjectName("muted")
        details.setWordWrap(True)
        layout.addWidget(details)

        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setValue(round(float(source.get("progress") or 0) * 1000))
        progress.setTextVisible(False)
        layout.addWidget(progress)

        actions = QHBoxLayout()
        details_btn = QPushButton("Подробнее")
        details_btn.clicked.connect(lambda: on_detail(source))
        actions.addWidget(details_btn)
        if action:
            if collecting:
                action_text = "Сбор идёт"
            elif action == "refresh_gisp":
                action_text = "Обновить"
            elif state == "up_to_date":
                action_text = "Актуально"
            elif state == "connected":
                action_text = "Начать сбор"
            else:
                action_text = "Продолжить"
            action_btn = QPushButton(action_text)
            action_btn.setObjectName("primary")
            action_btn.setEnabled(not collecting and (state != "up_to_date" or action == "refresh_gisp"))
            action_btn.clicked.connect(lambda: on_action(str(action)))
            actions.addWidget(action_btn)
        actions.addStretch(1)
        layout.addLayout(actions)


class ProcureRadarWindow(QMainWindow):
    PAGE_SPECS = [
        ("overview", "Обзор"),
        ("sources", "Источники"),
        ("opportunities", "Возможности"),
        ("organizations", "Организации"),
        ("graph", "Граф связей"),
        ("purchases", "Закупки"),
        ("collection", "Сбор данных"),
    ]

    PAGE_SUBTITLES = {
        "overview": "Состояние базы и ключевые сигналы",
        "sources": "Свежесть, прогресс и продолжение синхронизации",
        "opportunities": "Категории со спросом и слабой конкуренцией",
        "organizations": "Заказчики, поставщики и их наблюдаемая активность",
        "graph": "Концентрация и зависимости buyer ↔ supplier",
        "purchases": "Последние закупки и связанные результаты",
        "collection": "Управление фоновым сбором данных",
    }

    def __init__(self, db_path: str, region_code: int = 2) -> None:
        super().__init__()
        self.db_path = str(Path(db_path))
        self.region_code = int(region_code)
        self._source_cache: dict[str, dict[str, Any]] = {}
        self._graph_rows: list[dict[str, Any]] = []
        self._opportunity_rows_cache: list[dict[str, Any]] = []
        self._organization_rows_cache: list[dict[str, Any]] = []
        self._process: QProcess | None = None
        self._collection_action: str | None = None
        self._collection_command: list[str] | None = None
        self._collection_stop_requested = False
        self._collection_chunk_text = ""
        self._collection_line_buffer = ""
        self._collection_session_chunks = 0
        self._collection_session_items = 0
        self._collection_session_requests = 0
        self._collection_last_rpm: float | None = None
        self._collection_current_date: str | None = None
        self._collection_next_page: int | None = None
        self._collection_total: int | None = None
        self._page_indexes: dict[str, int] = {}
        self._nav_buttons: dict[str, QPushButton] = {}
        self._current_page = "overview"
        self._detail_target: tuple[str, str] | None = None

        # UI data lifecycle. Heavy analytics never run on the GUI thread.
        self._generation = 0
        self._cache: dict[str, Any] = {}
        self._loading: set[tuple[str, int]] = set()
        self._task_handlers: dict[tuple[str, int], Callable[[Any], None]] = {}
        self._task_labels: dict[tuple[str, int], str] = {}
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(2)

        self.setWindowTitle("Procure Radar")
        self.resize(1500, 900)
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(APP_STYLESHEET)

        self._build_shell()
        self._build_pages()
        self._show_page("overview")
        self.refresh_all()

    # ---------- shell ----------
    def _build_shell(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        self.sidebar = self._build_sidebar()
        root_layout.addWidget(self.sidebar)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.setHandleWidth(2)
        root_layout.addWidget(self.main_splitter, 1)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(22, 14, 14, 14)
        center_layout.setSpacing(12)
        center_layout.addLayout(self._build_topbar())
        self.pages = QStackedWidget()
        center_layout.addWidget(self.pages, 1)
        self.main_splitter.addWidget(center)

        self.detail_panel = self._build_detail_panel()
        self.main_splitter.addWidget(self.detail_panel)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setCollapsible(1, True)
        # Empty card panel should not consume 25-30% of the application on launch.
        self.main_splitter.setSizes([1320, 0])

        self.statusBar().showMessage(f"База: {self.db_path}  ·  Регион: {self.region_code}")

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(184)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(11, 17, 11, 14)
        layout.setSpacing(3)

        brand_box = QVBoxLayout()
        brand_box.setContentsMargins(9, 0, 9, 16)
        brand = QLabel("RADAR")
        brand.setObjectName("brand")
        sub = QLabel("PROCUREMENT INTELLIGENCE")
        sub.setObjectName("brandSub")
        brand_box.addWidget(brand)
        brand_box.addWidget(sub)
        layout.addLayout(brand_box)

        for key, label in self.PAGE_SPECS:
            btn = QPushButton(label)
            btn.setObjectName("nav")
            btn.setProperty("active", False)
            btn.clicked.connect(lambda _checked=False, k=key: self._show_page(k))
            layout.addWidget(btn)
            self._nav_buttons[key] = btn

        layout.addStretch(1)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background:{BORDER_SOFT};")
        layout.addWidget(divider)
        self.task_label = QLabel("Сбор не запущен")
        self.task_label.setObjectName("muted")
        self.task_label.setWordWrap(True)
        layout.addWidget(self.task_label)
        self.stop_btn = QPushButton("Остановить сбор")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_task)
        layout.addWidget(self.stop_btn)
        return sidebar

    def _build_topbar(self) -> QHBoxLayout:
        top = QHBoxLayout()
        top.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        self.page_title = QLabel("Обзор")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel(self.PAGE_SUBTITLES["overview"])
        self.page_subtitle.setObjectName("muted")
        title_box.addWidget(self.page_title)
        title_box.addWidget(self.page_subtitle)
        top.addLayout(title_box)
        top.addStretch(1)

        self.loading_bar = QProgressBar()
        self.loading_bar.setFixedWidth(86)
        self.loading_bar.setRange(0, 0)
        self.loading_bar.setVisible(False)
        top.addWidget(self.loading_bar)
        self.loading_label = QLabel("")
        self.loading_label.setObjectName("muted")
        self.loading_label.setMinimumWidth(100)
        top.addWidget(self.loading_label)

        label = QLabel("Регион")
        label.setObjectName("muted")
        top.addWidget(label)
        self.region_spin = QSpinBox()
        self.region_spin.setRange(1, 99)
        self.region_spin.setValue(self.region_code)
        self.region_spin.setFixedWidth(68)
        top.addWidget(self.region_spin)
        apply_btn = QPushButton("Применить")
        apply_btn.clicked.connect(self._apply_region)
        top.addWidget(apply_btn)
        refresh_btn = QPushButton("Обновить")
        refresh_btn.setObjectName("primary")
        refresh_btn.clicked.connect(lambda: self.refresh_all(force=True))
        top.addWidget(refresh_btn)
        self.detail_toggle_btn = QPushButton("Карточка")
        self.detail_toggle_btn.setObjectName("ghost")
        self.detail_toggle_btn.clicked.connect(self._toggle_detail_panel)
        top.addWidget(self.detail_toggle_btn)
        return top

    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(520)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(17, 16, 17, 16)
        layout.setSpacing(8)
        head = QHBoxLayout()
        self.detail_title = QLabel("Карточка")
        self.detail_title.setObjectName("sectionTitle")
        head.addWidget(self.detail_title, 1)
        close_btn = QPushButton("×")
        close_btn.setObjectName("ghost")
        close_btn.setFixedWidth(34)
        close_btn.clicked.connect(self._collapse_detail_panel)
        head.addWidget(close_btn)
        layout.addLayout(head)
        self.detail_hint = QLabel("Контекст выбранного объекта")
        self.detail_hint.setObjectName("muted")
        layout.addWidget(self.detail_hint)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_text.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.detail_text, 1)
        self.detail_text.setPlainText("Выбери источник, рынок, организацию или узел графа.")
        return panel

    # ---------- pages ----------
    def _build_pages(self) -> None:
        builders: list[tuple[str, str, Callable[[], QWidget]]] = [
            ("overview", "Обзор", self._build_overview),
            ("sources", "Источники", self._build_sources),
            ("opportunities", "Возможности", self._build_opportunities),
            ("organizations", "Организации", self._build_organizations),
            ("graph", "Граф связей", self._build_graph),
            ("purchases", "Закупки", self._build_purchases),
            ("collection", "Сбор данных", self._build_collection),
        ]
        self._page_labels = {key: label for key, label, _ in builders}
        for key, _label, builder in builders:
            idx = self.pages.addWidget(builder())
            self._page_indexes[key] = idx

    def _page_base(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(11)
        return page, layout

    def _build_overview(self) -> QWidget:
        page, layout = self._page_base()

        metrics_header = QHBoxLayout()
        title = QLabel("Состояние базы")
        title.setObjectName("sectionTitle")
        metrics_header.addWidget(title)
        self.overview_updated = QLabel("загрузка…")
        self.overview_updated.setObjectName("muted")
        metrics_header.addWidget(self.overview_updated)
        metrics_header.addStretch(1)
        layout.addLayout(metrics_header)

        self.metrics_grid = QGridLayout()
        self.metrics_grid.setSpacing(8)
        captions = [
            "Закупки",
            "Контракты",
            "Поставщики",
            "Заказчики",
            "Покрытие протоколами",
            "Связано с закупками",
        ]
        self.metric_cards: list[MetricCard] = []
        for col, caption in enumerate(captions):
            card = MetricCard(caption)
            self.metrics_grid.addWidget(card, 0, col)
            self.metrics_grid.setColumnStretch(col, 1)
            self.metric_cards.append(card)
        layout.addLayout(self.metrics_grid)

        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.setChildrenCollapsible(False)
        lower.setHandleWidth(8)
        layout.addWidget(lower, 1)

        source_panel = QFrame()
        source_panel.setObjectName("panel")
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(15, 14, 15, 14)
        source_header = QHBoxLayout()
        source_title = QLabel("Источники")
        source_title.setObjectName("sectionTitle")
        source_header.addWidget(source_title)
        source_header.addStretch(1)
        open_sources = QPushButton("Все источники")
        open_sources.setObjectName("ghost")
        open_sources.clicked.connect(lambda: self._show_page("sources"))
        source_header.addWidget(open_sources)
        source_layout.addLayout(source_header)
        self.overview_sources_layout = QVBoxLayout()
        self.overview_sources_layout.setSpacing(5)
        source_layout.addLayout(self.overview_sources_layout)
        source_layout.addStretch(1)
        lower.addWidget(source_panel)

        rel_panel = QFrame()
        rel_panel.setObjectName("panel")
        rel_layout = QVBoxLayout(rel_panel)
        rel_layout.setContentsMargins(15, 14, 15, 14)
        rel_header = QHBoxLayout()
        rel_title = QLabel("Сильные связи")
        rel_title.setObjectName("sectionTitle")
        rel_header.addWidget(rel_title)
        rel_header.addStretch(1)
        open_graph = QPushButton("Открыть граф")
        open_graph.setObjectName("ghost")
        open_graph.clicked.connect(lambda: self._show_page("graph"))
        rel_header.addWidget(open_graph)
        rel_layout.addLayout(rel_header)
        self.overview_rel_table = self._make_table(
            ["Score", "Заказчик", "Поставщик", "Тип", "Сумма ₽"],
            widths={0: 68, 1: 132, 2: 132, 3: 160, 4: 105},
            stretch_col=3,
        )
        self.overview_rel_table.selectionModel().selectionChanged.connect(
            lambda *_: self._overview_relationship_selected()
        )
        rel_layout.addWidget(self.overview_rel_table)
        lower.addWidget(rel_panel)
        lower.setSizes([540, 660])
        return page

    def _build_sources(self) -> QWidget:
        page, layout = self._page_base()
        header = QHBoxLayout()
        title = QLabel("Подключённые источники")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        hint = QLabel("Прогресс и checkpoint обновляются после каждого прохода")
        hint.setObjectName("muted")
        header.addWidget(hint)
        header.addStretch(1)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self.sources_grid = QGridLayout(scroll_widget)
        self.sources_grid.setContentsMargins(0, 4, 8, 8)
        self.sources_grid.setHorizontalSpacing(9)
        self.sources_grid.setVerticalSpacing(9)
        self.sources_grid.setColumnStretch(0, 1)
        self.sources_grid.setColumnStretch(1, 1)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)
        return page

    def _build_opportunities(self) -> QWidget:
        page, layout = self._page_base()
        controls = QHBoxLayout()
        self.opp_group = QComboBox()
        self.opp_group.addItems(["все", "goods", "services", "works", "property", "other"])
        self.opp_group.setCurrentText("goods")
        self.opp_group.currentTextChanged.connect(lambda _value: self._render_opportunity_group())
        controls.addWidget(QLabel("Группа"))
        controls.addWidget(self.opp_group)
        refresh = QPushButton("Пересчитать")
        refresh.clicked.connect(lambda: self.refresh_opportunities(force=True))
        controls.addWidget(refresh)
        controls.addStretch(1)
        self.opp_count_label = QLabel("")
        self.opp_count_label.setObjectName("muted")
        controls.addWidget(self.opp_count_label)
        layout.addLayout(controls)

        self.opp_table = self._make_table(
            ["Score", "Код", "Сегмент", "Спрос ₽", "Закупок", "Supplier gap", "Distribution", "Контракты", "Наименование"],
            widths={0: 70, 1: 190, 2: 145, 3: 105, 4: 76, 5: 95, 6: 94, 7: 88},
            stretch_col=8,
        )
        self.opp_table.selectionModel().selectionChanged.connect(lambda *_: self._opportunity_selected())
        layout.addWidget(self.opp_table, 1)
        return page

    def _build_organizations(self) -> QWidget:
        page, layout = self._page_base()
        controls = QHBoxLayout()
        self.org_search = QLineEdit()
        self.org_search.setPlaceholderText("ИНН или название организации")
        self.org_search.setMaximumWidth(430)
        controls.addWidget(self.org_search)
        clear = QPushButton("Очистить")
        clear.clicked.connect(lambda: self.org_search.clear())
        controls.addWidget(clear)
        refresh = QPushButton("Обновить список")
        refresh.clicked.connect(lambda: self.refresh_organizations(force=True))
        controls.addWidget(refresh)
        controls.addStretch(1)
        self.org_count_label = QLabel("")
        self.org_count_label.setObjectName("muted")
        controls.addWidget(self.org_count_label)
        layout.addLayout(controls)

        self._org_search_timer = QTimer(self)
        self._org_search_timer.setSingleShot(True)
        self._org_search_timer.setInterval(180)
        self._org_search_timer.timeout.connect(self._filter_organizations)
        self.org_search.textChanged.connect(lambda _text: self._org_search_timer.start())

        self.org_table = self._make_table(
            ["ИНН", "Название", "Роль", "Контрактов", "Наблюдаемая сумма ₽"],
            widths={0: 140, 2: 155, 3: 92, 4: 140},
            stretch_col=1,
        )
        self.org_table.selectionModel().selectionChanged.connect(lambda *_: self._organization_selected())
        layout.addWidget(self.org_table, 1)
        return page

    def _build_graph(self) -> QWidget:
        page, layout = self._page_base()
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Минимум контрактов"))
        self.graph_min_contracts = QSpinBox()
        self.graph_min_contracts.setRange(1, 50)
        self.graph_min_contracts.setValue(2)
        controls.addWidget(self.graph_min_contracts)
        controls.addWidget(QLabel("Минимальная сумма ₽"))
        self.graph_min_value = QLineEdit("100000")
        self.graph_min_value.setMaximumWidth(135)
        controls.addWidget(self.graph_min_value)
        build = QPushButton("Построить")
        build.setObjectName("primary")
        build.clicked.connect(lambda: self.refresh_graph(force=True))
        controls.addWidget(build)
        controls.addStretch(1)
        hint = QLabel("wheel — масштаб · drag — панорама · клик — карточка")
        hint.setObjectName("muted")
        controls.addWidget(hint)
        layout.addLayout(controls)

        self.graph_scene = QGraphicsScene(self)
        self.graph_view = GraphView()
        self.graph_view.setScene(self.graph_scene)
        self.graph_scene.selectionChanged.connect(self._graph_selection_changed)
        layout.addWidget(self.graph_view, 1)
        return page

    def _build_purchases(self) -> QWidget:
        page, layout = self._page_base()
        controls = QHBoxLayout()
        title = QLabel("Последние закупки")
        title.setObjectName("sectionTitle")
        controls.addWidget(title)
        controls.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(lambda: self.refresh_purchases(force=True))
        controls.addWidget(refresh)
        layout.addLayout(controls)

        self.purchase_table = self._make_table(
            ["№ закупки", "Дата", "НМЦК ₽", "Stage", "Объект"],
            widths={0: 190, 1: 92, 2: 115, 3: 65},
            stretch_col=4,
        )
        self.purchase_table.selectionModel().selectionChanged.connect(lambda *_: self._purchase_selected())
        layout.addWidget(self.purchase_table, 1)
        return page

    def _build_collection(self) -> QWidget:
        page, layout = self._page_base()

        status_panel = QFrame()
        status_panel.setObjectName("panel")
        status_layout = QVBoxLayout(status_panel)
        status_layout.setContentsMargins(16, 15, 16, 15)
        status_layout.setSpacing(8)

        status_top = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.collection_source_label = QLabel("Сбор данных")
        self.collection_source_label.setObjectName("sectionTitle")
        self.collection_state_label = QLabel("Ничего не запущено")
        self.collection_state_label.setObjectName("muted")
        title_box.addWidget(self.collection_source_label)
        title_box.addWidget(self.collection_state_label)
        status_top.addLayout(title_box, 1)
        self.collection_range_label = QLabel("—")
        self.collection_range_label.setObjectName("muted")
        status_top.addWidget(self.collection_range_label)
        status_layout.addLayout(status_top)

        self.collection_progress = QProgressBar()
        self.collection_progress.setRange(0, 1000)
        self.collection_progress.setValue(0)
        self.collection_progress.setTextVisible(False)
        status_layout.addWidget(self.collection_progress)

        status_meta = QHBoxLayout()
        self.collection_current_label = QLabel("Текущая позиция: —")
        self.collection_current_label.setObjectName("muted")
        self.collection_session_label = QLabel("Сессия: —")
        self.collection_session_label.setObjectName("muted")
        status_meta.addWidget(self.collection_current_label)
        status_meta.addStretch(1)
        status_meta.addWidget(self.collection_session_label)
        status_layout.addLayout(status_meta)
        layout.addWidget(status_panel)

        actions_panel = QFrame()
        actions_panel.setObjectName("panel")
        panel_layout = QVBoxLayout(actions_panel)
        panel_layout.setContentsMargins(15, 14, 15, 14)
        panel_layout.setSpacing(9)
        action_title = QLabel("Источники")
        action_title.setObjectName("sectionTitle")
        panel_layout.addWidget(action_title)
        subtitle = QLabel(
            "Один клик запускает последовательные безопасные чанки до завершения. "
            "Остановить можно в любой момент — checkpoint сохраняется."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        panel_layout.addWidget(subtitle)

        actions = QGridLayout()
        self.collection_action_buttons: dict[str, QPushButton] = {}
        specs = [
            ("Продолжить закупки", "continue_purchases", "primary"),
            ("Продолжить контракты", "continue_contracts", "success"),
            ("Продолжить РЗН", "continue_rzn", ""),
            ("Продолжить ФСА", "continue_fsa", ""),
            ("Обновить ГИСП", "refresh_gisp", ""),
        ]
        for idx, (text, action, object_name) in enumerate(specs):
            btn = QPushButton(text)
            if object_name:
                btn.setObjectName(object_name)
            btn.clicked.connect(lambda _checked=False, a=action: self._run_source_action(a))
            self.collection_action_buttons[action] = btn
            actions.addWidget(btn, idx // 3, idx % 3)

        self.collection_log_toggle = QPushButton("Технический журнал")
        self.collection_log_toggle.setObjectName("ghost")
        self.collection_log_toggle.clicked.connect(self._toggle_collection_log)
        actions.addWidget(self.collection_log_toggle, 1, 2)
        for col in range(3):
            actions.setColumnStretch(col, 1)
        panel_layout.addLayout(actions)
        layout.addWidget(actions_panel)

        self.collection_log_panel = QFrame()
        self.collection_log_panel.setObjectName("panel")
        log_layout = QVBoxLayout(self.collection_log_panel)
        log_layout.setContentsMargins(12, 10, 12, 12)
        log_header = QHBoxLayout()
        log_title = QLabel("Технический журнал")
        log_title.setStyleSheet("font-weight: 600;")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        clear_btn = QPushButton("Очистить")
        clear_btn.setObjectName("ghost")
        clear_btn.clicked.connect(self._clear_log)
        log_header.addWidget(clear_btn)
        log_layout.addLayout(log_header)
        self.collection_log = QTextEdit()
        self.collection_log.setReadOnly(True)
        self.collection_log.setFont(QFont("Cascadia Mono", 9))
        log_layout.addWidget(self.collection_log, 1)
        self.collection_log_panel.setVisible(False)
        layout.addWidget(self.collection_log_panel, 1)
        layout.addStretch(1)
        return page

    # ---------- async data lifecycle ----------
    def _conn(self) -> sqlite3.Connection:
        return connect_readonly(self.db_path)

    def _db_loader(self, fn: Callable[[sqlite3.Connection], Any]) -> Callable[[], Any]:
        db_path = self.db_path

        def load() -> Any:
            conn = connect_readonly(db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

        return load

    def _schedule_load(
        self,
        key: str,
        label: str,
        loader: Callable[[], Any],
        apply: Callable[[Any], None],
        *,
        force: bool = False,
    ) -> None:
        if not force and key in self._cache:
            apply(self._cache[key])
            return
        token = (key, self._generation)
        if token in self._loading:
            return
        self._loading.add(token)
        self._task_handlers[token] = apply
        self._task_labels[token] = label
        task = DataWorker(key, self._generation, loader)
        task.signals.result.connect(self._on_worker_result)
        task.signals.error.connect(self._on_worker_error)
        self._thread_pool.start(task)
        self._update_loading_indicator()

    def _on_worker_result(self, key: str, generation: int, result: object) -> None:
        token = (key, generation)
        self._loading.discard(token)
        apply = self._task_handlers.pop(token, None)
        self._task_labels.pop(token, None)
        if generation == self._generation:
            self._cache[key] = result
            if apply is not None:
                apply(result)
        self._update_loading_indicator()

    def _on_worker_error(self, key: str, generation: int, message: str) -> None:
        token = (key, generation)
        self._loading.discard(token)
        self._task_handlers.pop(token, None)
        label = self._task_labels.pop(token, key)
        if generation == self._generation:
            self._set_detail("Ошибка загрузки", f"{label}\n\n{message}", open_panel=True)
            self.statusBar().showMessage(f"{label}: {message}", 9000)
        self._update_loading_indicator()

    def _update_loading_indicator(self) -> None:
        current = [token for token in self._loading if token[1] == self._generation]
        if not current:
            self.loading_bar.setVisible(False)
            self.loading_label.setText("")
            return
        self.loading_bar.setVisible(True)
        labels = [self._task_labels.get(token, "данные") for token in current]
        self.loading_label.setText(labels[0] if len(labels) == 1 else f"Загрузка · {len(labels)}")

    def _invalidate_data(self) -> None:
        self._generation += 1
        self._cache.clear()
        self._source_cache.clear()
        self._opportunity_rows_cache = []
        self._organization_rows_cache = []
        self._update_loading_indicator()

    # ---------- navigation / refresh ----------
    def _show_page(self, key: str) -> None:
        if key not in self._page_indexes:
            return
        self._current_page = key
        self.pages.setCurrentIndex(self._page_indexes[key])
        self.page_title.setText(self._page_labels.get(key, key))
        self.page_subtitle.setText(self.PAGE_SUBTITLES.get(key, ""))
        for nav_key, btn in self._nav_buttons.items():
            btn.setProperty("active", nav_key == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        # No database work is done inline here. Tab switching stays immediate.
        QTimer.singleShot(0, self._ensure_current_page_data)

    def _ensure_current_page_data(self) -> None:
        key = self._current_page
        if key == "overview":
            self.refresh_all()
        elif key == "sources":
            self.refresh_sources_only()
        elif key == "opportunities":
            self.refresh_opportunities()
        elif key == "organizations":
            self.refresh_organizations()
        elif key == "graph":
            self.refresh_graph()
        elif key == "purchases":
            self.refresh_purchases()

    def _apply_region(self) -> None:
        new_region = int(self.region_spin.value())
        if new_region == self.region_code:
            return
        self.region_code = new_region
        self._invalidate_data()
        self.statusBar().showMessage(f"База: {self.db_path}  ·  Регион: {self.region_code}")
        self.refresh_all()
        self._ensure_current_page_data()

    def refresh_all(self, *, force: bool = False) -> None:
        if force:
            self._invalidate_data()
        region = self.region_code
        key = f"overview:{region}"

        def query(conn: sqlite3.Connection) -> dict[str, Any]:
            return {
                "metrics": dashboard_metrics(conn, region_code=region),
                "sources": source_statuses(conn, region_code=region),
                "relationships": relationship_rows(conn, region_code=region, limit=15),
            }

        self._schedule_load(
            key,
            "Обзор",
            self._db_loader(query),
            self._apply_overview_payload,
            force=force,
        )
        if force:
            QTimer.singleShot(0, self._ensure_current_page_data)

    def refresh_sources_only(self, *, force: bool = False) -> None:
        region = self.region_code
        key = f"sources:{region}"

        def apply(rows: object) -> None:
            if not isinstance(rows, list):
                return
            self._source_cache = {str(item["key"]): item for item in rows}
            self._render_overview_sources(rows)
            self._render_sources(rows)
            self._sync_collection_panel_from_source()

        self._schedule_load(
            key,
            "Статус источников",
            self._db_loader(lambda conn: source_statuses(conn, region_code=region)),
            apply,
            force=force,
        )

    def _apply_overview_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        metrics = payload.get("metrics") or {}
        sources = payload.get("sources") or []
        rels = payload.get("relationships") or []
        if isinstance(sources, list):
            self._source_cache = {str(item["key"]): item for item in sources}
            self._render_overview_sources(sources)
            self._render_sources(sources)
            self._sync_collection_panel_from_source()
        if isinstance(metrics, dict):
            self._render_metrics(metrics)
        if isinstance(rels, list):
            self._render_overview_relationships(rels)
        self.overview_updated.setText("готово")

    def _render_metrics(self, metrics: dict[str, Any]) -> None:
        values = [
            (self.fmt_int(metrics.get("purchases")), f"Закупки · до {metrics.get('purchase_date_to') or '—'}"),
            (self.fmt_int(metrics.get("contracts")), f"Контракты · до {metrics.get('contract_date_to') or '—'}"),
            (self.fmt_int(metrics.get("suppliers")), "Поставщики"),
            (self.fmt_int(metrics.get("buyers")), "Заказчики"),
            (self.fmt_pct(metrics.get("protocol_coverage")), "Покрытие протоколами"),
            (self.fmt_pct(metrics.get("contract_linkage")), "Связано с закупками"),
        ]
        for card, (value, caption) in zip(self.metric_cards, values):
            card.set_metric(value, caption)

    def _render_overview_sources(self, sources: list[dict[str, Any]]) -> None:
        self._clear_layout(self.overview_sources_layout)
        for source in sources:
            row = QFrame()
            row.setObjectName("sourceCard")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(11, 8, 11, 8)
            action = str(source.get("action") or "")
            collecting = bool(self._collection_action and action == self._collection_action)
            state = "collecting" if collecting else str(source.get("state") or ("connected" if source.get("connected") else "empty"))
            status_color = {
                "collecting": BLUE,
                "up_to_date": GREEN,
                "needs_resume": YELLOW,
                "connected": GREEN,
                "empty": RED,
            }.get(state, MUTED)
            status = QLabel("●")
            status.setStyleSheet(f"color:{status_color};")
            lay.addWidget(status)
            text = QVBoxLayout()
            text.setSpacing(0)
            title = QLabel(str(source.get("title") or "Источник"))
            title.setStyleSheet("font-weight: 600;")
            meta = QLabel(self.source_range(source))
            meta.setObjectName("muted")
            text.addWidget(title)
            text.addWidget(meta)
            lay.addLayout(text, 1)
            count = QLabel(self.fmt_int(source.get("count")))
            count.setStyleSheet("font-weight: 650;")
            lay.addWidget(count)
            btn = QPushButton("Открыть")
            btn.setObjectName("ghost")
            btn.clicked.connect(lambda _checked=False, s=source: self._show_source_detail(s))
            lay.addWidget(btn)
            self.overview_sources_layout.addWidget(row)
        self.overview_sources_layout.addStretch(1)

    def _render_sources(self, sources: list[dict[str, Any]]) -> None:
        self._clear_layout(self.sources_grid)
        for idx, source in enumerate(sources):
            card = SourceCard(
                source,
                on_detail=self._show_source_detail,
                on_action=self._run_source_action,
                active_action=self._collection_action,
            )
            self.sources_grid.addWidget(card, idx // 2, idx % 2)
        rows = math.ceil(max(1, len(sources)) / 2)
        for row in range(rows):
            self.sources_grid.setRowStretch(row, 0)
        self.sources_grid.setRowStretch(rows, 1)

    def _render_overview_relationships(self, rows: list[dict[str, Any]]) -> None:
        self._set_table_rows(
            self.overview_rel_table,
            rows,
            lambda row: [
                f"{float(row.get('relationship_score') or 0):.1f}",
                str(row.get("buyer_inn") or "—"),
                str(row.get("supplier_inn") or "—"),
                self.rel_type_ru(str(row.get("relationship_type") or "")),
                self.fmt_money(row.get("pair_value_rub")),
            ],
        )

    def refresh_opportunities(self, *, force: bool = False) -> None:
        region = self.region_code
        key = f"opportunities:{region}"

        def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            # One expensive market pass per data generation; group filters are local.
            return opportunity_rows(conn, region_code=region, group=None, limit=5000)

        def apply(rows: object) -> None:
            self._opportunity_rows_cache = list(rows) if isinstance(rows, list) else []
            self._render_opportunity_group()

        self._schedule_load(
            key,
            "Opportunity Radar",
            self._db_loader(query),
            apply,
            force=force,
        )

    def _render_opportunity_group(self) -> None:
        if not hasattr(self, "opp_table"):
            return
        group = self.opp_group.currentText()
        rows = self._opportunity_rows_cache
        if group != "все":
            rows = [row for row in rows if str(row.get("group") or "") == group]
        rows = rows[:250]
        self.opp_count_label.setText(f"{self.fmt_int(len(rows))} строк")

        def values(row: dict[str, Any]) -> list[str]:
            code = row.get("market_code") or row.get("code") or row.get("ktru_code") or row.get("okpd2_code") or "—"
            return [
                f"{float(row.get('score') or 0):.2f}",
                str(code),
                str(row.get("segment") or "—"),
                self.fmt_money(row.get("demand_rub") or row.get("demand") or 0),
                self.fmt_int(row.get("procurement_count") or row.get("procurements") or row.get("purchases") or 0),
                self.fmt_pct(row.get("supplier_gap_share") or row.get("supplier_gap") or 0),
                self.fmt_optional_score(row.get("distribution_gap_score")),
                self.fmt_pct(row.get("contract_coverage") or row.get("winner_purchase_coverage") or 0),
                str(row.get("label") or "—"),
            ]

        self._set_table_rows(self.opp_table, rows, values)

    def refresh_organizations(self, *, force: bool = False) -> None:
        region = self.region_code
        key = f"organizations:{region}"

        def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return organization_rows(conn, region_code=region, query=None, limit=5000)

        def apply(rows: object) -> None:
            self._organization_rows_cache = list(rows) if isinstance(rows, list) else []
            self._filter_organizations()

        self._schedule_load(
            key,
            "Организации",
            self._db_loader(query),
            apply,
            force=force,
        )

    def _filter_organizations(self) -> None:
        if not hasattr(self, "org_table"):
            return
        query = self.org_search.text().strip().lower()
        if query:
            rows = [
                row
                for row in self._organization_rows_cache
                if query in str(row.get("inn") or "").lower()
                or query in str(row.get("name") or "").lower()
            ]
        else:
            rows = self._organization_rows_cache
        rows = rows[:500]
        self.org_count_label.setText(f"{self.fmt_int(len(rows))} организаций")
        self._set_table_rows(
            self.org_table,
            rows,
            lambda row: [
                str(row.get("inn") or "—"),
                str(row.get("name") or "—"),
                str(row.get("role") or "—"),
                self.fmt_int(row.get("contracts")),
                self.fmt_money(row.get("value")),
            ],
        )

    def refresh_graph(self, *, force: bool = False) -> None:
        if not hasattr(self, "graph_scene"):
            return
        try:
            min_value = max(0.0, float(self.graph_min_value.text().replace(" ", "").replace(",", ".")))
        except ValueError:
            min_value = 100_000.0
            self.graph_min_value.setText("100000")
        min_contracts = int(self.graph_min_contracts.value())
        region = self.region_code
        key = f"graph:{region}:{min_contracts}:{min_value:.2f}"

        def query(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = relationship_rows(
                conn,
                region_code=region,
                min_contracts=min_contracts,
                min_value_rub=min_value,
                limit=50,
            )
            inns = sorted({str(r["buyer_inn"]) for r in rows} | {str(r["supplier_inn"]) for r in rows})
            return {"rows": rows, "names": organization_names(conn, inns)}

        def apply(payload: object) -> None:
            data = payload if isinstance(payload, dict) else {}
            rows = data.get("rows") or []
            names = data.get("names") or {}
            self._graph_rows = list(rows)
            self._draw_graph(list(rows), dict(names))

        self._schedule_load(
            key,
            "Граф связей",
            self._db_loader(query),
            apply,
            force=force,
        )

    def refresh_purchases(self, *, force: bool = False) -> None:
        region = self.region_code
        key = f"purchases:{region}"

        def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return recent_purchases(conn, region_code=region, limit=150)

        def apply(rows: object) -> None:
            data = list(rows) if isinstance(rows, list) else []
            self._set_table_rows(
                self.purchase_table,
                data,
                lambda row: [
                    str(row.get("purchase_number") or "—"),
                    str(row.get("published_at") or "")[:10],
                    self.fmt_money(row.get("max_price") or 0),
                    str(row.get("stage") or "—"),
                    str(row.get("object_info") or "—"),
                ],
            )

        self._schedule_load(
            key,
            "Закупки",
            self._db_loader(query),
            apply,
            force=force,
        )

    # ---------- details ----------
    def _overview_relationship_selected(self) -> None:
        row = self._selected_payload(self.overview_rel_table)
        if row:
            self._show_relationship_detail(row)

    def _opportunity_selected(self) -> None:
        row = self._selected_payload(self.opp_table)
        if not row:
            return
        code = row.get("code") or row.get("market_code") or "—"
        lines = [
            str(row.get("label") or code),
            f"Код: {code}",
            f"Сегмент: {row.get('segment') or '—'}",
            f"Score: {float(row.get('score') or 0):.2f}",
            f"Спрос: {self.fmt_money(row.get('demand_rub') or 0)} ₽",
            f"Закупок: {self.fmt_int(row.get('procurement_count') or row.get('procurements') or 0)}",
            "",
            f"Supplier gap: {self.fmt_pct(row.get('supplier_gap_share') or 0)}",
            f"No-bid gap: {self.fmt_pct(row.get('no_bid_gap_share') or 0)}",
            f"Barrier gap: {self.fmt_pct(row.get('barrier_gap_share') or 0)}",
            f"Протоколы: {self.fmt_pct(row.get('protocol_coverage') or 0)}",
            f"Покрытие контрактами: {self.fmt_pct(row.get('winner_purchase_coverage') or 0)}",
            f"Победителей: {self.fmt_int(row.get('known_contract_suppliers') or 0)}",
            f"TOP-1 победитель: {self.fmt_optional_pct(row.get('top1_supplier_share'))}",
            f"GISP: {row.get('gisp_match_state') or '—'}",
            f"Distribution gap: {self.fmt_optional_score(row.get('distribution_gap_score'))}",
        ]
        self._detail_target = ("market", str(code))
        self._set_detail("Рынок", "\n".join(lines), open_panel=True)

    def _organization_selected(self) -> None:
        row = self._selected_payload(self.org_table)
        if row and row.get("inn"):
            self._show_organization(str(row["inn"]))

    def _show_organization(self, inn: str) -> None:
        inn = str(inn)
        self._detail_target = ("organization", inn)
        self._set_detail("Организация", f"{inn}\n\nЗагрузка карточки…", open_panel=True)
        region = self.region_code
        key = f"organization-card:{region}:{inn}"

        def query(conn: sqlite3.Connection) -> dict[str, Any]:
            return organization_card(conn, region_code=region, inn=inn)

        def apply(payload: object) -> None:
            if self._detail_target != ("organization", inn) or not isinstance(payload, dict):
                return
            card = payload
            lines = [
                str(card.get("name") or inn),
                f"ИНН: {inn}",
                f"Роль: {card.get('role') or '—'}",
                f"Контрактов: {self.fmt_int(card.get('contracts'))}",
                f"Наблюдаемая сумма: {self.fmt_money(card.get('value'))} ₽",
                "",
                "Крупнейшие связи",
            ]
            partners = card.get("partners") or []
            if not partners:
                lines.append("—")
            for idx, partner in enumerate(partners, 1):
                partner_name = partner.get("name") or partner.get("inn")
                lines.append(
                    f"{idx}. {partner.get('direction')} · {partner_name}\n"
                    f"   {partner.get('inn')} · {self.fmt_int(partner.get('contracts'))} контр. · "
                    f"{self.fmt_money(partner.get('value'))} ₽ · {self.fmt_pct(partner.get('share'))}"
                )
            self._set_detail("Организация", "\n".join(lines), open_panel=True)

        self._schedule_load(key, "Карточка организации", self._db_loader(query), apply)

    def _purchase_selected(self) -> None:
        row = self._selected_payload(self.purchase_table)
        if not row:
            return
        number = str(row.get("purchase_number") or "")
        self._detail_target = ("purchase", number)
        self._set_detail("Закупка", f"№ {number}\n\nЗагрузка…", open_panel=True)
        row_copy = dict(row)
        key = f"purchase-card:{number}"

        def query(conn: sqlite3.Connection) -> dict[str, Any]:
            protocol = conn.execute(
                """
                SELECT applications_count, admitted_count, rejected_count, final_price, is_abandoned
                FROM tender_protocols
                WHERE purchase_number=?
                ORDER BY COALESCE(published_at,'') DESC, id DESC
                LIMIT 1
                """,
                (number,),
            ).fetchone()
            contracts = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(price),0) AS value FROM contracts WHERE purchase_number=?",
                (number,),
            ).fetchone()
            return {
                "protocol": dict(protocol) if protocol else None,
                "contracts": dict(contracts) if contracts else None,
            }

        def apply(payload: object) -> None:
            if self._detail_target != ("purchase", number):
                return
            data = payload if isinstance(payload, dict) else {}
            protocol = data.get("protocol")
            contracts = data.get("contracts")
            lines = [
                f"№ {number}",
                f"Дата: {str(row_copy.get('published_at') or '')[:19]}",
                f"НМЦК: {self.fmt_money(row_copy.get('max_price') or 0)} ₽",
                f"Stage: {row_copy.get('stage')}",
                "",
                str(row_copy.get("object_info") or "Описание отсутствует"),
            ]
            if protocol:
                lines += [
                    "",
                    "Последний протокол",
                    f"заявок {protocol['applications_count']} · допущено {protocol['admitted_count']} · отклонено {protocol['rejected_count']}",
                    f"final price: {self.fmt_money(protocol['final_price']) if protocol['final_price'] is not None else '—'} ₽",
                    f"несостоявшаяся: {'да' if protocol['is_abandoned'] else 'нет'}",
                ]
            if contracts:
                lines += ["", f"Связанные контракты: {contracts['n']} · {self.fmt_money(contracts['value'])} ₽"]
            self._set_detail("Закупка", "\n".join(lines), open_panel=True)

        self._schedule_load(key, "Карточка закупки", self._db_loader(query), apply)

    def _show_relationship_detail(self, row: dict[str, Any]) -> None:
        self._detail_target = ("relationship", f"{row.get('buyer_inn')}:{row.get('supplier_inn')}")
        lines = [
            f"Заказчик: {row.get('buyer_inn')}",
            f"Поставщик: {row.get('supplier_inn')}",
            "",
            f"Тип: {self.rel_type_ru(str(row.get('relationship_type') or ''))}",
            f"Score: {float(row.get('relationship_score') or 0):.2f}",
            f"Evidence: {row.get('evidence') or '—'}",
            f"Контрактов пары: {self.fmt_int(row.get('contracts'))}",
            f"Закупок пары: {self.fmt_int(row.get('purchases'))}",
            f"Сумма: {self.fmt_money(row.get('pair_value_rub'))} ₽",
            f"Доля у заказчика: {self.fmt_pct(row.get('buyer_share'))}",
            f"Зависимость поставщика: {self.fmt_pct(row.get('supplier_dependency'))}",
            f"Взаимная зависимость: {self.fmt_pct(row.get('mutual_dependency'))}",
        ]
        self._set_detail("Связь организаций", "\n".join(lines), open_panel=True)

    def _show_source_detail(self, source: dict[str, Any]) -> None:
        self._detail_target = ("source", str(source.get("key") or ""))
        history = source.get("history") or {}
        state = str(source.get("state") or ("connected" if source.get("connected") else "empty"))
        state_text = {
            "up_to_date": "актуально",
            "needs_resume": "требуется продолжение",
            "connected": "подключён",
            "empty": "нет данных",
        }.get(state, state)
        lines = [
            str(source.get("title") or "Источник"),
            "",
            f"Статус: {state_text}",
            f"Строк: {self.fmt_int(source.get('count'))}",
            f"Диапазон: {self.source_range(source)}",
            f"Обновлено: {source.get('updated_at') or '—'}",
            f"Прогресс: {float(source.get('progress') or 0) * 100:.1f}%",
            f"Детали: {source.get('details') or '—'}",
        ]
        if history:
            lines += ["", "Checkpoint / sync state"]
            for key in (
                "since_date", "until_date", "oldest_published_at", "next_page", "rows_seen",
                "total_elements", "completed", "updated_at", "source_path", "source_scope",
            ):
                if key in history:
                    lines.append(f"{key}: {history.get(key)}")
        self._set_detail("Источник", "\n".join(lines), open_panel=True)

    # ---------- graph ----------
    def _draw_graph(self, rows: list[dict[str, Any]], names: dict[str, str]) -> None:
        self.graph_scene.clear()
        if not rows:
            text = self.graph_scene.addText("Нет связей для текущих фильтров")
            text.setDefaultTextColor(QColor(MUTED))
            self.graph_scene.setSceneRect(0, 0, 900, 520)
            return

        buyers: list[str] = []
        suppliers: list[str] = []
        for row in rows:
            buyer = str(row.get("buyer_inn") or "")
            supplier = str(row.get("supplier_inn") or "")
            if buyer and buyer not in buyers:
                buyers.append(buyer)
            if supplier and supplier not in suppliers:
                suppliers.append(supplier)
        buyers = buyers[:22]
        suppliers = suppliers[:22]
        allowed_buyers = set(buyers)
        allowed_suppliers = set(suppliers)

        node_w, node_h = 228.0, 50.0
        width = 1240.0
        height = max(620.0, 64.0 * max(len(buyers), len(suppliers)) + 90.0)
        left_x, right_x = 70.0, width - node_w - 70.0

        def positions(items: list[str], x: float) -> dict[str, tuple[float, float]]:
            if not items:
                return {}
            gap = (height - 110.0) / max(1, len(items) - 1)
            return {inn: (x, 55.0 + idx * gap) for idx, inn in enumerate(items)}

        bpos = positions(buyers, left_x)
        spos = positions(suppliers, right_x)
        max_value = max(float(row.get("pair_value_rub") or 0) for row in rows) or 1.0

        for row in reversed(rows):
            buyer = str(row.get("buyer_inn") or "")
            supplier = str(row.get("supplier_inn") or "")
            if buyer not in allowed_buyers or supplier not in allowed_suppliers:
                continue
            x1, y1 = bpos[buyer]
            x2, y2 = spos[supplier]
            rel_type = str(row.get("relationship_type") or "distributed")
            color = {
                "mutual_concentration": RED,
                "supplier_dependent": YELLOW,
                "buyer_dependent": BLUE,
                "distributed": "#536172",
            }.get(rel_type, "#536172")
            ratio = max(0.0, float(row.get("pair_value_rub") or 0) / max_value)
            pen = QPen(QColor(color), 1.1 + 4.4 * math.sqrt(ratio))
            pen.setCosmetic(True)
            line = QGraphicsLineItem(x1 + node_w, y1 + node_h / 2, x2, y2 + node_h / 2)
            line.setPen(pen)
            line.setOpacity(0.78)
            line.setZValue(0)
            line.setData(0, row)
            self.graph_scene.addItem(line)

        for role, items, positions_map, accent in (
            ("buyer", buyers, bpos, ACCENT),
            ("supplier", suppliers, spos, GREEN),
        ):
            for inn in items:
                x, y = positions_map[inn]
                node = QGraphicsRectItem(0, 0, node_w, node_h)
                node.setPos(x, y)
                node.setBrush(QBrush(QColor(SURFACE_ALT)))
                node.setPen(QPen(QColor(accent), 1.2))
                node.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                node.setData(0, inn)
                node.setData(1, role)
                node.setZValue(2)
                self.graph_scene.addItem(node)

                name = names.get(inn) or inn
                if len(name) > 28:
                    name = name[:25] + "…"
                title = QGraphicsSimpleTextItem(name, node)
                title.setBrush(QBrush(QColor(TEXT)))
                title.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
                title.setPos(10, 7)
                inn_text = QGraphicsSimpleTextItem(inn, node)
                inn_text.setBrush(QBrush(QColor(MUTED)))
                inn_text.setFont(QFont("Segoe UI", 8))
                inn_text.setPos(10, 26)

        buyer_title = self.graph_scene.addText("ЗАКАЗЧИКИ")
        buyer_title.setDefaultTextColor(QColor(MUTED))
        buyer_title.setPos(left_x, 8)
        supplier_title = self.graph_scene.addText("ПОСТАВЩИКИ")
        supplier_title.setDefaultTextColor(QColor(MUTED))
        supplier_title.setPos(right_x, 8)
        self.graph_scene.setSceneRect(0, 0, width, height)
        self.graph_view.resetTransform()
        self.graph_view.fitInView(self.graph_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _graph_selection_changed(self) -> None:
        for item in self.graph_scene.selectedItems():
            inn = item.data(0)
            if inn:
                self._show_organization(str(inn))
                return

    # ---------- detail panel ----------
    def _set_detail(self, title: str, body: str, *, open_panel: bool = False) -> None:
        self.detail_title.setText(title)
        self.detail_text.setPlainText(body)
        self.detail_text.moveCursor(QTextCursor.MoveOperation.Start)
        if open_panel:
            self._expand_detail_panel()

    def _expand_detail_panel(self) -> None:
        sizes = self.main_splitter.sizes()
        if len(sizes) < 2 or sizes[1] > 60:
            return
        total = max(sum(sizes), self.main_splitter.width())
        detail = min(390, max(320, int(total * 0.27)))
        self.main_splitter.setSizes([max(620, total - detail), detail])

    def _collapse_detail_panel(self) -> None:
        sizes = self.main_splitter.sizes()
        total = max(sum(sizes), self.main_splitter.width())
        self.main_splitter.setSizes([total, 0])

    def _toggle_detail_panel(self) -> None:
        sizes = self.main_splitter.sizes()
        if len(sizes) >= 2 and sizes[1] > 60:
            self._collapse_detail_panel()
        else:
            self._expand_detail_panel()

    # ---------- collection / QProcess ----------
    def _run_source_action(self, action: str) -> None:
        process_running = (
            self._process is not None
            and self._process.state() != QProcess.ProcessState.NotRunning
        )
        if process_running or self._collection_action is not None:
            self._set_detail(
                "Сбор уже идёт",
                "Останови текущую сессию или дождись её завершения.",
                open_panel=True,
            )
            return
        if not self._source_cache:
            self._set_detail(
                "Источники ещё загружаются",
                "Дождись обновления статуса источников и повтори действие.",
                open_panel=True,
            )
            self.refresh_sources_only(force=True)
            return
        command = self._command_for_action(action, self._source_cache)
        if not command:
            return

        self._collection_action = action
        self._collection_command = command
        self._collection_stop_requested = False
        self._collection_session_chunks = 0
        self._collection_session_items = 0
        self._collection_session_requests = 0
        self._collection_last_rpm = None
        self._collection_current_date = None
        self._collection_next_page = None
        self._collection_total = None
        self._collection_chunk_text = ""
        self._collection_line_buffer = ""
        self.stop_btn.setEnabled(True)
        self._update_collection_action_buttons()
        self._render_sources(list(self._source_cache.values()))
        self._render_overview_sources(list(self._source_cache.values()))
        self._sync_collection_panel_from_source()
        self._append_log(
            f"\n[UI] Новая сессия: {self._collection_action_title(action)}. "
            f"Без ручных повторов; остановка — кнопкой 'Остановить сбор'.\n"
        )
        self._show_page("collection")
        self._launch_collection_chunk()

    def _command_for_action(self, action: str, sources: dict[str, dict[str, Any]]) -> list[str] | None:
        exe = self._cli_prefix()
        today = date.today().isoformat()
        if action == "continue_purchases":
            history = (sources.get("purchases") or {}).get("history") or {}
            since = history.get("since_date") or "2024-01-01"
            until = history.get("until_date") or today
            return exe + [
                "ingest-history", "--region", str(self.region_code), "--since", str(since),
                "--until", str(until), "--pages", "20", "--max-details", "50", "--db", self.db_path,
            ]
        if action == "continue_contracts":
            history = (sources.get("contracts") or {}).get("history") or {}
            since = history.get("since_date") or "2024-01-01"
            until = history.get("until_date") or today
            return exe + [
                "ingest-contract-history", "--region", str(self.region_code), "--since", str(since),
                "--until", str(until), "--pages", "20", "--db", self.db_path,
            ]
        if action == "continue_rzn":
            return exe + ["rzn-sync", "--pages", "10", "--active-only", "--db", self.db_path]
        if action == "continue_fsa":
            return exe + ["fsa-sync", "--pages", "10", "--db", self.db_path]
        if action == "refresh_gisp":
            history = (sources.get("gisp") or {}).get("history") or {}
            path = history.get("source_path")
            if not path:
                self._set_detail("ГИСП", "В базе нет пути последнего XLSX.", open_panel=True)
                return None
            return exe + [
                "gisp-import", str(path), "--scope", str(history.get("source_scope") or "active"), "--db", self.db_path,
            ]
        self._set_detail("Неизвестное действие", action, open_panel=True)
        return None

    def _cli_prefix(self) -> list[str]:
        executable = shutil.which("procure-radar")
        if executable:
            return [executable]
        return [sys.executable, "-m", "procure_radar.cli"]

    @staticmethod
    def _collection_source_key(action: str | None) -> str | None:
        return {
            "continue_purchases": "purchases",
            "continue_contracts": "contracts",
            "continue_rzn": "rzn",
            "continue_fsa": "fsa",
            "refresh_gisp": "gisp",
        }.get(action or "")

    @staticmethod
    def _collection_action_title(action: str | None) -> str:
        return {
            "continue_purchases": "ГосПлан · закупки",
            "continue_contracts": "ГосПлан · контракты",
            "continue_rzn": "Росздравнадзор · медизделия",
            "continue_fsa": "ФСА · сертификаты",
            "refresh_gisp": "ГИСП · реестр российской продукции",
        }.get(action or "", "Сбор данных")

    def _launch_collection_chunk(self) -> None:
        if self._collection_action is None:
            return
        if self._collection_stop_requested:
            self._finish_collection_session("stopped")
            return
        command = self._collection_command
        if not command:
            self._finish_collection_session("error", "Команда сбора отсутствует")
            return
        self._collection_chunk_text = ""
        self._collection_line_buffer = ""
        next_chunk = self._collection_session_chunks + 1
        self._append_log(f"\n[UI] Чанк {next_chunk}\n$ " + " ".join(command) + "\n")
        self.collection_state_label.setText(f"Сбор идёт · чанк {next_chunk}")
        self.task_label.setText(f"● {self._collection_action_title(self._collection_action)}")
        self._start_process(command[0], command[1:])

    def _start_process(self, program: str, args: list[str]) -> None:
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self._process_output_ready)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self._process = process
        self.stop_btn.setEnabled(True)
        process.start(program, args)

    def _process_output_ready(self) -> None:
        if self._process is None:
            return
        raw = bytes(self._process.readAllStandardOutput())
        text = raw.decode("utf-8", errors="replace")
        if not text:
            return
        self._collection_chunk_text += text
        self._append_log(text)
        self._consume_collection_output(text)

    def _consume_collection_output(self, text: str) -> None:
        self._collection_line_buffer += text
        parts = self._collection_line_buffer.split("\n")
        self._collection_line_buffer = parts.pop()
        changed = False
        for line in parts:
            chunk = parse_collection_chunk(line)
            if chunk.current_date:
                self._collection_current_date = chunk.current_date
                changed = True
            if chunk.avg_rpm is not None:
                self._collection_last_rpm = chunk.avg_rpm
                changed = True
            if chunk.next_page is not None:
                self._collection_next_page = chunk.next_page
                changed = True
            if chunk.total is not None:
                self._collection_total = chunk.total
                changed = True
        if changed:
            self._sync_collection_panel_from_source()

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._collection_line_buffer:
            self._consume_collection_output("\n")
        chunk = parse_collection_chunk(self._collection_chunk_text)
        self._collection_session_chunks += 1
        if chunk.items is not None:
            self._collection_session_items += chunk.items
        if chunk.api_requests is not None:
            self._collection_session_requests += chunk.api_requests
        elif chunk.pages is not None:
            self._collection_session_requests += chunk.pages
        if chunk.avg_rpm is not None:
            self._collection_last_rpm = chunk.avg_rpm
        if chunk.current_date:
            self._collection_current_date = chunk.current_date
        if chunk.next_page is not None:
            self._collection_next_page = chunk.next_page
        if chunk.total is not None:
            self._collection_total = chunk.total

        self._append_log(
            f"\n[UI] Чанк завершён, code={exit_code}, "
            f"status={chunk.status or '-'}, stop={chunk.stop_reason or '-'}\n"
        )
        self._process = None
        self._sync_collection_panel_from_source()

        action = self._collection_action
        if should_chain_collection(
            action,
            exit_code=exit_code,
            chunk=chunk,
            stop_requested=self._collection_stop_requested,
        ):
            self.collection_state_label.setText("Checkpoint сохранён · продолжаю автоматически…")
            self.task_label.setText(f"● {self._collection_action_title(action)} · продолжаю")
            self.refresh_sources_only(force=True)
            QTimer.singleShot(300, self._launch_collection_chunk)
            return

        if self._collection_stop_requested:
            self._finish_collection_session("stopped")
            return
        if exit_code != 0:
            self._finish_collection_session("error", f"Процесс завершился с code={exit_code}")
            return
        if chunk.complete:
            self._finish_collection_session("complete")
            return
        if not is_repeatable_action(action):
            self._finish_collection_session("complete")
            return

        # Successful exit without a known completion marker: do not loop blindly.
        reason = chunk.stop_reason or chunk.status or "неизвестное состояние"
        self._finish_collection_session("paused", f"Сбор остановился: {reason}")

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        msg = self._process.errorString() if self._process is not None else "Неизвестная ошибка"
        self._append_log(f"\n[UI] Не удалось запустить процесс: {msg}\n")
        self._process = None
        self._finish_collection_session("error", msg)

    def _finish_collection_session(self, state: str, message: str | None = None) -> None:
        title = self._collection_action_title(self._collection_action)
        self._process = None
        self.stop_btn.setEnabled(False)
        if state == "complete":
            state_text = "✓ Сбор завершён"
            self.task_label.setText("Сбор завершён")
        elif state == "stopped":
            state_text = "◷ Остановлено пользователем · checkpoint сохранён"
            self.task_label.setText("Сбор остановлен")
        elif state == "paused":
            state_text = "◷ Требуется продолжение"
            self.task_label.setText("Сбор приостановлен")
        else:
            state_text = "! Ошибка сбора"
            self.task_label.setText("Ошибка сбора")
        if message:
            state_text += f" · {message}"
        self.collection_source_label.setText(title)
        self.collection_state_label.setText(state_text)

        old_action = self._collection_action
        self._collection_action = None
        self._collection_command = None
        self._collection_stop_requested = False
        self._update_collection_action_buttons()
        self._append_log(f"\n[UI] Сессия: {state_text}\n")

        # Any completed chunk may have changed the DB; invalidate analytic caches once per session.
        self._invalidate_data()
        self.refresh_all()
        if self._current_page not in {"overview", "sources", "collection"}:
            QTimer.singleShot(0, self._ensure_current_page_data)
        if old_action:
            QTimer.singleShot(0, lambda: self.refresh_sources_only(force=True))

    def _stop_task(self) -> None:
        if self._collection_action is None:
            return
        self._collection_stop_requested = True
        self.collection_state_label.setText("Останавливаю после безопасной точки…")
        self.task_label.setText("Останавливаю сбор…")
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            self._finish_collection_session("stopped")
            return
        self._append_log("\n[UI] Остановка процесса…\n")
        process.terminate()
        QTimer.singleShot(1500, self._kill_process_if_needed)

    def _kill_process_if_needed(self) -> None:
        process = self._process
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def _update_collection_action_buttons(self) -> None:
        if not hasattr(self, "collection_action_buttons"):
            return
        active = self._collection_action is not None
        for button in self.collection_action_buttons.values():
            button.setEnabled(not active)

    def _sync_collection_panel_from_source(self) -> None:
        if not hasattr(self, "collection_progress"):
            return
        action = self._collection_action
        source_key = self._collection_source_key(action)
        source = self._source_cache.get(source_key or "") if source_key else None
        if source:
            self.collection_source_label.setText(str(source.get("title") or self._collection_action_title(action)))
            self.collection_range_label.setText(self.source_range(source))
            progress = float(source.get("progress") or 0.0)
            live_progress = self._live_history_progress(source, self._collection_current_date)
            if live_progress is not None:
                progress = max(progress, live_progress)
            self.collection_progress.setValue(round(max(0.0, min(1.0, progress)) * 1000))
        elif action:
            self.collection_source_label.setText(self._collection_action_title(action))

        if self._collection_current_date:
            current = self._collection_current_date
        elif self._collection_next_page is not None:
            current = f"страница {self._collection_next_page}"
            if self._collection_total is not None:
                current += f" · всего записей {self.fmt_int(self._collection_total)}"
        else:
            current = "—"
        self.collection_current_label.setText(f"Текущая позиция: {current}")
        parts = [f"чанков {self._collection_session_chunks}"]
        if self._collection_session_items:
            parts.append(f"строк {self.fmt_int(self._collection_session_items)}")
        if self._collection_session_requests:
            parts.append(f"API {self.fmt_int(self._collection_session_requests)}")
        if self._collection_last_rpm is not None:
            parts.append(f"{self._collection_last_rpm:.2f} req/min")
        self.collection_session_label.setText("Сессия: " + " · ".join(parts))

    @staticmethod
    def _live_history_progress(source: dict[str, Any], current_date: str | None) -> float | None:
        if not current_date:
            return None
        history = source.get("history") or {}
        since = str(history.get("since_date") or "")[:10]
        until = str(history.get("until_date") or "")[:10]
        if not since or not until:
            return None
        try:
            lo = date.fromisoformat(since)
            hi = date.fromisoformat(until)
            cur = date.fromisoformat(current_date)
        except ValueError:
            return None
        total = max(1, (hi - lo).days)
        done = max(0, min(total, (hi - cur).days))
        return done / total

    def _toggle_collection_log(self) -> None:
        if not hasattr(self, "collection_log_panel"):
            return
        visible = not self.collection_log_panel.isVisible()
        self.collection_log_panel.setVisible(visible)
        self.collection_log_toggle.setText("Скрыть журнал" if visible else "Технический журнал")

    def _append_log(self, text: str) -> None:
        if not hasattr(self, "collection_log"):
            return
        self.collection_log.moveCursor(QTextCursor.MoveOperation.End)
        self.collection_log.insertPlainText(text)
        self.collection_log.moveCursor(QTextCursor.MoveOperation.End)

    def _clear_log(self) -> None:
        if hasattr(self, "collection_log"):
            self.collection_log.clear()

    # ---------- tables / utility ----------
    @staticmethod
    def _make_table(
        headers: list[str],
        *,
        widths: dict[int, int] | None = None,
        stretch_col: int | None = None,
    ) -> QTableView:
        table = QTableView()
        model = RowsTableModel(headers, table)
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.horizontalHeader().setHighlightSections(False)
        table.horizontalHeader().setStretchLastSection(False)
        # ResizeToContents scans every cell and was a visible source of UI stalls.
        for col in range(len(headers)):
            table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        if widths:
            for col, width in widths.items():
                table.setColumnWidth(col, width)
        if stretch_col is not None:
            table.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        return table

    @staticmethod
    def _set_table_rows(
        table: QTableView,
        rows: list[dict[str, Any]],
        values: Callable[[dict[str, Any]], list[str]],
    ) -> None:
        model = table.model()
        if isinstance(model, RowsTableModel):
            model.set_rows(rows, values)

    @staticmethod
    def _selected_payload(table: QTableView) -> dict[str, Any] | None:
        model = table.model()
        if not isinstance(model, RowsTableModel):
            return None
        rows = table.selectionModel().selectedRows()
        if not rows:
            index = table.currentIndex()
            if not index.isValid():
                return None
            return model.payload(index.row())
        return model.payload(rows[0].row())

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                ProcureRadarWindow._clear_layout(child_layout)

    @staticmethod
    def source_range(source: dict[str, Any]) -> str:
        start = source.get("date_from")
        end = source.get("date_to")
        if start and end:
            return f"{start} → {end}"
        if end:
            return f"до {end}"
        return "до —"

    @staticmethod
    def fmt_int(value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def fmt_money(value: Any) -> str:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return "—"
        if abs(n) >= 1_000_000_000:
            return f"{n / 1_000_000_000:.2f} млрд"
        if abs(n) >= 1_000_000:
            return f"{n / 1_000_000:.2f} млн"
        if abs(n) >= 1_000:
            return f"{n / 1_000:.1f} тыс"
        return f"{n:.0f}"

    @staticmethod
    def fmt_pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.0f}%"
        except (TypeError, ValueError):
            return "—"

    @classmethod
    def fmt_optional_pct(cls, value: Any) -> str:
        return "—" if value is None else cls.fmt_pct(value)

    @staticmethod
    def fmt_optional_score(value: Any) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def rel_type_ru(value: str) -> str:
        return {
            "mutual_concentration": "взаимная концентрация",
            "supplier_dependent": "зависит поставщик",
            "buyer_dependent": "зависит заказчик",
            "distributed": "распределено",
        }.get(value, value)

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(750):
                self._process.kill()
        self._thread_pool.clear()
        self._thread_pool.waitForDone(1200)
        event.accept()


def launch_qt_ui(db_path: str, *, region_code: int = 2) -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ProcureRadarWindow(db_path, region_code=region_code)
    window.show()
    if owns_app:
        raise SystemExit(app.exec())
