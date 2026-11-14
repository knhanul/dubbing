"""속성 패널: 선택한 클립/자막/더빙/BGM 과 출력 설정을 숫자로 미세 조정합니다."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from ..schema import (EASINGS, FITS, MOTION_LABELS, MOTIONS, TRANSITION_LABELS, TRANSITIONS, Clip, Transition,
                      guess_type)
from ..subtitles import detach_takes, ensure_items, has_subtitles, to_srt
from .model import ProjectModel
from .widgets import NudgeSpin


def _rel(project, path: str) -> str:
    import os

    try:
        return Path(os.path.relpath(path, project.base_dir)).as_posix()
    except ValueError:
        return path


class _Page(QWidget):
    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        self._loading = False
        self.form = QFormLayout(self)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

    # 위젯 값 → 프로젝트 반영 헬퍼
    def _set(self, key: str, fn: Callable):
        if self._loading:
            return
        self.model.edit(fn, merge_key=key)

    def combo(self, items: list[tuple[str, str]]) -> QComboBox:
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


class ClipPage(_Page):
    def __init__(self, model):
        super().__init__(model)
        self.lbl_file = QLabel()
        self.lbl_file.setWordWrap(True)
        self.lbl_info = QLabel()
        self.ed_label = QLineEdit()
        self.duration = NudgeSpin(0.1, 3600)
        self.chk_fit = QCheckBox("목표 길이에 맞춰 속도 자동")
        self.fit_duration = NudgeSpin(0.1, 3600)
        self.speed = NudgeSpin(0.1, 16, steps=(0.1, 0.01), suffix=" 배")
        self.trim_start = NudgeSpin(0, 36000)
        self.chk_trim_end = QCheckBox("끝 자르기")
        self.trim_end = NudgeSpin(0.1, 36000)
        self.mute = QCheckBox("원본 소리 끄기")
        self.volume = self.dspin(0, 4, 0.05, 2, " 배")
        self.motion = self.combo([(m, MOTION_LABELS[m]) for m in MOTIONS])
        self.strength = self.dspin(0, 1, 0.01, 3)
        self.easing = self.combo([("ease", "부드럽게(가속/감속)"), ("linear", "일정한 속도")])
        self.fit = self.combo([(None, "출력 설정 따름"), ("cover", "꽉 채우기(잘라냄)"), ("contain", "전체 보이기(여백)")])
        self.chk_tr = QCheckBox("이 클립만 전환 따로 지정")
        self.tr_type = self.combo([(t, TRANSITION_LABELS.get(t, t)) for t in TRANSITIONS])
        self.tr_dur = NudgeSpin(0, 10)
        self.lbl_timing = QLabel()

        f = self.form
        f.addRow("파일", self.lbl_file)
        f.addRow("", self.lbl_info)
        f.addRow("이름표", self.ed_label)
        self.row_duration = ("표시 시간", self.duration)
        f.addRow("표시 시간", self.duration)
        f.addRow(self.chk_fit, self.fit_duration)
        f.addRow("재생 속도", self.speed)
        f.addRow("시작 자르기", self.trim_start)
        f.addRow(self.chk_trim_end, self.trim_end)
        f.addRow("", self.mute)
        f.addRow("원본 소리 크기", self.volume)
        f.addRow("움직임", self.motion)
        f.addRow("움직임 강도", self.strength)
        f.addRow("움직임 속도감", self.easing)
        f.addRow("화면 맞춤", self.fit)
        f.addRow(self.chk_tr, self.tr_type)
        f.addRow("전환 길이", self.tr_dur)
        f.addRow("계산 결과", self.lbl_timing)
        btns = QHBoxLayout()
        self.btn_left = QPushButton("◀ 앞으로")
        self.btn_right = QPushButton("뒤로 ▶")
        self.btn_dup = QPushButton("복제")
        self.btn_del = QPushButton("삭제")
        for b in (self.btn_left, self.btn_right, self.btn_dup, self.btn_del):
            btns.addWidget(b)
        f.addRow(btns)

        c = lambda: self.clip  # noqa: E731
        self.ed_label.editingFinished.connect(lambda: self._set("label", lambda p: setattr(c(), "label", self.ed_label.text())))
        self.duration.valueChanged.connect(lambda v: self._set("dur", lambda p: setattr(c(), "duration", v)))
        self.chk_fit.toggled.connect(self._fit_toggled)
        self.fit_duration.valueChanged.connect(lambda v: self._set("fitd", lambda p: setattr(c(), "fit_duration", v)))
        self.speed.valueChanged.connect(lambda v: self._set("speed", lambda p: setattr(c(), "speed", v)))
        self.trim_start.valueChanged.connect(lambda v: self._set("ts", lambda p: setattr(c(), "trim_start", v)))
        self.chk_trim_end.toggled.connect(self._trim_toggled)
        self.trim_end.valueChanged.connect(lambda v: self._set("te", lambda p: setattr(c(), "trim_end", v)))
        self.mute.toggled.connect(lambda v: self._set("mute", lambda p: setattr(c(), "mute", v)))
        self.volume.valueChanged.connect(lambda v: self._set("vol", lambda p: setattr(c(), "volume", v)))
        self.motion.currentIndexChanged.connect(
            lambda _: self._set("motion", lambda p: setattr(c(), "motion", self.motion.currentData())))
        self.strength.valueChanged.connect(lambda v: self._set("str", lambda p: setattr(c(), "motion_strength", v)))
        self.easing.currentIndexChanged.connect(
            lambda _: self._set("ease", lambda p: setattr(c(), "motion_easing", self.easing.currentData())))
        self.fit.currentIndexChanged.connect(lambda _: self._set("fit", lambda p: setattr(c(), "fit", self.fit.currentData())))
        self.chk_tr.toggled.connect(self._tr_toggled)
        self.tr_type.currentIndexChanged.connect(lambda _: self._tr_changed())
        self.tr_dur.valueChanged.connect(lambda _: self._tr_changed())
        self.btn_left.clicked.connect(lambda: self._move(-1))
        self.btn_right.clicked.connect(lambda: self._move(1))
        self.btn_dup.clicked.connect(self._dup)
        self.btn_del.clicked.connect(self._delete)

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
        self.lbl_file.setText(f"<b>{self.idx + 1}번</b> {c.file}")
        src = self.model.duration_of(c) if not img else None
        self.lbl_info.setText("이미지" if img else f"영상 · 원본 {src:.2f}초" if src else "영상")
        self.ed_label.setText(c.label)
        self.duration.setValue(c.duration or tm.duration)
        self.duration.setEnabled(img and not c.fit_duration)
        self.chk_fit.setChecked(bool(c.fit_duration))
        self.fit_duration.setValue(c.fit_duration or tm.duration)
        self.fit_duration.setEnabled(bool(c.fit_duration))
        for w in (self.speed, self.trim_start, self.chk_trim_end, self.trim_end, self.mute, self.volume):
            w.setEnabled(not img)
        self.speed.setEnabled(not img and not c.fit_duration)
        self.speed.setValue(c.speed)
        self.trim_start.setValue(c.trim_start)
        self.chk_trim_end.setChecked(c.trim_end is not None)
        self.trim_end.setValue(c.trim_end if c.trim_end is not None else (src or 0))
        self.trim_end.setEnabled(not img and c.trim_end is not None)
        self.mute.setChecked(c.mute)
        self.volume.setValue(c.volume)
        for w in (self.motion, self.strength, self.easing):
            w.setEnabled(img)
        self.set_combo(self.motion, c.motion)
        self.strength.setValue(c.motion_strength)
        self.set_combo(self.easing, c.motion_easing)
        self.set_combo(self.fit, c.fit)
        self.chk_tr.setChecked(c.transition is not None)
        self.chk_tr.setEnabled(self.idx > 0)
        tr = c.transition or self.model.project.transition
        self.set_combo(self.tr_type, tr.type)
        self.tr_dur.setValue(tr.duration)
        self.tr_type.setEnabled(c.transition is not None and self.idx > 0)
        self.tr_dur.setEnabled(c.transition is not None and self.idx > 0)
        tri = tm.transition_in
        self.lbl_timing.setText(
            f"시작 {tm.start:.2f}초 · 길이 {tm.duration:.3f}초 · 속도 ×{tm.speed:.3f}"
            + (f"\n전환 {tri.type} {tri.duration:.2f}초" if tri.duration else ""))
        self._loading = False

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

    def _tr_toggled(self, on):
        if self._loading:
            return
        g = self.model.project.transition
        self._set(None, lambda p: setattr(self.clip, "transition", Transition(g.type if g.type != "none" else "crossfade",
                                                                              g.duration or 0.5) if on else None))
        self.refresh()

    def _tr_changed(self):
        if self._loading or self.clip.transition is None:
            return
        self._set("tr", lambda p: setattr(self.clip, "transition",
                                          Transition(self.tr_type.currentData(), self.tr_dur.value())))

    def _move(self, d):
        i, j = self.idx, self.idx + d
        clips = self.model.project.clips
        if 0 <= j < len(clips):
            clips[i], clips[j] = clips[j], clips[i]
            self.model.selection.index = j
            self.model.commit()
            self.model.selection_changed.emit()

    def _dup(self):
        clips = self.model.project.clips
        clips.insert(self.idx + 1, copy.deepcopy(self.clip))
        self.model.commit()
        self.model.select("clip", self.idx + 1)

    def _delete(self):
        del self.model.project.clips[self.idx]
        self.model.commit()
        self.model.select("")


class SubtitlePage(_Page):
    def __init__(self, model):
        super().__init__(model)
        self.lbl = QLabel()
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(70)
        self.start = NudgeSpin(0, 36000)
        self.end = NudgeSpin(0, 36000)
        self.lbl_len = QLabel()
        self.btn_move_m = QPushButton("◀ 전체 -0.1초")
        self.btn_move_p = QPushButton("전체 +0.1초 ▶")
        self.btn_after = QPushButton("이 자막부터 끝까지 함께 이동…")
        self.btn_del = QPushButton("자막 삭제")
        f = self.form
        f.addRow("자막", self.lbl)
        f.addRow("내용", self.text)
        f.addRow("시작", self.start)
        f.addRow("끝", self.end)
        f.addRow("길이", self.lbl_len)
        row = QHBoxLayout()
        row.addWidget(self.btn_move_m)
        row.addWidget(self.btn_move_p)
        f.addRow("줄 이동", row)
        self.after = NudgeSpin(-3600, 3600)
        f.addRow("이후 자막 모두 이동", self.after)
        f.addRow(self.btn_del)
        f.addRow(QLabel("<small>※ 표시 값은 전역 오프셋 적용 전 시간입니다.</small>"))

        self.text.textChanged.connect(self._text_changed)
        self.start.valueChanged.connect(lambda v: self._set("s", lambda p: self._item_set(start=v)))
        self.end.valueChanged.connect(lambda v: self._set("e", lambda p: self._item_set(end=v)))
        self.btn_move_m.clicked.connect(lambda: self._shift(-0.1))
        self.btn_move_p.clicked.connect(lambda: self._shift(0.1))
        self._after_prev = 0.0
        self.after.valueChanged.connect(self._after_changed)
        self.btn_del.clicked.connect(self._delete)
        self.btn_after.hide()

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
        it = self.item()
        if it is None:
            return
        self._loading = True
        self.lbl.setText(f"<b>{it.index}번</b>")
        if self.text.toPlainText() != it.text:
            self.text.setPlainText(it.text)
        self.start.setValue(it.start)
        self.end.setValue(it.end)
        self.lbl_len.setText(f"{it.end - it.start:.3f} 초")
        self._after_prev = 0.0
        self.after.setValue(0.0)
        self._loading = False

    def _text_changed(self):
        if self._loading:
            return
        it = self.item()
        if it:
            self._set("txt", lambda p: setattr(it, "text", self.text.toPlainText()))

    def _shift(self, d):
        it = self.item()
        if it:
            d = max(d, -it.start)
            self._set("shift", lambda p: (setattr(it, "start", round(it.start + d, 3)),
                                          setattr(it, "end", round(it.end + d, 3))))
            self.refresh()

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
            detach_takes(self.model.project, {it.index})
            self.model.project.subtitle.items = [x for x in ensure_items(self.model.project) if x is not it]
            self.model.commit()
            self.model.select("")


class TakePage(_Page):
    play_requested = Signal(int, bool)  # take index, 영상과 함께

    def __init__(self, model):
        super().__init__(model)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        self.offset = NudgeSpin(-3600, 36000)
        self.lbl_pos = QLabel()
        self.volume = self.dspin(0, 4, 0.05, 2, " 배")
        self.btn_play = QPushButton("▶ 테이크만 듣기")
        self.btn_play_v = QPushButton("▶ 영상과 함께 듣기")
        self.btn_del = QPushButton("테이크 삭제")
        f = self.form
        f.addRow("테이크", self.lbl)
        f.addRow("오프셋", self.offset)
        f.addRow("타임라인 위치", self.lbl_pos)
        f.addRow("볼륨", self.volume)
        row = QHBoxLayout()
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_play_v)
        f.addRow(row)
        f.addRow(self.btn_del)
        f.addRow(QLabel("<small>오프셋은 자막 시작 기준입니다(음수 = 자막보다 먼저 시작).</small>"))
        self.offset.valueChanged.connect(lambda v: self._set("off", lambda p: setattr(self.take, "offset", v)))
        self.volume.valueChanged.connect(lambda v: self._set("tvol", lambda p: setattr(self.take, "volume", v)))
        self.btn_play.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, False))
        self.btn_play_v.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, True))
        self.btn_del.clicked.connect(self._delete)

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
        who = f"자막 {t.subtitle}번" if t.subtitle is not None else "연속 세션"
        self.lbl.setText(f"<b>{who}</b><br>{t.file}")
        self.offset.setValue(t.offset)
        self.lbl_pos.setText(f"{self.model.take_positions()[i]:.3f} 초")
        self.volume.setValue(t.volume)
        self._loading = False

    def _delete(self):
        del self.model.project.dubbing.takes[self.model.selection.index]
        self.model.commit()
        self.model.select("")


class ProjectPage(QWidget):
    """출력 / 전역 전환 / 자막 / 배경음악 / 더빙 설정."""

    remove_subtitles_requested = Signal()
    style_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        self._loading = False
        lay = QVBoxLayout(self)
        g_out = QGroupBox("출력")
        fo = QFormLayout(g_out)
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setSingleStep(2)
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setSingleStep(2)
        self.preset = QComboBox()
        for label, (pw, ph) in [("직접 입력", (0, 0)), ("1920×1080 가로", (1920, 1080)), ("1080×1920 세로(쇼츠)", (1080, 1920)),
                                ("1280×720", (1280, 720)), ("1080×1080 정사각", (1080, 1080)), ("3840×2160 4K", (3840, 2160))]:
            self.preset.addItem(label, (pw, ph))
        self.fps = QDoubleSpinBox(); self.fps.setRange(1, 120); self.fps.setDecimals(3)
        self.fit = QComboBox()
        self.fit.addItem("꽉 채우기(잘라냄)", "cover"); self.fit.addItem("전체 보이기(여백)", "contain")
        self.bg = QPushButton()
        self.orig_vol = QDoubleSpinBox(); self.orig_vol.setRange(0, 4); self.orig_vol.setSingleStep(0.05)
        self.out_file = QLineEdit()
        fo.addRow("크기 프리셋", self.preset)
        row = QHBoxLayout(); row.addWidget(self.w); row.addWidget(QLabel("×")); row.addWidget(self.h)
        fo.addRow("가로 × 세로", row)
        fo.addRow("fps", self.fps)
        fo.addRow("화면 맞춤", self.fit)
        fo.addRow("여백 색", self.bg)
        fo.addRow("원본 소리 전체 볼륨", self.orig_vol)
        fo.addRow("출력 파일 이름", self.out_file)
        lay.addWidget(g_out)

        g_tr = QGroupBox("기본 전환 (클립에서 따로 지정하지 않으면 사용)")
        ft = QFormLayout(g_tr)
        self.tr_type = QComboBox()
        for t in TRANSITIONS:
            self.tr_type.addItem(TRANSITION_LABELS.get(t, t), t)
        self.tr_dur = NudgeSpin(0, 10)
        ft.addRow("종류", self.tr_type)
        ft.addRow("길이", self.tr_dur)
        lay.addWidget(g_tr)

        g_sub = QGroupBox("자막")
        fs = QFormLayout(g_sub)
        self.sub_file = QLabel("-")
        self.btn_sub = QPushButton("자막 파일 선택…")
        self.btn_sub_clear = QPushButton("자막 전체 제거")
        self.btn_sub_clear.setToolTip("프로젝트에서 자막을 모두 뺍니다. 원본 SRT 파일은 지워지지 않습니다.")
        self.btn_sub_export = QPushButton("수정한 자막을 SRT 로 저장…")
        self.sub_offset = NudgeSpin(-3600, 3600)
        self.burn = QCheckBox("영상에 자막 입히기(끄면 켜고 끌 수 있는 소프트 자막)")
        self.btn_style = QPushButton("🅰 자막 스타일 (글꼴·색·위치·효과)…")
        self.btn_style.setToolTip("오른쪽 '🅰 자막 스타일' 탭으로 이동합니다")
        r = QHBoxLayout(); r.addWidget(self.sub_file, 1); r.addWidget(self.btn_sub); r.addWidget(self.btn_sub_clear)
        fs.addRow("파일", r)
        fs.addRow("", self.btn_sub_export)
        fs.addRow("전체 오프셋", self.sub_offset)
        fs.addRow("", self.burn)
        fs.addRow("스타일", self.btn_style)
        lay.addWidget(g_sub)

        g_bgm = QGroupBox("배경음악")
        fb = QFormLayout(g_bgm)
        self.bgm_file = QLabel("-")
        self.btn_bgm = QPushButton("음악 선택…")
        self.btn_bgm_clear = QPushButton("빼기")
        self.bgm_vol = QDoubleSpinBox(); self.bgm_vol.setRange(0, 4); self.bgm_vol.setSingleStep(0.05)
        self.bgm_start = NudgeSpin(0, 36000)
        self.bgm_fstart = NudgeSpin(0, 36000)
        self.fade_in = NudgeSpin(0, 60)
        self.fade_out = NudgeSpin(0, 60)
        self.loop = QCheckBox("반복 재생")
        self.duck = QCheckBox("목소리 나올 때 자동으로 줄이기(덕킹)")
        self.duck_ratio = QDoubleSpinBox(); self.duck_ratio.setRange(1, 20)
        r3 = QHBoxLayout(); r3.addWidget(self.bgm_file, 1); r3.addWidget(self.btn_bgm); r3.addWidget(self.btn_bgm_clear)
        fb.addRow("파일", r3)
        fb.addRow("볼륨", self.bgm_vol)
        fb.addRow("타임라인 시작", self.bgm_start)
        fb.addRow("음악 파일 안 시작 위치", self.bgm_fstart)
        fb.addRow("페이드 인", self.fade_in)
        fb.addRow("페이드 아웃", self.fade_out)
        fb.addRow("", self.loop)
        fb.addRow("", self.duck)
        fb.addRow("덕킹 강도(비율)", self.duck_ratio)
        lay.addWidget(g_bgm)

        g_dub = QGroupBox("더빙")
        fd = QFormLayout(g_dub)
        self.dub_vol = QDoubleSpinBox(); self.dub_vol.setRange(0, 4); self.dub_vol.setSingleStep(0.05)
        self.latency = QDoubleSpinBox(); self.latency.setRange(-2000, 2000); self.latency.setSuffix(" ms"); self.latency.setSingleStep(10)
        self.latency.setToolTip("녹음된 목소리가 늦게 들리면 값을 올리세요(그만큼 앞당겨 배치).")
        self.dub_folder = QLineEdit()
        fd.addRow("목소리 볼륨", self.dub_vol)
        fd.addRow("녹음 지연 보정", self.latency)
        fd.addRow("테이크 폴더", self.dub_folder)
        lay.addWidget(g_dub)
        lay.addStretch(1)

        self._connect()
        model.changed.connect(self.refresh)
        self.refresh()

    def _set(self, key, fn):
        if not self._loading:
            self.model.edit(fn, merge_key=key)

    def _connect(self):
        P = lambda: self.model.project  # noqa: E731
        o = lambda: P().output  # noqa: E731
        self.preset.currentIndexChanged.connect(self._preset)
        self.w.valueChanged.connect(lambda v: self._set("w", lambda p: setattr(p.output, "width", v - v % 2)))
        self.h.valueChanged.connect(lambda v: self._set("h", lambda p: setattr(p.output, "height", v - v % 2)))
        self.fps.valueChanged.connect(lambda v: self._set("fps", lambda p: setattr(p.output, "fps", v)))
        self.fit.currentIndexChanged.connect(lambda _: self._set("fit", lambda p: setattr(p.output, "fit", self.fit.currentData())))
        self.bg.clicked.connect(lambda: self._pick_color(o().background, lambda c: setattr(o(), "background", c)))
        self.orig_vol.valueChanged.connect(lambda v: self._set("ov", lambda p: setattr(p.output, "original_volume", v)))
        self.out_file.editingFinished.connect(lambda: self._set("of", lambda p: setattr(p.output, "file", self.out_file.text() or "output.mp4")))
        self.tr_type.currentIndexChanged.connect(lambda _: self._set("gt", lambda p: setattr(p.transition, "type", self.tr_type.currentData())))
        self.tr_dur.valueChanged.connect(lambda v: self._set("gtd", lambda p: setattr(p.transition, "duration", v)))
        s = lambda: P().subtitle  # noqa: E731
        self.btn_sub.clicked.connect(self._pick_sub)
        self.btn_sub_clear.clicked.connect(self.remove_subtitles_requested.emit)
        self.btn_sub_export.clicked.connect(self._export_sub)
        self.sub_offset.valueChanged.connect(lambda v: self._set("so", lambda p: setattr(p.subtitle, "offset", v)))
        self.burn.toggled.connect(lambda v: self._set("burn", lambda p: setattr(p.subtitle, "burn_in", v)))
        self.btn_style.clicked.connect(self.style_requested.emit)
        b = lambda: P().bgm  # noqa: E731
        self.btn_bgm.clicked.connect(self._pick_bgm)
        self.btn_bgm_clear.clicked.connect(lambda: self._set(None, lambda p: setattr(p.bgm, "file", None)))
        self.bgm_vol.valueChanged.connect(lambda v: self._set("bv", lambda p: setattr(p.bgm, "volume", v)))
        self.bgm_start.valueChanged.connect(lambda v: self._set("bs", lambda p: setattr(p.bgm, "start_offset", v)))
        self.bgm_fstart.valueChanged.connect(lambda v: self._set("bfs", lambda p: setattr(p.bgm, "file_start", v)))
        self.fade_in.valueChanged.connect(lambda v: self._set("fi", lambda p: setattr(p.bgm, "fade_in", v)))
        self.fade_out.valueChanged.connect(lambda v: self._set("fo", lambda p: setattr(p.bgm, "fade_out", v)))
        self.loop.toggled.connect(lambda v: self._set("loop", lambda p: setattr(p.bgm, "loop", v)))
        self.duck.toggled.connect(lambda v: self._set("duck", lambda p: setattr(p.bgm, "duck_under_voice", v)))
        self.duck_ratio.valueChanged.connect(lambda v: self._set("dr", lambda p: setattr(p.bgm, "duck_ratio", v)))
        self.dub_vol.valueChanged.connect(lambda v: self._set("dv", lambda p: setattr(p.dubbing, "volume", v)))
        self.latency.valueChanged.connect(lambda v: self._set("lat", lambda p: setattr(p.dubbing, "latency_ms", v)))
        self.dub_folder.editingFinished.connect(lambda: self._set("df", lambda p: setattr(p.dubbing, "folder", self.dub_folder.text() or "takes")))
        _ = b

    def _preset(self, i):
        if self._loading:
            return
        pw, ph = self.preset.itemData(i)
        if pw:
            self._set(None, lambda p: (setattr(p.output, "width", pw), setattr(p.output, "height", ph)))

    def _pick_color(self, current, setter):
        c = QColorDialog.getColor(QColor(current), self, "색 선택")
        if c.isValid():
            self._set(None, lambda p: setter(c.name().upper()))

    def _pick_sub(self):
        f, _ = QFileDialog.getOpenFileName(self, "자막 파일", str(self.model.project.base_dir), "자막 (*.srt *.vtt)")
        if f:
            def fn(p):
                self._auto_base(p, f)
                p.subtitle.file = _rel(p, f)
                p.subtitle.items = None
            self._set(None, fn)

    def _export_sub(self):
        f, _ = QFileDialog.getSaveFileName(self, "SRT 저장", str(self.model.project.base_dir / "subtitle_edited.srt"), "SRT (*.srt)")
        if f:
            Path(f).write_text(to_srt(self.model.cues), encoding="utf-8")

    def _pick_bgm(self):
        f, _ = QFileDialog.getOpenFileName(self, "배경음악", str(self.model.project.base_dir),
                                           "오디오 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus)")
        if f:
            self._set(None, lambda p: (self._auto_base(p, f), setattr(p.bgm, "file", _rel(p, f))))

    def _auto_base(self, p, f):
        """저장한 적 없고 아무것도 없는 '제목 없음' 프로젝트면 처음 고른 파일의 폴더를 작업 폴더로."""
        if self.model.path is None and not p.clips and not p.subtitle.file and not p.bgm.file:
            p.base_dir = Path(f).parent

    @staticmethod
    def _swatch(btn: QPushButton, color: str):
        btn.setText(color)
        btn.setStyleSheet(f"background:{color}; color:{'#000' if QColor(color).lightness() > 128 else '#fff'};")

    def refresh(self):
        self._loading = True
        p = self.model.project
        o = p.output
        self.w.setValue(o.width); self.h.setValue(o.height); self.fps.setValue(o.fps)
        self.preset.setCurrentIndex(max(0, self.preset.findData((o.width, o.height))))
        self.fit.setCurrentIndex(max(0, self.fit.findData(o.fit)))
        self._swatch(self.bg, o.background)
        self.orig_vol.setValue(o.original_volume)
        self.out_file.setText(o.file)
        self.tr_type.setCurrentIndex(max(0, self.tr_type.findData(p.transition.type)))
        self.tr_dur.setValue(p.transition.duration)
        st = p.subtitle
        self.sub_file.setText((st.file or ("-" if not st.items else "(파일 없음)")) + (" (미세 조정됨)" if st.items is not None else ""))
        self.btn_sub_clear.setEnabled(has_subtitles(p))
        self.btn_sub_export.setEnabled(has_subtitles(p))
        self.sub_offset.setValue(st.offset)
        self.burn.setChecked(st.burn_in)
        b = p.bgm
        self.bgm_file.setText(b.file or "-")
        self.bgm_vol.setValue(b.volume); self.bgm_start.setValue(b.start_offset); self.bgm_fstart.setValue(b.file_start)
        self.fade_in.setValue(b.fade_in); self.fade_out.setValue(b.fade_out); self.loop.setChecked(b.loop)
        self.duck.setChecked(b.duck_under_voice); self.duck_ratio.setValue(b.duck_ratio)
        self.dub_vol.setValue(p.dubbing.volume); self.latency.setValue(p.dubbing.latency_ms)
        self.dub_folder.setText(p.dubbing.folder)
        self._loading = False


class Inspector(QTabWidget):
    """'선택 항목' 탭 + '프로젝트 설정' 탭."""

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.stack = QStackedWidget()
        self.empty = QLabel("타임라인에서 클립·자막·더빙을 클릭하면\n여기서 0.1초 단위로 미세 조정할 수 있습니다.\n\n"
                            "끌어서 옮길 때 Shift = 0.01초 단위")
        self.empty.setAlignment(Qt.AlignCenter)
        self.clip_page = ClipPage(model)
        self.sub_page = SubtitlePage(model)
        self.take_page = TakePage(model)
        for w in (self.empty, self.clip_page, self.sub_page, self.take_page):
            self.stack.addWidget(w)
        self.addTab(_scroll(self.stack), "선택 항목")
        self.project_page = ProjectPage(model)
        self.addTab(_scroll(self.project_page), "프로젝트 설정")
        model.selection_changed.connect(self._on_selection)
        model.changed.connect(self._refresh_current)

    def _on_selection(self):
        k = self.model.selection.kind
        page = {"clip": self.clip_page, "subtitle": self.sub_page, "take": self.take_page}.get(k)
        if k == "bgm":
            self.setCurrentIndex(1)
            return
        self.stack.setCurrentWidget(page or self.empty)
        if page:
            page.refresh()
            self.setCurrentIndex(0)

    def _refresh_current(self):
        w = self.stack.currentWidget()
        if hasattr(w, "refresh"):
            w.refresh()


def _scroll(w: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidget(w)
    s.setWidgetResizable(True)
    return s
