"""미리보기 플레이어: 영상 위에 현재 자막을 실시간으로 겹쳐 보여줍니다.

자막은 렌더하지 않고 프로젝트 값으로 바로 그리므로, 자막을 미세 조정하면 즉시 반영됩니다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QVBoxLayout, QWidget)


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


class _View(QGraphicsView):
    """자막을 그리는 그래픽 뷰 (외곽선 있는 텍스트)."""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.text = ""
        self.notice = ""
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#000000"))
        self.setFrameShape(QGraphicsView.NoFrame)

    def drawForeground(self, painter: QPainter, rect):
        painter.save()
        painter.resetTransform()
        vw, vh = self.viewport().width(), self.viewport().height()
        if self.text:
            f = QFont(self.font())
            f.setBold(True)
            f.setPixelSize(max(14, int(vh * 0.055)))
            lines = self.text.split("\n")
            lh = f.pixelSize() * 1.3
            y0 = vh - vh * 0.06 - lh * len(lines)
            from PySide6.QtGui import QFontMetricsF

            fm = QFontMetricsF(f)
            for i, line in enumerate(lines):
                w = fm.horizontalAdvance(line)
                path = QPainterPath()
                path.addText((vw - w) / 2, y0 + lh * (i + 1), f, line)
                painter.setPen(QPen(QColor("#000000"), max(2, f.pixelSize() / 9), Qt.SolidLine, Qt.RoundCap,
                                    Qt.RoundJoin))
                painter.drawPath(path)
                painter.fillPath(path, QColor("#ffffff"))
        if self.notice:
            f = QFont(self.font())
            f.setPixelSize(13)
            painter.setFont(f)
            painter.fillRect(QRectF(6, 6, min(vw - 12, 9 * len(self.notice) + 20), 22), QColor(0, 0, 0, 160))
            painter.setPen(QColor("#ffd166"))
            painter.drawText(QRectF(14, 6, vw, 22), Qt.AlignVCenter, self.notice)
        painter.restore()


class PreviewPlayer(QWidget):
    position_changed = Signal(float)  # 초
    duration_changed = Signal(float)
    playing_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.scene = QGraphicsScene(self)
        self.video_item = QGraphicsVideoItem()
        self.scene.addItem(self.video_item)
        self.player.setVideoOutput(self.video_item)
        self.view = _View(self.scene)
        self.media_path: Optional[Path] = None
        self.subtitle_fn = None  # t -> str
        self._base_volume = 1.0
        self._raw_ms, self._raw_at = -1, 0.0

        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.setToolTip("재생/일시정지 (Space)")
        self.btn_back = QPushButton("⏮")
        self.btn_back.setToolTip("처음으로")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.lbl_time = QLabel("00:00.00 / 00:00.00")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(100)
        self.vol.setMaximumWidth(90)
        self.vol.setToolTip("미리보기 원본 소리 크기")

        bar = QHBoxLayout()
        bar.addWidget(self.btn_back)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lbl_time)
        bar.addWidget(QLabel("🔊"))
        bar.addWidget(self.vol)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)

        self.btn_play.clicked.connect(self.toggle)
        self.btn_back.clicked.connect(lambda: self.seek(0))
        self.slider.sliderMoved.connect(lambda v: self.seek(v / 1000))
        self.vol.valueChanged.connect(lambda v: self._apply_volume())
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.video_item.nativeSizeChanged.connect(lambda _: self._fit())
        # positionChanged 는 간격이 크므로 타이머로 부드럽게 갱신
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._last_pos = -1.0

    # --- 미디어 ---
    def load(self, path: Optional[Path], keep_position: bool = True) -> None:
        pos = self.position() if keep_position else 0.0
        self.player.stop()
        self.media_path = Path(path) if path else None
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        if keep_position and pos > 0:
            QTimer.singleShot(150, lambda: self.seek(pos))

    def set_notice(self, text: str) -> None:
        self.view.notice = text
        self.view.viewport().update()

    def position(self) -> float:
        """재생 중에는 positionChanged 간격 사이를 보간해서 부드러운 카운트다운을 만듭니다."""
        raw = self.player.position()
        now = time.perf_counter()
        if raw != self._raw_ms:
            self._raw_ms, self._raw_at = raw, now
        if self.is_playing():
            return raw / 1000.0 + min(0.25, now - self._raw_at) * self.player.playbackRate()
        return raw / 1000.0

    def duration(self) -> float:
        return self.player.duration() / 1000.0

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def play(self):
        if self.media_path:
            self.player.play()

    def pause(self):
        self.player.pause()

    def toggle(self):
        self.pause() if self.is_playing() else self.play()

    def seek(self, t: float):
        self.player.setPosition(int(max(0.0, t) * 1000))
        self._tick(force=True)

    def set_volume_factor(self, factor: float):
        """녹음 중 원본 소리 줄이기용 (슬라이더 값에 곱해짐)."""
        self._base_volume = max(0.0, min(1.0, factor))
        self._apply_volume()

    def _apply_volume(self):
        self.audio.setVolume(self.vol.value() / 100.0 * self._base_volume)

    # --- 내부 ---
    def _fit(self):
        size = self.video_item.nativeSize()
        if size.isEmpty():
            size = QSizeF(1920, 1080)
        self.video_item.setSize(size)
        self.scene.setSceneRect(QRectF(0, 0, size.width(), size.height()))
        self.view.fitInView(self.video_item, Qt.KeepAspectRatio)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _on_duration(self, ms):
        self.slider.setRange(0, int(ms))
        self.duration_changed.emit(ms / 1000.0)

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setText("⏸ 일시정지" if playing else "▶ 재생")
        self.playing_changed.emit(playing)

    def _tick(self, force: bool = False):
        t = self.position()
        if not force and abs(t - self._last_pos) < 1e-4:
            return
        self._last_pos = t
        if not self.slider.isSliderDown():
            self.slider.setValue(int(t * 1000))
        self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(self.duration())}")
        text = self.subtitle_fn(t) if self.subtitle_fn else ""
        if text != self.view.text:
            self.view.text = text
            self.view.viewport().update()
        self.position_changed.emit(t)

    def refresh_overlay(self):
        self._tick(force=True)
