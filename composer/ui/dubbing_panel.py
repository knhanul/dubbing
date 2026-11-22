"""더빙 패널: 다음 자막 미리 보기 + 카운트다운 + 마이크 녹음(줄별/연속 세션)."""
from __future__ import annotations

import math
import statistics
import time
from pathlib import Path
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
from ..dubbing.takes import count_versions, find_take, next_take_path, set_take
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
    REC_TEXT = "● 이 줄 녹음 (R)"
    SESSION_TEXT = "● 여기부터 계속 (Shift+R)"
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
        self._last_t = 0.0
        self.stop_at = 0.0
        self.extending = False  # 자막 끝에서 아직 말하는 중이라 더 기다리는 중
        self.capped = False
        self._sync: list[float] = []  # 녹음 0초가 놓일 타임라인 위치 추정값들
        self.take_player = QMediaPlayer(self)
        self.take_audio = QAudioOutput(self)
        self.take_player.setAudioOutput(self.take_audio)
        self._live_players = []
        for _ in range(3):
            mp, ao = QMediaPlayer(self), QAudioOutput(self)
            mp.setAudioOutput(ao)
            mp._pending_ms = None  # 불러오기가 끝나면 이 위치부터 재생 (Windows: 불러오기 전 위치 옮기기는 무시됨)
            mp.mediaStatusChanged.connect(lambda st, mp=mp: self._live_loaded(mp))
            self._live_players.append(mp)
        self._live_i = 0
        self._live_prev: Optional[tuple] = None  # 지난 재생 위치 — None 이면 다음 틱이 '재생 시작'
        self._rec_p0 = 0.0

        # 배치 (위에서부터): [자막 보며 녹음 | 자유롭게 녹음] · 마이크 한 줄 + 소리 막대 · 지금/다음 자막 카드 ·
        # [● 이 줄 녹음][● 여기부터 계속][■] · 자막 목록(진행률, 안 한 줄만 보기, 녹음본 배지 ▾) · ▸ 녹음 설정 · 바닥: 대본 출력
        from .widgets import DIM, Footer, Section, Segmented, field, hrow

        # --- 지금 / 다음 자막 카드 ---
        big = QFont(self.font()); big.setPointSize(13); big.setBold(True)
        mid = QFont(self.font()); mid.setPointSize(10); mid.setBold(True)
        self.lbl_cur_title = QLabel("지금 자막")
        self.lbl_cur_title.setStyleSheet(DIM + "font-size:11px;")
        self.lbl_cur = QLabel("—"); self.lbl_cur.setFont(big); self.lbl_cur.setWordWrap(True)
        self.lbl_cur_time = QLabel(""); self.lbl_cur_time.setFont(mid); self.lbl_cur_time.setStyleSheet("color:#f0c463")
        self.bar_cur = QProgressBar(); self.bar_cur.setRange(0, 1000); self.bar_cur.setTextVisible(False)
        self.bar_cur.setMaximumHeight(4)
        self.lbl_next_title = QLabel("다음")
        self.lbl_next_title.setStyleSheet(DIM + "font-size:11px;")
        self.lbl_next = QLabel("—"); self.lbl_next.setWordWrap(True)
        self.lbl_next.setStyleSheet("color:#9fc3f5")
        self.lbl_next_time = QLabel("")
        self.lbl_cue = QLabel(""); self.lbl_cue.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        cuefont = QFont(self.font()); cuefont.setPointSize(15); cuefont.setBold(True)
        self.lbl_cue.setFont(cuefont)

        # 자막 글이 바뀌어도 크기가 절대 변하지 않게(바뀌면 옆의 미리보기 영상이 흔들림): 줄 수만큼 고정 높이
        for lab, lines in ((self.lbl_cur, 2), (self.lbl_cur_time, 1), (self.lbl_next, 1), (self.lbl_next_time, 1),
                           (self.lbl_cue, 1)):
            fix_label(lab, lines)

        disp = QWidget()
        disp.setObjectName("nowCard")
        disp.setAttribute(Qt.WA_StyledBackground, True)
        disp.setStyleSheet("QWidget#nowCard{background:#1b1d22;border:1px solid #3a3f49;border-radius:5px}")
        dl = QVBoxLayout(disp)
        dl.setContentsMargins(10, 6, 10, 6)
        dl.setSpacing(2)
        row = QHBoxLayout(); row.addWidget(self.lbl_cur_title); row.addStretch(1); row.addWidget(self.lbl_cue)
        dl.addLayout(row)
        dl.addWidget(self.lbl_cur); dl.addWidget(self.lbl_cur_time); dl.addWidget(self.bar_cur)
        nx = QHBoxLayout(); nx.setSpacing(6)
        nx.addWidget(self.lbl_next_title); nx.addWidget(self.lbl_next, 1); nx.addWidget(self.lbl_next_time)
        dl.addLayout(nx)

        # --- 녹음 버튼 ---
        self.btn_rec = QPushButton(self.REC_TEXT)
        self.btn_rec.setToolTip("목록에서 고른 자막 한 줄을 녹음합니다 — 조금 앞부터 재생, 자막이 끝나면(+여유) 자동으로 멈춤. "
                                "다음 자막이 가까우면 그 앞에서 멈춥니다.")
        self.btn_merge = QPushButton("🔗 다음 줄과 합치기")
        self.btn_merge.setToolTip("고른 자막과 바로 다음 자막을 하나로 합칩니다 (짧은 줄을 한 번에 녹음할 때)")
        self.btn_merge.clicked.connect(self.merge_selected)
        self.btn_split = QPushButton("✂ 합친 줄 나누기")
        self.btn_split.setToolTip("합친 자막을 다시 둘로 나눕니다. 줄바꿈이 있으면 그 자리에서, 없으면 가운데에서")
        self.btn_split.clicked.connect(self.split_selected)
        self.btn_rec.setStyleSheet("QPushButton{background:#b3261e;color:white;font-weight:bold;padding:6px;border-radius:4px}")
        self.btn_rec.setMinimumHeight(34)
        self.btn_session = QPushButton(self.SESSION_TEXT)
        self.btn_session.setToolTip("지금 재생 위치부터 영상 끝까지 쭉 이어서 녹음합니다 (Shift+R 로 끝내기)")
        self.btn_session.setMinimumHeight(34)
        from PySide6.QtWidgets import QSizePolicy
        for b in (self.btn_rec, self.btn_session):  # 글자 길이가 패널 폭을 밀지 않게
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setMinimumWidth(120)
        self.btn_stop = QPushButton("■")
        self.btn_stop.setToolTip("정지")
        self.btn_stop.setFixedSize(40, 34)
        rb = QHBoxLayout()
        rb.setSpacing(6)
        rb.addWidget(self.btn_rec, 5); rb.addWidget(self.btn_merge, 3); rb.addWidget(self.btn_split, 3); rb.addWidget(self.btn_session, 4); rb.addWidget(self.btn_stop)
        self.strip = MicStrip()  # 입력 소리: ● REC · 레벨 미터 · 흘러가는 파형 · 무음 경고
        self.strip.dev_label.hide()  # 장치 이름은 바로 위 마이크 칸에 이미 보임

        # --- 마이크 한 줄 (두 방식 공통) ---
        self.dev = QComboBox()
        self.dev.setToolTip("녹음할 마이크. ★ = Windows 기본 장치")
        self.dev.setMinimumHeight(26)
        self.dev.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.dev.setMinimumContentsLength(10)
        self.btn_dev = QPushButton("↻")
        self.btn_dev.setToolTip("장치 새로고침 — 마이크를 새로 꽂았으면 누르세요")
        self.btn_dev.setFixedSize(30, 26)
        self.btn_test = QPushButton("🎧 테스트")
        self.btn_test.setCheckable(True)
        self.btn_test.setFixedHeight(26)
        self.btn_test.setToolTip("녹음하지 않고 마이크 소리 크기만 봅니다 (아래 막대와 파형) — 초록~노랑이면 좋아요")
        mic_row = hrow(QLabel("🎤"), self.dev, self.btn_test, self.btn_dev, stretches=[0, 1, 0, 0], spacing=4)
        self.preroll = QDoubleSpinBox(); self.preroll.setRange(0, 10); self.preroll.setValue(2.0); self.preroll.setSuffix(" 초"); self.preroll.setSingleStep(0.5)
        self.preroll.setToolTip("녹음 직전에 앞부분을 이만큼 들려줘서 타이밍을 맞추기 쉽게 (프리롤)")
        self.postroll = QDoubleSpinBox(); self.postroll.setRange(0, 10); self.postroll.setValue(0.5); self.postroll.setSuffix(" 초"); self.postroll.setSingleStep(0.1)
        self.postroll.setToolTip("한 줄 녹음: 자막이 끝난 뒤 이만큼 더 녹음하고 멈춤 (그때 말하는 중이면 말이 끝날 때까지)")
        self.rec_vol = QSpinBox(); self.rec_vol.setRange(0, 100); self.rec_vol.setValue(20); self.rec_vol.setSuffix(" %")
        self.rec_vol.setToolTip("녹음하는 동안 원본 소리 크기 (0 = 끄기)")
        self.auto_next = QCheckBox("녹음 후 다음 줄로"); self.auto_next.setChecked(True)
        self.auto_next.setToolTip("한 줄 녹음이 끝나면 다음 줄을 자동으로 고릅니다")
        self.live = QCheckBox("재생할 때 녹음 함께 듣기"); self.live.setChecked(True)
        self.live.setToolTip("미리보기를 재생할 때 녹음한 목소리도 함께 들려줍니다")
        from .widgets import Collapsible
        self.opts = Collapsible("녹음 설정 — 시작 전 듣기 · 끝난 뒤 여유 · 원본 소리", key="rec_opts")
        f = self.opts.form
        f.addRow(hrow(field("시작 전 듣기", self.preroll, label_w=70), field("끝난 뒤 여유", self.postroll, label_w=70)))
        f.addRow(hrow(field("녹음 중 원본", self.rec_vol, label_w=70), self.auto_next))
        f.addRow(self.live)

        # --- 자막 목록 ---
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "시작", "끝", "길이", "내용", "녹음"])
        self.table.horizontalHeaderItem(5).setToolTip("✔ = 녹음 있음 · ✔ 3개 ▾ = 다시 녹음한 녹음본이 3개 (눌러서 고르기)")
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(170)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        for i in (0, 1, 2, 3, 5):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        for i in (2, 3):  # 좁은 패널: 끝·길이는 줄에 마우스를 올리면 (넓으면 보임)
            self.table.setColumnHidden(i, True)
        self.list_sec = Section("자막 목록")
        self.btn_todo = self.list_sec.add_link("안 한 줄만 보기", tip="아직 녹음하지 않은 줄만 목록에 보이기")
        self.btn_todo.setCheckable(True)
        self.btn_todo.toggled.connect(lambda on: (self.btn_todo.setText("모든 줄 보기" if on else "안 한 줄만 보기"),
                                                  self._apply_filter()))
        self.list_tip = QLabel("줄을 누르면 그 자리로 · 두 번 누르면 그 녹음 듣기 · '녹음' 칸 ▾ 를 누르면 녹음본 고르기")
        self.list_tip.setWordWrap(True)
        self.list_tip.setStyleSheet(DIM + "font-size:11px;")
        self.list_sec.add(self.table)
        self.list_sec.add(self.list_tip)

        # 두 가지 녹음 방법: 자막 보며 녹음 / 자유롭게 녹음(대본 먼저 → 녹음으로 자막 만들기)
        from .free_record import FreeRecordPage
        follow = QWidget()
        fl = QVBoxLayout(follow)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        fl.addWidget(disp)
        fl.addLayout(rb)
        fl.addWidget(self.list_sec, 1)
        self.free_page = FreeRecordPage(self)
        self.free_page.status.connect(self.status.emit)
        from PySide6.QtWidgets import QButtonGroup, QStackedWidget
        self.mode_tabs = QStackedWidget()
        self.mode_tabs.addWidget(follow)
        self.mode_tabs.addWidget(self.free_page)
        seg = QHBoxLayout(); seg.setSpacing(0)
        self.btn_mode_follow = QPushButton("🎤 자막 보며 녹음")
        self.btn_mode_follow.setToolTip("이미 있는 자막 시간에 맞춰 한 줄씩 녹음")
        self.btn_mode_free = QPushButton("🎙 자유롭게 녹음 (대본 먼저)")
        self.btn_mode_free.setToolTip("대본을 보며 편하게 녹음한 뒤, 녹음에 맞춰 자막(SRT)을 자동으로 만들기")
        self.mode_group = QButtonGroup(self)
        for k, b in enumerate((self.btn_mode_follow, self.btn_mode_free)):
            b.setCheckable(True)
            b.setFixedHeight(30)
            b.setStyleSheet(Segmented.CSS)
            self.mode_group.addButton(b, k)
            seg.addWidget(b, 1)
        self.btn_mode_follow.setChecked(True)
        self._user_chose_mode = False  # 사용자가 직접 모드를 바꾸면 True
        self.mode_group.idClicked.connect(self._on_mode_clicked)
        self.mode_tabs.currentChanged.connect(lambda i: self.mode_group.button(i).setChecked(True))
        self.btn_script = QPushButton("📄 대본 출력…")
        self.btn_script.setToolTip("자막·대본을 종이로 보며 녹음하려면 — 인쇄용으로 만들기 (Ctrl+P)")
        self.btn_script.clicked.connect(self.script_requested.emit)
        self.footer = Footer()
        script_tip = QLabel("대본을 종이로 보며 녹음하려면")
        script_tip.setStyleSheet(DIM + "font-size:11px;")
        self.footer.add(hrow(script_tip, self.btn_script, stretches=[1, 0]))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addLayout(seg)
        lay.addWidget(mic_row)
        lay.addWidget(self.strip)
        lay.addWidget(self.mode_tabs, 1)
        lay.addWidget(self.opts)

        self.btn_rec.clicked.connect(self.record_line)
        self.btn_session.clicked.connect(self.toggle_session)
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_dev.clicked.connect(self.refresh_devices)
        self.btn_test.toggled.connect(self.set_monitoring)
        self.dev.currentIndexChanged.connect(self._device_changed)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellClicked.connect(self._cell_clicked)
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
    def _on_mode_clicked(self, i: int):
        self._user_chose_mode = True
        self.mode_tabs.setCurrentIndex(i)

    def auto_select_mode(self):
        """프로젝트를 열거나 새로 만들 때: 자막이 있으면 '자막 보며 녹음', 없으면 '자유롭게 녹음'.
        사용자가 이미 모드를 고른 뒤에는 같은 프로젝트 안에서 다시 바꾸지 않습니다."""
        if self._user_chose_mode:
            return
        from ..subtitles import has_subtitles
        has = has_subtitles(self.model.project)
        self.mode_tabs.setCurrentIndex(0 if has else 1)
        (self.btn_mode_follow if has else self.btn_mode_free).setChecked(True)

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
        if rec and self.mode == "line" and not self.player.is_playing():
            # 영상이 끝나 재생이 멈췄으면 재생 위치 대신 녹음 경과 시간으로 '자막 끝 + 여유'를 판단
            if self._check_line_stop(self._rec_p0 + r.recorded_seconds):
                return
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
        n_rec = 0
        first_todo = None
        for r, c in enumerate(cues):
            take = find_take(self.model.project, c.index)
            n = count_versions(self.model.project, c.index) if take else 0
            n_rec += 1 if take else 0
            if take is None and first_todo is None:
                first_todo = r
            vals = [str(c.index), fmt_time(c.start), fmt_time(c.end), f"{c.duration:.2f}", c.text.replace("\n", " / "),
                    ("✔" + (f" {n}개 ▾" if n > 1 else "")) if take else ("▶ 여기부터" if r == first_todo else "")]
            tip = f"{c.index}번 · {fmt_time(c.start)} ~ {fmt_time(c.end)} ({c.duration:.2f}초)\n{c.text}"
            for col, v in enumerate(vals):
                it = self.table.item(r, col)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, col, it)
                it.setText(v)
                it.setToolTip(tip if col != 5 else ("녹음본 고르기 — 눌러서 열기" if n > 1 else "녹음 있음" if take else "아직 녹음 안 함"))
                if col == 5:
                    it.setForeground(QColor("#3fa36b" if take else "#ffd54f"))
                # 녹음 없는 첫 줄 강조
                if r == first_todo:
                    it.setBackground(QColor("#3a3520"))
                else:
                    it.setBackground(QColor())
            if c.index == sel:
                self.table.selectRow(r)
        # 선택된 게 없고 녹음 없는 첫 줄이 있으면 그 줄을 선택
        if sel is None and first_todo is not None and cues:
            self.table.selectRow(first_todo)
            self.model.select("subtitle", cues[first_todo].index)
        self.table.blockSignals(False)
        self.list_sec.set_title(f"자막 {len(cues)}줄 · 녹음 {n_rec}줄" if cues else "자막 목록")
        self._apply_filter()

    def _apply_filter(self):
        """'안 한 줄만 보기': 녹음이 있는 줄 숨기기."""
        only_todo = self.btn_todo.isChecked()
        for r, c in enumerate(self.model.cues):
            self.table.setRowHidden(r, only_todo and find_take(self.model.project, c.index) is not None)

    def _cell_clicked(self, row: int, col: int):
        if col == 5:
            self.version_menu(row)

    def version_menu(self, row: int, exec_menu: bool = True):
        """목록 '녹음' 칸 ▾: 그 줄의 녹음본 고르기 (이름·★·시각·길이). 고르면 그 녹음본을 씀 (Ctrl+Z 로 되돌리기)."""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        from ..dubbing.takes import take_note, take_versions, use_version
        c = self._row_cue(row)
        if c is None:
            return None
        take = find_take(self.model.project, c.index)
        if take is None:
            return None
        vers = take_versions(self.model.project, take)
        if len(vers) < 2:
            return None
        menu = QMenu(self)
        menu.addSection(f"{c.index}번 줄 녹음본 {len(vers)}개")
        locked = self.model.project.tracks.dubbing.locked
        for v in vers:
            note = take_note(self.model.project, v.rel)
            when = time.strftime("%H:%M", time.localtime(v.mtime)) if v.mtime else "-"
            label = (f"v{v.version}" + (f" · {note['name']}" if note["name"] else "") + (" ★" if note["star"] else "")
                     + f" · {when} · {v.length:.1f}초")
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(v.current)
            a.setEnabled(not locked)
            ti = self.model.project.dubbing.takes.index(take)
            a.triggered.connect(lambda _=False, vv=v, ti=ti: None if vv.current else
                                self.model.edit(lambda p: use_version(p.dubbing.takes[ti], vv.rel, vv.length)))
        menu.addSeparator()
        k = self.model.project.dubbing.takes.index(take)
        menu.addAction("🛠 자세히 (이름 · ★ · 듣기 · 지우기)…", lambda: self.model.select("take", k))
        if exec_menu:
            menu.exec(QCursor.pos())
        return menu

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
            if self._check_line_stop(t):
                self._last_t = t
                return
            self.lbl_cue.setText(("● 녹음 중 " + self.lbl_cue.text()).strip())
            self.lbl_cue.setStyleSheet("color:#ff4d4d")
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
        """재생 중 더빙 조각을 함께 틉니다. 조각 시작을 지나갈 때 틀고, 재생을 조각 가운데서 시작하거나
        위치를 옮기면 그 조각을 해당 위치부터 틉니다."""
        prev, self._live_prev = self._live_prev, (t1,)
        jumped = prev is None or not (0 <= t1 - t0 < 0.5)  # 재생 시작 또는 위치 옮김
        stops = getattr(self, "_live_stops", {})
        for mp, end in list(stops.items()):  # 자르기로 나눈 더빙: 구간 끝에서 멈춤
            if t1 >= end:
                mp.stop()
                stops.pop(mp, None)
        if jumped:
            self.stop_live(keep_prev=True)
            stops = {}
        for take, pos in zip(self.model.project.dubbing.takes, self.model.take_positions()):
            ts = float(take.trim_start or 0.0)
            begin = pos + ts
            path = self.model.project.resolve(take.file)
            if not path or not path.exists():
                continue
            if jumped:
                flen = self.model.take_file_len(take.file)
                end = pos + (take.trim_end if take.trim_end is not None else flen)
                if not (begin <= t1 < end - 0.05):
                    continue
                start_in_file = t1 - pos  # 조각 가운데부터
            elif t0 < max(begin, 0.001) <= t1:
                start_in_file = max(ts, -pos)
            else:
                continue
            mp = self._live_players[self._live_i % len(self._live_players)]
            self._live_i += 1
            from ..tracks import audible
            gain = 1.0 if audible(self.model.project, "dubbing") else 0.0  # 🔇/S
            mp.audioOutput().setVolume(min(1.0, take.volume * self.model.project.dubbing.volume) * gain)
            if take.trim_end is not None:
                stops[mp] = pos + take.trim_end
            else:
                stops.pop(mp, None)
            self._live_start(mp, path, start_in_file)
        self._live_stops = stops

    def _live_start(self, mp, path, start_s: float):
        mp.stop()
        mp._pending_ms = int(start_s * 1000) if start_s > 0.01 else None
        mp.setSource(QUrl.fromLocalFile(str(path)))
        if mp._pending_ms is None:
            mp.play()
        else:
            self._live_loaded(mp)  # 이미 불러왔으면 바로

    def _live_loaded(self, mp):
        ms = getattr(mp, "_pending_ms", None)
        if ms is None or mp.mediaStatus() not in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                  QMediaPlayer.MediaStatus.BufferedMedia):
            return
        mp._pending_ms = None
        mp.setPosition(ms)
        mp.play()

    def stop_live(self, keep_prev: bool = False):
        if not keep_prev:
            self._live_prev = None  # 다음 재생은 '시작'으로 (조각 가운데면 거기부터)
        for mp in self._live_players:
            mp._pending_ms = None
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

    def merge_selected(self) -> bool:
        from ..edit_ops import merge_next_cue
        cue = self.selected_cue()
        if cue is None:
            self.status.emit("합칠 자막 줄을 먼저 고르세요")
            return False
        box = {}
        self.model.edit(lambda p: box.setdefault("ok", merge_next_cue(p, cue.index)))
        if not box.get("ok"):
            self.status.emit("다음 자막이 없어 합칠 수 없습니다")
            return False
        self.status.emit(f"🔗 {cue.index}번과 다음 자막을 합쳤습니다 (Ctrl+Z 로 되돌리기)")
        return True

    def split_selected(self) -> bool:
        from ..edit_ops import split_merged_cue
        cue = self.selected_cue()
        if cue is None:
            self.status.emit("나눌 자막 줄을 먼저 고르세요")
            return False
        box = {}
        self.model.edit(lambda p: box.setdefault("ok", split_merged_cue(p, cue.index)))
        if not box.get("ok"):
            self.status.emit("나눌 수 없습니다 — 자막이 너무 짧거나 글이 한 조각입니다")
            return False
        self.status.emit(f"✂ {cue.index}번 자막을 둘로 나눴습니다 (Ctrl+Z 로 되돌리기)")
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
        stop = cue.end + self.postroll.value()
        # 다음 자막이 가까우면 그 시작 0.1초 전까지만 녹음 (현재 자막 끝보다 앞이어도 지킴)
        nxt = self.model.cue_timeline.next(cue.start)
        if nxt is not None and nxt.index == cue.index:
            nxt = self.model.cue_timeline.next(cue.end)
        if nxt is not None and nxt.start > cue.start + 0.15:
            stop = min(stop, nxt.start - 0.1)
        self.stop_at = stop
        self._next_cap = stop  # 말이 길어져도 이 시각을 넘기지 않음
        self.extending = False
        self.player.pause()
        self.player.seek(p0)
        self.player.set_volume_factor(self.rec_vol.value() / 100.0)
        self.recorder.start(path)
        self.player.play()
        self.player.set_notice(f"● 녹음 중: {cue.index}번 자막 (R·스페이스·■ 정지로 끝내기)")
        self.status.emit(f"{cue.index}번 자막 녹음 중… 자막이 끝나면 자동으로 멈춰요(말이 길면 끝날 때까지) · "
                         "R·스페이스·■ 정지로 바로 끝내기")
        self._sync_rec_buttons()

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
        self.player.set_notice("● 여기부터 연속 녹음 중 (Shift+R·스페이스·■ 정지로 끝내기)")
        self.status.emit("여기부터 연속 녹음 중… (Shift+R·스페이스·■ 정지로 끝내기)")
        self._sync_rec_buttons()

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

    VOICE_WINDOW = 0.2   # 자막 끝(+여유)에서 최근 이만큼 안에 말소리가 있었으면 '아직 말하는 중'
    SILENCE_STOP = 0.4   # 말이 길어질 때: 이만큼 계속 조용하면 멈춤
    MAX_EXTEND = 5.0     # 말이 길어져도 자막 끝에서 최대 이만큼 (시끄러운 방에서 끝없이 녹음되지 않게)

    def _check_line_stop(self, t: float) -> bool:
        """선택 자막 녹음: 자막 끝 + 여유에서 멈춤. 그때 아직 말하는 중이면 말이 끝날 때까지(무음 0.4초, 최대 5초 더)."""
        if self.mode != "line" or self.rec_cue is None:
            return False
        if t < self.stop_at - 1e-6:
            return False
        quiet = self.recorder.tap.voice_quiet_seconds
        hard = getattr(self, "_next_cap", None) or (self.rec_cue.end + max(self.MAX_EXTEND, self.postroll.value()))
        # 다음 자막 앞에서 막힌 경우: 말이 이어져도 그 시각을 넘기지 않음
        if t >= hard - 1e-6:
            self.capped = True
            self.stop_recording()
            return True
        if not self.extending:
            if quiet >= self.VOICE_WINDOW:
                self.stop_recording()
                return True
            self.extending = True
            self.player.set_notice("● 말이 끝날 때까지 녹음 중…")
            self.status.emit(f"{self.rec_cue.index}번 자막: 아직 말하는 중이라 말이 끝날 때까지 녹음해요 "
                             f"(다음 자막 전까지만 · R·스페이스·■ 정지로 바로 끝내기)")
        if quiet >= self.SILENCE_STOP:
            self.stop_recording()
            return True
        return False


    def _sync_rec_buttons(self):
        """녹음 중에는 누른 버튼이 '■ … 끝내기' 로 바뀜 (다시 누르면 끝남)."""
        self.btn_rec.setText("■ 녹음 끝내기 (R)" if self.mode == "line" else self.REC_TEXT)
        self.btn_session.setText("■ 계속 녹음 끝내기 (Shift+R)" if self.mode == "session" else self.SESSION_TEXT)

    def stop_recording(self):
        if not self.mode:
            self.player.pause()
            return
        length = self.recorder.stop()
        self.player.pause()
        self.player.set_volume_factor(1.0)
        self.player.set_notice("")
        mode, cue = self.mode, self.rec_cue
        capped, self.capped = self.capped, False
        self.extending = False
        self.mode = None
        self._sync_rec_buttons()
        if length < 0.05:
            if mode == "free":
                self.free_page.recording_stopped()
            self.status.emit("녹음된 소리가 없습니다.")
            return
        idx = cue.index if cue else None
        base = cue.start if cue else 0.0
        offset = self._measured_start(base + self.rec_offset) - base
        take = set_take(self.model.project, idx, self.rec_rel, offset)
        extra = self._fit_cue_to_voice(take, cue) if mode == "line" and cue is not None else ""
        if mode == "line":
            extra += self._trim_take_to_neighbors(take)
        self.model.commit()  # 녹음 + 자막 끝 늘리기 = 되돌리기 한 번
        msg = f"저장됨: {self.rec_rel} ({length:.1f}초)" + extra
        if capped:
            msg += f" — 소리가 계속 들려서 자막 끝 {self.MAX_EXTEND:g}초 뒤에 멈췄어요(주변 소음 확인)"
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

    CUE_TAIL = 0.2  # 자막 끝 = 마지막 말소리 + 0.2초
    TAKE_TAIL = 0.3  # 더빙 조각 끝 = 마지막 말소리 + 0.3초 (끝의 무음은 잘라 냄, 파일은 그대로)
    NEXT_GAP = 0.05  # 다음 자막 시작보다 이만큼 앞까지만

    def _fit_cue_to_voice(self, take, cue) -> str:
        """선택 자막 녹음이 끝난 뒤: 끝의 무음을 잘라 내고, 목소리가 자막보다 늦게 끝나면 자막 끝을 늘림."""
        from ..dubbing.levels import speech_end
        from ..subtitles import ensure_items, project_cues
        from ..timeline import take_position
        pr = self.model.project
        path = pr.resolve(take.file)
        se = speech_end(path) if path else None
        if se is None:
            return ""
        flen = self.model.take_file_len(take.file)
        if se + self.TAKE_TAIL < flen - 1e-3:
            take.trim_end = round(se + self.TAKE_TAIL, 3)
        cues = project_cues(pr)
        by = {c.index: c for c in cues}
        cur = by.get(cue.index)
        if cur is None:
            return ""
        voice_end = take_position(take, by, pr.dubbing.latency_ms) + se
        want = round(voice_end + self.CUE_TAIL, 3)
        if want <= cur.end + 1e-3:
            return ""
        if pr.tracks.subtitle.locked:
            return f" — 🔒 자막 트랙이 잠겨 있어 {cue.index}번 자막 끝은 늘리지 않았어요(목소리는 {fmt_time(voice_end)}까지)"
        nxt = min((c.start for c in cues if c.start > cur.start + 1e-6), default=None)
        note = ""
        if nxt is not None and want > nxt - self.NEXT_GAP:
            want = round(nxt - self.NEXT_GAP, 3)
            note = f" (다음 자막과 겹치지 않게 {fmt_time(want)}까지만 늘렸어요)"
            if want <= cur.end + 1e-3:
                return " — 다음 자막과 붙어 있어 자막 끝은 늘리지 못했어요"
        it = next((i for i in ensure_items(pr) if i.index == cue.index), None)
        if it is None:
            return ""
        it.end = round(want - pr.subtitle.offset, 3)
        return f" — {cue.index}번 자막 끝을 {fmt_time(cur.end)} → {fmt_time(want)}로 늘렸어요" + note

    def _trim_take_to_neighbors(self, take) -> str:
        """저장한 더빙이 앞/뒤 더빙과 겹치면 그 사이만 남기도록 잘라냅니다."""
        from ..edit_ops import take_span
        pr = self.model.project
        try:
            k = pr.dubbing.takes.index(take)
        except ValueError:
            return ""
        s, e = take_span(pr, k, self.model.take_file_len)
        others = []
        for i, tk in enumerate(pr.dubbing.takes):
            if i == k:
                continue
            os, oe = take_span(pr, i, self.model.take_file_len)
            others.append((os, oe))
        note = ""
        # 앞 조각 끝 이후로 시작
        prev_end = max((oe for os, oe in others if oe <= s + 0.2), default=None)
        if prev_end is not None and s < prev_end - 0.02:
            cut = round((take.trim_start or 0.0) + (prev_end - s) + 0.02, 3)
            take.trim_start = cut
            note = " — 앞 더빙과 겹치지 않게 앞을 잘랐습니다"
        # 뒤 조각 시작 전으로 끝
        next_start = min((os for os, oe in others if os >= e - 0.2), default=None)
        if next_start is not None and e > next_start + 0.02:
            flen = self.model.take_file_len(take.file)
            te = take.trim_end if take.trim_end is not None else flen
            take.trim_end = round(te - (e - next_start) - 0.02, 3)
            note += " — 다음 더빙과 겹치지 않게 뒤를 잘랐습니다"
        return note

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

    def play_version(self, take_index: int, rel: str) -> bool:
        """녹음본 ▶: 그 녹음본을 이 조각 자리에서 영상과 함께 (입 모양·타이밍 비교). 조각이 없으면 파일만."""
        takes = self.model.project.dubbing.takes
        path = self.model.project.resolve(rel)
        if not path or not path.exists():
            self.status.emit(f"파일이 없습니다: {rel}")
            return False
        if not (0 <= take_index < len(takes)):
            return self.play_file(rel)
        take = takes[take_index]
        self.stop_live()
        pos = self.model.take_positions()[take_index]
        ts = float(take.trim_start or 0.0)
        self.take_player.setSource(QUrl.fromLocalFile(str(path)))
        self.take_audio.setVolume(min(1.0, take.volume))
        self.player.seek(max(0.0, pos + ts))
        self.take_player.setPosition(int(max(ts, -pos) * 1000))
        self.player.play()
        self.take_player.play()
        self.status.emit(f"▶ {Path(rel).name} — 영상과 함께 듣는 중")
        return True

    def play_file(self, rel: str) -> bool:
        """녹음 기록의 ▶ 듣기: 파일 하나만 처음부터."""
        path = self.model.project.resolve(rel)
        if not path or not path.exists():
            self.status.emit(f"파일이 없습니다: {rel}")
            return False
        self.stop_live()
        self.take_player.setSource(QUrl.fromLocalFile(str(path)))
        self.take_audio.setVolume(1.0)
        self.take_player.play()
        self.status.emit(f"▶ {Path(rel).name} 듣는 중")
        return True

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
