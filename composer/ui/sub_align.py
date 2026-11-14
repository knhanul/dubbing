"""'자막 맞추기' 편집 화면 — 더빙 파형 위에 자막 블록을 놓고 직접 맞춥니다.

- 블록 가장자리 끌기 = 시작/끝 조절 (무음 경계 자석, Shift 누르면 자석 끔) · 블록 가운데 끌기 = 옮기기
- 블록 클릭 = 그 구간만 듣기 · 더블클릭 = 문구 고치기 · Ctrl+휠 / 🔍 = 확대·축소
- 노란색 = 음성 인식과 무음 감지가 어긋나거나 인식 일치율이 낮은 자막 → '⚠ 다음 노란색으로'
- ⌨ 탭으로 맞추기: 들으면서 스페이스(또는 큰 버튼)를 누를 때마다 다음 자막이 시작
- 모든 편집 ↶ 되돌리기 / ↷ 다시 하기, '✔ 적용'을 눌러야 자막 트랙에 들어감
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..cue_edit import CueList, EditCue, TapSession
from .preview import fmt_time

RULER_H = 20
EDGE_PX = 6
COL_OK = QColor(26, 115, 232, 110)
COL_LOW = QColor(249, 171, 0, 150)
COL_SEL = QColor(255, 255, 255)


class AlignView(QWidget):
    """파형 + 자막 블록. 시간 ↔ 화면: x = t * pps."""
    selected = Signal(int)
    seek_requested = Signal(float)
    play_requested = Signal(float, float)
    edit_requested = Signal(int)
    edited = Signal()

    def __init__(self, cl: CueList, peaks=None, duration: float = 0.0, parent=None):
        super().__init__(parent)
        self.cl = cl
        self.peaks = peaks if peaks is not None else []
        self.per_sec = 100
        self.duration = max(duration, max([c.end for c in cl.cues] + [0.0]) + 2.0)
        self.pps = 80.0  # 1초 = 80 화면점
        self.sel: Optional[int] = None
        self.playhead = 0.0
        self._drag = None  # (모드, 번호, 누른 x, 원래 시작, 원래 끝, key)
        self._drag_n = 0
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._resize()

    # --- 크기/확대
    def _resize(self):
        self.setMinimumWidth(int(self.duration * self.pps) + 40)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(int(self.duration * self.pps) + 40, 180)

    def set_zoom(self, pps: float):
        self.pps = max(10.0, min(800.0, pps))
        self._resize()

    def x_of(self, t: float) -> float:
        return 10 + t * self.pps

    def t_of(self, x: float) -> float:
        return max(0.0, (x - 10) / self.pps)

    # --- 그리기
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#16181d"))
        h = self.height()
        x0, x1 = self.visibleRegion().boundingRect().left(), self.visibleRegion().boundingRect().right()
        t0, t1 = self.t_of(x0) - 1, self.t_of(x1) + 1
        # 말한 구간(무음 감지) 띠
        for s, e in self.cl.regions:
            if e < t0 or s > t1:
                continue
            p.fillRect(QRectF(self.x_of(s), RULER_H, (e - s) * self.pps, h - RULER_H), QColor(255, 255, 255, 14))
        # 파형
        mid = RULER_H + (h - RULER_H) / 2
        amp = (h - RULER_H) / 2 - 4
        p.setPen(QPen(QColor("#5f9df7"), 1))
        n = len(self.peaks)
        if n:
            step = max(1.0, self.per_sec / self.pps)  # 화면점 하나에 몇 칸
            x = max(x0, 10)
            while x <= x1:
                k = int(self.t_of(x) * self.per_sec)
                if k >= n:
                    break
                v = float(max(self.peaks[k:k + int(step) + 1]))
                p.drawLine(QPointF(x, mid - v * amp), QPointF(x, mid + v * amp))
                x += 1
        # 줄자
        p.fillRect(QRectF(x0, 0, x1 - x0 + 1, RULER_H), QColor("#202329"))
        tick = next(s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120) if s * self.pps >= 70)
        p.setPen(QColor("#9aa0a6"))
        f = QFont(self.font()); f.setPointSize(8); p.setFont(f)
        k = int(max(0.0, t0) / tick)
        while k * tick <= t1:
            t = k * tick
            x = self.x_of(t)
            p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
            p.drawText(QPointF(x + 2, RULER_H - 7), fmt_time(t) if tick >= 1 else f"{t:.1f}")
            k += 1
        # 자막 블록
        f2 = QFont(self.font()); f2.setPointSize(9); p.setFont(f2)
        for i, c in enumerate(self.cl.cues):
            if c.end < t0 or c.start > t1:
                continue
            r = QRectF(self.x_of(c.start), RULER_H + 4, max(2.0, (c.end - c.start) * self.pps), h - RULER_H - 8)
            p.fillRect(r, COL_LOW if c.low else COL_OK)
            p.setPen(QPen(COL_SEL if i == self.sel else QColor(255, 255, 255, 90), 2 if i == self.sel else 1))
            p.drawRect(r)
            lines = max(1, c.text.count("\n") + 1)
            p.fillRect(QRectF(r.left() + 1, r.top() + 1, r.width() - 2, min(r.height() - 2, 16 * lines + 6)),
                       QColor(0, 0, 0, 150))  # 파형 위에서도 글이 잘 보이게
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                       f"{i + 1}{' ⚠' if c.low else ''}  {c.text}")
        # 재생 위치
        p.setPen(QPen(QColor("#ea4335"), 2))
        x = self.x_of(self.playhead)
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.end()

    # --- 마우스
    def hit(self, x: float) -> tuple[Optional[int], str]:
        for i, c in enumerate(self.cl.cues):
            a, b = self.x_of(c.start), self.x_of(c.end)
            if a - EDGE_PX <= x <= b + EDGE_PX:
                if abs(x - a) <= EDGE_PX and (b - a) > EDGE_PX * 2 or x < a:
                    return i, "start"
                if abs(x - b) <= EDGE_PX or x > b:
                    return i, "end"
                return i, "move"
        return None, ""

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if self._drag is None:
            i, mode = self.hit(x)
            self.setCursor(Qt.SizeHorCursor if mode in ("start", "end") else
                           Qt.OpenHandCursor if mode == "move" else Qt.ArrowCursor)
            return
        mode, i, px, s0, e0, key, moved = self._drag
        if not moved and abs(x - px) < 3:
            return
        self._drag = (mode, i, px, s0, e0, key, True)
        dt = (x - px) / self.pps
        snap_on = not (ev.modifiers() & Qt.ShiftModifier)  # Shift = 자석 끄기
        tol = 8 / self.pps
        if mode == "start":
            self.cl.set_start(i, s0 + dt, snap_on, tol, key)
        elif mode == "end":
            self.cl.set_end(i, e0 + dt, snap_on, tol, key)
        else:
            self.cl.move(i, s0 + dt, snap_on, tol, key)
        self.update()
        self.edited.emit()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        x = ev.position().x()
        i, mode = self.hit(x)
        if i is None:
            self._drag = None
            self.seek_requested.emit(self.t_of(x))
            return
        self._drag_n += 1
        c = self.cl.cues[i]
        self._drag = (mode, i, x, c.start, c.end, ("drag", self._drag_n), False)
        self.sel = i
        self.selected.emit(i)
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag is None:
            return
        mode, i, px, s0, e0, key, moved = self._drag
        self._drag = None
        self.cl.end_group()
        if not moved:  # 클릭 = 그 구간만 듣기
            c = self.cl.cues[i]
            self.play_requested.emit(c.start, c.end)

    def mouseDoubleClickEvent(self, ev):
        i, _ = self.hit(ev.position().x())
        if i is not None:
            self.edit_requested.emit(i)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.pps * (1.25 if ev.angleDelta().y() > 0 else 0.8))
            ev.accept()
        else:
            ev.ignore()


class SubAlignDialog(QDialog):
    """cues: EditCue 목록, regions: 말한 구간(타임라인 초), sources: (소리 파일, 타임라인 위치)."""

    def __init__(self, cues: list[EditCue], regions=(), sources=(), gap: float = 0.05, max_chars: int = 20,
                 note: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("자막 맞추기 — 더빙에 맞춰 자막 시간 고치기")
        self.resize(1180, 680)
        self.cl = CueList(cues, regions, gap, max_chars)
        self.tap: Optional[TapSession] = None
        self._play_end: Optional[float] = None
        self._clock0: Optional[tuple[float, float]] = None  # 소리 없이 재생할 때(perf, 시작 t)
        self._mute_text = False
        self.confirm: Callable[[str, str], bool] = \
            lambda t, m: QMessageBox.question(self, t, m) == QMessageBox.Yes

        # --- 소리 준비 (녹음들을 한 파일로)
        peaks, self.audio = None, None
        self._tmp = tempfile.TemporaryDirectory(prefix="vc_align_")
        if sources:
            try:
                from ..autosub import mix_sources, wave_peaks
                self.audio = mix_sources(list(sources), Path(self._tmp.name) / "dub.wav")
                peaks = wave_peaks(self.audio)
            except Exception:  # noqa: BLE001
                self.audio = None
        dur = (len(peaks) / 100.0) if peaks is not None else 0.0
        self.player = None
        if self.audio:
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
                self.player = QMediaPlayer(self)
                self._out = QAudioOutput(self)
                self.player.setAudioOutput(self._out)
                self.player.setSource(QUrl.fromLocalFile(str(self.audio)))
            except Exception:  # noqa: BLE001
                self.player = None

        # --- 위쪽 안내 + 버튼
        hint = QLabel("블록 <b>가장자리를 끌면</b> 시작/끝, <b>가운데를 끌면</b> 옮기기 (말 시작·끝에 자석처럼 붙음, "
                      "<b>Shift</b> 누르면 자석 끔). <b>클릭</b> = 그 자막만 듣기, <b>더블클릭</b> = 문구 고치기, "
                      "<b>Ctrl+휠</b> = 확대. <span style='color:#f9ab00'>■ 노란색</span> = 확인이 필요한 자막.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#bdc1c6;")
        self.lbl_note = QLabel(note)
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color:#9aa0a6;")
        self.lbl_note.setVisible(bool(note))

        def btn(text, tip="", slot=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            if slot:
                b.clicked.connect(slot)
            return b
        self.btn_undo = btn("↶ 되돌리기", "Ctrl+Z", self.undo)
        self.btn_redo = btn("↷ 다시 하기", "Ctrl+Y", self.redo)
        self.btn_play_cue = btn("▶ 이 자막 듣기", "선택한 자막 구간만 재생", self.play_selected)
        self.btn_split = btn("✂ 여기서 나누기", "빨간 재생 위치에서 선택한 자막을 둘로 나눕니다.\n"
                             "문구는 아래 문구 칸의 커서 위치(없으면 시간 비율)에서 나뉩니다.", self.split_here)
        self.btn_merge = btn("🔗 다음과 합치기", "선택한 자막과 바로 다음 자막을 하나로", self.merge_next)
        self.btn_next_low = btn("⚠ 다음 노란색으로", "확인이 필요한 다음 자막으로 가서 들려 줍니다", self.goto_next_low)
        self.btn_ok_low = btn("👍 이 자막 확인함", "노란색 표시를 끕니다", self.mark_checked)
        self.btn_play = btn("⏯ 재생/멈춤", "", self.toggle_play)
        self.btn_zoom_out = btn("🔍−", "축소 (Ctrl+휠)", lambda: self.view.set_zoom(self.view.pps * 0.7))
        self.btn_zoom_in = btn("🔍+", "확대 (Ctrl+휠)", lambda: self.view.set_zoom(self.view.pps * 1.4))
        self.btn_tap = QToolButton()
        self.btn_tap.setText("⌨ 탭으로 맞추기")
        self.btn_tap.setCheckable(True)
        self.btn_tap.setToolTip("음성을 들으며 문장이 시작될 때마다 스페이스(또는 큰 버튼)를 누르세요")
        self.btn_tap.toggled.connect(self.set_tap_mode)
        bar = QHBoxLayout()
        for w in (self.btn_undo, self.btn_redo, None, self.btn_play, self.btn_play_cue, self.btn_split, self.btn_merge,
                  None, self.btn_next_low, self.btn_ok_low, None, self.btn_zoom_out, self.btn_zoom_in, None, self.btn_tap):
            if w is None:
                bar.addSpacing(10)
            else:
                bar.addWidget(w)
        bar.addStretch(1)

        # --- 파형
        self.view = AlignView(self.cl, peaks, dur)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.view)
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(190)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        # --- 문구 칸
        self.lbl_sel = QLabel("자막을 고르세요")
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(64)
        self.text.setPlaceholderText("선택한 자막 문구 (여기서 고치면 바로 반영 · 줄바꿈 = 자막 안의 줄)")

        # --- 탭 모드 화면
        self.tap_box = QWidget()
        tl = QVBoxLayout(self.tap_box)
        big = QFont(self.font()); big.setPointSize(20); big.setBold(True)
        mid = QFont(self.font()); mid.setPointSize(13)
        self.lbl_tap_cur = QLabel(); self.lbl_tap_cur.setFont(big); self.lbl_tap_cur.setWordWrap(True)
        self.lbl_tap_next = QLabel(); self.lbl_tap_next.setFont(mid); self.lbl_tap_next.setWordWrap(True)
        self.lbl_tap_next.setStyleSheet("color:#9aa0a6;")
        self.btn_tap_hit = QPushButton("⏎ 지금 시작! (스페이스)")
        self.btn_tap_hit.setMinimumHeight(56)
        self.btn_tap_hit.setStyleSheet("QPushButton{background:#1a73e8;color:white;font-size:18px;font-weight:bold}")
        self.btn_tap_hit.clicked.connect(self.tap_now)
        self.btn_tap_restart = QPushButton("↺ 선택한 자막부터 다시")
        self.btn_tap_restart.clicked.connect(self.tap_restart)
        r = QHBoxLayout(); r.addWidget(self.btn_tap_hit, 1); r.addWidget(self.btn_tap_restart)
        tl.addWidget(QLabel("말이 시작될 때 누르세요 — 이번 자막:"))
        tl.addWidget(self.lbl_tap_cur)
        tl.addWidget(self.lbl_tap_next)
        tl.addLayout(r)
        self.tap_box.setVisible(False)

        left = QWidget()
        ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.scroll, 1)
        ll.addWidget(self.lbl_sel)
        ll.addWidget(self.text)
        ll.addWidget(self.tap_box)

        # --- 목록
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["번호", "시작", "끝", "대본 문구", "인식 문구", "신뢰도"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        for k in (0, 1, 2, 5):
            hh.setSectionResizeMode(k, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.table)
        split.setSizes([680, 480])

        self.lbl_count = QLabel()
        bb = QDialogButtonBox()
        self.btn_apply = bb.addButton("✔ 적용", QDialogButtonBox.AcceptRole)
        self.btn_apply.setToolTip("고친 자막을 자막 트랙에 넣습니다 (본 화면에서 Ctrl+Z 로 되돌릴 수 있음)")
        self.btn_cancel = bb.addButton("취소", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bottom = QHBoxLayout(); bottom.addWidget(self.lbl_count, 1); bottom.addWidget(bb)

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.lbl_note)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

        # --- 연결
        self.view.selected.connect(self.select)
        self.view.seek_requested.connect(self.seek)
        self.view.play_requested.connect(self.play_range)
        self.view.edit_requested.connect(self.edit_text)
        self.view.edited.connect(self._after_edit_light)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.edit_text(r))
        self.text.textChanged.connect(self._text_changed)
        QShortcut(QKeySequence.Undo, self, self.undo, context=Qt.WindowShortcut)
        QShortcut(QKeySequence.Redo, self, self.redo, context=Qt.WindowShortcut)
        QShortcut(QKeySequence("Ctrl+Y"), self, self.redo, context=Qt.WindowShortcut)
        self.sc_space = QShortcut(QKeySequence(Qt.Key_Space), self, self.tap_now, context=Qt.WindowShortcut)
        self.sc_space.setEnabled(False)  # 탭 모드에서만 (평소엔 문구 칸에 띄어쓰기)
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh()
        first = self.cl.next_low(None)
        if self.cl.cues:
            self.select(first if first is not None else 0, scroll=True)

    # ------------------------------------------------------------------ 상태
    def refresh(self):
        self._fill_table()
        self.view.update()
        self.btn_undo.setEnabled(self.cl.can_undo())
        self.btn_redo.setEnabled(self.cl.can_redo())
        n_low = self.cl.low_count()
        sel = self.view.sel
        has = sel is not None and 0 <= sel < len(self.cl)
        for b in (self.btn_play_cue, self.btn_split):
            b.setEnabled(has)
        self.btn_merge.setEnabled(has and sel < len(self.cl) - 1)
        self.btn_ok_low.setEnabled(has and self.cl.cues[sel].low)
        self.btn_next_low.setEnabled(n_low > 0)
        self.btn_next_low.setText(f"⚠ 다음 노란색으로 ({n_low})" if n_low else "⚠ 노란색 없음")
        self.lbl_count.setText(f"자막 {len(self.cl)}개 · 확인 필요 {n_low}개")
        self._update_tap_labels()

    def _fill_table(self):
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.cl))
        for r, c in enumerate(self.cl.cues):
            vals = [str(r + 1), fmt_time(c.start), fmt_time(c.end), c.text.replace("\n", " / "), c.heard,
                    "–" if c.conf is None else f"{c.conf:.0%}"]
            for k, v in enumerate(vals):
                it = t.item(r, k)
                if it is None:
                    it = QTableWidgetItem()
                    t.setItem(r, k, it)
                it.setText(v)
                it.setBackground(QBrush(QColor(249, 171, 0, 90) if c.low else QColor(0, 0, 0, 0)))
                if k == 5:
                    it.setToolTip(c.reason or "")
        if self.view.sel is not None and self.view.sel < len(self.cl):
            t.selectRow(self.view.sel)
        t.blockSignals(False)

    def _after_edit_light(self):
        """끄는 중: 목록 시간 칸만 빨리 고침."""
        i = self.view.sel
        if i is not None and i < self.table.rowCount():
            c = self.cl.cues[i]
            self.table.item(i, 1).setText(fmt_time(c.start))
            self.table.item(i, 2).setText(fmt_time(c.end))
        self.btn_undo.setEnabled(True)
        self.btn_redo.setEnabled(False)

    def select(self, i: Optional[int], scroll: bool = False):
        if i is None or not (0 <= i < len(self.cl)):
            self.view.sel = None
            self.lbl_sel.setText("자막을 고르세요")
            self._set_text_box("")
            self.refresh()
            return
        self.view.sel = i
        c = self.cl.cues[i]
        why = f" — ⚠ {c.reason}" if c.low and c.reason else (" — ⚠ 확인 필요" if c.low else "")
        heard = f" · 인식: “{c.heard}”" if c.heard else ""
        self.lbl_sel.setText(f"{i + 1}번 자막  {fmt_time(c.start)} → {fmt_time(c.end)}{heard}{why}")
        self._set_text_box(c.text)
        if scroll:
            self.scroll.ensureVisible(int(self.view.x_of(c.start)), 60, 120, 0)
        self.refresh()

    def _set_text_box(self, text: str):
        self._mute_text = True
        self.text.setPlainText(text)
        self._mute_text = False

    def _table_selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        if rows:
            r = min(rows)
            if r != self.view.sel:
                self.select(r, scroll=True)
                self.seek(self.cl.cues[r].start)

    def _text_changed(self):
        if self._mute_text or self.view.sel is None:
            return
        i = self.view.sel
        self.cl.set_text(i, self.text.toPlainText(), key=("text", i))
        it = self.table.item(i, 3)
        if it:
            it.setText(self.cl.cues[i].text.replace("\n", " / "))
        self.view.update()
        self.btn_undo.setEnabled(True)

    # ------------------------------------------------------------------ 편집
    def undo(self):
        if self.cl.undo():
            self._reselect()

    def redo(self):
        if self.cl.redo():
            self._reselect()

    def _reselect(self):
        s = self.view.sel
        self.select(min(s, len(self.cl) - 1) if s is not None and len(self.cl) else None)

    def split_here(self) -> bool:
        i = self.view.sel
        if i is None:
            return False
        t = self.view.playhead
        cur = self.text.textCursor().position()
        n = len(self.cl.cues[i].text.strip())
        cursor = cur if self.text.hasFocus() and 0 < cur < n else None
        if not self.cl.split(i, t, cursor):
            self._status("빨간 재생 위치를 이 자막 안쪽(가장자리에서 0.2초 이상)에 두고 누르세요.")
            return False
        self.select(i)
        return True

    def merge_next(self) -> bool:
        i = self.view.sel
        if i is None or not self.cl.merge_next(i):
            return False
        self.select(i)
        return True

    def edit_text(self, i: int):
        self.select(i)
        self.text.setFocus()
        self.text.selectAll()

    def goto_next_low(self) -> Optional[int]:
        j = self.cl.next_low(self.view.sel)
        if j is not None:
            self.select(j, scroll=True)
            self.play_range(self.cl.cues[j].start, self.cl.cues[j].end)
        return j

    def mark_checked(self):
        if self.view.sel is not None:
            self.cl.mark_checked(self.view.sel)
            self.select(self.view.sel)

    def _status(self, msg: str):
        self.lbl_count.setText(msg)

    # ------------------------------------------------------------------ 재생
    def now(self) -> float:
        if self.player is not None and self._playing_player():
            return self.player.position() / 1000.0
        if self._clock0 is not None:
            return self._clock0[1] + (time.perf_counter() - self._clock0[0])
        return self.view.playhead

    def _playing_player(self) -> bool:
        from PySide6.QtMultimedia import QMediaPlayer
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def is_playing(self) -> bool:
        return (self.player is not None and self._playing_player()) or self._clock0 is not None

    def seek(self, t: float):
        self.view.playhead = max(0.0, t)
        if self.player is not None:
            self.player.setPosition(int(t * 1000))
        if self._clock0 is not None:
            self._clock0 = (time.perf_counter(), t)
        self.view.update()

    def play(self, t: Optional[float] = None, end: Optional[float] = None):
        if t is not None:
            self.seek(t)
        self._play_end = end
        if self.player is not None:
            self.player.play()
        else:  # 소리가 없어도 시계는 흐름 (탭 맞추기·테스트용)
            self._clock0 = (time.perf_counter(), self.view.playhead)

    def pause(self):
        if self.player is not None:
            self.player.pause()
        self._clock0 = None
        self._play_end = None

    def toggle_play(self):
        self.pause() if self.is_playing() else self.play()

    def play_range(self, s: float, e: float):
        self.play(s, e)

    def play_selected(self):
        i = self.view.sel
        if i is not None:
            c = self.cl.cues[i]
            self.play_range(c.start, c.end)

    def _tick(self):
        if not self.is_playing():
            return
        t = self.now()
        self.view.playhead = t
        if self._play_end is not None and t >= self._play_end:
            self.pause()
        self.view.update()

    # ------------------------------------------------------------------ 탭으로 맞추기
    def set_tap_mode(self, on: bool):
        self.tap_box.setVisible(on)
        self.text.setEnabled(not on)
        self.sc_space.setEnabled(on)
        if on:
            self.tap_restart()
        else:
            self.pause()
            self.tap = None
            self.cl.end_group()
            self.refresh()

    def tap_restart(self):
        """선택한 자막부터(없으면 처음부터) 다시: 조금 앞에서부터 재생."""
        i = self.view.sel or 0
        self.tap = TapSession(self.cl, i)
        prev_start = self.cl.cues[i - 1].start if i > 0 else 0.0
        start = max(prev_start, (self.cl.cues[i].start if self.cl.cues else 0.0) - 1.5)
        self.play(max(0.0, start))
        self._update_tap_labels()
        self.btn_tap_hit.setFocus()

    def tap_now(self, t: Optional[float] = None) -> Optional[int]:
        if self.tap is None:
            return None
        k = self.tap.tap(self.now() if t is None else t)
        if k is not None:
            self.view.sel = k
            self.refresh()
            self.scroll.ensureVisible(int(self.view.x_of(self.cl.cues[k].start)), 60, 200, 0)
        if self.tap.done:
            self.lbl_tap_cur.setText("✔ 끝까지 맞췄습니다. 마지막 자막 끝은 직접 조절하세요.")
        return k

    def _update_tap_labels(self):
        if self.tap is None:
            return
        if self.tap.done:
            return
        self.lbl_tap_cur.setText(f"{self.tap.i + 1}. {self.tap.current_text()}")
        nxt = self.tap.next_text()
        self.lbl_tap_next.setText(f"다음: {nxt}" if nxt else "다음: (마지막 자막)")

    # ------------------------------------------------------------------ 끝내기
    def result_cues(self):
        return self.cl.to_cues()

    def reject(self):
        if self.cl.dirty and not self.confirm("자막 맞추기", "고친 내용을 버리고 닫을까요?"):
            return
        self.pause()
        super().reject()

    def accept(self):
        self.pause()
        super().accept()

    def done(self, r):
        self._timer.stop()
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        super().done(r)
        try:
            self._tmp.cleanup()
        except Exception:  # noqa: BLE001  (Windows 에서 아직 파일을 쥐고 있으면 나중에 지워짐)
            pass
