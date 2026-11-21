"""inspector 공통 베이스와 헬퍼.

inspector.py 와 페이지 클래스들이 공유.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from .model import ProjectModel
from .widgets import DIM, Columns, Section, icon_button

SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
PRIMARY_CSS = "QPushButton{background:#2f6fbf;color:white;font-weight:bold;border:1px solid #2f6fbf;border-radius:4px;padding:4px 8px}"
REC_TEXT_CSS = "QPushButton{color:#ff6b62;font-weight:bold}"


def _rel(project, path: str) -> str:
    import os
    try:
        return Path(os.path.relpath(path, project.base_dir)).as_posix()
    except ValueError:
        return path


class _Page(QWidget):
    """머리줄(무엇을 골랐는지 + 작은 아이콘 단추) · 섹션들(넓으면 2단)."""

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        self._loading = False
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(8, 8, 8, 8)
        self.outer.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(4)
        txt = QVBoxLayout()
        txt.setSpacing(1)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:13px;font-weight:bold;")
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(DIM + "font-size:11px;")
        txt.addWidget(self.head)
        txt.addWidget(self.hint)
        top.addLayout(txt, 1)
        self.head_btns = QHBoxLayout()
        self.head_btns.setSpacing(4)
        top.addLayout(self.head_btns)
        top.setAlignment(self.head_btns, Qt.AlignTop)
        self.outer.addLayout(top)
        self.cols = Columns()
        self.outer.addWidget(self.cols)
        self.outer.addStretch(1)

    def section(self, title: str = "", col: int = 0) -> Section:
        return self.cols.add(Section(title), col)

    def head_button(self, text: str, slot, tip: str = "") -> QToolButton:
        b = icon_button(text, tip)
        b.clicked.connect(slot)
        self.head_btns.addWidget(b)
        return b

    def _set(self, key, fn: Callable):
        if self._loading:
            return
        self.model.edit(fn, merge_key=key)

    def combo(self, items) -> QComboBox:
        cb = QComboBox()
        for value, label in items:
            cb.addItem(label, value)
        return cb

    @staticmethod
    def set_combo(cb: QComboBox, value):
        i = cb.findData(value)
        cb.setCurrentIndex(max(0, i))
