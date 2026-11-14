"""녹음할 때 '소리가 들어오는지' 한눈에 보이게: 레벨 미터 · 흘러가는 파형 · 깜빡이는 ● REC · 무음 경고."""
from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..dubbing.levels import FLOOR_DB, RED_DB, SILENCE_WARNING, YELLOW_DB, level_zone, to_db

ZONE_COLORS = {"green": QColor("#3fbf5f"), "yellow": QColor("#e6c229"), "red": QColor("#e5484d")}
CLIP_HOLD_S = 2.0


def _frac(db: float) -> float:
    return max(0.0, min(1.0, (db - FLOOR_DB) / -FLOOR_DB))


def fmt_elapsed(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s // 60):02d}:{s % 60:04.1f}"


class LevelMeter(QWidget):
    """가로 막대: 초록(작음~적당) · 노랑(큼) · 빨강(너무 큼) + 최고점 선 + 오른쪽 CLIP 표시."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(20)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.peak_db = -90.0
        self.hold_db = -90.0
        self.active = False
        self._clip_count = 0
        self._clip_until = 0.0
        self.setToolTip("마이크 입력 크기 — 말할 때 초록~노랑이 좋아요. 빨강·CLIP 이면 마이크를 멀리 하거나 입력 볼륨을 낮추세요")

    @property
    def clipping(self) -> bool:
        return time.monotonic() < self._clip_until

    @property
    def zone(self) -> str:
        return level_zone(self.peak_db)

    def set_levels(self, peak_db: float, hold_db: float, clip_count: int, active: bool = True) -> None:
        self.peak_db, self.hold_db, self.active = peak_db, hold_db, active
        if clip_count > self._clip_count:
            self._clip_until = time.monotonic() + CLIP_HOLD_S
        self._clip_count = clip_count
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        clip_w = 40
        bar = QRectF(0, 0, w - clip_w - 4, h)
        p.fillRect(bar, QColor("#17181c"))
        for lo, hi, z in ((FLOOR_DB, YELLOW_DB, "green"), (YELLOW_DB, RED_DB, "yellow"), (RED_DB, 0.0, "red")):
            c = QColor(ZONE_COLORS[z]); c.setAlpha(45)  # 흐린 바탕 = 구간 안내
            p.fillRect(QRectF(bar.width() * _frac(lo), 0, bar.width() * (_frac(hi) - _frac(lo)), h), c)
        if self.active:
            x_end = bar.width() * _frac(self.peak_db)
            for lo, hi, z in ((FLOOR_DB, YELLOW_DB, "green"), (YELLOW_DB, RED_DB, "yellow"), (RED_DB, 0.0, "red")):
                x0, x1 = bar.width() * _frac(lo), min(x_end, bar.width() * _frac(hi))
                if x1 > x0:
                    p.fillRect(QRectF(x0, 2, x1 - x0, h - 4), ZONE_COLORS[z])
            xh = bar.width() * _frac(self.hold_db)
            if self.hold_db > FLOOR_DB:
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.drawLine(QPointF(xh, 1), QPointF(xh, h - 1))
        p.setPen(QColor("#ffffff") if self.active else QColor("#8a8f98"))
        f = QFont(self.font()); f.setPointSize(8); p.setFont(f)
        txt = f"{max(self.peak_db, -60):.0f} dB" if self.active else "꺼짐"
        p.drawText(bar.adjusted(4, 0, -4, 0), Qt.AlignRight | Qt.AlignVCenter, txt)
        cr = QRectF(w - clip_w, 0, clip_w, h)
        on = self.clipping
        p.fillRect(cr, QColor("#e5484d") if on else QColor("#2a2c33"))
        p.setPen(QColor("#ffffff") if on else QColor("#6b707a"))
        f.setBold(True); p.setFont(f)
        p.drawText(cr, Qt.AlignCenter, "CLIP")


class LiveWave(QWidget):
    """오른쪽으로 새 소리가 들어와 왼쪽으로 흘러가는 파형(10ms 당 한 줄)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.values: deque = deque(maxlen=1200)
        self.recording = False
        self.active = False

    def push(self, vals) -> None:
        self.values.extend(vals)
        if vals:
            self.update()

    def clear(self) -> None:
        self.values.clear()
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#121317"))
        mid = h / 2
        p.setPen(QPen(QColor("#33363e"), 1))
        p.drawLine(QPointF(0, mid), QPointF(w, mid))
        if not self.active:
            p.setPen(QColor("#8a8f98"))
            p.drawText(self.rect(), Qt.AlignCenter, "🎧 마이크 테스트를 켜면 여기에 소리 모양이 보여요")
            return
        col = QColor("#ff5a5f") if self.recording else QColor("#4cc38a")
        p.setPen(QPen(col, 1))
        vals = list(self.values)[-int(w):]
        x0 = w - len(vals)
        for i, v in enumerate(vals):
            a = _frac(to_db(v)) * (mid - 2)
            if a < 0.5:
                continue
            x = x0 + i + 0.5
            p.drawLine(QPointF(x, mid - a), QPointF(x, mid + a))
        if not vals or max(vals[-100:] or [0]) < 0.003:
            p.setPen(QColor("#8a8f98"))
            p.drawText(self.rect().adjusted(6, 0, -6, 0), Qt.AlignLeft | Qt.AlignTop, "소리를 내 보세요")


class RecIndicator(QLabel):
    """녹음 중: 깜빡이는 빨간 ● REC + 녹음 시간. 아니면 대기/소리 확인 중."""

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFont(self.font()); f.setBold(True); f.setPointSize(11)
        self.setFont(f)
        self.setMinimumWidth(130)
        self.setStyleSheet("QLabel{background:#17181c;border-radius:4px;padding:1px 6px}")
        self.blink_on = False
        self.set_state(False, 0.0, False)

    def set_state(self, recording: bool, elapsed: float, monitoring: bool) -> None:
        if recording:
            self.blink_on = int(elapsed * 2) % 2 == 0  # 0.5초마다 깜빡
            dot = "#ff3b30" if self.blink_on else "#5a1d1a"
            self.setText(f"<span style='color:{dot}'>●</span> <span style='color:#ff6b62'>REC</span> "
                         f"<span style='color:#ffffff'>{fmt_elapsed(elapsed)}</span>")
        elif monitoring:
            self.setText("<span style='color:#4cc38a'>🎧 소리 확인 중</span>")
        else:
            self.setText("<span style='color:#8a8f98'>○ 녹음 대기</span>")


class MicStrip(QWidget):
    """두 녹음 탭에 똑같이 들어가는 '입력 소리' 줄."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rec = RecIndicator()
        self.meter = LevelMeter()
        self.wave = LiveWave()
        self.dev_label = QLabel("")
        self.dev_label.setStyleSheet("color:#9aa0a6")
        self.warn = QLabel("⚠ " + SILENCE_WARNING)
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("QLabel{background:#5c3b00;color:#ffd27a;font-weight:bold;padding:4px;border-radius:3px}")
        self.warn.hide()
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.rec); top.addWidget(self.meter, 1)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(3)
        lay.addLayout(top)
        lay.addWidget(self.wave)
        lay.addWidget(self.dev_label)
        lay.addWidget(self.warn)

    def update_levels(self, snap: dict, wave: list, *, is_open: bool, recording: bool, elapsed: float,
                      warn: bool, device: str = "") -> None:
        self.meter.set_levels(snap["peak_db"], snap["hold_db"], snap["clip_count"], active=is_open)
        self.wave.active = is_open
        self.wave.recording = recording
        self.wave.push(wave)
        if not is_open:
            self.wave.update()
        self.rec.set_state(recording, elapsed, is_open)
        self.dev_label.setText(f"입력 장치: {device}" if device else "")
        self.warn.setVisible(warn)
