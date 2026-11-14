"""드래그 앤 드롭 공용 도우미 + 끌어서 순서를 바꿀 수 있는 클립 목록."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QSettings, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from ..schema import MOTION_LABELS, MOTIONS
from .model import ProjectModel


def paths_from_mime(mime: QMimeData) -> list[Path]:
    """탐색기에서 끌어온 로컬 파일/폴더 경로들."""
    if mime is None or not mime.hasUrls():
        return []
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


def mime_has_files(mime: QMimeData) -> bool:
    return bool(paths_from_mime(mime))


class ClipList(QListWidget):
    """클립 목록. 끌어서 순서 바꾸기 + 탐색기 파일 드롭(놓은 위치에 삽입)."""

    files_dropped = Signal(list, int)  # 경로들, 삽입 위치
    order_changed = Signal(list)  # 새 순서(기존 클립 번호 목록)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(True)

    def _row_at(self, pos) -> int:
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        rect = self.visualItemRect(item)
        row = self.row(item)
        return row + 1 if pos.y() > rect.center().y() else row

    def dragEnterEvent(self, ev):
        if ev.source() is not self and mime_has_files(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.source() is not self and mime_has_files(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if ev.source() is not self and mime_has_files(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            self.files_dropped.emit(paths_from_mime(ev.mimeData()), self._row_at(ev.position().toPoint()))
            return
        super().dropEvent(ev)
        self.emit_order()

    def emit_order(self):
        order = [self.item(i).data(Qt.UserRole) for i in range(self.count())]
        if order != sorted(order):
            self.order_changed.emit(order)


class ClipListPanel(QWidget):
    files_dropped = Signal(list, int)

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.list = ClipList()
        self.btn_up = QPushButton("▲ 위로")
        self.btn_down = QPushButton("▼ 아래로")
        hint = QLabel("끌어서 순서를 바꾸세요. 탐색기에서 파일·폴더를 끌어 놓으면 놓은 위치에 추가됩니다.")
        hint.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.btn_up)
        row.addWidget(self.btn_down)
        row.addStretch(1)
        g = QGroupBox("새로 추가하는 이미지 기본값 (드래그 앤 드롭 · 클립 추가)")
        f = QFormLayout(g)
        self.settings = QSettings("knhanul", "VideoComposer")
        self.image_duration = QDoubleSpinBox()
        self.image_duration.setRange(0.1, 600)
        self.image_duration.setSingleStep(0.5)
        self.image_duration.setDecimals(2)
        self.image_duration.setSuffix(" 초")
        self.image_duration.setValue(float(self.settings.value("import/image_duration", 4.0)))
        self.image_motion = QComboBox()
        for m in MOTIONS:
            self.image_motion.addItem(MOTION_LABELS[m], m)
        i = self.image_motion.findData(self.settings.value("import/image_motion", "zoom_in"))
        self.image_motion.setCurrentIndex(max(0, i))
        f.addRow("이미지 표시 시간", self.image_duration)
        f.addRow("이미지 움직임", self.image_motion)
        self.image_duration.valueChanged.connect(lambda v: self.settings.setValue("import/image_duration", v))
        self.image_motion.currentIndexChanged.connect(
            lambda _: self.settings.setValue("import/image_motion", self.image_motion.currentData()))
        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        lay.addWidget(g)
        self.list.order_changed.connect(self.apply_order)
        self.list.files_dropped.connect(self.files_dropped.emit)
        self.list.currentRowChanged.connect(self._selected)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        model.changed.connect(self.refresh)
        model.selection_changed.connect(self._sync_selection)
        self._loading = False
        self.refresh()

    def refresh(self):
        self._loading = True
        self.list.clear()
        tl = self.model.timeline.clips
        for i, c in enumerate(self.model.project.clips):
            d = tl[i].duration if i < len(tl) else 0.0
            extra = f" · {MOTION_LABELS.get(c.motion, c.motion)}" if c.type == "image" and c.motion != "none" else ""
            icon = "🖼" if c.type == "image" else "🎬"
            it = QListWidgetItem(f"{i + 1}. {icon} {c.label or c.file}  ({d:.2f}초{extra})")
            it.setData(Qt.UserRole, i)
            it.setToolTip(c.file)
            self.list.addItem(it)
        self._loading = False
        self._sync_selection()

    def apply_order(self, order: list[int]):
        """order[k] = 새 k 번째 자리에 올 기존 클립 번호."""
        clips = self.model.project.clips
        if sorted(order) != list(range(len(clips))):
            self.refresh()
            return
        sel = self.model.selection
        new_sel = order.index(sel.index) if sel.kind == "clip" and 0 <= sel.index < len(order) else None
        self.model.project.clips = [clips[i] for i in order]
        self.model.commit()
        if new_sel is not None:
            self.model.select("clip", new_sel)

    def _move(self, d: int):
        i = self.list.currentRow()
        j = i + d
        n = len(self.model.project.clips)
        if 0 <= i < n and 0 <= j < n:
            order = list(range(n))
            order[i], order[j] = order[j], order[i]
            self.model.select("clip", i)
            self.apply_order(order)

    def _selected(self, row: int):
        if not self._loading and row >= 0:
            self.model.select("clip", row)

    def _sync_selection(self):
        s = self.model.selection
        self.list.blockSignals(True)
        if s.kind == "clip" and 0 <= s.index < self.list.count():
            self.list.setCurrentRow(s.index)
        else:
            self.list.clearSelection()
        self.list.blockSignals(False)
