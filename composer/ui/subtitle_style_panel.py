"""'🅰 자막 스타일' 탭: 글꼴·색·배경 상자·위치·나타나기 효과.

값을 바꾸면 곧바로 프로젝트 JSON(subtitle.style)에 기록되고 미리보기 화면 위 자막에 즉시 반영됩니다
(미리보기 영상을 다시 렌더하지 않음). 영상 내보내기에서는 같은 설정으로 ASS 자막을 만들어 영상에 새깁니다.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Callable, Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QLinearGradient, QPainter
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ..schema import SubtitleStyle
from ..subtitle_style import APPEAR_LABELS, POSITION_LABELS, appear_state
from .model import ProjectModel
from .sub_paint import paint_subtitle

SAMPLE_TEXT = "안녕하세요, 자막 미리보기입니다\n가나다라 ABC 123"


class StyleSample(QWidget):
    """현재 스타일로 견본 자막을 그리고, 나타나기 효과를 반복해서 보여 줍니다."""

    LOOP = 2.4  # 초

    def __init__(self, parent=None):
        super().__init__(parent)
        self.style_ = SubtitleStyle()
        self.out_w, self.out_h = 1920, 1080
        self.text = SAMPLE_TEXT
        self.setMinimumHeight(150)
        self._t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def set_style(self, style: SubtitleStyle, out_w: int, out_h: int):
        sig = (tuple(asdict(style).items()), out_w, out_h)
        self.style_ = style
        self.out_w, self.out_h = max(16, out_w), max(16, out_h)
        if sig != getattr(self, "_sig", None):  # 스타일이 바뀌었을 때만 효과를 처음부터
            self._sig = sig
            self.replay()

    def replay(self):
        self._t0 = time.monotonic()
        self.update()

    def heightForWidth(self, w):  # noqa: N802
        return int(w * self.out_h / self.out_w)

    def frame_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        ar = self.out_w / self.out_h
        fw, fh = (w, w / ar) if w / h < ar else (h * ar, h)
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def elapsed(self) -> float:
        return (time.monotonic() - self._t0) % self.LOOP

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111"))
        fr = self.frame_rect()
        g = QLinearGradient(fr.topLeft(), fr.bottomRight())
        g.setColorAt(0, QColor("#3a6186")); g.setColorAt(0.5, QColor("#89a2b5")); g.setColorAt(1, QColor("#e0c3a0"))
        p.fillRect(fr, g)
        el = self.elapsed()
        if el < self.LOOP - 0.25:  # 끝에 잠깐 비워서 효과가 다시 시작되는 게 보이게
            dur = self.LOOP - 0.25
            st = appear_state(self.style_, self.text, el, dur - el, dur)
            paint_subtitle(p, fr, self.text, self.style_, self.out_h, st)
        p.end()


class ColorButton(QPushButton):
    def set_color(self, color: str):
        self.color = color
        self.setText(color.upper())
        light = QColor(color).lightness() > 128
        self.setStyleSheet(f"background:{color}; color:{'#000' if light else '#fff'}; padding:3px 8px;")


class SubtitleStylePanel(QWidget):
    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self._loading = False
        # 테스트에서 바꿔 끼울 수 있는 색 고르기 함수: (현재색, 제목) -> "#RRGGBB" | None
        self.pick_color: Callable[[str, str], Optional[str]] = self._pick_color_dialog

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.sample = StyleSample()
        outer.addWidget(self.sample)
        row = QHBoxLayout()
        self.btn_replay = QPushButton("▶ 효과 다시 보기")
        self.btn_reset = QPushButton("기본값으로")
        row.addWidget(self.btn_replay); row.addStretch(1); row.addWidget(self.btn_reset)
        outer.addLayout(row)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget(); lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- 글꼴 ---
        g_font = QGroupBox("글꼴")
        f = QFormLayout(g_font)
        self.font_combo = QFontComboBox()
        self.korean_only = QCheckBox("한글 글꼴만 보기")
        self.korean_only.setChecked(True)
        self.font_preview = QLabel("가나다라마바사 안녕하세요 ABC 123")
        self.font_preview.setMinimumHeight(34)
        self.font_preview.setStyleSheet("background:#20232a; color:#fff; padding:4px 8px; border-radius:4px;")
        self.size = QSpinBox(); self.size.setRange(8, 300); self.size.setSuffix(" px")
        self.size.setToolTip("출력 영상 높이 기준 픽셀 (1080p 에서 48px 정도가 보통)")
        self.bold = QCheckBox("굵게")
        f.addRow("글꼴", self.font_combo)
        f.addRow("", self.korean_only)
        f.addRow("견본", self.font_preview)
        r = QHBoxLayout(); r.addWidget(self.size); r.addWidget(self.bold); r.addStretch(1)
        f.addRow("크기", r)
        lay.addWidget(g_font)

        # --- 색 ---
        g_col = QGroupBox("색")
        f = QFormLayout(g_col)
        self.color = ColorButton()
        self.outline_color = ColorButton()
        self.outline = QDoubleSpinBox(); self.outline.setRange(0, 20); self.outline.setSingleStep(0.5); self.outline.setSuffix(" px")
        self.box = QCheckBox("글자 뒤에 배경 상자")
        self.box_color = ColorButton()
        self.box_opacity = QSlider(Qt.Horizontal); self.box_opacity.setRange(0, 100)
        self.box_opacity_lbl = QLabel()
        self.box_padding = QSpinBox(); self.box_padding.setRange(0, 200); self.box_padding.setSuffix(" px")
        f.addRow("글자", self.color)
        r = QHBoxLayout(); r.addWidget(self.outline_color); r.addWidget(QLabel("두께")); r.addWidget(self.outline)
        f.addRow("외곽선", r)
        f.addRow("", self.box)
        f.addRow("상자 색", self.box_color)
        r = QHBoxLayout(); r.addWidget(self.box_opacity, 1); r.addWidget(self.box_opacity_lbl)
        f.addRow("상자 불투명도", r)
        f.addRow("상자 여백", self.box_padding)
        lay.addWidget(g_col)

        # --- 위치 ---
        g_pos = QGroupBox("위치")
        f = QFormLayout(g_pos)
        self.position = QComboBox()
        for k, v in POSITION_LABELS.items():
            self.position.addItem(v, k)
        self.margin = QSpinBox(); self.margin.setRange(0, 2000); self.margin.setSuffix(" px")
        self.margin.setToolTip("아래/위에 둘 때 화면 가장자리에서 떨어진 거리 (출력 영상 기준 픽셀)")
        f.addRow("자리", self.position)
        f.addRow("가장자리 여백", self.margin)
        lay.addWidget(g_pos)

        # --- 나타나기 ---
        g_ap = QGroupBox("나타나기")
        f = QFormLayout(g_ap)
        self.appear = QComboBox()
        for k, v in APPEAR_LABELS.items():
            self.appear.addItem(v, k)
        self.appear_duration = QDoubleSpinBox(); self.appear_duration.setRange(0.05, 5); self.appear_duration.setSingleStep(0.05)
        self.appear_duration.setDecimals(2); self.appear_duration.setSuffix(" 초")
        self.appear_duration.setToolTip("효과 시간. 타이핑은 한 자막을 다 치는 시간(글자가 많으면 글자당 최소 0.04초)")
        f.addRow("효과", self.appear)
        f.addRow("효과 시간", self.appear_duration)
        lay.addWidget(g_ap)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#9aa0a6;")
        lay.addWidget(self.note)
        lay.addStretch(1)

        self._connect()
        self._apply_writing_system()
        model.changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    @property
    def st(self) -> SubtitleStyle:
        return self.model.project.subtitle.style

    def _set(self, key: Optional[str], attr: str, value):
        if self._loading or getattr(self.st, attr) == value:
            return
        self.model.edit(lambda p: setattr(p.subtitle.style, attr, value), merge_key=key)

    def _connect(self):
        self.font_combo.currentFontChanged.connect(lambda f: self._set("st_font", "font", f.family()))
        self.korean_only.toggled.connect(lambda _: self._apply_writing_system())
        self.size.valueChanged.connect(lambda v: self._set("st_size", "size", int(v)))
        self.bold.toggled.connect(lambda v: self._set(None, "bold", bool(v)))
        self.outline.valueChanged.connect(lambda v: self._set("st_ol", "outline", float(v)))
        self.box.toggled.connect(lambda v: self._set(None, "box", bool(v)))
        self.box_opacity.valueChanged.connect(lambda v: self._set("st_bo", "box_opacity", round(v / 100, 2)))
        self.box_padding.valueChanged.connect(lambda v: self._set("st_bp", "box_padding", int(v)))
        self.position.currentIndexChanged.connect(lambda _: self._set(None, "position", self.position.currentData()))
        self.margin.valueChanged.connect(lambda v: self._set("st_mv", "margin_v", int(v)))
        self.appear.currentIndexChanged.connect(lambda _: self._set(None, "appear", self.appear.currentData()))
        self.appear_duration.valueChanged.connect(lambda v: self._set("st_ad", "appear_duration", round(float(v), 3)))
        for btn, attr, title in ((self.color, "color", "글자 색"), (self.outline_color, "outline_color", "외곽선 색"),
                                 (self.box_color, "box_color", "배경 상자 색")):
            btn.clicked.connect(lambda _=False, a=attr, t=title: self._choose(a, t))
        self.btn_replay.clicked.connect(self.sample.replay)
        self.btn_reset.clicked.connect(self.reset_defaults)

    def _apply_writing_system(self):
        cur = self.font_combo.currentFont()
        self._loading = True
        self.font_combo.setWritingSystem(QFontDatabase.Korean if self.korean_only.isChecked() else QFontDatabase.Any)
        self.font_combo.setCurrentFont(cur)
        self._loading = False

    def _pick_color_dialog(self, current: str, title: str) -> Optional[str]:
        c = QColorDialog.getColor(QColor(current), self, title)
        return c.name().upper() if c.isValid() else None

    def _choose(self, attr: str, title: str):
        c = self.pick_color(getattr(self.st, attr), title)
        if c:
            self._set(None, attr, c.upper())

    def reset_defaults(self):
        d = SubtitleStyle()
        self.model.edit(lambda p: setattr(p.subtitle, "style", SubtitleStyle(**asdict(d))))

    # ------------------------------------------------------------------
    def refresh(self):
        st = self.st
        p = self.model.project
        self._loading = True
        try:
            f = QFont(st.font)
            if self.font_combo.currentFont().family() != st.font:
                if self.korean_only.isChecked() and self.font_combo.findText(st.font) < 0:
                    self.korean_only.setChecked(False)  # 목록에 없는 글꼴이면 전체 목록으로
                    self.font_combo.setWritingSystem(QFontDatabase.Any)
                self.font_combo.setCurrentFont(f)
            pf = QFont(st.font); pf.setPixelSize(20); pf.setBold(st.bold)
            self.font_preview.setFont(pf)
            self.size.setValue(st.size)
            self.bold.setChecked(st.bold)
            self.color.set_color(st.color)
            self.outline_color.set_color(st.outline_color)
            self.outline.setValue(st.outline)
            self.box.setChecked(st.box)
            self.box_color.set_color(st.box_color)
            self.box_opacity.setValue(int(round(st.box_opacity * 100)))
            self.box_opacity_lbl.setText(f"{int(round(st.box_opacity * 100))}%")
            self.box_padding.setValue(st.box_padding)
            for w in (self.box_color, self.box_opacity, self.box_padding):
                w.setEnabled(st.box)
            self.position.setCurrentIndex(max(0, self.position.findData(st.position)))
            self.margin.setValue(st.margin_v)
            self.margin.setEnabled(st.position != "center")
            self.appear.setCurrentIndex(max(0, self.appear.findData(st.appear)))
            self.appear_duration.setValue(st.appear_duration)
            self.appear_duration.setEnabled(st.appear != "none")
        finally:
            self._loading = False
        self.sample.set_style(st, p.output.width, p.output.height)
        if not p.subtitle.burn_in:
            self.note.setText("⚠ 지금은 '소프트 자막'(켜고 끄는 자막)으로 내보내도록 되어 있어, 최종 영상에서는 이 스타일·효과가 "
                              "적용되지 않습니다(재생 프로그램이 정함). ⚙ 출력 설정 ▸ 자막 ▸ '영상에 자막 입히기'를 켜세요.")
        else:
            self.note.setText("바꾸면 미리보기 화면의 자막에 바로 반영됩니다(다시 렌더하지 않음). 영상을 내보낼 때 같은 모양으로 "
                              "영상에 새겨집니다. 미리보기의 효과는 비슷하게 흉내 낸 것이라 실제와 약간 다를 수 있습니다.")
