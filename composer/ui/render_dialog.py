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
    def __init__(self, default_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("최종 렌더")
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
        f.addRow("인코더", self.encoder)
        f.addRow("움직임 품질", self.quality)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("렌더 시작")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "저장 위치", self.path.text(), "MP4 (*.mp4)")
        if p:
            self.path.setText(p)

    def settings(self) -> RenderSettings:
        return RenderSettings(draft=False, encoder=self.encoder.currentData(), upscale=self.quality.currentData())
