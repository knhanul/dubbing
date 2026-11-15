"""더빙 패널: 다음 자막 미리 보기 + 카운트다운 + 마이크 녹음(줄별/연속 세션)."""
from __future__ import annotations

import math
import statistics
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..dubbing.levels import SILENCE_WARN_S, SILENCE_WARNING
from ..dubbing.recorder import Recorder, RecorderUnavailable, list_input_devices
from .level_widgets import MicStrip
from ..dubbing.takes import find_take, next_take_path, set_take
from .model import ProjectModel
from .preview import PreviewPlayer, fmt_time


def fix_label(lab: QLabel, lines: int) -> None:
    """글 길이와 상관없이 크기가 고정된 라벨 (넘치는 글은 잘림, 전체는 툴팁)."""
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QSizePolicy

    fm = QFontMetrics(lab.font())
    lab.setFixedHeight(fm.lineSpacing() * lines + 6)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    lab.setMinimumWidth(0)
    lab.setAlignment(lab.alignment() | Qt.AlignTop) if lines > 1 else None


class DubbingPanel(QWidget):
    status = Signal(str)
    script_requested = Signal()

    def __init__(self, model: ProjectModel, player: PreviewPlayer, parent=None):
        super().__init__(parent)
        self.model = model
        self.player = player
        self.recorder = Recorder()
        self.mode: Optional[str] = None  # None | line | session
        self.rec_cue = None
        self.rec_rel = ""
        self.rec_offset = 0.0
        self.stop_at = 0.0
        self._last_t = 0.0
        self._sync: list[float] = []  # 녹음 0초가 놓일 타임라인 위치 추정값들
        self.take_player = QMediaPlayer(self)
        self.take_audio = QAudioOutput(self)
        self.take_player.setAudioOutput(self.take_audio)
        self._live_players = []
        for _ in range(3):
            mp, ao = QMediaPlayer(self), QAudioOutput(self)
            mp.setAudioOutput(ao)
            self._live_players.append(mp)
        self._live_i = 0
        self._rec_p0 = 0.0

        # --- 큰 자막 표시 ---
        big = QFont(self.font()); big.setPointSize(17); big.setBold(True)
        mid = QFont(self.font()); mid.setPointSize(12); mid.setBold(True)
        self.lbl_cur_title = QLabel("현재 자막")
        self.lbl_cur = QLabel("—"); self.lbl_cur.setFont(big); self.lbl_cur.setWordWrap(True)
        self.lbl_cur_time = QLabel(""); self.lbl_cur_time.setFont(mid)
        self.bar_cur = QProgressBar(); self.bar_cur.setRange(0, 1000); self.bar_cur.setTextVisible(False)
        self.bar_cur.setMaximumHeight(10)
        self.lbl_next_title = QLabel("다음 자막 (미리 보기)")
        self.lbl_next = QLabel("—"); self.lbl_next.setFont(mid); self.lbl_next.setWordWrap(True)
        self.lbl_next.setStyleSheet("color:#7fb3ff")
        self.lbl_next_time = QLabel(""); self.lbl_next_time.setFont(big)
        self.lbl_cue = QLabel(""); self.lbl_cue.setAlignment(Qt.AlignCenter)
        cuefont = QFont(self.font()); cuefont.setPointSize(26); cuefont.setBold(True)
        self.lbl_cue.setFont(cuefont)

        # 자막 글이 바뀌어도 크기가 절대 변하지 않게(바뀌면 옆의 미리보기 영상이 흔들림): 줄 수만큼 고정 높이
        for lab, lines in ((self.lbl_cur, 3), (self.lbl_cur_time, 1), (self.lbl_next, 2), (self.lbl_next_time, 1),
                           (self.lbl_cue, 1)):
            fix_label(lab, lines)

        disp = QGroupBox()
        dl = QVBoxLayout(disp)
        dl.addWidget(self.lbl_cur_title); dl.addWidget(self.lbl_cur); dl.addWidget(self.lbl_cur_time); dl.addWidget(self.bar_cur)
        dl.addSpacing(6)
        row = QHBoxLayout(); row.addWidget(self.lbl_next_title); row.addStretch(1); row.addWidget(self.lbl_cue)
        dl.addLayout(row); dl.addWidget(self.lbl_next); dl.addWidget(self.lbl_next_time)

        # --- 녹음 버튼 ---
        self.btn_rec = QPushButton("● 선택 자막 녹음 (R)")
        self.btn_rec.setToolTip("목록에서 고른 자막 하나를 녹음합니다 — 조금 앞부터 재생, 자막이 끝나면 자동으로 멈춤 (R)")
        self.btn_rec.setStyleSheet("QPushButton{background:#b3261e;color:white;font-weight:bold;padding:6px}")
        self.btn_session = QPushButton("● 여기부터 연속 녹음 (Shift+R)")
        self.btn_session.setToolTip("지금 재생 위치부터 영상 끝까지 이어서 녹음합니다 (Shift+R 로 끝내기)")
        self.btn_stop = QPushButton("■ 정지")
        rb = QHBoxLayout()
        for b in (self.btn_rec, self.btn_session, self.btn_stop):
            rb.addWidget(b)
        self.strip = MicStrip()  # 입력 소리: ● REC · 레벨 미터 · 흘러가는 파형 · 무음 경고

        # --- 마이크 줄 (두 방식 공통, 맨 위) ---
        g = QGroupBox("🎤 마이크")
        gm = QVBoxLayout(g)
        self.dev = QComboBox()
        self.dev.setToolTip("녹음할 마이크. ★ = Windows 기본 장치")
        self.dev.setMinimumHeight(26)
        self.btn_dev = QPushButton("🔄 장치 새로고침")
        self.btn_test = QPushButton("🎧 마이크 테스트")
        self.btn_test.setCheckable(True)
        self.btn_test.setToolTip("녹음하지 않고 마이크 소리 크기만 봅니다(위 미터와 파형)")
        dr = QHBoxLayout(); dr.addWidget(self.dev, 1); dr.addWidget(self.btn_dev); dr.addWidget(self.btn_test)
        self.preroll = QDoubleSpinBox(); self.preroll.setRange(0, 10); self.preroll.setValue(2.0); self.preroll.setSuffix(" 초"); self.preroll.setSingleStep(0.5)
        self.postroll = QDoubleSpinBox(); self.postroll.setRange(0, 10); self.postroll.setValue(0.5); self.postroll.setSuffix(" 초"); self.postroll.setSingleStep(0.1)
        self.rec_vol = QSpinBox(); self.rec_vol.setRange(0, 100); self.rec_vol.setValue(20); self.rec_vol.setSuffix(" %")
        self.rec_vol.setToolTip("녹음하는 동안 원본 소리 크기 (0 = 음소거)")
        self.auto_next = QCheckBox("녹음 후 다음 줄로 자동 이동"); self.auto_next.setChecked(True)
        self.live = QCheckBox("재생할 때 녹음한 목소리 함께 듣기"); self.live.setChecked(True)
        gm.addLayout(dr)
        gm.addWidget(self.strip)
        from .widgets import Collapsible
        self.opts = Collapsible("녹음 옵션 — 프리롤 · 끝난 뒤 여유 · 원본 소리 · 자동 이동", key="rec_opts")
        f = self.opts.form
        f.addRow("미리 재생(프리롤)", self.preroll)
        f.addRow("끝난 뒤 여유", self.postroll)
        f.addRow("녹음 중 원본 소리", self.rec_vol)
        f.addRow("", self.auto_next)
        f.addRow("", self.live)

        # --- 자막 목록 ---
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["번호", "시작", "끝", "길이", "내용", "녹음"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        for i in (0, 1, 2, 3, 5):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)

        # 두 가지 녹음 방법: 자막 따라 녹음 / 자유 녹음(대본 먼저 → 녹음으로 자막 만들기)
        from .free_record import FreeRecordPage
        follow = QWidget()
        fl = QVBoxLayout(follow)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(disp)
        fl.addLayout(rb)
        tip = QLabel("자막 줄을 두 번 누르면 그 녹음을 듣습니다(없으면 그 줄로 이동)")
        tip.setStyleSheet("color:#9aa0a6;")
        fl.addWidget(tip)
        fl.addWidget(self.table, 1)
        self.free_page = FreeRecordPage(self)
        self.free_page.status.connect(self.status.emit)
        # 방식 고르기: [ 자막 따라 녹음 | 자유 녹음 ] (눌러서 바꾸는 단추 두 개)
        from PySide6.QtWidgets import QButtonGroup, QStackedWidget
        self.mode_tabs = QStackedWidget()
        self.mode_tabs.addWidget(follow)
        self.mode_tabs.addWidget(self.free_page)
        seg = QHBoxLayout(); seg.setSpacing(0)
        self.btn_mode_follow = QPushButton("🎤 자막 따라 녹음")
        self.btn_mode_follow.setToolTip("이미 있는 자막 시간에 맞춰 한 줄씩 녹음")
        self.btn_mode_free = QPushButton("🎙 자유 녹음 (대본 먼저)")
        self.btn_mode_free.setToolTip("대본을 보며 편하게 녹음한 뒤, 녹음에 맞춰 자막(SRT)을 자동으로 만들기")
        self.mode_group = QButtonGroup(self)
        for k, b in enumerate((self.btn_mode_follow, self.btn_mode_free)):
            b.setCheckable(True)
            b.setMinimumHeight(30)
            b.setStyleSheet("QPushButton:checked{background:#3b4250;color:#fff;font-weight:bold;border:1px solid #8ab4f8}")
            self.mode_group.addButton(b, k)
            seg.addWidget(b)
        self.btn_mode_follow.setChecked(True)
        self.mode_group.idClicked.connect(self.mode_tabs.setCurrentIndex)
        self.mode_tabs.currentChanged.connect(lambda i: self.mode_group.button(i).setChecked(True))
        seg.addSpacing(8)
        self.btn_script = QPushButton("📄 대본 출력…")
        self.btn_script.setToolTip("자막·대본을 인쇄용으로 (Ctrl+P)")
        self.btn_script.clicked.connect(self.script_requested.emit)
        seg.addWidget(self.btn_script)

        lay = QVBoxLayout(self)
        lay.addWidget(g)
        lay.addLayout(seg)
        lay.addWidget(self.mode_tabs, 1)
        lay.addWidget(self.opts)

        self.btn_rec.clicked.connect(self.record_line)
        self.btn_session.clicked.connect(self.toggle_session)
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_dev.clicked.connect(self.refresh_devices)
        self.btn_test.toggled.connect(self.set_monitoring)
        self.dev.currentIndexChanged.connect(self._device_changed)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellDoubleClicked.connect(lambda r, c: self._row_activated(r))
        player.position_changed.connect(self.on_position)
        model.changed.connect(self.refresh_table)
        model.selection_changed.connect(self._sync_selection)
        self.meter_timer = QTimer(self)
        self.strips = [self.strip]
        self._warned = False
        self.meter_timer.setInterval(33)  # 약 30fps
        self.meter_timer.timeout.connect(self._meter)
        self.meter_timer.start()
        self._devices_loaded = False
        self.refresh_table()

    # --- 장치 ---
    def refresh_devices(self):
        self.dev.blockSignals(True)
        self.dev.clear()
        try:
            devs = list_input_devices()
        except RecorderUnavailable as e:
            self.dev.addItem(str(e), None)
            self.dev.blockSignals(False)
            return
        default_row = 0
        for i, d in enumerate(devs):
            self.dev.addItem(d.label(), d.index)
            if d.is_default:
                default_row = i
        if not devs:
            self.dev.addItem("마이크가 없습니다", None)
        self.dev.setCurrentIndex(default_row)
        self.dev.blockSignals(False)
        self._device_changed()

    def ensure_devices(self):
        if not self._devices_loaded:
            self._devices_loaded = True
            self.refresh_devices()

    def _device_changed(self):
        if self.mode:
            return
        idx = self.dev.currentData()
        if idx is None:
            return
        try:
            self.recorder.open(idx)
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"마이크를 열 수 없습니다: {e}")

    def set_monitoring(self, on: bool):
        """🎧 마이크 테스트: 녹음 없이 소리 크기만 보기 / 끄기."""
        if on:
            self.ensure_devices()
            if not self.recorder.is_open:
                try:
                    self.recorder.open(self.dev.currentData())
                except Exception as e:  # noqa: BLE001
                    self.status.emit(f"마이크를 열 수 없습니다: {e}")
            if self.recorder.is_open:
                self.status.emit("🎧 마이크 테스트 중 — 말해 보세요. 초록~노랑이면 좋아요")
        elif not self.mode:
            self.recorder.close()
        self._meter()

    def _meter(self):
        """녹음 콜백이 쌓아 둔 레벨/파형을 화면 스레드에서 꺼내 그립니다(약 30fps)."""
        r = self.recorder
        snap = r.tap.snapshot()
        wave = r.tap.take_wave()
        rec = bool(self.mode) and r.is_recording
        warn = rec and snap["silent_s"] >= SILENCE_WARN_S
        if warn and not self._warned:
            self._warned = True
            self.status.emit("⚠ " + SILENCE_WARNING)
        if not rec:
            self._warned = False
        dev = self.dev.currentText() if r.is_open else ""
        for s in self.strips:
            s.update_levels(snap, wave, is_open=r.is_open, recording=rec,
                            elapsed=r.recorded_seconds if rec else 0.0, warn=warn, device=dev)
        self.btn_test.blockSignals(True)
        self.btn_test.setChecked(r.is_open)
        self.btn_test.blockSignals(False)
        self.btn_test.setEnabled(not self.mode)

    # --- 자막 목록 ---
    def refresh_table(self):
        cues = self.model.cues
        sel = self.model.selection.index if self.model.selection.kind == "subtitle" else None
        self.table.blockSignals(True)
        self.table.setRowCount(len(cues))
        for r, c in enumerate(cues):
            take = find_take(self.model.project, c.index)
            vals = [str(c.index), fmt_time(c.start), fmt_time(c.end), f"{c.duration:.2f}", c.text.replace("\n", " / "),
                    "✔" if take else ""]
            for col, v in enumerate(vals):
                it = self.table.item(r, col)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, col, it)
                it.setText(v)
                if col == 5:
                    it.setForeground(QColor("#3fa36b"))
            if c.index == sel:
                self.table.selectRow(r)
        self.table.blockSignals(False)

    def _row_cue(self, row):
        cues = self.model.cues
        return cues[row] if 0 <= row < len(cues) else None

    def selected_cue(self):
        if self.model.selection.kind == "subtitle":
            return self.model.cue_by_index(self.model.selection.index)
        rows = self.table.selectionModel().selectedRows()
        return self._row_cue(rows[0].row()) if rows else None

    def _table_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            c = self._row_cue(rows[0].row())
            if c:
                self.model.select("subtitle", c.index)

    def _sync_selection(self):
        if self.model.selection.kind != "subtitle":
            return
        for r, c in enumerate(self.model.cues):
            if c.index == self.model.selection.index:
                self.table.blockSignals(True)
                self.table.selectRow(r)
                self.table.scrollToItem(self.table.item(r, 0))
                self.table.blockSignals(False)
                break

    def _row_activated(self, row):
        """자막 줄 두 번 누르기 = 그 줄 녹음 듣기 (녹음이 없으면 그 줄로 이동)."""
        c = self._row_cue(row)
        if not c:
            return
        k = next((i for i, t in enumerate(self.model.project.dubbing.takes) if t.subtitle == c.index), None)
        if k is None or self.mode:
            self._seek_row(row)
        else:
            self.play_take(k, False)

    def _seek_row(self, row):
        c = self._row_cue(row)
        if c:
            self.player.seek(max(0, c.start - self.preroll.value()))

    def select_relative(self, d: int):
        cues = self.model.cues
        if not cues:
            return
        cur = self.selected_cue()
        if cur is None:
            t = self.player.position()
            i = next((k for k, c in enumerate(cues) if c.start >= t), len(cues) - 1)
        else:
            i = next(k for k, c in enumerate(cues) if c.index == cur.index) + d
        i = max(0, min(len(cues) - 1, i))
        self.model.select("subtitle", cues[i].index)
        if not self.player.is_playing():
            self.player.seek(cues[i].start)

    # --- 표시 갱신 ---
    def on_position(self, t: float):
        st = self.model.cue_timeline.status(t)
        cur, nxt = st["current"], st["next"]
        if cur:
            self.lbl_cur.setText(cur.text)
            self.lbl_cur_time.setText(f"⏱ {st['until_end']:.1f}초 후 사라짐  (표시 {cur.duration:.1f}초)")
            self.bar_cur.setValue(int((st["progress"] or 0) * 1000))
        else:
            self.lbl_cur.setText("—")
            self.lbl_cur_time.setText("")
            self.bar_cur.setValue(0)
        if nxt:
            u = st["until_next"]
            self.lbl_next.setText(f"{nxt.index}. {nxt.text}")
            self.lbl_next_time.setText(f"⏳ {u:.1f}초 후 표시  (표시 {nxt.duration:.1f}초)")
            soon = u <= 3.0
            self.lbl_next_time.setStyleSheet("color:#ff6b6b" if u <= 1.0 else "color:#ffd166" if soon else "")
            self.lbl_cue.setText(str(max(1, math.ceil(u - 1e-6))) if soon and u > 0 else "")
        else:
            self.lbl_next.setText("— (마지막 자막입니다)")
            self.lbl_next_time.setText("")
            self.lbl_cue.setText("")
        if self.mode:
            self._sample_sync()
            self.lbl_cue.setText(("● 녹음 중 " + self.lbl_cue.text()).strip())
            self.lbl_cue.setStyleSheet("color:#ff4d4d")
            if self.mode == "line" and t >= self.stop_at:
                self.stop_recording()
        else:
            self.lbl_cue.setStyleSheet("color:#ffd166")
            if self.live.isChecked() and self.player.is_playing():
                self._live_takes(self._last_t, t)
        self._last_t = t

    def _sample_sync(self):
        """재생이 실제로 시작된 뒤 '재생 위치 - 녹음 경과 시간'을 여러 번 재서 녹음 시작 위치를 추정합니다.
        (seek/재생 시작 지연 때문에 목소리가 늦게 배치되는 것을 막음)"""
        if not self.player.is_playing() or self.recorder.started_at is None or len(self._sync) >= 40:
            return
        if self.player.player.position() / 1000.0 <= self._rec_p0 + 0.02:
            return  # 아직 움직이기 전
        elapsed = time.perf_counter() - self.recorder.started_at
        self._sync.append(self.player.position() - elapsed)

    def _measured_start(self, fallback: float) -> float:
        vals = self._sync[5:] if len(self._sync) > 8 else self._sync
        if not vals:
            return fallback
        est = statistics.median(vals)
        # 비정상 값(1초 이상 차이)은 무시
        return est if abs(est - fallback) < 1.0 else fallback

    def _live_takes(self, t0: float, t1: float):
        if not (0 < t1 - t0 < 0.5):
            return
        stops = getattr(self, "_live_stops", {})
        for mp, end in list(stops.items()):  # 자르기로 나눈 더빙: 구간 끝에서 멈춤
            if t1 >= end:
                mp.stop()
                stops.pop(mp, None)
        for take, pos in zip(self.model.project.dubbing.takes, self.model.take_positions()):
            ts = float(take.trim_start or 0.0)
            begin = pos + ts
            if t0 < max(begin, 0.001) <= t1:
                path = self.model.project.resolve(take.file)
                if path and path.exists():
                    mp = self._live_players[self._live_i % len(self._live_players)]
                    self._live_i += 1
                    mp.setSource(QUrl.fromLocalFile(str(path)))
                    from ..tracks import audible
                    gain = 1.0 if audible(self.model.project, "dubbing") else 0.0  # 🔇/S
                    mp.audioOutput().setVolume(min(1.0, take.volume * self.model.project.dubbing.volume) * gain)
                    start_in_file = max(ts, -pos)
                    if start_in_file > 0:
                        mp.setPosition(int(start_in_file * 1000))
                    if take.trim_end is not None:
                        stops[mp] = pos + take.trim_end
                    else:
                        stops.pop(mp, None)
                    mp.play()
        self._live_stops = stops

    def stop_live(self):
        for mp in self._live_players:
            mp.stop()
        self.take_player.stop()

    # --- 녹음 ---
    def _check_recorder(self) -> bool:
        self.ensure_devices()
        # 미리보기 영상이 없어도 타임라인 시계에 맞춰 녹음할 수 있습니다(화면엔 클립 그림 + 자막).
        if not self.recorder.is_open:
            try:
                self.recorder.open(self.dev.currentData())
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "마이크", f"마이크를 열 수 없습니다.\n{e}")
                return False
        return True

    def record_line(self):
        if self.mode:
            self.stop_recording()
            return
        cue = self.selected_cue()
        if cue is None:
            cue = self.model.cue_timeline.next(self.player.position()) or self.model.cue_timeline.current(self.player.position())
        if cue is None:
            QMessageBox.information(self, "더빙", "녹음할 자막 줄을 선택해 주세요.")
            return
        if not self._check_recorder():
            return
        self.stop_live()
        self.model.select("subtitle", cue.index)
        p0 = max(0.0, cue.start - self.preroll.value())
        rel, path = next_take_path(self.model.project, cue.index)
        self.mode, self.rec_cue, self.rec_rel = "line", cue, rel
        self.rec_offset = p0 - cue.start
        self._rec_p0, self._sync = p0, []
        self.stop_at = cue.end + self.postroll.value()
        self.player.pause()
        self.player.seek(p0)
        self.player.set_volume_factor(self.rec_vol.value() / 100.0)
        self.recorder.start(path)
        self.player.play()
        self.player.set_notice(f"● 녹음 중: {cue.index}번 자막")
        self.status.emit(f"{cue.index}번 자막 녹음 중… (R 또는 정지로 끝내기)")

    def toggle_session(self):
        if self.mode:
            self.stop_recording()
            return
        if not self._check_recorder():
            return
        self.stop_live()
        p0 = self.player.position()
        rel, path = next_take_path(self.model.project, None)
        self.mode, self.rec_cue, self.rec_rel, self.rec_offset = "session", None, rel, p0
        self._rec_p0, self._sync = p0, []
        self.player.set_volume_factor(self.rec_vol.value() / 100.0)
        self.recorder.start(path)
        self.player.play()
        self.player.set_notice("● 여기부터 연속 녹음 중 (Shift+R 로 끝내기)")
        self.status.emit("여기부터 연속 녹음 중… (Shift+R 또는 정지로 끝내기)")

    def start_free_recording(self, play_video: bool = True):
        """자유 녹음: 자막과 상관없이 지금 재생 위치부터 녹음 (영상 재생은 선택)."""
        if self.mode:
            return
        if self.model.project.tracks.dubbing.locked:
            self.status.emit("🔒 더빙 트랙이 잠겨 있습니다.")
            return
        if not self._check_recorder():
            return
        self.stop_live()
        p0 = self.player.position()
        rel, path = next_take_path(self.model.project, None)
        self.mode, self.rec_cue, self.rec_rel, self.rec_offset = "free", None, rel, p0
        self._rec_p0, self._sync = p0, []
        self._free_played = play_video
        self.player.set_volume_factor(self.rec_vol.value() / 100.0)
        self.recorder.start(path)
        if play_video:
            self.player.play()
        self.player.set_notice("● 자유 녹음 중 — 대본을 편하게 읽으세요")
        self.status.emit(f"자유 녹음 중… ({fmt_time(p0)}부터)")

    def stop_recording(self):
        if not self.mode:
            self.player.pause()
            return
        length = self.recorder.stop()
        self.player.pause()
        self.player.set_volume_factor(1.0)
        self.player.set_notice("")
        mode, cue = self.mode, self.rec_cue
        self.mode = None
        if length < 0.05:
            if mode == "free":
                self.free_page.recording_stopped()
            self.status.emit("녹음된 소리가 없습니다.")
            return
        idx = cue.index if cue else None
        base = cue.start if cue else 0.0
        offset = self._measured_start(base + self.rec_offset) - base
        set_take(self.model.project, idx, self.rec_rel, offset)
        self.model.commit()
        msg = f"저장됨: {self.rec_rel} ({length:.1f}초)"
        from ..edit_ops import take_overlaps
        if take_overlaps(self.model.project, self.model.take_file_len)[1]:  # 새 녹음은 그 자리 그대로, 겹침만 알림
            msg += " — ⚠ 다른 더빙 조각과 겹칩니다(타임라인 빨간 빗금). 끌어서 옮기면 옆 끝에 붙습니다"
        self.status.emit(msg)
        if mode == "line" and self.auto_next.isChecked():
            cues = self.model.cues
            k = next((i for i, c in enumerate(cues) if c.index == idx), None)
            if k is not None and k + 1 < len(cues):
                self.model.select("subtitle", cues[k + 1].index)
                self.player.seek(max(0.0, cues[k + 1].start - self.preroll.value()))
        else:
            ti = len(self.model.project.dubbing.takes) - 1
            self.model.select("take", ti)
        if mode == "free":
            if not getattr(self, "_free_played", True):  # 영상 없이 녹음했으면 다음 녹음은 이어서
                self.player.seek(self.rec_offset + length)
            self.free_page.recording_stopped()

    def play_take(self, take_index: int, with_video: bool):
        takes = self.model.project.dubbing.takes
        if not (0 <= take_index < len(takes)):
            return
        take = takes[take_index]
        path = self.model.project.resolve(take.file)
        if not path or not path.exists():
            QMessageBox.warning(self, "더빙", f"파일이 없습니다: {take.file}")
            return
        self.stop_live()
        pos = self.model.take_positions()[take_index]
        self.take_player.setSource(QUrl.fromLocalFile(str(path)))
        self.take_audio.setVolume(min(1.0, take.volume))
        if with_video:
            self.player.seek(max(0.0, pos + (take.trim_start or 0.0)))
            self.take_player.setPosition(int(max(float(take.trim_start or 0.0), -pos) * 1000))
            self.player.play()
        self.take_player.play()

    def play_selected(self, with_video: bool):
        if self.model.selection.kind == "take":
            self.play_take(self.model.selection.index, with_video)
            return
        cue = self.selected_cue()
        if cue is None:
            return
        for i, t in enumerate(self.model.project.dubbing.takes):
            if t.subtitle == cue.index:
                self.play_take(i, with_video)
                return
        self.status.emit("이 자막에는 아직 녹음이 없습니다.")
