"""드래그 앤 드롭 공용 도우미 + 새 이미지 기본값 창. (클립 순서는 타임라인에서 끌어서 바꿈)"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QSettings
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QLabel, QPushButton

from ..schema import MOTION_LABELS, MOTIONS


def paths_from_mime(mime: QMimeData) -> list[Path]:
    """탐색기에서 끌어온 로컬 파일/폴더 경로들."""
    if mime is None or not mime.hasUrls():
        return []
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


def mime_has_files(mime: QMimeData) -> bool:
    return bool(paths_from_mime(mime))


def _truthy(v) -> bool:
    return str(v).lower() in ("true", "1", "yes")


class ImageDefaultsDialog(QDialog):
    """➕ 미디어 추가 ▾ ▸ 새 이미지 기본값: 끌어 놓기·추가할 때 이미지 표시 시간 · 움직임 · 순서 묻기."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("새로 추가하는 이미지 기본값")
        f = QFormLayout(self)
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
        self.skip_order = QCheckBox("여러 파일을 넣을 때 '순서 정하기'를 묻지 않고 이름순")
        self.skip_order.setToolTip("끄면 이미지·영상을 2개 이상 넣을 때 썸네일을 클릭해 순서를 정하는 창이 뜹니다")
        self.skip_order.setChecked(_truthy(self.settings.value("import/skip_order_dialog", False)))
        self.skip_order.toggled.connect(lambda v: self.settings.setValue("import/skip_order_dialog", bool(v)))
        f.addRow("", self.skip_order)
        self.image_duration.valueChanged.connect(lambda v: self.settings.setValue("import/image_duration", v))
        self.image_motion.currentIndexChanged.connect(
            lambda _: self.settings.setValue("import/image_motion", self.image_motion.currentData()))
        hint = QLabel("클립 순서는 타임라인에서 끌어서 바꿉니다. 탐색기에서 끌어 놓으면 놓은 자리에 들어갑니다.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa0a6;")
        f.addRow(hint)
        btn = QPushButton("닫기")
        btn.clicked.connect(self.accept)
        f.addRow(btn)
