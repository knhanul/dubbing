"""🛠 속성 패널: 타임라인에서 고른 것(클립·전환·자막·더빙·배경음악)만 보여 줍니다.

* 기본으로는 슬라이더·선택 칸만. 0.1초/0.01초 숫자 입력은 '▸ 숫자로 정확히'를 펼칠 때만(펼친 상태 기억, 모든 칸 공통)
* 길이·자르기·위치는 타임라인에서 끌어서 — 여기엔 현재 값이 글자로 나옴
* 아무것도 안 골랐으면 프로젝트 요약 + 시작 버튼
* OutputPage(📤 출력 탭)와 SubtitleFilePanel(🅰 자막 탭 맨 위)도 여기서 만듦
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from ..schema import MOTION_LABELS, MOTIONS, TRANSITION_LABELS, TRANSITIONS, Clip, Transition
from ..subtitles import detach_takes, ensure_items, has_subtitles, to_srt
from .model import ProjectModel
from .size_presets import combo_size, fill_size_combo, sync_size_combo
from .widgets import Collapsible, NudgeSpin, ValueSlider

NUM_TITLE = "숫자로 정확히 (미세 조정)"
DIM = "color:#9aa0a6;"
SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]


def _rel(project, path: str) -> str:
    import os

    try:
        return Path(os.path.relpath(path, project.base_dir)).as_posix()
    except ValueError:
        return path


class _Page(QWidget):
    """머리글(무엇을 골랐는지) · 기본 칸 · ▸ 숫자로 정확히 · (▸ 고급) · 아래 버튼."""

    def __init__(self, model: ProjectModel, advanced: str = ""):
        super().__init__()
        self.model = model
        self._loading = False
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(6, 6, 6, 6)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:13px;font-weight:bold;")
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(DIM)
        self.outer.addWidget(self.head)
        self.outer.addWidget(self.hint)
        basic = QWidget()
        self.form = QFormLayout(basic)
        self.form.setContentsMargins(0, 4, 0, 4)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.outer.addWidget(basic)
        self.num = Collapsible(NUM_TITLE, key="num")
        self.num.head.setToolTip("0.1초 / 0.01초 단위로 숫자를 직접 넣습니다. 보통은 타임라인에서 끌면 됩니다")
        self.outer.addWidget(self.num)
        self.adv = Collapsible(advanced, key="adv_" + type(self).__name__) if advanced else None
        if self.adv:
            self.outer.addWidget(self.adv)
        self.actions = QHBoxLayout()
        self.outer.addLayout(self.actions)
        self.outer.addStretch(1)

    # 위젯 값 → 프로젝트 반영 헬퍼 (merge_key 로 끌기·연속 입력은 되돌리기 한 번)
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

    def dspin(self, lo, hi, step=0.1, dec=3, suffix="") -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(dec)
        s.setSuffix(suffix)
        s.setKeyboardTracking(False)
        return s

    def button(self, text: str, slot, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        self.actions.addWidget(b)
        return b


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


class ClipPage(_Page):
    def __init__(self, model):
        super().__init__(model, advanced="고급 — 이름표 · 움직임 속도감")
        self.lbl_file = self.head
        self.lbl_info = self.hint
        # --- 기본 (슬라이더·선택) ---
        self.motion = self.combo([(m, MOTION_LABELS[m]) for m in MOTIONS])
        self.strength_slider = ValueSlider(0, 1, 0.01, "{:.2f}")
        self.speed_preset = QComboBox()
        for v in SPEEDS:
            self.speed_preset.addItem(f"{v:g}×", v)
        self.speed_preset.addItem("직접 입력…", None)
        self.mute = QCheckBox("원본 소리 끄기")
        self.vol_slider = ValueSlider(0, 2, 0.05, "{:.0%}")
        self.fit = self.combo([(None, "출력 설정 따름"), ("cover", "꽉 채우기(잘라냄)"), ("contain", "전체 보이기(여백)")])
        f = self.form
        f.addRow("움직임", self.motion)
        f.addRow("움직임 강도", self.strength_slider)
        f.addRow("재생 속도", self.speed_preset)
        snd = QHBoxLayout(); snd.addWidget(self.mute); snd.addWidget(self.vol_slider, 1)
        self._snd = QWidget(); self._snd.setLayout(snd); snd.setContentsMargins(0, 0, 0, 0)
        f.addRow("원본 소리", self._snd)
        f.addRow("화면 맞춤", self.fit)
        # --- ▸ 숫자로 정확히 ---
        self.duration = NudgeSpin(0.1, 3600)
        self.chk_fit = QCheckBox("목표 길이에 맞춰 속도 자동")
        self.fit_duration = NudgeSpin(0.1, 3600)
        self.speed = NudgeSpin(0.1, 16, steps=(0.1, 0.01), suffix=" 배")
        self.trim_start = NudgeSpin(0, 36000)
        self.chk_trim_end = QCheckBox("끝 자르기")
        self.trim_end = NudgeSpin(0.1, 36000)
        self.volume = self.dspin(0, 4, 0.05, 2, " 배")
        self.strength = self.dspin(0, 1, 0.01, 3)
        n = self.num
        n.addRow("표시 시간", self.duration)
        n.addRow(self.chk_fit, self.fit_duration)
        n.addRow("재생 속도", self.speed)
        n.addRow("시작 자르기", self.trim_start)
        n.addRow(self.chk_trim_end, self.trim_end)
        n.addRow("원본 소리 크기", self.volume)
        n.addRow("움직임 강도", self.strength)
        # --- ▸ 고급 ---
        self.ed_label = QLineEdit()
        self.ed_label.setPlaceholderText("타임라인에 보일 이름 (비우면 파일 이름)")
        self.easing = self.combo([("ease", "부드럽게(가속/감속)"), ("linear", "일정한 속도")])
        self.adv.addRow("이름표", self.ed_label)
        self.adv.addRow("움직임 속도감", self.easing)
        self.btn_dup = self.button("⧉ 복제", self._dup, "바로 뒤에 같은 클립을 하나 더")
        self.btn_del = self.button("🗑 삭제 (Delete)", self._delete, "지우면 뒤 클립이 당겨 붙습니다")

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
        self.volume.valueChanged.connect(lambda v: self._set("vol", lambda p: setattr(c(), "volume", v)))
        self.vol_slider.valueChanged.connect(lambda v: self._set("vol", lambda p: setattr(c(), "volume", v)))
        self.motion.currentIndexChanged.connect(
            lambda _: self._set("motion", lambda p: setattr(c(), "motion", self.motion.currentData())))
        self.strength.valueChanged.connect(lambda v: self._set("str", lambda p: setattr(c(), "motion_strength", v)))
        self.strength_slider.valueChanged.connect(lambda v: self._set("str", lambda p: setattr(c(), "motion_strength", v)))
        self.easing.currentIndexChanged.connect(
            lambda _: self._set("ease", lambda p: setattr(c(), "motion_easing", self.easing.currentData())))
        self.fit.currentIndexChanged.connect(lambda _: self._set("fit", lambda p: setattr(c(), "fit", self.fit.currentData())))

    @property
    def idx(self) -> int:
        return self.model.selection.index

    @property
    def clip(self) -> Clip:
        return self.model.project.clips[self.idx]

    def refresh(self):
        if not (0 <= self.idx < len(self.model.project.clips)):
            return
        self._loading = True
        c = self.clip
        tm = self.model.timeline.clips[self.idx]
        img = c.type == "image"
        src = self.model.duration_of(c) if not img else None
        kind = "이미지" if img else "동영상"
        self.head.setText(f"{self.idx + 1}번 클립 · {c.label or c.file} ({kind}) · 길이 {tm.duration:.2f}초")
        extra = f"원본 {src:.2f}초 · 쓰는 구간 {tm.source_in:.2f}~{tm.source_out:.2f}초 · 속도 ×{tm.speed:.3f}\n" if not img and src else ""
        self.hint.setText(extra + f"시작 {tm.start:.2f}초 — 길이·자르기는 타임라인에서 클립 끝을 끌어 조절, "
                                  f"정확한 숫자는 아래 '{NUM_TITLE}'")
        f, n = self.form, self.num.form
        for w in (self.motion, self.strength_slider):
            f.setRowVisible(w, img)
        for w in (self.speed_preset, self._snd):
            f.setRowVisible(w, not img)
        n.setRowVisible(self.duration, img)
        n.setRowVisible(self.strength, img)
        for w in (self.speed, self.trim_start, self.trim_end, self.volume):
            n.setRowVisible(w, not img)
        self.ed_label.setText(c.label)
        self.duration.setValue(c.duration or tm.duration)
        self.duration.setEnabled(img and not c.fit_duration)
        self.chk_fit.setChecked(bool(c.fit_duration))
        self.fit_duration.setValue(c.fit_duration or tm.duration)
        self.fit_duration.setEnabled(bool(c.fit_duration))
        for w in (self.speed, self.trim_start, self.chk_trim_end, self.trim_end, self.mute, self.volume, self.vol_slider):
            w.setEnabled(not img)
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
        self.volume.setValue(c.volume)
        self.vol_slider.setValue(c.volume)
        self.vol_slider.setEnabled(not img and not c.mute)
        self.set_combo(self.motion, c.motion)
        self.strength.setValue(c.motion_strength)
        self.strength_slider.setValue(c.motion_strength)
        self.strength_slider.setEnabled(c.motion != "none")
        self.set_combo(self.easing, c.motion_easing)
        self.set_combo(self.fit, c.fit)
        self._loading = False

    def _speed_preset(self, i: int):
        v = self.speed_preset.itemData(i)
        if v is None:  # 직접 입력 → 숫자 칸 펼치고 거기로
            self.num.set_open(True)
            self.speed.spin.setFocus()
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


class TransitionPage(_Page):
    """타임라인의 전환 블록을 골랐을 때: 종류·길이·없애기."""

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.lbl_info = self.hint
        self.tr_type = self.combo([(t, TRANSITION_LABELS.get(t, t)) for t in TRANSITIONS if t != "none"])
        self.tr_slider = ValueSlider(0.05, 3.0, 0.05, "{:.2f}초")
        self.form.addRow("종류", self.tr_type)
        self.form.addRow("길이", self.tr_slider)
        self.tr_dur = NudgeSpin(0.05, 10)
        self.num.addRow("전환 길이", self.tr_dur)
        self.btn_del = self.button("🗑 전환 없애기 (Delete)", self.remove)
        self.tr_type.currentIndexChanged.connect(lambda _: self._apply("trt"))
        self.tr_dur.valueChanged.connect(lambda v: self._apply("trd", v))
        self.tr_slider.valueChanged.connect(lambda v: self._apply("trd", v))

    @property
    def idx(self) -> int:
        return self.model.selection.index

    def _apply(self, key, dur=None):
        i = self.idx
        if self._loading or not (0 < i < len(self.model.project.clips)):
            return
        d = self.tr_dur.value() if dur is None else dur
        self._set(key, lambda p: setattr(p.clips[i], "transition", Transition(self.tr_type.currentData(), round(d, 3))))

    def remove(self):
        i = self.idx
        if 0 < i < len(self.model.project.clips):
            self.model.edit(lambda p: setattr(p.clips[i], "transition", Transition("none", 0.0)))
            self.model.select("")

    def refresh(self):
        from ..timeline import effective_transition
        i = self.idx
        tl = self.model.timeline.clips
        if not (0 < i < len(tl)):
            return
        self._loading = True
        want = effective_transition(self.model.project, i)
        got = tl[i].transition_in
        self.head.setText(f"전환 · {i}번 ↔ {i + 1}번 클립 (경계 {tl[i].start:.2f}초)")
        self.set_combo(self.tr_type, got.type)
        self.tr_dur.setValue(want.duration)
        self.tr_slider.setValue(want.duration)
        note = (f"실제 길이 {got.duration:.2f}초 — 앞뒤 클립 중 짧은 쪽 길이까지만 됩니다. " if want.duration > got.duration + 1e-3
                else "")
        self.hint.setText(note + "경계 가운데에 걸칩니다(전체 길이는 그대로). 타임라인에서 블록 양 끝을 끌어도 길이가 바뀝니다.")
        self._loading = False


class SubtitlePage(_Page):
    style_requested = Signal()

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(80)
        self.text.setPlaceholderText("자막 글 (줄바꿈 가능)")
        self.form.addRow("내용", self.text)
        self.btn_style = QPushButton("🅰 자막 모양 바꾸기 → 자막 탭")
        self.btn_style.clicked.connect(self.style_requested.emit)
        self.form.addRow("", self.btn_style)
        self.start = NudgeSpin(0, 36000)
        self.end = NudgeSpin(0, 36000)
        self.after = NudgeSpin(-3600, 3600)
        self.num.addRow("시작", self.start)
        self.num.addRow("끝", self.end)
        self.num.addRow("이 자막부터 끝까지 함께 이동", self.after)
        self.btn_del = self.button("🗑 자막 삭제 (Delete)", self._delete)
        self.text.textChanged.connect(self._text_changed)
        self.start.valueChanged.connect(lambda v: self._set("s", lambda p: self._item_set(start=v)))
        self.end.valueChanged.connect(lambda v: self._set("e", lambda p: self._item_set(end=v)))
        self._after_prev = 0.0
        self.after.valueChanged.connect(self._after_changed)

    def item(self):
        items = ensure_items(self.model.project)
        return next((i for i in items if i.index == self.model.selection.index), None)

    def _item_set(self, start=None, end=None):
        it = self.item()
        if it is None:
            return
        if start is not None:
            it.start = round(min(start, it.end - 0.05), 3)
        if end is not None:
            it.end = round(max(end, it.start + 0.05), 3)

    def refresh(self):
        from .preview import fmt_time
        it = self.item()
        if it is None:
            return
        self._loading = True
        self.head.setText(f"{it.index}번 자막 · {fmt_time(it.start)} ~ {fmt_time(it.end)} ({it.end - it.start:.2f}초)")
        self.hint.setText("시간은 타임라인에서 끌어 옮기거나 양 끝을 끌어 조절 ([ / ] 키 = 0.1초)"
                          + (" · 표시 값은 전체 오프셋 적용 전" if self.model.project.subtitle.offset else ""))
        if self.text.toPlainText() != it.text:
            self.text.setPlainText(it.text)
        self.start.setValue(it.start)
        self.end.setValue(it.end)
        self._after_prev = 0.0
        self.after.setValue(0.0)
        self._loading = False

    def _text_changed(self):
        if self._loading:
            return
        it = self.item()
        if it:
            self._set("txt", lambda p: setattr(it, "text", self.text.toPlainText()))

    def _after_changed(self, v):
        if self._loading:
            return
        it = self.item()
        if not it:
            return
        d = v - self._after_prev
        self._after_prev = v

        def fn(p):
            for x in ensure_items(p):
                if x.start >= it.start:
                    dd = max(d, -x.start)
                    x.start, x.end = round(x.start + dd, 3), round(x.end + dd, 3)
        self._set("after", fn)

    def _delete(self):
        it = self.item()
        if it:
            def fn(p):
                detach_takes(p, {it.index})
                p.subtitle.items = [x for x in ensure_items(p) if x is not it]
            self.model.edit(fn)
            self.model.select("")


class TakePage(_Page):
    play_requested = Signal(int, bool)  # take index, 영상과 함께
    rerecord_requested = Signal(int)

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.vol_slider = ValueSlider(0, 2, 0.05, "{:.0%}")
        self.form.addRow("볼륨", self.vol_slider)
        row = QHBoxLayout()
        self.btn_play = QPushButton("▶ 듣기")
        self.btn_play_v = QPushButton("▶ 영상과 함께")
        self.btn_rerec = QPushButton("● 다시 녹음")
        self.btn_rerec.setStyleSheet("QPushButton{color:#ff6b62;font-weight:bold}")
        for b in (self.btn_play, self.btn_play_v, self.btn_rerec):
            row.addWidget(b)
        self.form.addRow(row)
        self.offset = NudgeSpin(-3600, 36000)
        self.lbl_pos = QLabel()
        self.volume = self.dspin(0, 4, 0.05, 2, " 배")
        self.num.addRow("오프셋(자막 시작 기준)", self.offset)
        self.num.addRow("타임라인 위치", self.lbl_pos)
        self.num.addRow("볼륨", self.volume)
        self.btn_del = self.button("🗑 테이크 삭제 (Delete)", self._delete)
        self.offset.valueChanged.connect(lambda v: self._set("off", lambda p: setattr(self.take, "offset", v)))
        self.volume.valueChanged.connect(lambda v: self._set("tvol", lambda p: setattr(self.take, "volume", v)))
        self.vol_slider.valueChanged.connect(lambda v: self._set("tvol", lambda p: setattr(self.take, "volume", v)))
        self.btn_play.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, False))
        self.btn_play_v.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, True))
        self.btn_rerec.clicked.connect(lambda: self.rerecord_requested.emit(self.model.selection.index))

    @property
    def take(self):
        return self.model.project.dubbing.takes[self.model.selection.index]

    def refresh(self):
        takes = self.model.project.dubbing.takes
        i = self.model.selection.index
        if not (0 <= i < len(takes)):
            return
        self._loading = True
        t = takes[i]
        who = f"{t.subtitle}번 자막 녹음" if t.subtitle is not None else "자유 녹음"
        pos = self.model.take_positions()[i]
        self.head.setText(f"더빙 · {who}")
        self.hint.setText(f"{t.file} · 타임라인 {pos + (t.trim_start or 0):.2f}초부터 — 타임라인에서 끌어 옮기기")
        self.offset.setValue(t.offset)
        self.lbl_pos.setText(f"{pos:.3f} 초")
        self.volume.setValue(t.volume)
        self.vol_slider.setValue(t.volume)
        self._loading = False

    def _delete(self):
        i = self.model.selection.index
        self.model.edit(lambda p: p.dubbing.takes.pop(i))
        self.model.select("")


class BgmPage(_Page):
    """배경음악 막대를 골랐을 때: 볼륨 · 페이드 · 반복 · 목소리 나올 때 줄이기."""

    remove_requested = Signal()
    pick_requested = Signal()

    def __init__(self, model):
        super().__init__(model)
        self.btn_bgm = QPushButton("바꾸기…")
        self.btn_bgm.clicked.connect(self.pick_requested.emit)
        r = QHBoxLayout(); r.addWidget(self.hint, 1); r.addWidget(self.btn_bgm)
        self.outer.insertLayout(2, r)
        self.vol_slider = ValueSlider(0, 2, 0.01, "{:.0%}")
        self.fade_in_slider = ValueSlider(0, 10, 0.1, "{:.1f}초")
        self.fade_out_slider = ValueSlider(0, 10, 0.1, "{:.1f}초")
        self.loop = QCheckBox("반복 재생 (영상보다 짧으면 처음부터 다시)")
        self.duck = QCheckBox("목소리 나올 때 자동으로 줄이기")
        f = self.form
        f.addRow("볼륨", self.vol_slider)
        f.addRow("페이드 인", self.fade_in_slider)
        f.addRow("페이드 아웃", self.fade_out_slider)
        f.addRow("", self.loop)
        f.addRow("", self.duck)
        self.bgm_vol = self.dspin(0, 4, 0.05, 2, " 배")
        self.bgm_start = NudgeSpin(0, 36000)
        self.bgm_fstart = NudgeSpin(0, 36000)
        self.fade_in = NudgeSpin(0, 60)
        self.fade_out = NudgeSpin(0, 60)
        self.duck_ratio = QDoubleSpinBox(); self.duck_ratio.setRange(1, 20)
        self.duck_ratio.setToolTip("클수록 목소리 나올 때 음악이 더 많이 줄어듭니다")
        n = self.num
        n.addRow("볼륨", self.bgm_vol)
        n.addRow("타임라인 시작", self.bgm_start)
        n.addRow("음악 파일 안 시작 위치", self.bgm_fstart)
        n.addRow("페이드 인", self.fade_in)
        n.addRow("페이드 아웃", self.fade_out)
        n.addRow("줄이는 강도(비율)", self.duck_ratio)
        self.btn_bgm_clear = self.button("🗑 배경음악 빼기 (Delete)", self.remove_requested.emit,
                                         "프로젝트에서 음악을 뺍니다. 원본 음악 파일은 그대로")
        S = self._set
        self.vol_slider.valueChanged.connect(lambda v: S("bv", lambda p: setattr(p.bgm, "volume", v)))
        self.bgm_vol.valueChanged.connect(lambda v: S("bv", lambda p: setattr(p.bgm, "volume", v)))
        self.bgm_start.valueChanged.connect(lambda v: S("bs", lambda p: setattr(p.bgm, "start_offset", v)))
        self.bgm_fstart.valueChanged.connect(lambda v: S("bfs", lambda p: setattr(p.bgm, "file_start", v)))
        self.fade_in.valueChanged.connect(lambda v: S("fi", lambda p: setattr(p.bgm, "fade_in", v)))
        self.fade_in_slider.valueChanged.connect(lambda v: S("fi", lambda p: setattr(p.bgm, "fade_in", v)))
        self.fade_out.valueChanged.connect(lambda v: S("fo", lambda p: setattr(p.bgm, "fade_out", v)))
        self.fade_out_slider.valueChanged.connect(lambda v: S("fo", lambda p: setattr(p.bgm, "fade_out", v)))
        self.loop.toggled.connect(lambda v: S("loop", lambda p: setattr(p.bgm, "loop", v)))
        self.duck.toggled.connect(lambda v: S("duck", lambda p: setattr(p.bgm, "duck_under_voice", v)))
        self.duck_ratio.valueChanged.connect(lambda v: S("dr", lambda p: setattr(p.bgm, "duck_ratio", v)))

    def refresh(self):
        b = self.model.project.bgm
        self._loading = True
        self.head.setText(f"♪ 배경음악 · {b.file or '없음'}")
        self.hint.setText(f"타임라인 {b.start_offset:.2f}초부터 — 막대를 끌어 옮기기")
        self.vol_slider.setValue(b.volume); self.bgm_vol.setValue(b.volume)
        self.bgm_start.setValue(b.start_offset); self.bgm_fstart.setValue(b.file_start)
        self.fade_in.setValue(b.fade_in); self.fade_in_slider.setValue(b.fade_in)
        self.fade_out.setValue(b.fade_out); self.fade_out_slider.setValue(b.fade_out)
        self.loop.setChecked(b.loop)
        self.duck.setChecked(b.duck_under_voice); self.duck_ratio.setValue(b.duck_ratio)
        self.duck_ratio.setEnabled(b.duck_under_voice)
        self._loading = False


class EmptyPage(QWidget):
    """아무것도 안 골랐을 때: 프로젝트 요약 + 시작 버튼."""

    ai_requested = Signal()
    add_requested = Signal()
    bgm_requested = Signal()
    sub_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        lay = QVBoxLayout(self)
        lay.addSpacing(10)
        self.summary = QLabel()
        self.summary.setAlignment(Qt.AlignCenter)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size:13px;")
        lay.addWidget(self.summary)
        lay.addSpacing(10)
        self.btn_ai = QPushButton("🤖 AI 로 구성하기")
        self.btn_add = QPushButton("➕ 미디어 추가 (이미지·영상)")
        self.btn_bgm = QPushButton("♪ 배경음악 넣기")
        self.btn_sub = QPushButton("📝 자막 파일 불러오기")
        for b, sig in ((self.btn_ai, self.ai_requested), (self.btn_add, self.add_requested),
                       (self.btn_bgm, self.bgm_requested), (self.btn_sub, self.sub_requested)):
            b.setMinimumHeight(32)
            b.clicked.connect(sig.emit)
            lay.addWidget(b)
        lay.addSpacing(14)
        msg = QLabel("타임라인에서 <b>클립 · 전환 · 자막 · 더빙 · 배경음악</b>을 누르면<br>그 항목의 설정만 여기에 나옵니다."
                     "<br><br><small>끌어서 옮기기 · 끝 끌어서 길이 · Shift = 자석 끄고 0.01초</small>")
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        msg.setStyleSheet(DIM)
        lay.addWidget(msg)
        lay.addStretch(1)
        model.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        p = self.model.project
        from .preview import fmt_time
        n_sub = len(self.model.cues)
        self.summary.setText(f"<b>{self.model.path.stem if self.model.path else '이 프로젝트'}</b><br>길이 {fmt_time(self.model.timeline.total)} · "
                             f"클립 {len(p.clips)}개 · 자막 {n_sub}줄 · 더빙 {len(p.dubbing.takes)}개"
                             + (f"<br>♪ {p.bgm.file}" if p.bgm.file else ""))
        self.btn_bgm.setText("♪ 배경음악 바꾸기" if p.bgm.file else "♪ 배경음악 넣기")


class SubtitleFilePanel(QGroupBox):
    """🅰 자막 탭 맨 위: 자막 파일 불러오기 · SRT 로 저장 · 전체 빼기."""

    remove_subtitles_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__("자막 파일")
        self.model = model
        lay = QVBoxLayout(self)
        self.sub_file = QLabel("-")
        self.sub_file.setWordWrap(True)
        self.btn_sub = QPushButton("📝 불러오기…")
        self.btn_sub_export = QPushButton("💾 SRT 로 저장…")
        self.btn_sub_export.setToolTip("지금 자막(고친 내용 포함)을 SRT 파일로 저장")
        self.btn_sub_clear = QPushButton("🗑 자막 전체 빼기")
        self.btn_sub_clear.setToolTip("프로젝트에서 자막을 모두 뺍니다. 원본 SRT 파일은 지워지지 않습니다.")
        lay.addWidget(self.sub_file)
        r = QHBoxLayout()
        for b in (self.btn_sub, self.btn_sub_export, self.btn_sub_clear):
            r.addWidget(b)
        lay.addLayout(r)
        self.btn_sub.clicked.connect(self.pick_sub)
        self.btn_sub_export.clicked.connect(self.export_sub)
        self.btn_sub_clear.clicked.connect(self.remove_subtitles_requested.emit)
        self.ask_open = lambda: QFileDialog.getOpenFileName(self, "자막 파일", str(self.model.project.base_dir),
                                                            "자막 (*.srt *.vtt)")[0]
        self.ask_save = lambda: QFileDialog.getSaveFileName(self, "SRT 저장", str(self.model.project.base_dir / "subtitle_edited.srt"),
                                                            "SRT (*.srt)")[0]
        model.changed.connect(self.refresh)
        self.refresh()

    def pick_sub(self, f: str = "") -> bool:
        f = f or self.ask_open()
        if not f or self.model.project.tracks.subtitle.locked:
            return False

        def fn(p):
            _auto_base(self.model, p, f)
            p.subtitle.file = _rel(p, f)
            p.subtitle.items = None
        self.model.edit(fn)
        return True

    def export_sub(self, f: str = "") -> bool:
        f = f or self.ask_save()
        if not f:
            return False
        Path(f).write_text(to_srt(self.model.cues), encoding="utf-8")
        return True

    def refresh(self):
        p = self.model.project
        st = p.subtitle
        lk = p.tracks.subtitle.locked
        self.setTitle("자막 파일" + ("  🔒 잠김 (타임라인 트랙 머리에서 풀기)" if lk else ""))
        self.sub_file.setText((st.file or ("자막 없음" if not st.items else "(파일 없이 고친 자막)"))
                              + (f" · {len(ensure_items(p))}줄" if has_subtitles(p) else "")
                              + (" (고친 내용 있음)" if st.items is not None and st.file else ""))
        self.btn_sub.setEnabled(not lk)
        self.btn_sub_clear.setEnabled(has_subtitles(p) and not lk)
        self.btn_sub_export.setEnabled(has_subtitles(p))


def _auto_base(model, p, f):
    """저장한 적 없고 아무것도 없는 '제목 없음' 프로젝트면 처음 고른 파일의 폴더를 작업 폴더로."""
    if model.path is None and not p.clips and not p.subtitle.file and not p.bgm.file:
        p.base_dir = Path(f).parent


def set_bgm_file(model: ProjectModel, f: str) -> None:
    model.edit(lambda p: (_auto_base(model, p, f), setattr(p.bgm, "file", _rel(p, f))))


SIZE_CARDS = [("▭ 가로 16:9", 1920, 1080), ("▯ 세로 쇼츠 9:16", 1080, 1920), ("□ 정사각 1:1", 1080, 1080)]


class OutputPage(QWidget):
    """📤 출력 탭: 영상 크기 · 화면 맞춤 · 기본 전환 · 자막 넣는 방식 · ▸ 고급 · 내보내기."""

    export_requested = Signal()
    voice_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        self._loading = False
        lay = QVBoxLayout(self)
        g_out = self.g_out = QGroupBox("영상 크기")
        fo = QFormLayout(g_out)
        cards = QHBoxLayout()
        self.size_cards = []
        grp = QButtonGroup(self)
        grp.setExclusive(False)
        for text, w, h in SIZE_CARDS:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setMinimumHeight(40)
            b.setToolTip(f"{w}×{h}")
            b.clicked.connect(lambda _=False, w=w, h=h: self._set(None, lambda p: (setattr(p.output, "width", w),
                                                                              setattr(p.output, "height", h))))
            cards.addWidget(b)
            self.size_cards.append(b)
        fo.addRow(cards)
        self.preset = QComboBox()
        fill_size_combo(self.preset)
        self.fit = QComboBox()
        self.fit.addItem("꽉 채우기(잘라냄)", "cover"); self.fit.addItem("전체 보이기(여백)", "contain")
        fo.addRow("크기", self.preset)
        fo.addRow("화면 맞춤", self.fit)
        lay.addWidget(g_out)

        g_tr = QGroupBox("기본 전환 (새 경계 · ＋ 전환에 쓰임)")
        ft = QFormLayout(g_tr)
        self.tr_type = QComboBox()
        for t in TRANSITIONS:
            self.tr_type.addItem(TRANSITION_LABELS.get(t, t), t)
        self.tr_dur = ValueSlider(0, 3, 0.05, "{:.2f}초")
        self.btn_tr_all = QPushButton("모든 경계에 적용")
        self.btn_tr_all.setToolTip("클립마다 따로 정한 전환·없앤 전환을 지우고 모든 경계에 기본 전환을 씁니다 (Ctrl+Z 로 되돌리기)")
        ft.addRow("종류", self.tr_type)
        ft.addRow("길이", self.tr_dur)
        ft.addRow("", self.btn_tr_all)
        lay.addWidget(g_tr)

        g_sub = QGroupBox("자막 넣는 방식")
        fs = QVBoxLayout(g_sub)
        self.burn = QRadioButton("영상에 새기기 (어디서나 보임)")
        self.soft = QRadioButton("켜고 끄는 자막 (플레이어에서 끌 수 있음)")
        fs.addWidget(self.burn)
        fs.addWidget(self.soft)
        lay.addWidget(g_sub)

        self.adv = Collapsible("고급 — 크기 직접 · fps · 여백 색 · 볼륨 · 오프셋 · 녹음 보정", key="out_adv")
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setSingleStep(2)
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setSingleStep(2)
        self.fps = QDoubleSpinBox(); self.fps.setRange(1, 120); self.fps.setDecimals(3)
        self.bg = QPushButton()
        self.orig_vol = QDoubleSpinBox(); self.orig_vol.setRange(0, 4); self.orig_vol.setSingleStep(0.05)
        self.sub_offset = NudgeSpin(-3600, 3600)
        self.dub_vol = QDoubleSpinBox(); self.dub_vol.setRange(0, 4); self.dub_vol.setSingleStep(0.05)
        self.latency = QDoubleSpinBox(); self.latency.setRange(-2000, 2000); self.latency.setSuffix(" ms"); self.latency.setSingleStep(10)
        self.latency.setToolTip("녹음된 목소리가 늦게 들리면 값을 올리세요(그만큼 앞당겨 배치).")
        self.dub_folder = QLineEdit()
        row = QHBoxLayout(); row.addWidget(self.w); row.addWidget(QLabel("×")); row.addWidget(self.h)
        a = self.adv
        a.addRow("가로 × 세로", row)
        a.addRow("fps", self.fps)
        a.addRow("여백 색 (전체 보이기)", self.bg)
        a.addRow("원본 소리 전체 볼륨", self.orig_vol)
        a.addRow("자막 전체 오프셋", self.sub_offset)
        a.addRow("목소리 전체 볼륨", self.dub_vol)
        a.addRow("녹음 지연 보정", self.latency)
        a.addRow("테이크 폴더", self.dub_folder)
        lay.addWidget(self.adv)

        ex = QHBoxLayout()
        self.btn_export = QPushButton("📤 영상 내보내기 (Ctrl+E)")
        self.btn_export.setMinimumHeight(38)
        self.btn_export.setStyleSheet("QPushButton{background:#2e7d32;color:white;font-weight:bold}")
        self.btn_voice = QPushButton("목소리만 WAV…")
        ex.addWidget(self.btn_export, 1)
        ex.addWidget(self.btn_voice)
        lay.addLayout(ex)
        lay.addStretch(1)
        self._connect()
        model.changed.connect(self.refresh)
        self.refresh()

    def _set(self, key, fn):
        if not self._loading:
            self.model.edit(fn, merge_key=key)

    def _connect(self):
        o = lambda: self.model.project.output  # noqa: E731
        self.preset.currentIndexChanged.connect(self._preset)
        self.w.valueChanged.connect(lambda v: self._set("w", lambda p: setattr(p.output, "width", v - v % 2)))
        self.h.valueChanged.connect(lambda v: self._set("h", lambda p: setattr(p.output, "height", v - v % 2)))
        self.fps.valueChanged.connect(lambda v: self._set("fps", lambda p: setattr(p.output, "fps", v)))
        self.fit.currentIndexChanged.connect(lambda _: self._set("fit", lambda p: setattr(p.output, "fit", self.fit.currentData())))
        self.bg.clicked.connect(lambda: self._pick_color(o().background, lambda c: setattr(o(), "background", c)))
        self.orig_vol.valueChanged.connect(lambda v: self._set("ov", lambda p: setattr(p.output, "original_volume", v)))
        self.tr_type.currentIndexChanged.connect(lambda _: self._set("gt", lambda p: setattr(p.transition, "type", self.tr_type.currentData())))
        self.tr_dur.valueChanged.connect(lambda v: self._set("gtd", lambda p: setattr(p.transition, "duration", v)))
        self.btn_tr_all.clicked.connect(self.apply_transition_all)
        self.sub_offset.valueChanged.connect(lambda v: self._set("so", lambda p: setattr(p.subtitle, "offset", v)))
        self.burn.toggled.connect(lambda v: self._set("burn", lambda p: setattr(p.subtitle, "burn_in", bool(v))))
        self.dub_vol.valueChanged.connect(lambda v: self._set("dv", lambda p: setattr(p.dubbing, "volume", v)))
        self.latency.valueChanged.connect(lambda v: self._set("lat", lambda p: setattr(p.dubbing, "latency_ms", v)))
        self.dub_folder.editingFinished.connect(lambda: self._set("df", lambda p: setattr(p.dubbing, "folder", self.dub_folder.text() or "takes")))
        self.btn_export.clicked.connect(self.export_requested.emit)
        self.btn_voice.clicked.connect(self.voice_requested.emit)

    def apply_transition_all(self) -> int:
        from ..edit_ops import apply_default_transition_all
        if self.model.project.tracks.clips.locked:
            return 0
        n = {}
        self._set(None, lambda p: n.setdefault("n", apply_default_transition_all(p)))
        return n.get("n", 0)

    def _preset(self, i):
        if self._loading:
            return
        pw, ph = combo_size(self.preset, i)
        if pw:
            self._set(None, lambda p: (setattr(p.output, "width", pw), setattr(p.output, "height", ph)))

    def _pick_color(self, current, setter):
        c = QColorDialog.getColor(QColor(current), self, "색 선택")
        if c.isValid():
            self._set(None, lambda p: setter(c.name().upper()))

    @staticmethod
    def _swatch(btn: QPushButton, color: str):
        btn.setText(color)
        btn.setStyleSheet(f"background:{color}; color:{'#000' if QColor(color).lightness() > 128 else '#fff'};")

    def refresh(self):
        self._loading = True
        p = self.model.project
        o = p.output
        self.w.setValue(o.width); self.h.setValue(o.height); self.fps.setValue(o.fps)
        sync_size_combo(self.preset, o.width, o.height)
        for b, (_, w, h) in zip(self.size_cards, SIZE_CARDS):
            b.setChecked(abs(o.width / o.height - w / h) < 0.01)
        self.fit.setCurrentIndex(max(0, self.fit.findData(o.fit)))
        self._swatch(self.bg, o.background)
        self.orig_vol.setValue(o.original_volume)
        self.tr_type.setCurrentIndex(max(0, self.tr_type.findData(p.transition.type)))
        self.tr_dur.setValue(p.transition.duration)
        self.btn_tr_all.setEnabled(len(p.clips) > 1 and not p.tracks.clips.locked)
        self.burn.setChecked(p.subtitle.burn_in)
        self.soft.setChecked(not p.subtitle.burn_in)
        self.sub_offset.setValue(p.subtitle.offset)
        self.dub_vol.setValue(p.dubbing.volume); self.latency.setValue(p.dubbing.latency_ms)
        self.dub_folder.setText(p.dubbing.folder)
        self._loading = False


class Inspector(QWidget):
    """'🛠 속성' 탭: 타임라인에서 고른 항목만 편집합니다."""

    style_requested = Signal()  # '🅰 자막' 탭 열기

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.stack = QStackedWidget()
        self.empty = EmptyPage(model)
        self.clip_page = ClipPage(model)
        self.sub_page = SubtitlePage(model)
        self.take_page = TakePage(model)
        self.tr_page = TransitionPage(model)
        self.bgm_page = BgmPage(model)
        self.pages = [self.clip_page, self.sub_page, self.take_page, self.tr_page, self.bgm_page]
        for w in [self.empty] + self.pages:
            self.stack.addWidget(w)
        for pg in self.pages:  # '숫자로 정확히' 펼침은 모든 칸이 함께(그리고 기억)
            pg.num.toggled.connect(lambda on, src=pg: self._sync_num(src, on))
        self.sub_page.style_requested.connect(self.style_requested.emit)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lock_banner = QLabel("🔒 잠긴 트랙의 항목입니다 — 타임라인 왼쪽 트랙 머리의 🔒 를 눌러 풀면 고칠 수 있습니다.")
        self.lock_banner.setWordWrap(True)
        self.lock_banner.setStyleSheet("background:#3b4250; color:#e8eaed; padding:6px; border-radius:4px;")
        self.lock_banner.hide()
        lay.addWidget(self.lock_banner)
        lay.addWidget(_scroll(self.stack))
        # 다른 탭에 들어가는 칸들
        self.output_page = OutputPage(model)
        self.sub_file_panel = SubtitleFilePanel(model)
        model.selection_changed.connect(self._on_selection)
        model.changed.connect(self._refresh_current)

    def _sync_num(self, src, on: bool):
        for pg in self.pages:
            if pg is not src and pg.num.is_open() != on:
                pg.num.set_open(on, remember=False)

    def page_for(self, kind: str):
        return {"clip": self.clip_page, "subtitle": self.sub_page, "take": self.take_page,
                "transition": self.tr_page, "bgm": self.bgm_page}.get(kind)

    def _on_selection(self):
        k = self.model.selection.kind
        page = self.page_for(k)
        if k == "bgm" and not self.model.project.bgm.file:
            page = None
        self.stack.setCurrentWidget(page or self.empty)
        if page:
            page.refresh()
        self._apply_lock()

    def _refresh_current(self):
        w = self.stack.currentWidget()
        if w is self.bgm_page and not self.model.project.bgm.file:
            self.stack.setCurrentWidget(self.empty)
            w = self.empty
        if hasattr(w, "refresh"):
            w.refresh()
        self._apply_lock()

    def _apply_lock(self):
        from ..tracks import locked
        w = self.stack.currentWidget()
        k = self.model.selection.kind
        is_locked = w is not self.empty and k in ("clip", "subtitle", "take", "transition", "bgm") and locked(self.model.project, k)
        if w is not self.empty:
            w.setEnabled(not is_locked)
        self.lock_banner.setVisible(is_locked)


def _scroll(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidget(w)
    s.setWidgetResizable(True)
    s.setFrameShape(QScrollArea.NoFrame)
    return s
