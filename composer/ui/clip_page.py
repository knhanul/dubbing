"""클립 설정 페이지.

inspector.py 에서 분리.
"""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit

from ..schema import MOTION_LABELS, Clip
from .inspector_base import _Page, SPEEDS, _rel
from .widgets import DIM, NudgeSpin, Segmented, ValueSlider, field, link_button, shrink

class ClipPage(_Page):
    effects_requested = Signal()  # '✨ 효과 탭에서 바꾸기'

    def __init__(self, model):
        super().__init__(model)
        self.lbl_file = self.head
        self.lbl_info = self.hint
        self.btn_dup = self.head_button("⧉", self._dup, "복제 — 바로 뒤에 같은 클립을 하나 더")
        self.btn_del = self.head_button("🗑", self._delete, "삭제 (Delete) — 지우면 뒤 클립이 당겨 붙습니다")

        # --- 길이 · 속도 ---
        self.duration = NudgeSpin(0.1, 3600)
        self.speed_preset = QComboBox()
        for v in SPEEDS:
            self.speed_preset.addItem(f"{v:g}×", v)
        self.speed_preset.addItem("직접 입력…", None)
        shrink(self.speed_preset, chars=4)
        self.speed = NudgeSpin(0.1, 16, steps=(0.1, 0.01), suffix=" 배")
        self.chk_fit = QCheckBox("목표 길이에 맞춰 속도 자동")
        self.chk_fit.setToolTip("영상이 이 길이에 딱 맞게 재생 속도를 알아서 정합니다")
        self.fit_duration = NudgeSpin(0.1, 3600)
        s = self.sec_time = self.section("길이 · 속도")
        self.f_duration = field("보이는 시간", self.duration, tip="이 사진이 화면에 보이는 시간")
        self.f_speed = field("재생 속도", self.speed_preset)
        self.f_speed_num = field("배속", self.speed, label_w=34)
        self.row_time = s.row(self.f_duration, self.f_speed, self.f_speed_num)
        s.row(self.chk_fit, self.fit_duration, stretches=[3, 2])

        # --- 화면 ---
        self.fit = Segmented([(None, "기본값", "📤 내보내기 탭의 화면 맞춤을 따릅니다"),
                              ("cover", "꽉 채우기", "화면을 꽉 채우고 넘치는 부분은 잘라냅니다"),
                              ("contain", "전체 보이기", "그림 전체가 보이게, 남는 곳은 여백 색")])
        self.ed_label = QLineEdit()
        self.ed_label.setPlaceholderText("타임라인에 보일 이름 (비우면 파일 이름)")
        s = self.section("화면")
        s.add(field("화면 맞춤", self.fit))
        s.add(field("이름표", self.ed_label))

        # --- 효과 (지금 걸린 것 요약 + 효과 탭 바로가기) ---
        self.sec_fx = self.section("효과")
        self.lbl_fx = QLabel()
        self.lbl_fx.setWordWrap(True)
        self.lbl_fx.setStyleSheet("color:#f0d27a;")
        self.btn_fx = link_button("✨ 효과 탭에서 바꾸기 ›", "움직임 · 색 필터 · 밝기 · 분위기")
        self.btn_fx.clicked.connect(lambda: self.effects_requested.emit())
        self.sec_fx.row(self.lbl_fx, self.btn_fx, stretches=[1, 0])

        # --- 원본 영상 (영상 클립만) ---
        self.trim_start = NudgeSpin(0, 36000)
        self.chk_trim_end = QCheckBox("끝 자르기")
        self.chk_trim_end.setToolTip("켜면 원본 영상의 이 시각까지만 씁니다")
        self.trim_end = NudgeSpin(0.1, 36000)
        self.mute = QCheckBox("끄기")
        self.vol_slider = ValueSlider(0, 2, 0.05, "{:.0%}")
        self.sec_src = self.section("원본 영상", col=1)
        self.sec_src.row(field("앞 자르기", self.trim_start, tip="원본 영상 앞에서 버릴 시간"),
                         hrow(self.chk_trim_end, self.trim_end, stretches=[0, 1], spacing=6))
        self._snd = hrow(self.mute, self.vol_slider, stretches=[0, 1], spacing=6)
        self.sec_src.add(field("원본 소리", self._snd))

        c = lambda: self.clip  # noqa: E731
        self.ed_label.editingFinished.connect(lambda: self._set("label", lambda p: setattr(c(), "label", self.ed_label.text())))
        self.duration.valueChanged.connect(lambda v: self._set("dur", lambda p: setattr(c(), "duration", v)))
        self.chk_fit.toggled.connect(self._fit_toggled)
        self.fit_duration.valueChanged.connect(lambda v: self._set("fitd", lambda p: setattr(c(), "fit_duration", v)))
        self.speed.valueChanged.connect(lambda v: self._set("speed", lambda p: setattr(c(), "speed", v)))
        self.speed_preset.activated.connect(self._speed_preset)
        self.trim_start.valueChanged.connect(lambda v: self._set("ts", lambda p: setattr(c(), "trim_start", v)))
        self.chk_trim_end.toggled.connect(self._trim_toggled)
        self.trim_end.valueChanged.connect(lambda v: self._set("te", lambda p: setattr(c(), "trim_end", v)))
        self.mute.toggled.connect(lambda v: self._set("mute", lambda p: setattr(c(), "mute", v)))
        self.vol_slider.valueChanged.connect(lambda v: self._set("vol", lambda p: setattr(c(), "volume", v)))
        self.fit.changed.connect(lambda v: self._set("fit", lambda p: setattr(c(), "fit", v)))

    @property
    def idx(self) -> int:
        return self.model.selection.index

    @property
    def clip(self) -> Clip:
        return self.model.project.clips[self.idx]

    def refresh(self):
        from .preview import fmt_time
        if not (0 <= self.idx < len(self.model.project.clips)):
            return
        self._loading = True
        c = self.clip
        tm = self.model.timeline.clips[self.idx]
        img = c.type == "image"
        src = self.model.duration_of(c) if not img else None
        kind = "이미지" if img else "동영상"
        self.head.setText(f"{self.idx + 1}번 · {c.label or c.file}")
        extra = f" · 원본 {src:.2f}초 중 {tm.source_in:.2f}~{tm.source_out:.2f}초" if not img and src else ""
        self.hint.setText(f"{kind} · {fmt_time(tm.start)} ~ {fmt_time(tm.start + tm.duration)} ({tm.duration:.2f}초){extra}"
                          " — 길이는 타임라인에서 클립 끝을 끌어도 됩니다")
        self.f_duration.setVisible(img)
        self.f_speed.setVisible(not img)
        self.f_speed_num.setVisible(not img)
        self.sec_src.setVisible(not img)
        self.ed_label.setText(c.label)
        self.duration.setValue(c.duration or tm.duration)
        self.duration.setEnabled(img and not c.fit_duration)
        self.chk_fit.setChecked(bool(c.fit_duration))
        self.fit_duration.setValue(c.fit_duration or tm.duration)
        self.fit_duration.setEnabled(bool(c.fit_duration))
        self.speed.setEnabled(not img and not c.fit_duration)
        self.speed_preset.setEnabled(not img and not c.fit_duration)
        self.speed.setValue(c.speed)
        k = next((i for i, v in enumerate(SPEEDS) if abs(v - c.speed) < 1e-6), None)
        last = self.speed_preset.count() - 1
        self.speed_preset.setItemText(last, "직접 입력…" if k is not None else f"직접: {c.speed:g}×")
        self.speed_preset.setCurrentIndex(k if k is not None else last)
        self.trim_start.setValue(c.trim_start)
        self.chk_trim_end.setChecked(c.trim_end is not None)
        self.trim_end.setValue(c.trim_end if c.trim_end is not None else (src or 0))
        self.trim_end.setEnabled(not img and c.trim_end is not None)
        self.mute.setChecked(c.mute)
        self.vol_slider.setValue(c.volume)
        self.vol_slider.setEnabled(not img and not c.mute)
        from ..effects import LOOK_LABELS, has_effects
        parts = []
        if c.motion != "none" and c.motion_strength > 0:
            parts.append(MOTION_LABELS.get(c.motion, c.motion))
        if c.look != "none":
            parts.append(LOOK_LABELS.get(c.look, c.look))
        if has_effects(c) and c.look == "none":
            parts.append("조정 있음")
        self.lbl_fx.setText(" · ".join(parts) if parts else "효과 없음")
        self.lbl_fx.setStyleSheet("color:#f0d27a;" if parts else DIM)
        self.sec_fx.set_dirty(bool(parts))
        self.fit.set_value(c.fit)
        self._loading = False

    def _speed_preset(self, i: int):
        v = self.speed_preset.itemData(i)
        if v is None:  # 직접 입력 → 옆 숫자 칸으로
            self.speed.spin.setFocus()
            self.speed.spin.selectAll()
            return
        self._set(None, lambda p: setattr(self.clip, "speed", float(v)))

    def _fit_toggled(self, on):
        if self._loading:
            return
        tm = self.model.timeline.clips[self.idx]
        self._set(None, lambda p: setattr(self.clip, "fit_duration", round(tm.duration, 3) if on else None))
        self.refresh()

    def _trim_toggled(self, on):
        if self._loading:
            return
        tm = self.model.timeline.clips[self.idx]
        self._set(None, lambda p: setattr(self.clip, "trim_end", round(tm.source_out, 3) if on else None))
        self.refresh()

    def _dup(self):
        clips = self.model.project.clips
        i = self.idx
        self.model.edit(lambda p: clips.insert(i + 1, copy.deepcopy(clips[i])))
        self.model.select("clip", i + 1)

    def _delete(self):
        from ..edit_ops import delete_clip
        i = self.idx
        self.model.edit(lambda p: delete_clip(p, i))
        self.model.select("")


