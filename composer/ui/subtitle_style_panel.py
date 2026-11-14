"""'🅰 자막 스타일' 탭: 글꼴·색·배경 상자·위치·나타나기 효과.

값을 바꾸면 곧바로 프로젝트 JSON(subtitle.style)에 기록되고 미리보기 화면 위 자막에 즉시 반영됩니다
(미리보기 영상을 다시 렌더하지 않음). 영상 내보내기에서는 같은 설정으로 ASS 자막을 만들어 영상에 새깁니다.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Callable, Optional

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFontComboBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListView, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from ..schema import SubtitleStyle
from ..subtitle_presets import Preset, PresetError, PresetStore, preset_from_style, style_from_preset
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


THUMB = QSize(128, 72)
NAME_ROLE = Qt.UserRole + 1


def preset_thumbnail(style: SubtitleStyle, out_w: int, out_h: int, text: str = "자막 예시") -> QPixmap:
    """스타일 모음 썸네일: 배경 그라데이션 위에 효과가 끝난 모양으로 그림."""
    pm = QPixmap(THUMB)
    pm.fill(QColor("#111"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    w, h = THUMB.width(), THUMB.height()
    ar = out_w / max(1, out_h)
    fw, fh = (w, w / ar) if w / h < ar else (h * ar, h)
    fr = QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)
    g = QLinearGradient(fr.topLeft(), fr.bottomRight())
    g.setColorAt(0, QColor("#3a6186")); g.setColorAt(0.5, QColor("#89a2b5")); g.setColorAt(1, QColor("#e0c3a0"))
    p.fillRect(fr, g)
    # 썸네일에서는 글자가 너무 작아지지 않게 약간 키움
    k = 3.0  # 72px 짜리 그림에서도 모양(색·외곽선·상자)을 알아볼 수 있게 글자 쪽만 키움 (자리는 그대로)
    big = SubtitleStyle(**{**asdict(style), "size": int(style.size * k), "outline": style.outline * k * 0.8,
                           "box_padding": int(style.box_padding * k * 0.7)})
    paint_subtitle(p, fr, text, big, out_h, appear_state(big, text, 99.0, 99.0, 198.0))
    p.end()
    return pm


class ColorButton(QPushButton):
    def set_color(self, color: str):
        self.color = color
        self.setText(color.upper())
        light = QColor(color).lightness() > 128
        self.setStyleSheet(f"background:{color}; color:{'#000' if light else '#fff'}; padding:3px 8px;")


class SubtitleStylePanel(QWidget):
    def __init__(self, model: ProjectModel, parent=None, store: Optional[PresetStore] = None):
        super().__init__(parent)
        self.model = model
        self._loading = False
        self.store = store or PresetStore()
        # 테스트에서 바꿔 끼울 수 있는 함수들
        self.pick_color: Callable[[str, str], Optional[str]] = self._pick_color_dialog
        self.ask_text: Callable[[str, str, str], Optional[str]] = self._ask_text
        self.ask_open_file: Callable[[], Optional[Path]] = self._ask_open_file
        self.ask_save_file: Callable[[str], Optional[Path]] = self._ask_save_file
        self.confirm: Callable[[str], bool] = lambda t: QMessageBox.question(self, "스타일 모음", t) == QMessageBox.Yes
        self.notify: Callable[[str], None] = lambda t: QMessageBox.information(self, "스타일 모음", t)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # --- 스타일 모음 ---
        g_pre = QGroupBox("스타일 모음 — 누르면 바로 적용 (Ctrl+Z 로 되돌리기)")
        pl = QVBoxLayout(g_pre)
        pl.setContentsMargins(6, 6, 6, 6)
        self.presets = QListWidget()
        self.presets.setViewMode(QListView.IconMode)
        self.presets.setIconSize(THUMB)
        self.presets.setGridSize(QSize(THUMB.width() + 12, THUMB.height() + 26))
        self.presets.setResizeMode(QListView.Adjust)
        self.presets.setMovement(QListView.Static)
        self.presets.setWrapping(True)
        self.presets.setUniformItemSizes(True)
        self.presets.setWordWrap(True)
        self.presets.setFixedHeight(2 * (THUMB.height() + 26) + 12)
        self.presets.setToolTip("스타일을 누르면 지금 프로젝트 자막에 적용됩니다")
        pl.addWidget(self.presets)
        r = QHBoxLayout()
        self.btn_save_preset = QPushButton("내 스타일로 저장…")
        self.btn_rename_preset = QPushButton("이름 바꾸기…")
        self.btn_delete_preset = QPushButton("삭제")
        self.btn_import_preset = QPushButton("가져오기…")
        self.btn_export_preset = QPushButton("내보내기…")
        for b in (self.btn_save_preset, self.btn_rename_preset, self.btn_delete_preset):
            r.addWidget(b)
        r.addStretch(1)
        for b in (self.btn_import_preset, self.btn_export_preset):
            r.addWidget(b)
        pl.addLayout(r)
        outer.addWidget(g_pre)

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
        self.presets.itemClicked.connect(lambda it: self.apply_preset(it.data(NAME_ROLE)))
        self.presets.currentItemChanged.connect(lambda *_: self._update_preset_buttons())
        self.btn_save_preset.clicked.connect(self.save_preset)
        self.btn_rename_preset.clicked.connect(self.rename_preset)
        self.btn_delete_preset.clicked.connect(self.delete_preset)
        self.btn_import_preset.clicked.connect(self.import_presets)
        self.btn_export_preset.clicked.connect(self.export_presets)

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

    # ------------------------------------------------------------------ 스타일 모음
    def _fonts(self) -> list[str]:
        return QFontDatabase.families()

    def _out(self):
        o = self.model.project.output
        return o.width, o.height

    def preset_style(self, preset: Preset) -> SubtitleStyle:
        return style_from_preset(preset, *self._out(), self._fonts())

    def fill_presets(self):
        cur = self.presets.currentItem().data(NAME_ROLE) if self.presets.currentItem() else None
        sig = (self._out(), tuple(p.name for p in self.store.all()),
               tuple(tuple(sorted(p.style.items())) for p in self.store.user))
        if sig != getattr(self, "_preset_sig", None):
            self._preset_sig = sig
            self.presets.blockSignals(True)
            self.presets.clear()
            w, h = self._out()
            for pr in self.store.all():
                st = self.preset_style(pr)
                it = QListWidgetItem(QIcon(preset_thumbnail(st, w, h)), pr.name if pr.builtin else f"★ {pr.name}")
                it.setData(NAME_ROLE, pr.name)
                tip = pr.description or "내 스타일"
                if pr.fonts and st.font != pr.fonts[0]:
                    tip += f"\n(글꼴 '{pr.fonts[0]}' 이 없어 '{st.font}' 로 대신합니다)"
                it.setToolTip(tip)
                self.presets.addItem(it)
            self.presets.blockSignals(False)
        # 지금 스타일과 같은 것을 표시
        match = next((pr.name for pr in self.store.all() if self.preset_style(pr) == self.st), None)
        target = match or cur
        self.presets.blockSignals(True)
        self.presets.setCurrentItem(self._preset_item(target) if target else None)
        if not match and self.presets.currentItem():
            self.presets.currentItem().setSelected(False)
        self.presets.blockSignals(False)
        self._update_preset_buttons()

    def _preset_item(self, name: str) -> Optional[QListWidgetItem]:
        for i in range(self.presets.count()):
            if self.presets.item(i).data(NAME_ROLE) == name:
                return self.presets.item(i)
        return None

    def selected_preset(self) -> Optional[Preset]:
        it = self.presets.currentItem()
        return self.store.find(it.data(NAME_ROLE)) if it else None

    def _update_preset_buttons(self):
        pr = self.selected_preset()
        user = bool(pr and not pr.builtin)
        self.btn_rename_preset.setEnabled(user)
        self.btn_delete_preset.setEnabled(user)
        self.btn_export_preset.setEnabled(bool(self.store.user))

    def apply_preset(self, name: str):
        pr = self.store.find(name)
        if pr is None:
            return
        st = self.preset_style(pr)
        if st == self.st:
            return
        self.model.edit(lambda p: setattr(p.subtitle, "style", st))  # 되돌리기 한 번

    def _ask_text(self, title: str, label: str, default: str) -> Optional[str]:
        t, ok = QInputDialog.getText(self, title, label, text=default)
        return t.strip() if ok and t.strip() else None

    def _ask_open_file(self) -> Optional[Path]:
        f, _ = QFileDialog.getOpenFileName(self, "자막 스타일 가져오기", "", "자막 스타일 (*.json)")
        return Path(f) if f else None

    def _ask_save_file(self, default: str) -> Optional[Path]:
        f, _ = QFileDialog.getSaveFileName(self, "내 자막 스타일 내보내기", default, "자막 스타일 (*.json)")
        if not f:
            return None
        p = Path(f)
        return p if p.suffix.lower() == ".json" else p.with_suffix(".json")

    def save_preset(self):
        name = self.ask_text("내 스타일로 저장", "스타일 이름:", "내 스타일")
        if not name:
            return
        existing = self.store.find(name)
        replace = False
        if existing is not None:
            if existing.builtin:
                name = self.store.unique_name(name)
            elif self.confirm(f"'{name}' 스타일이 이미 있습니다. 지금 스타일로 바꿀까요?"):
                replace = True
            else:
                return
        pr = self.store.add(preset_from_style(name, self.st, *self._out()), replace=replace)
        self.fill_presets()
        self.presets.setCurrentItem(self._preset_item(pr.name))

    def rename_preset(self):
        pr = self.selected_preset()
        if not pr or pr.builtin:
            return
        new = self.ask_text("이름 바꾸기", "새 이름:", pr.name)
        if not new or new == pr.name:
            return
        try:
            self.store.rename(pr.name, new)
        except PresetError as e:
            self.notify(str(e))
            return
        self.fill_presets()
        self.presets.setCurrentItem(self._preset_item(new))

    def delete_preset(self):
        pr = self.selected_preset()
        if not pr or pr.builtin:
            return
        if not self.confirm(f"내 스타일 '{pr.name}' 을(를) 지울까요?\n(프로젝트에 이미 적용된 자막 모양은 그대로입니다)"):
            return
        self.store.delete(pr.name)
        self.fill_presets()

    def import_presets(self):
        path = self.ask_open_file()
        if not path:
            return
        try:
            added = self.store.import_file(path)
        except (PresetError, OSError) as e:
            self.notify(f"가져오지 못했습니다:\n{e}")
            return
        self.fill_presets()
        self.notify(f"스타일 {len(added)}개를 가져왔습니다: " + ", ".join(p.name for p in added))

    def export_presets(self):
        if not self.store.user:
            self.notify("내보낼 내 스타일이 없습니다. 먼저 '내 스타일로 저장…'을 하세요.")
            return
        path = self.ask_save_file("내 자막 스타일.json")
        if not path:
            return
        self.store.export(path)
        self.notify(f"내 스타일 {len(self.store.user)}개를 저장했습니다:\n{path}")

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
        self.fill_presets()
        if not p.subtitle.burn_in:
            self.note.setText("⚠ 지금은 '소프트 자막'(켜고 끄는 자막)으로 내보내도록 되어 있어, 최종 영상에서는 이 스타일·효과가 "
                              "적용되지 않습니다(재생 프로그램이 정함). ⚙ 출력 설정 ▸ 자막 ▸ '영상에 자막 입히기'를 켜세요.")
        else:
            self.note.setText("바꾸면 미리보기 화면의 자막에 바로 반영됩니다(다시 렌더하지 않음). 영상을 내보낼 때 같은 모양으로 "
                              "영상에 새겨집니다. 미리보기의 효과는 비슷하게 흉내 낸 것이라 실제와 약간 다를 수 있습니다.")
