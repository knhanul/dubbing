"""백그라운드 렌더(진행률/취소)."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)

from ..render.encoders import ENCODER_LABELS, detect_encoder
from ..render.ffmpeg_builder import RenderSettings
from ..render.runner import RenderCancelled, RenderError, export_voice_wav, render_project
from ..schema import Project


class RenderWorker(QThread):
    progress = Signal(float, str)
    finished_ok = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, project: Project, output: Path, settings: RenderSettings, kind: str = "video"):
        super().__init__()
        self.project = project.copy()  # UI 에서 계속 편집해도 안전하도록 복사본으로 렌더
        self.output = output
        self.settings = settings
        self.kind = kind
        self.cancel_event = threading.Event()

    def run(self):
        try:
            if self.kind == "voice":
                res = export_voice_wav(self.project, self.output, self.cancel_event)
            else:
                if self.settings.encoder == "auto":
                    self.progress.emit(0.0, "인코더 확인 중…")
                    self.settings.encoder = detect_encoder()
                res = render_project(self.project, self.output, self.settings,
                                     lambda f, m: self.progress.emit(f, m), self.cancel_event)
            self.finished_ok.emit(res)
        except RenderCancelled:
            self.cancelled.emit()
        except RenderError as e:
            self.failed.emit(str(e), e.log)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}", "")

    def cancel(self):
        self.cancel_event.set()


class ProgressDialog(QDialog):
    def __init__(self, worker: RenderWorker, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.worker = worker
        self.bar = QProgressBar(); self.bar.setRange(0, 1000)
        self.msg = QLabel("시작하는 중…")
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.hide()
        self.btn = QPushButton("취소")
        lay = QVBoxLayout(self)
        lay.addWidget(self.msg); lay.addWidget(self.bar); lay.addWidget(self.log); lay.addWidget(self.btn)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._done)
        worker.failed.connect(self._failed)
        worker.cancelled.connect(self.reject)
        self.btn.clicked.connect(self._cancel)
        self.result_obj = None

    def _on_progress(self, f, m):
        self.bar.setValue(int(f * 1000))
        self.msg.setText(f"{m}  ({f * 100:.0f}%)")

    def _cancel(self):
        if self.worker.isRunning():
            self.msg.setText("취소하는 중…")
            self.worker.cancel()
        else:
            self.reject()

    def _done(self, res):
        self.result_obj = res
        self.accept()

    def _failed(self, msg, log):
        self.msg.setText("❌ " + msg)
        if log:
            self.log.setPlainText(log)
            self.log.show()
            self.resize(800, 500)
        self.btn.setText("닫기")

    def exec_worker(self) -> bool:
        self.worker.start()
        ok = self.exec() == QDialog.Accepted
        if self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        return ok


class FinalRenderOptions(QDialog):
    def __init__(self, default_path: Path, parent=None, width: int = 1920, height: int = 1080, fps: float = 30.0):
        super().__init__(parent)
        self.setWindowTitle("영상 내보내기")
        from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QSpinBox

        from .size_presets import fill_size_combo, sync_size_combo
        self._sync = sync_size_combo
        self.size_combo = QComboBox()
        fill_size_combo(self.size_combo)
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setSingleStep(2); self.w.setValue(int(width))
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setSingleStep(2); self.h.setValue(int(height))
        self.fps = QDoubleSpinBox(); self.fps.setRange(1, 120); self.fps.setDecimals(3); self.fps.setValue(float(fps))
        sync_size_combo(self.size_combo, width, height)
        self.size_combo.currentIndexChanged.connect(self._preset)
        self.w.valueChanged.connect(self._sizes_edited)
        self.h.valueChanged.connect(self._sizes_edited)
        self.size_note = QLabel("<small>여기서 바꾸면 프로젝트(⚙ 출력 설정)에도 반영됩니다.</small>")
        self.path = QLineEdit(str(default_path))
        b = QPushButton("…")
        b.clicked.connect(self._browse)
        self.encoder = QComboBox()
        self.encoder.addItem("자동 (NVIDIA GPU 있으면 사용)", "auto")
        for k, v in ENCODER_LABELS.items():
            self.encoder.addItem(v, k)
        self.quality = QComboBox()
        self.quality.addItem("이미지 움직임 고품질 (2배 슈퍼샘플링, 권장)", 2)
        self.quality.addItem("빠르게 (1배)", 1)
        self.quality.addItem("최고 품질 (3배, 느림)", 3)
        f = QFormLayout(self)
        r = QHBoxLayout(); r.addWidget(self.path, 1); r.addWidget(b)
        f.addRow("저장 위치", r)
        f.addRow("영상 크기", self.size_combo)
        rs = QHBoxLayout(); rs.addWidget(self.w); rs.addWidget(QLabel("×")); rs.addWidget(self.h)
        rs.addSpacing(12); rs.addWidget(QLabel("fps")); rs.addWidget(self.fps)
        f.addRow("가로 × 세로", rs)
        f.addRow("", self.size_note)
        f.addRow("인코더", self.encoder)
        f.addRow("움직임 품질", self.quality)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("📤 내보내기 시작")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _preset(self, i):
        from .size_presets import combo_size
        pw, ph = combo_size(self.size_combo, i)
        if pw:
            for sb, v in ((self.w, pw), (self.h, ph)):
                old = sb.blockSignals(True); sb.setValue(v); sb.blockSignals(old)

    def _sizes_edited(self, *_):
        self._sync(self.size_combo, self.w.value(), self.h.value())

    def output_size(self) -> tuple[int, int, float]:
        w, h = self.w.value(), self.h.value()
        return w - w % 2, h - h % 2, float(self.fps.value())

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "저장 위치", self.path.text(), "MP4 (*.mp4)")
        if p:
            self.path.setText(p)

    def settings(self) -> RenderSettings:
        return RenderSettings(draft=False, encoder=self.encoder.currentData(), upscale=self.quality.currentData())
