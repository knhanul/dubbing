"""미리보기 플레이어: 영상 위에 현재 자막을 실시간으로 겹쳐 보여줍니다.

자막은 렌더하지 않고 프로젝트 값으로 바로 그리므로, 자막을 미세 조정하면 즉시 반영됩니다.

미리보기 영상이 없을 때는 **타임라인 시계 모드**로 동작합니다. 자체 시계로 재생/정지/이동하고
(길이 = 타임라인 전체 길이), 화면에는 현재 클립의 그림(썸네일)을 보여 줍니다. 재생 위치, 시간 표시,
타임라인 재생선, 자막 오버레이, 더빙 패널은 모두 이 한 시계(position())를 따릅니다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider,
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
        self.progress_text = ""
        self.progress = None  # 0~1 또는 None
        self.empty_hint = ""
        # 자막 스타일·효과 (실시간 근사)
        from ..schema import SubtitleStyle
        self.style = SubtitleStyle()  # 전체 모양
        self.cue_style = None  # 지금 자막만 다르게 한 칸(dict) | None
        self.out_h = 1080
        self.cue_elapsed = 99.0
        self.cue_remaining = 99.0
        self.cue_duration = 99.0
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#000000"))
        self.setFrameShape(QGraphicsView.NoFrame)

    def look(self):
        """지금 그리는 자막의 실제 모양(전체 모양 + 이 자막만 다르게 한 칸)."""
        from ..subtitle_style import effective_style
        return effective_style(self.style, self.cue_style)

    def drawForeground(self, painter: QPainter, rect):
        painter.save()
        painter.resetTransform()
        vw, vh = self.viewport().width(), self.viewport().height()
        if self.text:
            from ..subtitle_style import appear_state
            from .sub_paint import paint_subtitle

            frame = self.mapFromScene(self.sceneRect()).boundingRect()
            frame = QRectF(frame).intersected(QRectF(0, 0, vw, vh)) or QRectF(0, 0, vw, vh)
            look = self.look()
            st = appear_state(look, self.text, self.cue_elapsed, self.cue_remaining, self.cue_duration)
            paint_subtitle(painter, frame, self.text, look, self.out_h, st)
        if self.empty_hint:
            f = QFont(self.font())
            f.setPixelSize(max(14, int(vh * 0.04)))
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor("#c8ccd4"))
            painter.drawText(QRectF(20, 0, vw - 40, vh), Qt.AlignCenter | Qt.TextWordWrap, "⬇\n" + self.empty_hint)
        if self.progress_text:
            f = QFont(self.font())
            f.setPixelSize(15)
            f.setBold(True)
            painter.setFont(f)
            bw, bh = min(vw - 40, 340), 54
            box = QRectF((vw - bw) / 2, vh * 0.42, bw, bh)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(box, 8, 8)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(box.adjusted(0, 6, 0, -22), Qt.AlignCenter, self.progress_text)
            if self.progress is not None:
                track = QRectF(box.left() + 16, box.bottom() - 16, bw - 32, 6)
                painter.setBrush(QColor("#44474f"))
                painter.drawRoundedRect(track, 3, 3)
                painter.setBrush(QColor("#4dd2ff"))
                painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * self.progress, 6), 3, 3)
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
        self.placeholder = QGraphicsPixmapItem()
        self.placeholder.setTransformationMode(Qt.SmoothTransformation)
        self.scene.addItem(self.placeholder)
        self.placeholder_fn = None  # t -> (key, QPixmap | None)  미리보기가 없을 때 보여 줄 그림
        self._placeholder_key = None
        self.frame_size = QSizeF(1920, 1080)  # 미리보기가 없을 때 화면 비율(출력 크기)
        self.before_play = None  # 미리보기 없이 재생하기 전에 물어볼 함수 () -> bool
        # 타임라인 시계 (미리보기 영상이 없을 때)
        self.timeline_duration = 0.0
        self._v_pos = 0.0
        self._v_playing = False
        self._v_t0 = 0.0
        self.player.setVideoOutput(self.video_item)
        self.view = _View(self.scene)
        self.media_path: Optional[Path] = None
        self.subtitle_fn = None  # t -> str
        self.cue_fn = None  # t -> (text, 나타난 뒤 초, 사라지기까지 초, 길이[, 이 자막만의 모양]) | None  (있으면 subtitle_fn 대신)
        self._base_volume = 1.0
        self._track_gain = 1.0  # 🔇/S (클립 원본 소리 트랙)
        self._raw_ms, self._raw_at = -1, 0.0

        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.setToolTip("재생/일시정지 (Space)")
        self.btn_refresh = QPushButton("🔄 미리보기 새로 만들기")
        self.btn_refresh.setToolTip("지금 바로 360p 미리보기를 다시 만듭니다 (F5). 자동 미리보기가 켜져 있으면 보통 누를 필요가 없습니다.")
        _fm = self.btn_refresh.fontMetrics()  # 글이 바뀌어도 버튼 크기 고정(배치가 흔들리지 않게)
        self.btn_refresh.setFixedWidth(max(_fm.horizontalAdvance(t) for t in ("🔄 미리보기 새로 만들기", "⏳ 미리보기 만드는 중…")) + 28)
        self.btn_back = QPushButton("⏮")
        self.btn_back.setToolTip("처음으로")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.lbl_time = QLabel("00:00.00 / 00:00.00")
        self.lbl_time.setFixedWidth(self.lbl_time.fontMetrics().horizontalAdvance("000:00.00 / 000:00.00") + 12)
        self.lbl_time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(100)
        self.vol.setMaximumWidth(90)
        self.vol.setToolTip("미리보기 원본 소리 크기")

        bar = QHBoxLayout()
        bar.addWidget(self.btn_back)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.btn_refresh)
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
    def has_media(self) -> bool:
        return self.media_path is not None

    def load(self, path: Optional[Path], keep_position: bool = True, resume: bool = False) -> None:
        """resume=True 면 재생 중이었을 때 새 영상에서 같은 위치부터 이어서 재생합니다."""
        pos = self.position() if keep_position else 0.0
        resume_play = resume and self.is_playing()
        was_playing = self._v_playing
        self._v_stop()
        self.player.stop()
        self.media_path = Path(path) if path else None
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        self.video_item.setVisible(self.has_media())
        self.placeholder.setVisible(not self.has_media())
        self._placeholder_key = None
        if not self.has_media():
            self._v_pos = min(pos, self.timeline_duration) if self.timeline_duration else pos
            self.slider.setRange(0, int(self.timeline_duration * 1000))
            self._fit()
            if was_playing:
                self._on_state(None)
            self._tick(force=True)
        elif (keep_position and pos > 0) or resume_play:
            def restore():
                self.seek(pos)
                if resume_play:
                    self.player.play()
            QTimer.singleShot(150, restore)

    def set_timeline_duration(self, d: float) -> None:
        self.timeline_duration = max(0.0, float(d))
        if not self.has_media():
            self.slider.setRange(0, int(self.timeline_duration * 1000))
            self.duration_changed.emit(self.timeline_duration)
            self._tick(force=True)

    def set_frame_size(self, w: int, h: int) -> None:
        self.frame_size = QSizeF(max(1, w), max(1, h))
        if not self.has_media():
            self._placeholder_key = None
            self._fit()
            self._tick(force=True)

    # --- 타임라인 시계 ---
    def _v_now(self) -> float:
        if not self._v_playing:
            return self._v_pos
        t = self._v_pos + (time.perf_counter() - self._v_t0)
        return min(t, self.timeline_duration) if self.timeline_duration else t

    def _v_stop(self):
        if self._v_playing:
            self._v_pos = self._v_now()
            self._v_playing = False

    def set_subtitle_style(self, style, out_h: int) -> None:
        """자막 스타일이 바뀌면 바로 다시 그림 (미리보기 렌더 없이)."""
        self.view.style = style
        self.view.out_h = out_h
        self.view.viewport().update()

    def set_progress(self, text: str, fraction) -> None:
        """미리보기 만드는 중 표시 (영상 가운데)."""
        self.view.progress_text = text
        self.view.progress = fraction
        self.btn_refresh.setText("⏳ 미리보기 만드는 중…" if text else "🔄 미리보기 새로 만들기")
        self.view.viewport().update()

    def set_empty_hint(self, text: str) -> None:
        if text != self.view.empty_hint:
            self.view.empty_hint = text
            self.view.viewport().update()

    def set_notice(self, text: str) -> None:
        self.view.notice = text
        self.view.viewport().update()

    def position(self) -> float:
        """재생 중에는 positionChanged 간격 사이를 보간해서 부드러운 카운트다운을 만듭니다."""
        if not self.has_media():
            return self._v_now()
        raw = self.player.position()
        now = time.perf_counter()
        if raw != self._raw_ms:
            self._raw_ms, self._raw_at = raw, now
        if self.is_playing():
            return raw / 1000.0 + min(0.25, now - self._raw_at) * self.player.playbackRate()
        return raw / 1000.0

    def duration(self) -> float:
        if not self.has_media():
            return self.timeline_duration
        return self.player.duration() / 1000.0

    def is_playing(self) -> bool:
        if not self.has_media():
            return self._v_playing
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def play(self):
        if self.has_media():
            self.player.play()
            return
        if self._v_playing:
            return
        if self.before_play is not None and not self.before_play():
            return
        if self.has_media():  # before_play 가 미리보기를 만들었을 수 있음
            self.player.play()
            return
        if self.timeline_duration and self._v_pos >= self.timeline_duration - 0.01:
            self._v_pos = 0.0
        self._v_t0 = time.perf_counter()
        self._v_playing = True
        self._on_state(None)

    def pause(self):
        if self.has_media():
            self.player.pause()
        elif self._v_playing:
            self._v_stop()
            self._on_state(None)
            self._tick(force=True)

    def toggle(self):
        self.pause() if self.is_playing() else self.play()

    def seek(self, t: float):
        t = max(0.0, t)
        if self.has_media():
            self.player.setPosition(int(t * 1000))
        else:
            self._v_pos = min(t, self.timeline_duration) if self.timeline_duration else t
            self._v_t0 = time.perf_counter()
        self._tick(force=True)

    def set_volume_factor(self, factor: float):
        """녹음 중 원본 소리 줄이기용 (슬라이더 값에 곱해짐)."""
        self._base_volume = max(0.0, min(1.0, factor))
        self._apply_volume()

    def set_track_gain(self, g: float):
        self._track_gain = max(0.0, min(1.0, g))
        self._apply_volume()

    def _apply_volume(self):
        self.audio.setVolume(self.vol.value() / 100.0 * self._base_volume * self._track_gain)
        cb = getattr(self, "on_volume", None)  # 메인 창: 따로 트는 배경음악도 같이
        if cb:
            cb()

    # --- 내부 ---
    def _fit(self):
        if self.has_media():
            size = self.video_item.nativeSize()
            if size.isEmpty():
                size = self.frame_size
            self.video_item.setSize(size)
        else:
            size = self.frame_size
        rect = QRectF(0, 0, size.width(), size.height())
        self.scene.setSceneRect(rect)
        self.view.fitInView(rect, Qt.KeepAspectRatio)

    def _update_placeholder(self, t: float):
        if self.has_media() or self.placeholder_fn is None:
            return
        key, pix = self.placeholder_fn(t)
        if key == self._placeholder_key:
            return
        self._placeholder_key = key
        if pix is None or pix.isNull():
            self.placeholder.setPixmap(QPixmap())
            return
        fw, fh = self.frame_size.width(), self.frame_size.height()
        scaled = pix.scaled(int(fw), int(fh), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.placeholder.setPixmap(scaled)
        self.placeholder.setPos((fw - scaled.width()) / 2, (fh - scaled.height()) / 2)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _on_duration(self, ms):
        if not self.has_media():
            return
        self.slider.setRange(0, int(ms))
        self.duration_changed.emit(ms / 1000.0)

    def _on_state(self, state):
        playing = self.is_playing() if state is None else state == QMediaPlayer.PlayingState
        self.btn_play.setText("⏸ 일시정지" if playing else "▶ 재생")
        self.playing_changed.emit(playing)

    def _tick(self, force: bool = False):
        t = self.position()
        if not self.has_media():
            if self._v_playing and self.timeline_duration and t >= self.timeline_duration:
                self._v_stop()
                self._on_state(None)
            self._update_placeholder(t)
        if not force and abs(t - self._last_pos) < 1e-4:
            return
        self._last_pos = t
        if not self.slider.isSliderDown():
            self.slider.setValue(int(t * 1000))
        self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(self.duration())}")
        v = self.view
        if self.cue_fn is not None:
            info = self.cue_fn(t)
            text, el, rem, dur = info[:4] if info else ("", 99.0, 99.0, 99.0)
            cue_style = info[4] if info and len(info) > 4 else None
            restyled = cue_style != v.cue_style
            v.cue_style = cue_style
            lk = v.look()
            animating = bool(text) and lk.appear != "none" and (el < max(lk.appear_duration, 0.05) + 0.1
                                                                or (lk.appear == "typing" and el < dur)
                                                                or (lk.appear == "fade" and rem < 0.2))
            changed = text != v.text or restyled or animating or (v.cue_elapsed < 1.5 and el >= 1.5)
            v.cue_elapsed, v.cue_remaining, v.cue_duration = el, rem, dur
        else:
            text = self.subtitle_fn(t) if self.subtitle_fn else ""
            changed = text != v.text
        if changed:
            v.text = text
            v.viewport().update()
        self.position_changed.emit(t)

    def refresh_overlay(self):
        self._tick(force=True)
