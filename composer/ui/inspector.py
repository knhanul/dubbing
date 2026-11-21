"""🛠 선택 패널: 타임라인에서 고른 것(클립·전환·자막·더빙·배경음악)만 보여 줍니다.

* 최소 폭 440px 기준 — 짧은 칸은 한 줄에 2개씩, 섹션은 작은 제목 + 가는 줄, 지우기·복제는 머리줄의 작은 아이콘
* 숫자 칸은 접지 않고 바로 보임 (휠/화살표 = 0.1초, Shift = 0.01초)
* 아무것도 안 골랐으면 '시작하기' 단계표 (넣기 → 자막 → 녹음 → 음악 → 내보내기, 지금 할 일 강조)
* 더빙 조각: 녹음본 고르기(이름 · ★ · 영상과 함께 듣기)가 맨 위
* OutputPage(📤 내보내기 탭)와 SubtitleFilePanel(🅰 자막 탭 맨 위)도 여기서 만듦
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from ..schema import MOTION_LABELS, TRANSITION_LABELS, TRANSITIONS, Clip, Transition
from ..subtitle_style import describe_override
from ..subtitles import detach_takes, ensure_items, has_subtitles, to_srt
from .model import ProjectModel
from .size_presets import combo_size, fill_size_combo, sync_size_combo
from .widgets import (DIM, Collapsible, Columns, Footer, NudgeSpin, Section, Segmented, ValueSlider, field, hrow,
                      icon_button, link_button, shrink)
from .inspector_base import _Page, SPEEDS, _rel, PRIMARY_CSS, REC_TEXT_CSS
from .clip_page import ClipPage

class TransitionPage(_Page):
    """타임라인의 전환 블록을 골랐을 때: 종류·길이·없애기."""

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.lbl_info = self.hint
        self.btn_del = self.head_button("🗑", self.remove, "전환 없애기 (Delete)")
        self.tr_type = self.combo([(t, TRANSITION_LABELS.get(t, t)) for t in TRANSITIONS if t != "none"])
        shrink(self.tr_type)
        self.tr_slider = ValueSlider(0.05, 3.0, 0.05, "{:.2f}초")
        self.tr_dur = NudgeSpin(0.05, 10)
        s = self.section("장면 바뀔 때")
        s.row(field("방식", self.tr_type, label_w=36), field("시간", self.tr_dur, label_w=36))
        s.add(field("길이", self.tr_slider, label_w=36))
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
        self.head.setText(f"전환 · {i}번 → {i + 1}번 클립")
        self.set_combo(self.tr_type, got.type)
        self.tr_dur.setValue(want.duration)
        self.tr_slider.setValue(want.duration)
        note = (f"실제 {got.duration:.2f}초 — 앞뒤 클립 중 짧은 쪽 길이까지만 됩니다. " if want.duration > got.duration + 1e-3
                else "")
        self.hint.setText(f"경계 {tl[i].start:.2f}초 · " + note + "타임라인에서 블록 양 끝을 끌어도 길이가 바뀝니다")
        self._loading = False


class SubtitlePage(_Page):
    style_requested = Signal(str)  # "selected" = 고른 자막만 꾸미기, "all" = 모든 자막 모양

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.btn_del = self.head_button("🗑", self._delete, "자막 삭제 (Delete) — 연결된 더빙은 남습니다")
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(64)
        self.text.setPlaceholderText("자막 글 (줄바꿈 가능)")
        s = self.section("내용")
        self.btn_style = s.add_link("🅰 모든 자막 모양 ›", lambda: self.style_requested.emit("all"),
                                    "모든 자막의 글꼴·크기·색·위치는 🅰 자막 탭에서")
        s.add(self.text)
        # 이 자막만 다른 모양 (색·크기·위치·효과 …)
        s = self.section("이 자막만 다른 모양")
        self.look_info = QLabel()
        self.look_info.setWordWrap(True)
        self.look_info.setStyleSheet("font-size:11px;")
        s.add(self.look_info)
        self.btn_item_style = QPushButton("✨ 이 자막만 꾸미기…")
        self.btn_item_style.setToolTip("🅰 자막 탭을 '고른 자막만'으로 엽니다 — 바꾼 칸만 이 자막에 적용되고, 나머지는 전체 모양을 따릅니다")
        self.btn_item_style.clicked.connect(lambda: self.style_requested.emit("selected"))
        self.btn_item_reset = QPushButton("전체 모양으로")
        self.btn_item_reset.setToolTip("이 자막만 다르게 한 모양을 지우고 모든 자막과 같은 모양으로 (Ctrl+Z 로 되돌리기)")
        self.btn_item_reset.clicked.connect(self._clear_style)
        s.row(self.btn_item_style, self.btn_item_reset)
        self.start = NudgeSpin(0, 36000)
        self.end = NudgeSpin(0, 36000)
        self.after = NudgeSpin(-3600, 3600)
        self.after.setToolTip("이 자막과 그 뒤 자막을 모두 함께 앞당기거나(−) 늦춥니다(+)")
        s = self.section("시간", col=1)
        s.row(field("시작", self.start, label_w=30), field("끝", self.end, label_w=20))
        s.add(field("뒤 자막도 함께 옮기기", self.after, label_w=120,
                    tip="이 자막과 그 뒤 자막을 모두 함께 앞당기거나(−) 늦춥니다(+)"))
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
        self.head.setText(f"{it.index}번 자막")
        self.hint.setText(f"{fmt_time(it.start)} ~ {fmt_time(it.end)} ({it.end - it.start:.2f}초) — 타임라인에서 끌어 옮기거나 "
                          "양 끝을 끌어 조절 ([ / ] 키 = 0.1초)"
                          + (" · 표시 값은 자막 전체 앞당기기 적용 전" if self.model.project.subtitle.offset else ""))
        if self.text.toPlainText() != it.text:
            self.text.setPlainText(it.text)
        self.start.setValue(it.start)
        self.end.setValue(it.end)
        n = self.model.selection.count
        idx = set(self.model.selection.indices)
        custom = any(x.style for x in self.model.project.subtitle.items or [] if x.index in idx)
        if it.style:
            self.look_info.setText("✨ 전체와 다른 칸: " + describe_override(it.style))
        else:
            self.look_info.setText("모든 자막과 같은 모양입니다." + (" (같이 고른 자막 중에는 따로 꾸민 것이 있음)" if custom else ""))
        self.btn_item_style.setText(f"✨ 고른 자막 {n}개만 꾸미기…" if n > 1 else "✨ 이 자막만 꾸미기…")
        self.btn_item_reset.setEnabled(custom)
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

    def _clear_style(self):
        idx = set(self.model.selection.indices)

        def fn(p):
            for x in ensure_items(p):
                if x.index in idx:
                    x.style = None
        self.model.edit(fn)  # 되돌리기 한 번

    def _delete(self):
        it = self.item()
        if it:
            def fn(p):
                detach_takes(p, {it.index})
                p.subtitle.items = [x for x in ensure_items(p) if x is not it]
            self.model.edit(fn)
            self.model.select("")


def history_title(n: int) -> str:
    return f"녹음본 {n}개 — 하나를 골라 쓰세요" if n > 1 else ("녹음본" if n else "녹음본 없음")


class TakePage(_Page):
    play_requested = Signal(int, bool)  # take index, 영상과 함께
    rerecord_requested = Signal(int)
    play_file_requested = Signal(str)  # 녹음본의 ▶ (파일 경로, 프로젝트 기준) — 메인 창이 영상과 함께 재생
    delete_file_requested = Signal(str)  # 녹음본의 🗑 파일 삭제 (확인은 메인 창에서)

    def __init__(self, model):
        super().__init__(model)
        self.lbl = self.head
        self.btn_del = self.head_button("🗑", self._delete, "이 더빙 조각을 타임라인에서 빼기 (Delete) — 녹음 파일은 남습니다")
        self.btn_play = QPushButton("▶ 듣기")
        self.btn_play.setToolTip("목소리만 듣기")
        self.btn_play_v = QPushButton("▶ 영상과 함께")
        self.btn_play_v.setToolTip("그 장면을 영상과 함께 재생 — 입 모양·타이밍 확인")
        self.btn_play_v.setStyleSheet(PRIMARY_CSS)
        self.btn_rerec = QPushButton("● 다시 녹음")
        self.btn_rerec.setToolTip("같은 줄을 다시 녹음합니다. 지금 녹음본은 지워지지 않고 아래 목록에 남아요")
        self.btn_rerec.setStyleSheet(REC_TEXT_CSS)
        top = self.cols.add(hrow(self.btn_play, self.btn_play_v, self.btn_rerec, spacing=6))
        top.setFixedHeight(30)
        # 녹음본: 같은 자막을 다시 녹음한 파일들 (v1, v2 …) — ◉ 고르기 · ★ · ✎ 이름 · ▶ · 🗑
        self.history = self.section("녹음본")
        self.history_box = QWidget()
        self.history_lay = QVBoxLayout(self.history_box)
        self.history_lay.setContentsMargins(0, 0, 0, 0)
        self.history_lay.setSpacing(2)
        self.history.add(self.history_box)
        self.history_tip = QLabel("동그라미를 누르면 그 녹음본으로 바뀌어요 (위치·볼륨은 그대로). ▶ 는 영상과 함께, "
                                  "✎ 로 이름(예: 밝게, 민수), ★ 로 마음에 드는 것 표시")
        self.history_tip.setWordWrap(True)
        self.history_tip.setStyleSheet(DIM + "font-size:11px;")
        self.history.add(self.history_tip)
        self.history_rows: list[dict] = []
        # 위치 · 소리
        self.offset = NudgeSpin(-3600, 36000)
        self.lbl_rel = QLabel()
        self.lbl_rel.setStyleSheet(DIM)
        self.lbl_pos = QLabel()
        self.vol_slider = ValueSlider(0, 2, 0.05, "{:.0%}")
        s = self.section("위치 · 소리", col=1)
        self.f_offset = field("자막보다", self.offset, label_w=48,
                              tip="+ 는 자막보다 늦게, − 는 빨리 시작합니다. 타임라인에서 끌어 옮겨도 됩니다")
        s.row(self.f_offset, self.lbl_rel, stretches=[3, 1])
        s.row(field("시작", self.lbl_pos, label_w=48), None, stretches=[3, 1])
        s.add(field("볼륨", self.vol_slider, label_w=48))
        self.offset.valueChanged.connect(lambda v: self._set("off", lambda p: setattr(self.take, "offset", v)))
        self.vol_slider.valueChanged.connect(lambda v: self._set("tvol", lambda p: setattr(self.take, "volume", v)))
        self.btn_play.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, False))
        self.btn_play_v.clicked.connect(lambda: self.play_requested.emit(self.model.selection.index, True))
        self.btn_rerec.clicked.connect(lambda: self.rerecord_requested.emit(self.model.selection.index))
        self.ask_name = lambda cur: QInputDialog.getText(self, "녹음본 이름", "이름 (예: 밝게, 차분하게, 민수)", text=cur)

    @property
    def take(self):
        return self.model.project.dubbing.takes[self.model.selection.index]

    def refresh(self):
        from .preview import fmt_time
        takes = self.model.project.dubbing.takes
        i = self.model.selection.index
        if not (0 <= i < len(takes)):
            return
        self._loading = True
        t = takes[i]
        pos = self.model.take_positions()[i]
        if t.subtitle is not None:
            cue = self.model.cue_by_index(t.subtitle)
            self.head.setText(f"{t.subtitle}번 자막의 목소리")
            words = cue.text.replace("\n", " ") if cue else ""
            self.hint.setText((f"\"{words[:40]}{'…' if len(words) > 40 else ''}\" · " if words else "") + Path(t.file).name)
            self.f_offset.label.setText("자막보다")
        else:
            self.head.setText("자유 녹음")
            self.hint.setText(Path(t.file).name)
            self.f_offset.label.setText("위치")
        from ..edit_ops import take_overlaps
        if i in take_overlaps(self.model.project, self.model.take_file_len)[1]:
            self.hint.setText(self.hint.text() + "\n⚠ 다른 더빙 조각과 겹칩니다 — 겹친 곳은 두 소리가 동시에 들립니다(빨간 빗금). "
                              "끌어서 놓으면 옆 끝에 붙습니다")
        self.offset.setValue(t.offset)
        if t.subtitle is not None:
            self.lbl_rel.setText("늦게" if t.offset > 0.0005 else "빨리" if t.offset < -0.0005 else "같이")
        else:
            self.lbl_rel.setText("")
        self.lbl_pos.setText(f"{fmt_time(pos + (t.trim_start or 0))}  (타임라인)")
        self.vol_slider.setValue(t.volume)
        self._fill_history(t)
        self._loading = False

    def _fill_history(self, take):
        import time
        from ..dubbing.takes import take_note, take_versions
        while self.history_lay.count():
            it = self.history_lay.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self.history_rows = []
        vers = take_versions(self.model.project, take)
        self.history.set_title(history_title(len(vers)))
        self.history_tip.setVisible(len(vers) > 1)
        if not vers:
            lab = QLabel("녹음 파일을 찾을 수 없습니다.")
            lab.setStyleSheet(DIM)
            self.history_lay.addWidget(lab)
            return
        for v in vers:
            note = take_note(self.model.project, v.rel)
            row = QWidget()
            row.setObjectName("verRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            if v.current:
                row.setStyleSheet("QWidget#verRow{background:#24344a;border:1px solid #3d5f8f;border-radius:4px}")
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 1, 2, 1)
            h.setSpacing(4)
            use = QRadioButton()
            use.setAutoExclusive(False)
            use.setChecked(v.current)
            use.setEnabled(not v.current)
            use.setToolTip("지금 쓰는 녹음본" if v.current else "이 녹음본으로 바꾸기 — 위치·볼륨·자르기 구간은 그대로 (Ctrl+Z 로 되돌리기)")
            ver = f"v{v.version}" if v.version else Path(v.rel).stem
            when = time.strftime("%m-%d %H:%M", time.localtime(v.mtime)) if v.mtime else "-"
            name = note["name"]
            lab = QLabel(f"{'✔ ' if v.current else ''}{ver}{' · ' + name if name else ''} · {when} · {v.length:.1f}초"
                         + (" · 지금 사용" if v.current else ""))
            lab.setToolTip(lab.text() + "\n" + v.rel)
            lab.setStyleSheet("font-weight:bold;color:#e8eaed;" if v.current else "")
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # 긴 이름은 잘리고 풍선 도움말로
            lab.setMinimumWidth(60)
            b_star = icon_button("★" if note["star"] else "☆", "마음에 드는 녹음본 표시", w=24)
            b_star.setCheckable(True)
            b_star.setChecked(note["star"])
            if note["star"]:
                b_star.setStyleSheet(b_star.styleSheet() + "QToolButton{color:#ffd54f}")
            b_name = icon_button("✎", "이름 붙이기 (예: 밝게, 차분하게, 민수)", w=24)
            b_play = icon_button("▶", "이 녹음본을 영상과 함께 듣기", w=24)
            b_del = icon_button("🗑", "지금 쓰는 녹음은 지울 수 없습니다" if v.current
                                else "이 녹음 파일을 지웁니다 (확인 후, 되돌릴 수 없음)", w=24)
            b_del.setEnabled(not v.current)
            use.clicked.connect(lambda _=False, vv=v: self.use_version(vv))
            b_star.clicked.connect(lambda on, r=v.rel: self.set_star(r, on))
            b_name.clicked.connect(lambda _=False, r=v.rel: self.rename_version(r))
            b_play.clicked.connect(lambda _=False, r=v.rel: self.play_file_requested.emit(r))
            b_del.clicked.connect(lambda _=False, r=v.rel: self.delete_file_requested.emit(r))
            h.addWidget(use)
            h.addWidget(lab, 1)
            for b in (b_star, b_name, b_play, b_del):
                h.addWidget(b)
            self.history_lay.addWidget(row)
            self.history_rows.append({"version": v, "label": lab, "play": b_play, "use": use, "delete": b_del,
                                      "star": b_star, "rename": b_name})

    def _can_edit(self) -> bool:
        i = self.model.selection.index
        return 0 <= i < len(self.model.project.dubbing.takes) and not self.model.project.tracks.dubbing.locked

    def use_version(self, v) -> bool:
        from ..dubbing.takes import use_version
        if not self._can_edit():
            return False
        i = self.model.selection.index
        if v.current:
            return False
        self.model.edit(lambda p: use_version(p.dubbing.takes[i], v.rel, v.length))
        return True

    def set_star(self, rel: str, on: bool) -> bool:
        from ..dubbing.takes import set_take_note
        if not self._can_edit():
            return False
        self.model.edit(lambda p: set_take_note(p, rel, star=on))
        return True

    def rename_version(self, rel: str, name: str | None = None) -> bool:
        from ..dubbing.takes import set_take_note, take_note
        if not self._can_edit():
            return False
        if name is None:
            name, ok = self.ask_name(take_note(self.model.project, rel)["name"])
            if not ok:
                return False
        self.model.edit(lambda p: set_take_note(p, rel, name=name))
        return True

    def _delete(self):
        i = self.model.selection.index
        self.model.edit(lambda p: p.dubbing.takes.pop(i))
        self.model.select("")


class BgmPage(_Page):
    """배경음악 막대를 골랐을 때: 볼륨 · 서서히 커지기/작아지기 · 반복 · 목소리 나올 때 줄이기 · 위치."""

    remove_requested = Signal()
    pick_requested = Signal()

    def __init__(self, model):
        super().__init__(model)
        self.lbl_file = self.hint
        self.btn_bgm = QPushButton("바꾸기…")
        self.btn_bgm.setFixedHeight(24)
        self.btn_bgm.clicked.connect(self.pick_requested.emit)
        self.head_btns.addWidget(self.btn_bgm)
        self.btn_bgm_clear = QPushButton("🗑 배경음악 빼기 (Delete)")
        self.btn_bgm_clear.setToolTip("프로젝트에서 음악을 뺍니다. 원본 음악 파일은 그대로")
        self.btn_bgm_clear.clicked.connect(self.remove_requested.emit)
        self.vol_slider = ValueSlider(0, 2, 0.01, "{:.0%}")
        self.fade_in_slider = ValueSlider(0, 10, 0.1, "{:.1f}초")
        self.fade_out_slider = ValueSlider(0, 10, 0.1, "{:.1f}초")
        self.loop = QCheckBox("반복 재생")
        self.loop.setToolTip("영상보다 음악이 짧으면 처음부터 다시")
        self.duck = QCheckBox("목소리 나올 때 줄이기")
        self.duck.setToolTip("더빙 목소리가 나오는 동안 음악 소리를 자동으로 줄입니다")
        self.duck_ratio = QDoubleSpinBox()
        self.duck_ratio.setRange(1, 20)
        self.duck_ratio.setPrefix("강도 ")
        self.duck_ratio.setDecimals(1)
        self.duck_ratio.setMaximumWidth(90)
        self.duck_ratio.setToolTip("클수록 목소리 나올 때 음악이 더 많이 줄어듭니다")
        s = self.section("소리")
        s.add(field("볼륨", self.vol_slider, label_w=72))
        s.add(field("처음 커지기", self.fade_in_slider, label_w=72, tip="음악이 처음에 서서히 커지는 시간 (페이드 인)"))
        s.add(field("끝 작아지기", self.fade_out_slider, label_w=72, tip="음악이 끝에서 서서히 작아지는 시간 (페이드 아웃)"))
        s.row(self.loop, self.duck, self.duck_ratio, stretches=[0, 0, 1])
        self.bgm_start = NudgeSpin(0, 36000)
        self.bgm_fstart = NudgeSpin(0, 36000)
        s = self.section("위치", col=1)
        s.row(field("타임라인", self.bgm_start, label_w=52, tip="타임라인에서 음악이 시작하는 시각"),
              field("음악 안", self.bgm_fstart, label_w=44, tip="음악 파일의 몇 초부터 쓸지 (앞부분 건너뛰기)"))
        s.add(self.btn_bgm_clear)
        S = self._set
        self.vol_slider.valueChanged.connect(lambda v: S("bv", lambda p: setattr(p.bgm, "volume", v)))
        self.bgm_start.valueChanged.connect(lambda v: S("bs", lambda p: setattr(p.bgm, "start_offset", v)))
        self.bgm_fstart.valueChanged.connect(lambda v: S("bfs", lambda p: setattr(p.bgm, "file_start", v)))
        self.fade_in_slider.valueChanged.connect(lambda v: S("fi", lambda p: setattr(p.bgm, "fade_in", v)))
        self.fade_out_slider.valueChanged.connect(lambda v: S("fo", lambda p: setattr(p.bgm, "fade_out", v)))
        self.loop.toggled.connect(lambda v: S("loop", lambda p: setattr(p.bgm, "loop", v)))
        self.duck.toggled.connect(lambda v: S("duck", lambda p: setattr(p.bgm, "duck_under_voice", v)))
        self.duck_ratio.valueChanged.connect(lambda v: S("dr", lambda p: setattr(p.bgm, "duck_ratio", v)))

    def refresh(self):
        b = self.model.project.bgm
        self._loading = True
        self.head.setText("♪ 배경음악")
        self.hint.setText(f"{b.file or '없음'} · 타임라인 {b.start_offset:.2f}초부터 — 막대를 끌어 옮기기")
        self.btn_bgm.setToolTip(f"다른 음악 파일로 바꾸기 (지금: {b.file or '없음'})")
        self.vol_slider.setValue(b.volume)
        self.bgm_start.setValue(b.start_offset)
        self.bgm_fstart.setValue(b.file_start)
        self.fade_in_slider.setValue(b.fade_in)
        self.fade_out_slider.setValue(b.fade_out)
        self.loop.setChecked(b.loop)
        self.duck.setChecked(b.duck_under_voice)
        self.duck_ratio.setValue(b.duck_ratio)
        self.duck_ratio.setEnabled(b.duck_under_voice)
        self._loading = False


class _Step(QWidget):
    """시작하기 단계 한 줄: (번호/✓) 제목 · 설명 · [단추]."""

    def __init__(self, n: int, title: str):
        super().__init__()
        self.setObjectName("step")
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        self.n = n
        self.badge = QLabel(str(n))
        self.badge.setFixedSize(22, 22)
        self.badge.setAlignment(Qt.AlignCenter)
        v = QVBoxLayout()
        v.setSpacing(0)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight:bold;")
        self.detail = QLabel()
        self.detail.setStyleSheet(DIM + "font-size:11px;")
        self.detail.setWordWrap(True)
        v.addWidget(self.title)
        v.addWidget(self.detail)
        self.btn = QPushButton()
        self.btn.setFixedHeight(26)
        h.addWidget(self.badge)
        h.addLayout(v, 1)
        h.addWidget(self.btn)
        self.state = ""

    def set_state(self, state: str, detail: str, button: str):
        """state: done | current | todo"""
        self.state = state
        self.detail.setText(detail)
        self.btn.setText(button)
        if state == "done":
            self.badge.setText("✓")
            self.badge.setStyleSheet("background:#2f8a57;color:white;border-radius:11px;font-weight:bold;")
            self.setStyleSheet("QWidget#step{background:#2a2d34;border-radius:5px}")
            self.btn.setStyleSheet("")
        elif state == "current":
            self.badge.setText(str(self.n))
            self.badge.setStyleSheet("background:#2f6fbf;color:white;border-radius:11px;font-weight:bold;")
            self.setStyleSheet("QWidget#step{background:#26303d;border:1px solid #5b8fd8;border-radius:5px}")
            self.btn.setStyleSheet(PRIMARY_CSS)
        else:
            self.badge.setText(str(self.n))
            self.badge.setStyleSheet("background:#3a3f49;color:#9aa0a6;border-radius:11px;")
            self.setStyleSheet("QWidget#step{background:#2a2d34;border-radius:5px}")
            self.btn.setStyleSheet("")


class EmptyPage(QWidget):
    """아무것도 안 골랐을 때: '시작하기' 단계표 — 넣기 → 자막 → 녹음 → 배경음악(선택) → 내보내기, 지금 할 일 강조."""

    ai_requested = Signal()
    add_requested = Signal()
    bgm_requested = Signal()
    sub_requested = Signal()
    record_requested = Signal()
    export_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.title = QLabel("처음 사용하시나요?")
        self.title.setStyleSheet("font-size:15px;font-weight:bold;")
        self.summary = QLabel("세 단계만 따라 하면 됩니다.")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(DIM + "font-size:12px;")
        lay.addWidget(self.title)
        lay.addWidget(self.summary)
        # 핵심 3단계 강조
        guide = QLabel(
            "<b>1.</b> 사진·영상·배경음악을 끌어다 놓거나 넣기<br>"
            "<b>2.</b> <b>AI로 구성하기</b>로 클립과 자막을 한 번에 만들기<br>"
            "<b>3.</b> 자막을 보며 목소리 녹음 → 내보내기"
        )
        guide.setWordWrap(True)
        guide.setStyleSheet("font-size:12px;padding:6px 0;")
        lay.addWidget(guide)
        self.steps = {k: _Step(i + 1, t) for i, (k, t) in enumerate(
            [("media", "사진·영상·음악 넣기"), ("ai", "AI로 구성하기 (클립+자막)"),
             ("rec", "목소리 녹음하기"), ("export", "영상으로 내보내기")])}
        for s in self.steps.values():
            lay.addWidget(s)
        self.btn_add = self.steps["media"].btn
        self.btn_ai_step = self.steps["ai"].btn
        self.btn_rec = self.steps["rec"].btn
        self.btn_export = self.steps["export"].btn
        # 호환: 기존 연결이 btn_sub / btn_bgm 을 참조할 수 있음
        self.btn_sub = self.btn_ai_step
        self.btn_bgm = self.btn_add
        self.btn_ai = QPushButton("🤖 AI로 구성하기 (추천)")
        self.btn_ai.setToolTip("폴더의 파일로 프롬프트를 만들어 줍니다.\nAI에 붙여넣고 답변을 받으면 클립과 자막이 한 번에 만들어집니다.")
        self.btn_ai.setStyleSheet("font-weight:bold;padding:6px;")
        lay.addWidget(self.btn_ai)
        for b, sig in ((self.btn_ai, self.ai_requested), (self.btn_ai_step, self.ai_requested),
                       (self.btn_add, self.add_requested),
                       (self.btn_rec, self.record_requested), (self.btn_export, self.export_requested)):
            b.clicked.connect(sig.emit)
        msg = QLabel("타임라인에서 <b>클립 · 전환 · 자막 · 더빙 · 배경음악</b>을 누르면 그 설정이 여기에 나옵니다.<br>"
                     "<small>끌어서 옮기기 · 끝을 끌어 길이 · Shift = 자석 끄고 0.01초</small>")
        msg.setWordWrap(True)
        msg.setStyleSheet(DIM)
        lay.addWidget(msg)
        lay.addStretch(1)
        model.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        from .preview import fmt_time
        from ..dubbing.takes import find_take
        p = self.model.project
        cues = self.model.cues
        n_sub = len(cues)
        n_rec = sum(1 for c in cues if find_take(p, c.index) is not None)
        self.summary.setText(f"{self.model.path.stem if self.model.path else '이 프로젝트'} · 길이 {fmt_time(self.model.timeline.total)} · "
                             f"클립 {len(p.clips)}개 · 자막 {n_sub}줄 · 더빙 {len(p.dubbing.takes)}개")
        done = {"media": bool(p.clips or p.bgm.file), "ai": n_sub > 0 or bool(p.clips),
                "rec": n_sub > 0 and n_rec >= n_sub, "export": False}
        order = ["media", "ai", "rec", "export"]
        cur = next((k for k in order if not done[k]), "export")
        o = p.output
        info = {
            "media": (f"클립 {len(p.clips)}개" + (f" · 음악 {p.bgm.file}" if p.bgm.file else "") if p.clips or p.bgm.file
                      else "이미지·영상·음악을 끌어 놓거나 눌러서 고르기",
                      "+ 더 넣기" if p.clips else "➕ 미디어 추가"),
            "ai": (f"클립 {len(p.clips)}개 · 자막 {n_sub}줄" if (p.clips or n_sub) else "AI가 클립과 자막을 함께 구성합니다",
                   "다시 구성" if (p.clips or n_sub) else "🤖 AI로 구성하기"),
            "rec": (f"{n_sub}줄 중 {n_rec}줄 녹음함" if n_sub else "자막이 있으면 한 줄씩 녹음할 수 있어요",
                    "● 이어서 녹음" if n_rec else "● 녹음 시작"),
            "export": (f"{o.width}×{o.height}", "📤 내보내기 ›"),
        }
        for k, step in self.steps.items():
            state = "done" if done[k] else ("current" if k == cur else "todo")
            step.set_state(state, *info[k])
        self.btn_rec.setEnabled(n_sub > 0)
        self.btn_export.setEnabled(bool(p.clips))


class SubtitleFilePanel(QWidget):
    """🅰 자막 탭 맨 위 한 줄: [📝 파일 · 줄 수] [불러오기] [저장] [⋯ 자막 전체 빼기]."""

    remove_subtitles_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.sub_file = QLabel("-")
        self.sub_file.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.sub_file.setStyleSheet("background:#1b1d22;border:1px solid #3a3f49;border-radius:3px;padding:3px 6px;")
        self.btn_sub = QPushButton("불러오기…")
        self.btn_sub.setToolTip("자막 파일(SRT·VTT) 불러오기")
        self.btn_sub_export = QPushButton("저장…")
        self.btn_sub_export.setToolTip("지금 자막(고친 내용 포함)을 SRT 파일로 저장")
        self.btn_sub_clear = QPushButton("🗑 자막 전체 빼기")
        self.btn_sub_clear.setToolTip("프로젝트에서 자막을 모두 뺍니다. 원본 SRT 파일은 지워지지 않습니다.")
        self.btn_sub_clear.hide()  # ⋯ 메뉴가 눌러 줌 (실수로 누르지 않게)
        self.btn_more = QToolButton()
        self.btn_more.setText("⋯")
        self.btn_more.setToolTip("자막 전체 빼기")
        self.btn_more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn_more)
        self.act_clear = menu.addAction("🗑 자막 전체 빼기", self.btn_sub_clear.click)
        menu.aboutToShow.connect(lambda: self.act_clear.setEnabled(self.btn_sub_clear.isEnabled()))
        self.btn_more.setMenu(menu)
        lay.addWidget(self.sub_file, 1)
        for b in (self.btn_sub, self.btn_sub_export, self.btn_more, self.btn_sub_clear):
            lay.addWidget(b)
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
        name = st.file or ("자막 없음" if not st.items else "(파일 없이 고친 자막)")
        text = ("🔒 " if lk else "📝 ") + name + (f" · {len(ensure_items(p))}줄" if has_subtitles(p) else "") \
            + (" · 고친 내용 있음" if st.items is not None and st.file else "")
        self.sub_file.setText(text)
        self.sub_file.setToolTip(text + ("\n잠김 — 타임라인 트랙 머리의 🔒 를 눌러 풀기" if lk else ""))
        self.btn_sub.setEnabled(not lk)
        self.btn_sub_clear.setEnabled(has_subtitles(p) and not lk)
        self.btn_sub_export.setEnabled(has_subtitles(p))


def _auto_base(model, p, f):
    """저장한 적 없고 아무것도 없는 '제목 없음' 프로젝트면 처음 고른 파일의 폴더를 작업 폴더로."""
    if model.path is None and not p.clips and not p.subtitle.file and not p.bgm.file:
        p.base_dir = Path(f).parent


def set_bgm_file(model: ProjectModel, f: str) -> None:
    model.edit(lambda p: (_auto_base(model, p, f), setattr(p.bgm, "file", _rel(p, f))))


SIZE_CARDS = [("▭ 가로 16:9 · 유튜브", 1920, 1080), ("▯ 세로 9:16 · 쇼츠", 1080, 1920), ("□ 정사각 1:1", 1080, 1080)]


class OutputPage(QWidget):
    """📤 내보내기 탭: 영상 모양 · 화질 · 화면 맞춤 · 자막 넣는 방식 · 장면 바뀔 때 · 처음·끝 · ▸ 자세히,
    바닥에 결과 요약 + 영상 내보내기(footer — 메인 창이 스크롤 바깥에 붙임)."""

    export_requested = Signal()
    voice_requested = Signal()

    def __init__(self, model: ProjectModel):
        super().__init__()
        self.model = model
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        cols = self.cols = Columns()
        lay.addWidget(cols)

        # --- 영상 모양 ---
        g_out = self.g_out = cols.add(Section("영상 모양"))
        cards = QHBoxLayout()
        cards.setSpacing(0)
        self.size_cards = []
        for text, w, h in SIZE_CARDS:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFixedHeight(30)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setStyleSheet(Segmented.CSS)
            b.setToolTip(f"{w}×{h}")
            b.clicked.connect(lambda _=False, w=w, h=h: self._set(None, lambda p: (setattr(p.output, "width", w),
                                                                              setattr(p.output, "height", h))))
            cards.addWidget(b, 1)
            self.size_cards.append(b)
        g_out.add(cards)
        self.preset = QComboBox()
        fill_size_combo(self.preset)
        self.preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset.setMinimumContentsLength(8)
        self.fit = QComboBox()
        self.fit.addItem("꽉 채우기(잘라냄)", "cover"); self.fit.addItem("전체 보이기(여백)", "contain")
        self.fit.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.fit.setMinimumContentsLength(6)
        g_out.row(field("화질", self.preset, label_w=30), field("화면 맞춤", self.fit, label_w=54))

        # --- 자막 넣는 방식 ---
        g_sub = cols.add(Section("자막 넣는 방식"))
        self.sub_mode = Segmented([(True, "영상에 새기기", "어디서나 보입니다 (끌 수 없음)"),
                                   (False, "따로 (켜고 끌 수 있게)", "플레이어에서 자막을 켜고 끌 수 있습니다")], height=28)
        self.burn = self.sub_mode.button(True)
        self.soft = self.sub_mode.button(False)
        g_sub.add(self.sub_mode)

        # --- 장면 바뀔 때 (기본 전환) ---
        g_tr = self.g_tr = cols.add(Section("장면 바뀔 때 (기본 전환)"), 1)
        self.tr_type = QComboBox()
        for t in TRANSITIONS:
            self.tr_type.addItem(TRANSITION_LABELS.get(t, t), t)
        self.tr_type.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.tr_type.setMinimumContentsLength(6)
        self.tr_dur = ValueSlider(0, 3, 0.05, "{:.2f}초")
        self.btn_tr_all = g_tr.add_link("모든 경계에 적용",
                                        tip="클립마다 따로 정한 전환·없앤 전환을 지우고 모든 경계에 기본 전환을 씁니다 (Ctrl+Z 로 되돌리기)")
        g_tr.row(field("방식", self.tr_type, label_w=30), field("시간", self.tr_dur, label_w=30))

        # --- 처음 · 끝 ---
        g_fade = self.g_fade = cols.add(Section("처음 · 끝"), 1)
        g_fade.setToolTip("검은 화면에서 서서히 나타나고 끝에서 서서히 사라짐 (소리도 함께)")
        self.fade_in = ValueSlider(0, 5, 0.1, "{:.1f}초")
        self.fade_out = ValueSlider(0, 5, 0.1, "{:.1f}초")
        self.fade_in.setToolTip("0 = 없음. 영상이 검은 화면에서 서서히 나타나고 소리도 서서히 커집니다 (페이드 인)")
        self.fade_out.setToolTip("0 = 없음. 영상 끝에서 검은 화면으로 서서히 사라지고 소리도 줄어듭니다 (페이드 아웃)")
        g_fade.row(field("나타나기", self.fade_in, label_w=48), field("사라지기", self.fade_out, label_w=48))
        self.fade_in.valueChanged.connect(lambda v: self._set("fin", lambda p: setattr(p.output, "fade_in", round(v, 2))))
        self.fade_out.valueChanged.connect(lambda v: self._set("fout", lambda p: setattr(p.output, "fade_out", round(v, 2))))

        # --- ▸ 자세히 ---
        self.adv = Collapsible("자세히 — 화질 직접 · 소리 크기 · 자막 · 목소리 밀림 · 녹음 폴더", key="out_adv")
        self.adv.head.setToolTip("가로×세로 · 초당 장면 수(fps) · 여백 색 · 원본/목소리 소리 크기 · 자막 전체 늦추기 · 목소리 밀림(녹음 지연 보정) · 녹음 폴더")
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setSingleStep(2)
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setSingleStep(2)
        self.fps = QDoubleSpinBox(); self.fps.setRange(1, 120); self.fps.setDecimals(3)
        self.fps.setToolTip("보통 30. 움직임이 많으면 60 (fps)")
        self.bg = QPushButton()
        self.bg.setToolTip("'전체 보이기'로 맞출 때 남는 곳의 색")
        self.orig_vol = QDoubleSpinBox(); self.orig_vol.setRange(0, 4); self.orig_vol.setSingleStep(0.05); self.orig_vol.setSuffix(" 배")
        self.sub_offset = NudgeSpin(-3600, 3600)
        self.sub_offset.setToolTip("모든 자막을 함께 늦추기(+) / 앞당기기(−)")
        self.dub_vol = QDoubleSpinBox(); self.dub_vol.setRange(0, 4); self.dub_vol.setSingleStep(0.05); self.dub_vol.setSuffix(" 배")
        self.latency = QDoubleSpinBox(); self.latency.setRange(-2000, 2000); self.latency.setSuffix(" ms"); self.latency.setSingleStep(10)
        self.latency.setToolTip("녹음한 목소리가 늘 조금 늦게 들리면 값을 올리세요 (그만큼 앞당겨 놓음) — 녹음 지연 보정")
        self.dub_folder = QLineEdit()
        self.dub_folder.setToolTip("녹음 파일을 저장할 폴더 (프로젝트 폴더 기준)")
        size_row = hrow(self.w, QLabel("×"), self.h, stretches=[1, 0, 1], spacing=4)
        a = self.adv
        a.addRow(hrow(field("가로×세로", size_row, label_w=64), field("초당 장면", self.fps, label_w=56)))
        a.addRow(hrow(field("여백 색", self.bg, label_w=64), field("원본 소리", self.orig_vol, label_w=56)))
        a.addRow(hrow(field("목소리", self.dub_vol, label_w=64), field("목소리 밀림", self.latency, label_w=56)))
        a.addRow(field("자막 전체 늦추기", self.sub_offset, label_w=96))
        a.addRow(field("녹음 폴더", self.dub_folder, label_w=64))
        lay.addWidget(self.adv)
        lay.addStretch(1)

        # --- 바닥 줄: 결과 요약 + 내보내기 ---
        self.footer = Footer()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(DIM + "font-size:11px;")
        self.btn_export = QPushButton("📤 영상 내보내기 (Ctrl+E)")
        self.btn_export.setMinimumHeight(36)
        self.btn_export.setStyleSheet("QPushButton{background:#2e7d32;color:white;font-weight:bold;border-radius:4px}")
        self.btn_voice = QPushButton("목소리만 WAV…")
        self.btn_voice.setMinimumHeight(36)
        self.btn_voice.setToolTip("녹음한 목소리만 하나의 WAV 파일로")
        self.footer.add(self.summary)
        self.footer.add(hrow(self.btn_export, self.btn_voice, stretches=[1, 0], spacing=6))
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
        self.sub_mode.changed.connect(lambda v: self._set("burn", lambda p: setattr(p.subtitle, "burn_in", bool(v))))
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
        else:  # 직접 입력 → 자세히 펼치기
            self.adv.set_open(True)
            self.w.setFocus()

    def _pick_color(self, current, setter):
        c = QColorDialog.getColor(QColor(current), self, "색 선택")
        if c.isValid():
            self._set(None, lambda p: setter(c.name().upper()))

    @staticmethod
    def _swatch(btn: QPushButton, color: str):
        btn.setText(color)
        btn.setStyleSheet(f"background:{color}; color:{'#000' if QColor(color).lightness() > 128 else '#fff'};")

    def summary_text(self) -> str:
        from ..dubbing.takes import find_take
        from .preview import fmt_time
        p = self.model.project
        o = p.output
        cues = self.model.cues
        n_rec = sum(1 for c in cues if find_take(p, c.index) is not None)
        parts = [f"{o.width}×{o.height} · {o.fps:g}fps", f"길이 {fmt_time(self.model.timeline.total)}"]
        if cues:
            parts.append("자막 새김" if p.subtitle.burn_in else "자막 따로")
            parts.append(f"목소리 {n_rec}줄 / {len(cues)}줄" + (" ⚠ 녹음 안 한 줄 있음" if n_rec < len(cues) else ""))
        elif p.dubbing.takes:
            parts.append(f"더빙 {len(p.dubbing.takes)}개")
        return " · ".join(parts)

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
        self.fade_in.setValue(o.fade_in)
        self.fade_out.setValue(o.fade_out)
        self.g_fade.set_dirty(o.fade_in > 0 or o.fade_out > 0)
        self.tr_type.setCurrentIndex(max(0, self.tr_type.findData(p.transition.type)))
        self.tr_dur.setValue(p.transition.duration)
        self.btn_tr_all.setEnabled(len(p.clips) > 1 and not p.tracks.clips.locked)
        self.sub_mode.set_value(bool(p.subtitle.burn_in))
        self.sub_offset.setValue(p.subtitle.offset)
        self.dub_vol.setValue(p.dubbing.volume); self.latency.setValue(p.dubbing.latency_ms)
        self.dub_folder.setText(p.dubbing.folder)
        self.summary.setText(self.summary_text())
        self.btn_export.setEnabled(bool(p.clips))
        self._loading = False


PAGE_TAB_NAMES = {"clip": "🛠 클립", "subtitle": "🛠 자막", "take": "🛠 더빙", "transition": "🛠 전환", "bgm": "🛠 음악"}
EMPTY_TAB_NAME = "🛠 선택"


class Inspector(QWidget):
    """'🛠 선택' 탭: 타임라인에서 고른 항목만 편집합니다. 탭 이름은 고른 것에 따라 (🛠 클립 / 🛠 자막 …)."""

    style_requested = Signal(str)  # '🅰 자막' 탭 열기 ("selected" = 고른 자막만, "all" = 모든 자막)
    title_changed = Signal(str)  # 탭 이름

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

    def page_for(self, kind: str):
        return {"clip": self.clip_page, "subtitle": self.sub_page, "take": self.take_page,
                "transition": self.tr_page, "bgm": self.bgm_page}.get(kind)

    def tab_title(self) -> str:
        w = self.stack.currentWidget()
        if w is self.empty:
            return EMPTY_TAB_NAME
        return PAGE_TAB_NAMES.get(self.model.selection.kind, EMPTY_TAB_NAME)

    def _on_selection(self):
        k = self.model.selection.kind
        page = self.page_for(k)
        if k == "bgm" and not self.model.project.bgm.file:
            page = None
        self.stack.setCurrentWidget(page or self.empty)
        if page:
            page.refresh()
        self._apply_lock()
        self.title_changed.emit(self.tab_title())

    def _refresh_current(self):
        w = self.stack.currentWidget()
        if w is self.bgm_page and not self.model.project.bgm.file:
            self.stack.setCurrentWidget(self.empty)
            w = self.empty
            self.title_changed.emit(self.tab_title())
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
