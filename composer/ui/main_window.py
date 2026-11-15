"""메인 창."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QScrollArea, QSizePolicy, QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..render.ffmpeg_builder import RenderSettings
from ..render.runner import cache_dir, clear_cache
from ..schema import Clip, Project, SubtitleSettings, Transition
from ..validate import has_errors, validate_project
from .auto_preview import AutoPreview
from .subtitle_style_panel import SubtitleStylePanel
from .dnd import ImageDefaultsDialog, paths_from_mime
from .dubbing_panel import DubbingPanel
from .inspector import Inspector, _rel, set_bgm_file
from .model import ProjectModel
from .preview import PreviewPlayer, fmt_time
from .prompt_dialog import PromptDialog
from .render_dialog import FinalRenderOptions, ProgressDialog, RenderWorker
from .timeline_widget import TimelineView

EMPTY_HINT = "이미지·동영상을 끌어다 놓거나 'AI 로 구성하기'로 시작하세요"
MEDIA_FILTER = "미디어 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 *.mov *.mkv *.webm *.avi *.m4v)"


class KeyFilter(QObject):
    """텍스트 입력 중이 아닐 때만 단축키(Space, R, ←/→ 등)를 처리합니다."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.KeyPress or not self.win.isActiveWindow():
            return False
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit, QComboBox)):
            return False
        return self.win.handle_key(ev)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1500, 950)
        self.model = ProjectModel()
        self.preview_revision = -1
        self.preview_is_direct = False

        self.player = PreviewPlayer()
        self.player.subtitle_fn = self._subtitle_at
        self.player.cue_fn = self._cue_at
        self.player.placeholder_fn = self._placeholder_at
        self.player.before_play = self._before_play
        self._thumbs: dict = {}
        self._play_without_preview_ok = False
        self.dub = DubbingPanel(self.model, self.player)
        self.inspector = Inspector(self.model)
        self.timeline = TimelineView(self.model)

        self.image_defaults = ImageDefaultsDialog(self)  # ➕ 미디어 추가 ▾ ▸ 새 이미지 기본값
        # 오른쪽: 작업 순서대로 4개 탭 — 🛠 속성 · 🎤 녹음 · 🅰 자막 · 📤 출력
        self.right = QTabWidget()
        # 오른쪽 패널 내용(자막 글 길이 등)이 바뀌어도 위쪽 영역 높이·미리보기 크기에 영향 주지 않게
        self.right.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        self.right.setMinimumWidth(320)

        def scroll(w):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QScrollArea.NoFrame)
            sc.setWidget(w)
            return sc
        self.dub_scroll = scroll(self.dub)  # 창이 낮아도 녹음 탭이 찌그러지지 않게 (스크롤)
        self.style_panel = SubtitleStylePanel(self.model)
        self.sub_tab = QWidget()
        sl = QVBoxLayout(self.sub_tab)
        sl.addWidget(self.inspector.sub_file_panel)
        sl.addWidget(self.style_panel, 1)
        self.sub_scroll = scroll(self.sub_tab)
        self.output_scroll = scroll(self.inspector.output_page)
        from .effects_panel import EffectsPanel
        self.effects = EffectsPanel(self.model)
        self.effects_scroll = scroll(self.effects)
        self.right.addTab(self.inspector, "🛠 속성")
        self.right.addTab(self.effects_scroll, "✨ 효과")
        self.right.addTab(self.dub_scroll, "🎤 녹음")
        self.right.addTab(self.sub_scroll, "🅰 자막")
        self.right.addTab(self.output_scroll, "📤 출력")
        self.right.setTabToolTip(0, "타임라인에서 고른 클립·전환·자막·더빙·배경음악의 설정")
        self.right.setTabToolTip(1, "고른 클립의 색 필터 · 밝기·대비·채도 · 분위기(비네팅·흐림·노이즈) · 움직임")
        self.right.setTabToolTip(2, "마이크 녹음 — 자막 따라 녹음 / 자유 녹음(대본 먼저) · 녹음으로 자막 만들기")
        self.right.setTabToolTip(3, "자막 파일 불러오기·저장 · 자막 모양(스타일)")
        self.right.setTabToolTip(4, "영상 크기 · 기본 전환 · 자막 넣는 방식 · 시작/끝 페이드 · 내보내기")
        self.inspector.clip_page.effects_requested.connect(lambda: self.right.setCurrentWidget(self.effects_scroll))
        self.inspector.style_requested.connect(self.show_style_tab)
        self.right.currentChanged.connect(
            lambda i: self.dub.ensure_devices() if self.right.widget(i) is self.dub_scroll else None)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.player)
        top.addWidget(self.right)
        top.setSizes([900, 560])
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        self._make_actions()
        main.addWidget(self._build_timeline_box())
        main.setSizes([700, 260])
        self.setCentralWidget(main)

        self.player.position_changed.connect(self.timeline.set_playhead)
        self.timeline.canvas.seek_requested.connect(self.player.seek)
        self.timeline.canvas.context_requested.connect(self._timeline_menu)
        # 드롭 이벤트가 끝난 뒤 처리(드롭 중에 창을 띄우면 Windows 탐색기 끌기가 멈춤)
        self.timeline.canvas.files_dropped.connect(lambda paths, idx: self._drop_later(paths, idx))
        # 탐색기에서 창 어디에 놓아도 받기 (미리보기 화면은 부모 창으로 넘김)
        self.setAcceptDrops(True)
        self.player.view.setAcceptDrops(False)
        self.player.view.viewport().setAcceptDrops(False)
        self.last_drop_report = ""
        ins = self.inspector
        ins.take_page.play_requested.connect(self.dub.play_take)
        ins.take_page.rerecord_requested.connect(self.rerecord_take)
        ins.take_page.play_file_requested.connect(self.dub.play_file)
        ins.take_page.delete_file_requested.connect(self.delete_take_file)
        ins.sub_file_panel.remove_subtitles_requested.connect(self.remove_all_subtitles)
        ins.bgm_page.remove_requested.connect(self.remove_bgm)
        ins.bgm_page.pick_requested.connect(lambda: self.add_bgm())
        ins.output_page.export_requested.connect(self.render_final)
        ins.output_page.voice_requested.connect(self.export_voice)
        ins.empty.ai_requested.connect(lambda: self.open_prompt_dialog())
        ins.empty.add_requested.connect(self.add_clips)
        ins.empty.bgm_requested.connect(lambda: self.add_bgm())
        ins.empty.sub_requested.connect(lambda: self.add_subtitle_file())
        self.dub.script_requested.connect(self.open_script_dialog)
        self.timeline.canvas.razor_requested.connect(self._razor)
        self.timeline.canvas.status_requested.connect(lambda t: self.statusBar().showMessage(t, 6000))
        self.timeline.canvas.add_requested.connect(
            lambda k: self.add_bgm() if k == "bgm" else self.add_subtitle_file())
        self.dub.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.model.changed.connect(self._on_changed)
        self.model.selection_changed.connect(self._on_selection)
        self.model.dirty_changed.connect(lambda _: self._title())
        self.model.path_changed.connect(self._title)
        self.player.playing_changed.connect(lambda p: None if p else self.dub.stop_live())

        self.settings = QSettings("knhanul", "VideoComposer")
        self._preview_seq = 0
        self._preview_worker = None
        self.auto = AutoPreview(self._render_revision_key, self._start_preview_worker, self._cancel_preview_worker,
                                delay_ms=1500, parent=self)
        self.auto.enabled = str(self.settings.value("view/auto_preview", True)).lower() not in ("false", "0")
        self.auto.state_changed.connect(lambda _: self._update_notice())
        self.model.changed.connect(self.auto.notify_changed)
        # 배경음악은 미리보기에서 따로 재생 (🔇/S 를 바꿔도 영상은 다시 만들지 않음)
        from .bgm_live import BgmLive
        self.bgm_live = BgmLive(self)
        self.player.position_changed.connect(lambda t: self._sync_bgm())
        self.player.playing_changed.connect(lambda _: self._sync_bgm())
        self.player.on_volume = self._apply_bgm_volume
        self.bgm_live.building_changed.connect(self._bgm_building)
        self.bgm_live.failed.connect(self._bgm_failed)
        self.player.playing_changed.connect(self._bgm_on_play)
        self.model.changed.connect(self._apply_track_audio)
        self._apply_track_audio()
        self.player.btn_refresh.clicked.connect(self.render_preview)

        self._build_menus()
        self.key_filter = KeyFilter(self)
        QApplication.instance().installEventFilter(self.key_filter)
        self._title()
        self.statusBar().showMessage("'🤖 AI 로 구성하기' 또는 '동영상+자막으로 더빙 시작'으로 시작하거나, "
                                     "탐색기에서 이미지·영상·폴더를 끌어다 놓으세요.")
        QTimer.singleShot(0, self._on_changed)

    # ------------------------------------------------------------------ 메뉴
    def _act(self, menu, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _make_actions(self):
        # 여러 곳(메뉴·툴바·타임라인 툴바)에 같은 동작을 하나의 QAction 으로
        self.act_undo = QAction("↶ 실행 취소", self, shortcut=QKeySequence("Ctrl+Z"), toolTip="실행 취소 (Ctrl+Z)")
        self.act_undo.triggered.connect(lambda: self.model.undo())
        self.act_redo = QAction("↷ 다시 실행", self, shortcut=QKeySequence("Ctrl+Y"), toolTip="다시 실행 (Ctrl+Y)")
        self.act_redo.triggered.connect(lambda: self.model.redo())
        self.act_export = QAction("📤 영상 내보내기…", self)
        self.act_export.setShortcut("Ctrl+E")
        self.act_export.triggered.connect(self.render_final)
        self.act_export_voice = QAction("목소리만 WAV로 내보내기…", self)
        self.act_export_voice.triggered.connect(self.export_voice)
        self.act_follow = QAction("📍 재생 위치 따라가기", self, checkable=True, checked=True)
        self.act_follow.setToolTip("재생할 때 타임라인이 재생 위치를 따라 움직임")
        self.act_follow.toggled.connect(lambda c: setattr(self.timeline, "follow", c))
        self.act_magnet = QAction("🧲 자석", self, checkable=True)
        self.act_magnet.setToolTip("끌 때 클립 끝·재생 위치·자막 경계·In/Out 에 붙기 (Shift 를 누르고 끌면 끔)")
        self.act_magnet.setChecked(str(QSettings("knhanul", "VideoComposer").value("timeline/magnet", True)).lower()
                                   not in ("false", "0"))
        self.timeline.canvas.magnet = self.act_magnet.isChecked()
        self.act_magnet.toggled.connect(self._set_magnet)
        self.act_clear_marks = QAction("In/Out 지우기", self, shortcut=QKeySequence("Esc"))
        self.act_clear_marks.setShortcutContext(Qt.WidgetShortcut)  # Esc 는 handle_key 에서 (입력 칸과 안 겹치게)
        self.act_clear_marks.triggered.connect(self.clear_marks)


    def _build_menus(self):
        from PySide6.QtWidgets import QMenu, QToolButton
        mb = self.menuBar()
        f = mb.addMenu("파일(&F)")
        self._act(f, "새로 만들기", self.new_project, "Ctrl+N")
        self._act(f, "열기…", self.open_project, "Ctrl+O")
        self.recent_menu = f.addMenu("최근 파일")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        f.addSeparator()
        self._act(f, "저장", self.save, "Ctrl+S")
        self._act(f, "다른 이름으로 저장…", self.save_as, "Ctrl+Shift+S")
        f.addSeparator()
        self._act(f, "➕ 미디어 추가 (이미지·영상)…", self.add_clips, "Ctrl+I")
        self._act(f, "♪ 배경음악 넣기…", lambda: self.add_bgm())
        self._act(f, "📝 자막 파일 불러오기…", lambda: self.add_subtitle_file())
        f.addSeparator()
        f.addAction(self.act_export)
        f.addAction(self.act_export_voice)
        self._act(f, "📄 대본 출력…", self.open_script_dialog, "Ctrl+P")
        f.addSeparator()
        self._act(f, "🤖 AI 로 구성하기…", self.open_prompt_dialog, "Ctrl+Shift+A")
        self._act(f, "동영상 + 자막으로 더빙 시작…", self.open_video_for_dubbing, "Ctrl+Shift+O")
        f.addSeparator()
        self._act(f, "캐시 비우기 (미리보기·내보내기 중간 파일)", self.clear_cache)
        f.addSeparator()
        self._act(f, "끝내기", self.close, "Alt+F4")

        e = mb.addMenu("편집(&E)")
        e.addAction(self.act_undo)
        e.addAction(self.act_redo)
        e.addSeparator()
        self._act(e, "AI 답변(JSON) 붙여넣어 적용…", lambda: self.open_prompt_dialog(tab=1), "Ctrl+Shift+V")
        self._act(e, "검사하기 (파일·값 확인)", self.validate_current)
        e.addSeparator()
        self._act(e, "✂ 재생 위치에서 자르기", lambda: self.cut_at(), "Ctrl+K")
        self._act(e, "구간 잘라내기 (In~Out)", self.cut_marked_range)
        e.addAction(self.act_clear_marks)
        e.addSeparator()
        self.act_clear_subs = self._act(e, "자막 전체 제거", self.remove_all_subtitles,
                                        tip="프로젝트에서 자막을 모두 뺍니다(원본 SRT 파일은 그대로)")
        self.act_clear_bgm = self._act(e, "배경음악 제거", self.remove_bgm,
                                       tip="프로젝트에서 배경음악을 뺍니다(원본 음악 파일은 그대로)")
        e.addSeparator()
        self._act(e, "새 이미지 기본값…", self.show_image_defaults)

        v = mb.addMenu("보기(&V)")
        self._act(v, "타임라인 확대", self.zoom_in, "Ctrl+=")
        self._act(v, "타임라인 축소", self.zoom_out, "Ctrl+-")
        self._act(v, "타임라인 전체 보기", self.zoom_fit, "Ctrl+0")
        v.addAction(self.act_follow)
        v.addAction(self.act_magnet)
        v.addSeparator()
        self.act_auto = QAction("자동 미리보기 (편집하면 알아서 다시 만들기)", self)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(self.auto.enabled)
        self.act_auto.toggled.connect(self._on_auto_toggled)
        v.addAction(self.act_auto)
        self._act(v, "🔄 미리보기 새로 만들기 (360p)", self.render_preview, "F5")

        h = mb.addMenu("도움말(&H)")
        self._act(h, "단축키", self.show_shortcuts, "F1")
        self._act(h, "정보", lambda: QMessageBox.about(self, APP_NAME, f"{APP_NAME} {__version__}\nAI JSON 으로 영상 자동 조립 + 더빙"))

        # --- 맨 위 툴바: 🤖 AI · 📂 열기 · 💾 저장 · ➕ 미디어 추가 ▾ · ↶ ↷ · (오른쪽) 📤 영상 내보내기 ---
        tb = self.top_toolbar = self.addToolBar("주요 기능")
        tb.setMovable(False)
        for text, slot in [("🤖 AI 로 구성하기", self.open_prompt_dialog), ("📂 열기", self.open_project),
                           ("💾 저장", self.save)]:
            a = QAction(text, self)
            a.triggered.connect(lambda _=False, s=slot: s())
            tb.addAction(a)
        self.btn_add_media = QToolButton()
        self.btn_add_media.setText("➕ 미디어 추가")
        self.btn_add_media.setToolTip("이미지·영상 추가 (Ctrl+I) — ▾ 에서 새 이미지 기본값")
        self.btn_add_media.setPopupMode(QToolButton.MenuButtonPopup)
        self.btn_add_media.clicked.connect(self.add_clips)
        am = QMenu(self.btn_add_media)
        am.addAction("➕ 이미지·영상 추가…", self.add_clips)
        am.addAction("♪ 배경음악 넣기…", lambda: self.add_bgm())
        am.addAction("📝 자막 파일 불러오기…", lambda: self.add_subtitle_file())
        am.addSeparator()
        am.addAction("새 이미지 기본값 (표시 시간 · 움직임 · 순서 묻기)…", self.show_image_defaults)
        self.btn_add_media.setMenu(am)
        tb.addWidget(self.btn_add_media)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.btn_export_top = QToolButton()
        self.btn_export_top.setDefaultAction(self.act_export)
        self.btn_export_top.setText("📤 영상 내보내기")
        self.btn_export_top.setStyleSheet("QToolButton{background:#2e7d32;color:white;font-weight:bold;padding:4px 14px;"
                                          "border-radius:4px}")
        tb.addWidget(self.btn_export_top)

    def show_shortcuts(self):
        QMessageBox.information(self, "단축키", "\n".join([
            "Space : 재생 / 일시정지 (녹음 중이면 정지)", "R : ● 선택 자막 녹음 (녹음 중이면 정지)", "Shift+R : ● 여기부터 연속 녹음 시작/정지",
            "← / → : 이전 / 다음 자막 줄", "Ctrl+← / → : 0.1초 뒤로/앞으로 (Shift 를 더하면 1초)",
            "[ / ] : 선택한 자막·테이크·배경음악을 0.1초 앞당기기/늦추기 (Shift = 0.01초)",
            "V : ↖ 선택 도구   C : ✂ 자르기 도구(누른 곳을 자름)   Esc : 선택 도구로 / In·Out 지우기",
            "Ctrl+K : 재생 위치에서 자르기   I / O : 구간 In / Out   Delete : 고른 항목 삭제(클립은 뒤가 당겨짐)",
            "Ctrl+= / Ctrl+- / Ctrl+0 : 타임라인 확대 / 축소 / 전체 보기",
            "Home : 처음으로", "F5 : 미리보기 새로 만들기 (보통은 자동)", "Ctrl+E : 영상 내보내기", "Ctrl+P : 대본 출력",
            "Ctrl+Z / Ctrl+Y : 실행 취소 / 다시 실행", "Ctrl+S : 저장",
            "타임라인: 끌어서 이동, 끝을 끌어서 길이, 🧲 자석으로 경계에 붙음, Shift 끌기 = 자석 끄고 0.01초, Ctrl+휠 = 확대",
        ]))

    # ------------------------------------------------------------------ 단축키
    def handle_key(self, ev) -> bool:
        k, mod = ev.key(), ev.modifiers()
        shift = bool(mod & Qt.ShiftModifier)
        ctrl = bool(mod & Qt.ControlModifier)
        if k == Qt.Key_Space and not ctrl:
            if self.dub.mode:
                self.dub.stop_recording()
            else:
                self.player.toggle()
            return True
        if k == Qt.Key_R and not ctrl:
            self.dub.toggle_session() if shift else self.dub.record_line()
            return True
        if k in (Qt.Key_Left, Qt.Key_Right):
            d = -1 if k == Qt.Key_Left else 1
            if ctrl:
                self.player.seek(self.player.position() + d * (1.0 if shift else 0.1))
            else:
                self.dub.select_relative(d)
            return True
        if k in (Qt.Key_BracketLeft, Qt.Key_BracketRight):
            self.nudge_selected((-1 if k == Qt.Key_BracketLeft else 1) * (0.01 if shift else 0.1))
            return True
        if k == Qt.Key_Home:
            self.player.seek(0)
            return True
        if k == Qt.Key_Delete:
            return self.delete_selected()
        if k == Qt.Key_V and not ctrl and not shift:
            self.set_tool("select")
            return True
        if k == Qt.Key_C and not ctrl and not shift:
            self.set_tool("razor")
            return True
        if k == Qt.Key_Escape:
            if self.timeline.canvas.tool == "razor":
                self.set_tool("select")
            else:
                self.clear_marks()
            return True
        if k == Qt.Key_I and not ctrl:
            self.set_mark_in()
            return True
        if k == Qt.Key_O and not ctrl:
            self.set_mark_out()
            return True
        return False

    # ------------------------------------------------------------------ 자르기 · 삭제 (프리미어처럼)
    def _build_timeline_box(self):
        """타임라인 툴바: ↖ 선택 · ✂ 자르기 | 🗑 삭제 | [ In · Out ] · ✂ 구간 잘라내기 · ☑ 모든 트랙 | 🧲 자석 |
        ＋ 전환 · ＋ 자막 · ● 녹음 | (오른쪽) 🔍− 확대 🔍+ · ⤢ 전체 보기 · 📍 따라가기"""
        from PySide6.QtGui import QActionGroup
        from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.setSpacing(3)

        def b(text, tip, slot):
            w = QPushButton(text)
            w.setToolTip(tip)
            w.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)  # 글자만큼만 (늘어나지 않게)
            w.clicked.connect(lambda _=False: slot())
            bar.addWidget(w)
            return w

        def tool(action):
            w = QToolButton()
            w.setDefaultAction(action)
            w.setToolButtonStyle(Qt.ToolButtonTextOnly)
            bar.addWidget(w)
            return w

        def sep():
            ln = QFrame(); ln.setFrameShape(QFrame.VLine); ln.setStyleSheet("color:#474b55;")
            bar.addWidget(ln)

        self.tool_group = QActionGroup(self)
        self.act_select = QAction("↖ 선택 (V)", self, checkable=True, checked=True)
        self.act_select.setToolTip("고르기·끌어서 옮기기·끝 끌어서 길이 (V)")
        self.act_razor = QAction("✂ 자르기 (C)", self, checkable=True)
        self.act_razor.setToolTip("누른 곳을 자릅니다 — 클립·자막·더빙 (C). 재생 위치에서 자르기는 Ctrl+K")
        for a, t in ((self.act_select, "select"), (self.act_razor, "razor")):
            self.tool_group.addAction(a)
            a.triggered.connect(lambda _=False, t=t: self.set_tool(t))
        self.btn_select = tool(self.act_select)
        self.btn_razor = tool(self.act_razor)
        sep()
        self.btn_delete = b("🗑 삭제", "고른 조각을 지웁니다 (Delete). 클립은 뒤 클립이 당겨져 붙습니다(리플 삭제).",
                            self.delete_selected)
        sep()
        self.btn_in = b("[ In", "구간 시작 표시 — 빨간 재생 위치 (I)", lambda: self.set_mark_in())
        self.btn_out = b("Out ]", "구간 끝 표시 — 빨간 재생 위치 (O)", lambda: self.set_mark_out())
        self.btn_cut_range = b("✂ 구간 잘라내기", "In~Out 구간을 빼고 뒤를 당겨 붙입니다 (Esc = In/Out 지우기)",
                               self.cut_marked_range)
        self.chk_all_tracks = QCheckBox("모든 트랙")
        self.chk_all_tracks.setChecked(True)
        self.chk_all_tracks.setToolTip("켜면 자막·더빙도 같은 구간을 빼고 함께 당깁니다(잠긴 트랙은 그대로)")
        bar.addWidget(self.chk_all_tracks)
        sep()
        self.btn_magnet = tool(self.act_magnet)
        sep()
        self.btn_add_tr = b("＋ 전환", "재생 위치에서 가장 가까운 클립 경계에 기본 전환 넣기", self.add_transition_here)
        self.btn_add_sub = b("＋ 자막", "재생 위치에 자막 한 줄 넣기 (2초)", self.add_subtitle_here)
        self.btn_rec = b("● 녹음 (R)", "녹음 탭으로 가서 녹음 시작 — 자막이 있으면 선택 자막 녹음, 없으면 자유 녹음",
                         self.record_from_timeline)
        self.btn_rec.setStyleSheet("QPushButton{color:#ff6b62;font-weight:bold}")
        bar.addStretch(1)
        self.btn_zoom_out = b("🔍−", "타임라인 축소 (Ctrl+-, Ctrl+휠)", self.zoom_out)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, 100)
        self.zoom_slider.setFixedWidth(110)
        self.zoom_slider.setToolTip("타임라인 확대/축소")
        self.zoom_slider.valueChanged.connect(lambda v: self.timeline.canvas.set_zoom(self._pps_of(v)))
        bar.addWidget(self.zoom_slider)
        self.btn_zoom_in = b("🔍+", "타임라인 확대 (Ctrl+=, Ctrl+휠)", self.zoom_in)
        self.btn_fit = b("⤢ 전체 보기", "프로젝트 전체가 한 화면에 들어오게 (Ctrl+0)", self.zoom_fit)
        self.btn_follow = tool(self.act_follow)
        self.timeline.canvas.zoom_changed.connect(self._sync_zoom_slider)
        self._sync_zoom_slider(self.timeline.canvas.pps)
        lay.addLayout(bar)
        lay.addWidget(self.timeline, 1)
        self._update_cut_buttons()
        return box

    # --- 확대 / 축소 (슬라이더는 로그 눈금: 5 ~ 800 px/초) ---
    @staticmethod
    def _pps_of(v: int) -> float:
        return 5.0 * (160.0 ** (v / 100.0))

    def _sync_zoom_slider(self, pps: float):
        import math
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(round(100 * math.log(max(5.0, pps) / 5.0) / math.log(160.0))))
        self.zoom_slider.blockSignals(False)

    def zoom_in(self):
        self.timeline.canvas.set_zoom(self.timeline.canvas.pps * 1.3)

    def zoom_out(self):
        self.timeline.canvas.set_zoom(self.timeline.canvas.pps / 1.3)

    def zoom_fit(self):
        total = max(self.model.timeline.total, 1.0)
        vw = max(200, self.timeline.viewport().width())
        self.timeline.canvas.set_zoom((vw - 40) / total)
        self.timeline.horizontalScrollBar().setValue(0)

    def _set_magnet(self, on: bool):
        self.timeline.canvas.magnet = bool(on)
        QSettings("knhanul", "VideoComposer").setValue("timeline/magnet", bool(on))

    def set_tool(self, t: str):
        self.timeline.canvas.set_tool(t)
        (self.act_razor if t == "razor" else self.act_select).setChecked(True)
        if t == "razor":
            self.statusBar().showMessage("✂ 자르기 도구 — 클립·자막·더빙에서 자를 곳을 누르세요 (V 또는 Esc = 선택 도구)", 6000)

    def _razor(self, kind: str, idx: int, t: float):
        self.model.select(kind, idx)
        self.cut_at(t)

    # --- ＋ 전환 / ＋ 자막 / ● 녹음 ---
    def add_transition_here(self) -> bool:
        from ..edit_ops import add_transition
        if self._locked_msg("clip"):
            return False
        t = self.player.position()
        tl = self.model.timeline
        box = {}
        self.model.edit(lambda p: box.setdefault("i", add_transition(p, tl, t)))
        i = box.get("i")
        if i is None:
            self.statusBar().showMessage("클립이 2개 이상 있어야 전환을 넣을 수 있습니다", 5000)
            return False
        self.model.select("transition", i)
        tr = self.model.timeline.clips[i].transition_in
        self.statusBar().showMessage(f"＋ {i}번 ↔ {i + 1}번 클립 사이에 전환 {tr.duration:.2f}초 (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def add_subtitle_here(self) -> bool:
        from ..edit_ops import insert_cue
        if self._locked_msg("subtitle"):
            return False
        t = self.player.position()
        box = {}
        self.model.edit(lambda p: box.setdefault("i", insert_cue(p, t)))
        idx = box.get("i")
        if idx is None:
            self.statusBar().showMessage("여기에는 자막을 넣을 자리가 없습니다 (다른 자막과 겹침)", 5000)
            return False
        self.model.select("subtitle", idx)
        self.right.setCurrentWidget(self.inspector)
        te = self.inspector.sub_page.text
        te.setFocus()
        te.selectAll()
        self.statusBar().showMessage("＋ 자막을 넣었습니다 — 오른쪽 '내용'에 글을 쓰세요 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    def record_from_timeline(self):
        """● 녹음: 녹음 탭으로 가서 — 자막이 있으면 선택(또는 재생 위치의) 자막 녹음, 없으면 자유 녹음."""
        if self.dub.mode:
            self.dub.stop_recording()
            return
        self.right.setCurrentWidget(self.dub_scroll)
        if self.model.cues:
            self.dub.mode_tabs.setCurrentIndex(0)
            if self.model.selection.kind != "subtitle":
                c = self.model.cue_timeline.current(self.player.position())
                if c is not None:
                    self.model.select("subtitle", c.index)
            self.dub.record_line()
        else:
            self.dub.mode_tabs.setCurrentIndex(1)
            self.dub.free_page.toggle_record()

    def rerecord_take(self, i: int):
        """더빙 속성의 ● 다시 녹음: 자막 줄 녹음이면 그 줄을 다시(새 버전), 자유 녹음이면 그 자리에서 새로 녹음."""
        takes = self.model.project.dubbing.takes
        if not (0 <= i < len(takes)) or self._locked_msg("take"):
            return
        tk = takes[i]
        self.right.setCurrentWidget(self.dub_scroll)
        if tk.subtitle is not None and self.model.cue_by_index(tk.subtitle) is not None:
            self.dub.mode_tabs.setCurrentIndex(0)
            self.model.select("subtitle", tk.subtitle)
            self.dub.record_line()
        else:
            self.dub.mode_tabs.setCurrentIndex(1)
            self.player.seek(self.model.take_positions()[i] + (tk.trim_start or 0.0))
            self.dub.free_page.toggle_record()
            self.statusBar().showMessage("새 녹음이 더해집니다 — 예전 녹음은 타임라인에서 고르고 Delete 로 지울 수 있어요", 8000)

    # --- 넣기 (배경음악 · 자막 파일) ---
    def ask_bgm_file(self) -> str:
        f, _ = QFileDialog.getOpenFileName(self, "배경음악", str(self.model.project.base_dir),
                                           "오디오 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus)")
        return f

    def add_bgm(self, f: str = "") -> bool:
        if self._locked_msg("bgm"):
            return False
        f = f or self.ask_bgm_file()
        if not f:
            return False
        set_bgm_file(self.model, f)
        self.model.select("bgm", 0)
        self.statusBar().showMessage(f"♪ 배경음악: {Path(f).name} — 오른쪽 🛠 속성에서 볼륨·페이드 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    def add_subtitle_file(self, f: str = "") -> bool:
        if self._locked_msg("subtitle"):
            return False
        if not self.inspector.sub_file_panel.pick_sub(f):
            return False
        self.statusBar().showMessage(f"📝 자막 {len(self.model.cues)}줄을 불러왔습니다 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    def delete_take_file(self, rel: str) -> bool:
        """녹음 기록의 🗑: 지금 어느 조각도 쓰지 않는 녹음 파일만, 확인 후 지움(파일 삭제는 되돌릴 수 없음)."""
        p = self.model.project
        if self._locked_msg("take"):
            return False
        norm = rel.replace("\\", "/").lower()
        if any(t.file.replace("\\", "/").lower() == norm for t in p.dubbing.takes):
            self.statusBar().showMessage("지금 쓰는 녹음은 지울 수 없습니다 — 다른 녹음을 '이걸로 쓰기' 한 뒤 지우세요", 6000)
            return False
        path = p.resolve(rel)
        if not path or not path.exists():
            return False
        if not self.confirm("녹음 파일 삭제", f"{Path(rel).name} 파일을 지울까요?\n지운 파일은 되돌릴 수 없습니다(Ctrl+Z 로도 안 됨)."):
            return False
        try:
            path.unlink()
        except OSError as e:
            self.statusBar().showMessage(f"지울 수 없습니다: {e}", 6000)
            return False
        self.model.commit()  # 바뀐 내용은 없지만 녹음 기록·녹음 탭 목록을 새로
        self.statusBar().showMessage(f"🗑 {Path(rel).name} 을(를) 지웠습니다", 5000)
        return True

    def show_image_defaults(self):
        self.image_defaults.show()
        self.image_defaults.raise_()

    def _update_cut_buttons(self):
        c = self.timeline.canvas
        ok = c.mark_in is not None and c.mark_out is not None and abs(c.mark_out - c.mark_in) >= 0.1
        self.btn_cut_range.setEnabled(ok)
        self.act_clear_marks.setEnabled(c.mark_in is not None or c.mark_out is not None)

    def _file_len(self, rel: str) -> float:
        from ..probe import probe
        p = self.model.project.resolve(rel)
        try:
            return float(probe(p).duration or 0.0) if p and p.exists() else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def cut_at(self, t: Optional[float] = None) -> bool:
        """✂ 자르기: 고른 클립/자막/더빙을(없거나 위치가 밖이면 재생 위치 아래 클립을) t 초에서 둘로."""
        from ..edit_ops import clip_index_at, split_clip, split_cue, split_take
        t = self.player.position() if t is None else t
        s = self.model.selection
        kind, idx = s.kind, s.index
        if kind == "clip":
            tm = self.model.timeline.clips[idx] if 0 <= idx < len(self.model.timeline.clips) else None
            if tm is None or not (tm.start < t < tm.end):
                kind = ""
        if kind not in ("clip", "subtitle", "take"):
            kind, idx = "clip", clip_index_at(self.model.timeline, t)
            if idx is None:
                self.statusBar().showMessage("재생 위치 아래에 자를 클립이 없습니다", 5000)
                return False
        if self._locked_msg(kind):
            return False
        box = {}
        tl = self.model.timeline
        if kind == "clip":
            self.model.edit(lambda p: box.setdefault("ok", split_clip(p, tl, idx, t)))
        elif kind == "subtitle":
            self.model.edit(lambda p: box.setdefault("ok", split_cue(p, idx, t)))
        else:
            # 타임라인에는 더빙이 녹음 지연 보정만큼 앞당겨 그려지므로, 보이는 위치 기준으로 자름
            tt = t + self.model.project.dubbing.latency_ms / 1000.0
            self.model.edit(lambda p: box.setdefault("ok", split_take(p, idx, tt, self._file_len)))
        if not box.get("ok"):
            where = "자를 위치(재생 헤드)를 고른 더빙 조각 안쪽에" if kind == "take" else "재생 위치를 그 항목 안쪽에"
            self.statusBar().showMessage(f"자를 수 없습니다 — {where} 두세요 (끝에서 0.1초 이상 안쪽)", 6000)
            return False
        name = {"clip": "클립", "subtitle": "자막", "take": "더빙"}[kind]
        self.model.select(kind, idx + 1)  # 뒤 조각을 고름
        self.statusBar().showMessage(f"✂ {name}을(를) {fmt_time(t)}에서 둘로 나눴습니다 (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def delete_selected(self) -> bool:
        """🗑 삭제 (Delete): 고른 조각 지우기. 클립은 리플 삭제(뒤가 당겨 붙음)."""
        kind = self.model.selection.kind
        if kind in ("clip", "subtitle", "take", "bgm", "transition") and self._locked_msg(kind):
            return True
        if kind == "clip":
            self.inspector.clip_page._delete()
            self.statusBar().showMessage("클립을 지우고 뒤 클립을 당겨 붙였습니다 (Ctrl+Z 로 되돌리기)", 5000)
            return True
        if kind == "subtitle":
            if self.model.cue_by_index(self.model.selection.index) is not None:
                self.inspector.sub_page._delete()
                self.statusBar().showMessage("자막 한 줄을 지웠습니다 (Ctrl+Z 로 되돌리기)", 5000)
            return True
        if kind == "take":
            self.inspector.take_page._delete()
            self.statusBar().showMessage("더빙 조각을 지웠습니다 (원본 파일은 그대로, Ctrl+Z 로 되돌리기)", 5000)
            return True
        if kind == "transition":
            self.inspector.tr_page.remove()
            self.statusBar().showMessage("전환을 없앴습니다 (Ctrl+Z 로 되돌리기)", 5000)
            return True
        if kind == "bgm":
            self.remove_bgm()
            return True
        if kind == "":
            self.statusBar().showMessage("지울 항목을 먼저 타임라인에서 고르세요", 4000)
        return False

    def set_mark_in(self, t: Optional[float] = None):
        c = self.timeline.canvas
        t = round(self.player.position() if t is None else t, 3)
        c.set_marks(t, c.mark_out if c.mark_out is not None and c.mark_out > t else None)
        self._update_cut_buttons()
        self.statusBar().showMessage(f"In {fmt_time(t)} — 이제 Out(O)을 표시하세요", 4000)

    def set_mark_out(self, t: Optional[float] = None):
        c = self.timeline.canvas
        t = round(self.player.position() if t is None else t, 3)
        c.set_marks(c.mark_in if c.mark_in is not None and c.mark_in < t else None, t)
        self._update_cut_buttons()
        self.statusBar().showMessage(f"Out {fmt_time(t)}", 4000)

    def clear_marks(self):
        self.timeline.canvas.set_marks(None, None)
        self._update_cut_buttons()

    def cut_marked_range(self) -> bool:
        """✂ 구간 잘라내기: In~Out 을 클립에서 빼고 당김(선택: 자막·더빙도 함께). 되돌리기 한 번."""
        from ..edit_ops import cut_range
        from ..timeline import build_timeline
        from ..tracks import locked
        c = self.timeline.canvas
        if c.mark_in is None or c.mark_out is None:
            self.statusBar().showMessage("먼저 In(I)과 Out(O)을 표시하세요", 5000)
            return False
        if self._locked_msg("clip"):
            return False
        a, b = c.mark_in, c.mark_out
        allt = self.chk_all_tracks.isChecked()
        skip = {k for k in ("subtitle", "dubbing") if locked(self.model.project, k)}
        box = {}

        def fn(p):
            box["r"] = cut_range(p, lambda: build_timeline(p, self.model.duration_of), a, b, allt, self._file_len, skip)
        self.model.edit(fn)
        removed, note = box.get("r", (0.0, ""))
        if removed <= 0:
            self.statusBar().showMessage("잘라낼 클립이 없습니다", 5000)
            return False
        self.clear_marks()
        self.model.select("")
        self.player.seek(min(a, b))
        extra = " (잠긴 트랙은 그대로)" if allt and skip else ""
        self.statusBar().showMessage(f"✂ {removed:.2f}초를 잘라내고 당겼습니다: {note}{extra} — Ctrl+Z 로 되돌리기", 8000)
        return True

    def _timeline_menu(self, gpos, t: float):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        m.addAction(f"✂ 여기서 자르기 ({fmt_time(t)})", lambda: self.cut_at(t))
        m.addAction("🗑 삭제", self.delete_selected).setEnabled(bool(self.model.selection.kind))
        m.addSeparator()
        m.addAction("[ 여기를 In 으로", lambda: self.set_mark_in(t))
        m.addAction("여기를 Out 으로 ]", lambda: self.set_mark_out(t))
        m.addAction("✂ 구간 잘라내기", self.cut_marked_range).setEnabled(self.btn_cut_range.isEnabled())
        m.addAction("In/Out 지우기 (Esc)", self.clear_marks).setEnabled(self.act_clear_marks.isEnabled())
        m.addSeparator()
        m.addAction("＋ 여기에 전환", lambda: (self.player.seek(t), self.add_transition_here()))
        m.addAction("＋ 여기에 자막", lambda: (self.player.seek(t), self.add_subtitle_here()))
        self._last_menu = m
        m.popup(gpos)

    def _apply_track_audio(self):
        """🔇/S: 미리보기 소리 크기만 바꿉니다(다시 렌더 없음). 배경음악 WAV 는 설정이 바뀌면 다시 만듦."""
        from ..tracks import audible
        p = self.model.project
        self.player.set_track_gain(1.0 if audible(p, "clips") else 0.0)  # → _apply_bgm_volume 도 불림
        self.bgm_live.update(p, self.model.timeline.total)
        self._sync_bgm()

    def _apply_bgm_volume(self):
        from ..tracks import audible
        if not hasattr(self, "bgm_live"):
            return
        self.bgm_live.set_volume(self.player.vol.value() / 100.0 * self.player._base_volume
                                 * (1.0 if audible(self.model.project, "bgm") else 0.0))

    BGM_PREP = "♪ 배경음악 준비 중… (잠시 뒤 함께 들려요)"

    def _bgm_building(self, on: bool):
        if on and self.player.is_playing():
            self.statusBar().showMessage(self.BGM_PREP)
        elif not on and self.statusBar().currentMessage() == self.BGM_PREP:
            self.statusBar().clearMessage()

    def _bgm_failed(self, err: str):
        first = (err or "").strip().splitlines()[0] if (err or "").strip() else "알 수 없는 오류"
        self.statusBar().showMessage(f"⚠ 배경음악을 미리보기에서 틀 준비를 하지 못했어요: {first} "
                                     "— 재생을 다시 누르면 한 번 더 해 봐요 (내보내기에는 영향 없음)", 12000)

    def _bgm_on_play(self, playing: bool):
        if not playing:
            return
        self.bgm_live.retry_if_failed(self.model.project, self.model.timeline.total)
        if self.bgm_live.building:
            self.statusBar().showMessage(self.BGM_PREP)

    def _sync_bgm(self):
        self.bgm_live.sync(self.player.position(), self.player.is_playing())

    def _locked_msg(self, kind: str) -> bool:
        """잠긴 트랙이면 상태 표시줄에 알리고 True."""
        from ..tracks import KIND_TRACK, TRACK_LABELS, locked
        if kind in KIND_TRACK and locked(self.model.project, kind):
            self.statusBar().showMessage(f"🔒 {TRACK_LABELS[KIND_TRACK[kind]]} 트랙이 잠겨 있습니다 — "
                                         "타임라인 왼쪽 트랙 머리의 🔒 를 눌러 푸세요", 6000)
            return True
        return False

    def nudge_selected(self, d: float):
        from ..subtitles import move_item

        s = self.model.selection
        if self._locked_msg(s.kind):
            return
        if s.kind == "subtitle":
            self.model.edit(lambda p: move_item(p, s.index, d), "nudge")
        elif s.kind == "take":
            t = self.model.project.dubbing.takes[s.index]
            self.model.edit(lambda p: setattr(t, "offset", round(t.offset + d, 3)), "nudge")
        elif s.kind == "bgm":
            self.model.edit(lambda p: setattr(p.bgm, "start_offset", max(0.0, round(p.bgm.start_offset + d, 3))), "nudge")

    # ------------------------------------------------------------------ 상태
    def show_output_tab(self):
        self.right.setCurrentWidget(self.output_scroll)

    def show_style_tab(self):
        self.right.setCurrentWidget(self.sub_scroll)
        self.style_panel.sample.replay()

    def _cue_at(self, t: float):
        if self.model.project.tracks.subtitle.hidden:  # 👁 자막 트랙 감춤
            return None
        c = self.model.cue_timeline.current(t)
        if not c:
            return None
        return c.text, t - c.start, c.end - t, c.end - c.start

    def _subtitle_at(self, t: float) -> str:
        if self.model.project.tracks.subtitle.hidden:
            return ""
        c = self.model.cue_timeline.current(t)
        return c.text if c else ""

    # ------------------------------------------------------------------ 미리보기 없는 재생
    def _placeholder_at(self, t: float):
        """미리보기 영상이 없을 때 화면에 보여 줄 현재 클립의 그림 (key, QPixmap)."""
        from PySide6.QtGui import QPixmap

        if self.model.project.tracks.clips.hidden:  # 👁 클립 감춤 → 검은 화면
            return ("hidden",), None
        tm = self.model.timeline.clip_at(t)
        if tm is None and self.model.timeline.clips:
            tm = self.model.timeline.clips[-1] if t >= self.model.timeline.total else self.model.timeline.clips[0]
        if tm is None:
            return None, None
        clip = self.model.project.clips[tm.index]
        path = self.model.project.resolve(clip.file)
        if not path or not path.exists():
            return ("missing", tm.index), None
        st = path.stat()
        key = (str(path), st.st_mtime, round(tm.source_in, 2))
        if key not in self._thumbs:
            pix = QPixmap()
            if clip.type == "image":
                pix.load(str(path))
            else:
                thumb = self._video_thumbnail(path, tm.source_in + min(0.5, max(0.0, tm.source_out - tm.source_in) / 2))
                if thumb:
                    pix.load(str(thumb))
            self._thumbs[key] = pix
        return key, self._thumbs[key]

    def _video_thumbnail(self, path: Path, at: float) -> Optional[Path]:
        import hashlib

        from ..probe import ffmpeg_path, popen_kwargs

        d = cache_dir(self.model.project) / "thumbs"
        d.mkdir(parents=True, exist_ok=True)
        st = path.stat()
        h = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}|{at:.2f}".encode()).hexdigest()[:16]
        out = d / f"{h}.jpg"
        if not out.exists():
            try:
                subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                                "-vf", "scale=960:-2", str(out)], timeout=15, capture_output=True, **popen_kwargs())
            except Exception:  # noqa: BLE001
                return None
        return out if out.exists() else None

    def ask_play_without_preview(self) -> str:
        """'render' | 'play' | 'cancel' (테스트에서 바꿔 끼울 수 있음)."""
        box = QMessageBox(QMessageBox.Question, "미리보기 없음",
                          "아직 미리보기 영상이 없습니다.\n\n"
                          "• 미리보기 만들기(F5): 전환·움직임·소리까지 실제 결과로 확인\n"
                          "• 영상 없이 재생: 클립 그림과 자막·카운트다운만 타임라인 시간에 맞춰 재생(더빙 연습용)",
                          QMessageBox.NoButton, self)
        b_render = box.addButton("미리보기 만들기 (F5)", QMessageBox.AcceptRole)
        b_play = box.addButton("영상 없이 재생", QMessageBox.ActionRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(b_render)
        box.exec()
        return "render" if box.clickedButton() is b_render else "play" if box.clickedButton() is b_play else "cancel"

    def _before_play(self) -> bool:
        """미리보기 영상 없이 재생을 누르면 한 번 묻습니다(프로젝트마다 한 번)."""
        if self._play_without_preview_ok or not self.model.project.clips:
            return True
        if self.auto.enabled and (self.auto.busy or self._preview_ready_to_render()):
            if not self.auto.busy:
                self.auto.request()
            return True  # 미리보기는 만드는 중 — 준비되면 재생 중에 자동으로 바뀜
        choice = self.ask_play_without_preview()
        if choice == "render":
            QTimer.singleShot(0, self.render_preview)
            return False
        if choice == "play":
            self._play_without_preview_ok = True
            self.statusBar().showMessage("미리보기 영상 없이 타임라인 시간으로 재생합니다 (F5 = 미리보기 만들기)", 6000)
            return True
        return False

    def _title(self):
        name = self.model.path.name if self.model.path else getattr(self, "untitled_name", "제목 없음")
        self.setWindowTitle(f"{name}{' *' if self.model.dirty else ''} — {APP_NAME}")

    def remove_all_subtitles(self) -> bool:
        """프로젝트에서 자막을 모두 뺍니다. 디스크의 자막 파일은 절대 지우지 않습니다."""
        from ..subtitles import clear_subtitles, has_subtitles

        if not has_subtitles(self.model.project) or self._locked_msg("subtitle"):
            return False
        if not self.confirm("자막 전체 제거", "프로젝트에서 자막을 모두 뺄까요? 원본 SRT 파일은 지워지지 않습니다."):
            return False
        holder = {}
        self.model.edit(lambda p: holder.setdefault("n", clear_subtitles(p)))  # 한 번의 실행 취소
        if self.model.selection.kind == "subtitle":
            self.model.select("")
        self.player.refresh_overlay()
        n = holder.get("n", 0)
        self.statusBar().showMessage("자막을 모두 뺐습니다 (Ctrl+Z 로 되돌리기)"
                                     + (f" · 더빙 {n}개는 지금 위치에 고정" if n else ""), 8000)
        return True

    def remove_bgm(self) -> bool:
        """프로젝트에서 배경음악을 뺍니다. 디스크의 음악 파일은 절대 지우지 않습니다."""
        if not self.model.project.bgm.file or self._locked_msg("bgm"):
            return False
        if not self.confirm("배경음악 제거", "프로젝트에서 배경음악을 뺄까요? 원본 음악 파일은 지워지지 않습니다."):
            return False
        self.model.edit(lambda p: setattr(p.bgm, "file", None))  # 한 번의 실행 취소, 다른 설정(볼륨 등)은 남김
        if self.model.selection.kind == "bgm":
            self.model.select("")
        self.statusBar().showMessage("배경음악을 뺐습니다 (Ctrl+Z 로 되돌리기)", 8000)
        return True

    def _on_changed(self):
        from ..subtitles import has_subtitles

        if hasattr(self, "act_clear_bgm"):
            self.act_clear_bgm.setEnabled(bool(self.model.project.bgm.file))
        if hasattr(self, "act_clear_subs"):
            self.act_clear_subs.setEnabled(has_subtitles(self.model.project))
        self.player.set_timeline_duration(self.model.timeline.total)
        self.player.set_frame_size(self.model.project.output.width, self.model.project.output.height)
        self.player._placeholder_key = None  # 클립이 바뀌었을 수 있으므로 그림 다시 고르기
        self.player.set_subtitle_style(self.model.project.subtitle.style, self.model.project.output.height)
        self.player.refresh_overlay()
        if self.preview_is_direct and not self._direct_source():
            self.preview_is_direct = False
            self.preview_revision = -1
        self._update_notice()

    def _update_notice(self):
        if not self.model.project.clips:
            self.player.set_notice("")
            self.player.set_empty_hint(EMPTY_HINT)
            self.timeline.canvas.empty_hint = EMPTY_HINT
            self.timeline.canvas.update()
            return
        self.player.set_empty_hint("")
        if self.timeline.canvas.empty_hint:
            self.timeline.canvas.empty_hint = ""
            self.timeline.canvas.update()
        auto = self.auto.enabled
        busy = auto and self.auto.busy
        if self.preview_is_direct:
            self.player.set_notice("")
        elif self.player.media_path is None:
            if busy:
                self.player.set_notice("미리보기 준비 중… 그동안 타임라인 시간으로 재생됩니다(클립 그림 + 자막)")
            else:
                self.player.set_notice("미리보기 영상 없음 — 타임라인 시간으로 재생(클립 그림 + 자막). 🔄 미리보기 새로 만들기")
        elif self.preview_revision != self._render_revision_key():
            if busy:
                self.player.set_notice("편집 내용을 반영한 미리보기를 만드는 중… (준비되면 자동으로 바뀜)")
            else:
                self.player.set_notice("화면이 최신 편집과 다릅니다 — 🔄 미리보기 새로 만들기 (자막·더빙은 바로 반영됨)")
        else:
            self.player.set_notice("")

    def _render_revision_key(self):
        """미리보기 영상에 영향을 주는 값(자막·더빙 제외)만으로 비교합니다."""
        d = self.model.project.to_dict()
        d.pop("subtitle", None)
        d.pop("dubbing", None)
        d.pop("title", None)
        d.pop("bgm", None)  # 배경음악은 따로 만든 WAV 를 같이 틂 (bgm_live)
        d["tracks"] = {"clips_hidden": d["tracks"]["clips"]["hidden"]}  # 🔇/S 는 소리 크기로 바로 반영
        d["output"].pop("file", None)
        for c in d.get("clips", []):
            c.pop("label", None)
        return hash(str(d))

    def _on_selection(self):
        s = self.model.selection
        # 고르면 🛠 속성 탭으로. 단 녹음 탭에서 자막·더빙을 고를 때는 녹음 흐름을 지키려고 그대로
        recording_tab = self.right.currentWidget() is self.dub_scroll
        effects_tab = self.right.currentWidget() is self.effects_scroll
        if s.kind == "clip" and effects_tab:
            pass  # ✨ 효과 탭에서 다른 클립을 고르면 효과 탭 그대로 (그 클립 효과로)
        elif s.kind in ("clip", "transition", "bgm") or (s.kind in ("subtitle", "take") and not recording_tab):
            self.right.setCurrentWidget(self.inspector)
        if s.kind == "clip" and 0 <= s.index < len(self.model.timeline.clips) and not self.player.is_playing():
            self.player.seek(self.model.timeline.clips[s.index].start)

    def _direct_source(self) -> Optional[Path]:
        """영상 하나뿐이고 변형이 없으면 렌더 없이 원본을 바로 미리보기."""
        p = self.model.project
        if len(p.clips) != 1 or p.tracks.clips.hidden:
            return None
        c = p.clips[0]
        if c.type != "video" or c.trim_start or c.trim_end or c.fit_duration or abs(c.speed - 1) > 1e-6:
            return None
        from ..effects import has_effects
        if has_effects(c) or (c.motion != "none" and c.motion_strength > 0) or p.output.fade_in > 0 or p.output.fade_out > 0:
            return None  # ✨ 효과·움직임·페이드가 있으면 미리보기를 만들어야 보임
        return p.resolve(c.file)

    def _load_preview_for_project(self):
        src = self._direct_source()
        self.preview_is_direct = bool(src and src.exists())
        if self.preview_is_direct:
            self.player.load(src, keep_position=False)
        else:
            self.player.load(None)
            self.preview_revision = -1
            if not self.auto.enabled:
                # 자동 미리보기를 끈 경우에만 예전 미리보기라도 보여 줌
                old = sorted(cache_dir(self.model.project).glob("preview_*.mp4"), key=lambda p: p.stat().st_mtime)
                if old:
                    self.player.load(old[-1], keep_position=False)
            else:
                self.auto.request()  # 열자마자 자동으로 만들기
        self._update_notice()

    # ------------------------------------------------------------------ 파일
    def maybe_save(self) -> bool:
        """저장 안 한 변경이 있으면 한 번만 묻습니다(저장 / 저장 안 함 / 취소).
        아무 내용도 없는 '제목 없음' 프로젝트는 묻지 않습니다."""
        if not self.model.dirty:
            return True
        from ..ai_apply import is_blank
        if is_blank(self.model.project, self.model.path is not None):
            return True
        box = QMessageBox(QMessageBox.Question, "저장", "현재 프로젝트의 변경 내용을 저장할까요?",
                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, self)
        box.button(QMessageBox.Save).setText("저장")
        box.button(QMessageBox.Discard).setText("저장 안 함")
        box.button(QMessageBox.Cancel).setText("취소")
        box.setDefaultButton(QMessageBox.Save)
        r = self.ask_save_changes(box)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def ask_save_changes(self, box: QMessageBox):
        """질문 창 표시 (테스트에서 바꿔 끼울 수 있음)."""
        return box.exec()

    def closeEvent(self, ev):
        if self.dub.mode:
            self.dub.stop_recording()
        if self.maybe_save():
            self.shutdown()
            ev.accept()
        else:
            ev.ignore()

    def shutdown(self):
        """멀티미디어 객체를 앱 종료 전에 정리합니다(종료 시 크래시 방지)."""
        self.auto.enabled = False
        self.auto.timer.stop()
        if hasattr(self, "bgm_live"):
            self.bgm_live.shutdown()
        w = getattr(self, "_preview_worker", None)
        if w is not None and w.isRunning():
            w.cancel()
            w.wait(10000)
        self.dub.recorder.close()
        self.dub.stop_live()
        self.player.player.stop()
        self.player.player.setVideoOutput(None)
        self.player.player.setSource(QUrl())

    def set_project(self, project: Project, path: Optional[Path] = None, untitled_name: str = "제목 없음"):
        self.dub.stop_live()
        self.untitled_name = untitled_name
        self._play_without_preview_ok = False
        self._thumbs.clear()
        self.auto.reset()
        self.model.set_project(project, path)
        if self.model.migrated:
            self.statusBar().showMessage("예전 형식 프로젝트를 새 전환 방식(경계 가운데)으로 바꿨습니다 — 보이는 시간은 그대로이고, "
                                         "저장하면 새 형식으로 저장됩니다.", 12000)
        self._load_preview_for_project()

    def open_prompt_dialog(self, tab: int = 0):
        if not isinstance(tab, int):
            tab = 0
        folder = self.model.project.base_dir if self.model.path or self.model.project.clips else None
        dlg = PromptDialog(folder, self)
        dlg.tabs.setCurrentIndex(tab)
        dlg.apply_requested.connect(self.apply_ai_result)
        dlg.exec()

    def apply_ai_result(self, data: dict, folder: Path):
        """검사를 통과한 AI 결과를 현재 타임라인에 반영합니다. 질문 창 없음 — Ctrl+Z 한 번으로 되돌림.
        저장된 프로젝트라면 파일 경로를 유지하므로 Ctrl+S 는 같은 파일에 저장합니다."""
        from ..ai_apply import apply_ai_result, is_blank

        self.dub.stop_live()
        blank = is_blank(self.model.project, self.model.path is not None)
        new = apply_ai_result(self.model.project, data, folder, keep_base=not blank)
        if blank:
            # 아무것도 없는 '제목 없음' 프로젝트: AI 폴더를 작업 폴더로 (되돌리면 다시 그 상태)
            self.untitled_name = "AI 프로젝트"
        self.model.replace_project(new)  # 한 번의 실행 취소 단계
        self.model.select("")
        self._load_preview_for_project()  # 이전 미리보기는 '최신 아님'으로 표시
        self._title()
        msg = "AI 구성을 타임라인에 반영했습니다. 미리보기를 자동으로 만드는 중입니다. 되돌리려면 Ctrl+Z"
        if self.model.path is None:
            msg += " · 저장하려면 Ctrl+S"
        self.statusBar().showMessage(msg, 15000)

    def new_project(self):
        """새로 만들기: 폴더를 묻지 않고 '제목 없음' 프로젝트를 엽니다.
        작업 폴더는 처음 추가하는 미디어 파일의 폴더로 자동으로 정해지고, 처음 저장할 때 저장 위치 기준으로 경로를 다시 씁니다."""
        if not self.maybe_save():
            return
        self.set_project(Project(base_dir=Path.home()), None)
        self.statusBar().showMessage(EMPTY_HINT, 8000)

    # ------------------------------------------------------------------ 최근 파일
    def recent_files(self) -> list:
        v = self.settings.value("file/recent", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or [])]

    def add_recent(self, path):
        items = [str(Path(path))] + [x for x in self.recent_files() if Path(x) != Path(path)]
        self.settings.setValue("file/recent", items[:10])

    def _fill_recent_menu(self):
        m = self.recent_menu
        m.clear()
        items = self.recent_files()
        if not items:
            a = m.addAction("(최근 파일 없음)")
            a.setEnabled(False)
            return
        for i, f in enumerate(items, 1):
            a = m.addAction(f"&{i % 10} {Path(f).name}   —   {Path(f).parent}")
            a.triggered.connect(lambda _=False, f=f: self.open_recent(f))
        m.addSeparator()
        m.addAction("목록 지우기").triggered.connect(lambda: self.settings.setValue("file/recent", []))

    def open_recent(self, f: str):
        p = Path(f)
        if not p.exists():
            QMessageBox.information(self, "최근 파일", f"파일을 찾을 수 없어 목록에서 뺍니다:\n{p}")
            self.settings.setValue("file/recent", [x for x in self.recent_files() if Path(x) != p])
            return
        if self.maybe_save():
            self.load_path(p)

    def open_video_for_dubbing(self):
        if not self.maybe_save():
            return
        v, _ = QFileDialog.getOpenFileName(self, "동영상 선택", "", "동영상 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)")
        if not v:
            return
        vp = Path(v)
        s, _ = QFileDialog.getOpenFileName(self, "자막 파일 선택 (없으면 취소)", str(vp.parent), "자막 (*.srt *.vtt)")
        p = Project(base_dir=vp.parent, title=vp.stem)
        p.transition = Transition("none", 0.0)
        from ..probe import probe

        try:
            mi = probe(vp)
            if mi.width and mi.height:
                p.output.width, p.output.height = mi.width - mi.width % 2, mi.height - mi.height % 2
            if mi.fps:
                p.output.fps = round(mi.fps, 3)
        except Exception:
            pass
        p.output.file = f"{vp.stem}_dubbed.mp4"
        p.clips = [Clip(file=vp.name, type="video")]
        if s:
            p.subtitle = SubtitleSettings(file=_rel(p, s))
        p.dubbing.folder = f"{vp.stem}_takes"
        self.set_project(p, None)
        self.right.setCurrentWidget(self.dub_scroll)
        self.dub.ensure_devices()
        self.model.save(vp.parent / f"{vp.stem}_dubbing.json")

    def open_project(self):
        if not self.maybe_save():
            return
        f, _ = QFileDialog.getOpenFileName(self, "열기", "", "프로젝트 (*.json)")
        if f:
            self.load_path(Path(f))

    def load_path(self, path: Path):
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "열기 실패", f"JSON 을 읽을 수 없습니다.\n{e}")
            return
        issues = validate_project(data, path.parent)
        if has_errors(issues):
            QMessageBox.warning(self, "프로젝트 확인", "문제가 있지만 열어 둡니다:\n\n" + "\n".join(map(str, issues[:20])))
        self.set_project(Project.from_dict(data, base_dir=path.parent), path)
        self.add_recent(path)

    def save(self) -> bool:
        if self.model.path is None:
            return self.save_as()
        self.model.save()
        self.add_recent(self.model.path)
        self.statusBar().showMessage(f"저장됨: {self.model.path}", 4000)
        return True

    def save_as(self) -> bool:
        # 저장된 적 없는 프로젝트는 미디어 폴더의 project.json 을 기본값으로 제안
        default = self.model.path or (Path(self.model.project.base_dir) / "project.json")
        title = "프로젝트 저장" if self.model.path is None else "다른 이름으로 저장"
        f, _ = QFileDialog.getSaveFileName(self, title, str(default), "프로젝트 (*.json)")
        if not f:
            return False
        self.model.save(Path(f))  # 상대 경로는 저장 위치 기준으로 다시 계산됨
        self.add_recent(Path(f))
        self.statusBar().showMessage(f"저장됨: {f}", 4000)
        return True

    def add_clips(self):
        files, _ = QFileDialog.getOpenFileNames(self, "클립 추가 (여러 개 선택 가능 — 다음 창에서 순서를 정함)",
                                                str(self.model.project.base_dir), MEDIA_FILTER)
        if files:
            self.handle_drop([Path(f) for f in files], None)

    # ------------------------------------------------------------------ 드래그 앤 드롭
    def dragEnterEvent(self, ev):
        if paths_from_mime(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        self.dragEnterEvent(ev)

    def dropEvent(self, ev):
        paths = paths_from_mime(ev.mimeData())
        if paths:
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            self._drop_later(paths, None)

    def _drop_later(self, paths, index):
        QTimer.singleShot(0, lambda: self.handle_drop(list(paths), index))

    def confirm(self, title: str, text: str) -> bool:
        """예/아니요 질문 (테스트에서 바꿔 끼울 수 있음)."""
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def notify(self, text: str, warning: bool = False) -> None:
        self.last_drop_report = text
        self.statusBar().showMessage(text.split("\n")[0], 8000)
        if warning:
            QMessageBox.information(self, "가져오기", text)

    def ask_media_order(self, files: list, dropped: list):
        """'순서 정하기' 창. 정한 순서(list[Path]) 또는 취소면 None. (테스트에서 바꿔 끼울 수 있음)"""
        from .order_dialog import OrderDialog

        dlg = OrderDialog(files, dropped, self)
        ok = dlg.exec() == OrderDialog.Accepted
        if ok and dlg.skip_next.isChecked():
            self.image_defaults.skip_order.setChecked(True)
        order = dlg.result_order if ok else None
        dlg.deleteLater()
        return order

    def handle_drop(self, paths: list, index: Optional[int] = None) -> None:
        """탐색기에서 끌어온 파일/폴더 처리. index 는 클립 삽입 위치(None = 끝에 추가)."""
        from ..importer import classify_paths, describe_unsupported, guess_base_dir, insert_media, rel_path

        plan = classify_paths(paths)
        notes: list[str] = []
        # 프로젝트(.json)만 놓았으면 그 프로젝트를 엽니다.
        if plan.projects and not (plan.media or plan.subtitles or plan.audio):
            if len(plan.projects) > 1:
                notes.append(f"프로젝트는 하나만 열 수 있어 {plan.projects[0].name} 만 엽니다.")
            if self.maybe_save():
                self.load_path(plan.projects[0])
                notes.insert(0, f"프로젝트를 열었습니다: {plan.projects[0].name}")
            if plan.unsupported:
                notes.append(describe_unsupported(plan.unsupported))
            self.notify("\n".join(notes), warning=bool(plan.unsupported) or len(plan.projects) > 1)
            return
        if plan.projects:
            notes.append(f"프로젝트 파일({', '.join(p.name for p in plan.projects)})은 다른 파일과 함께 놓으면 열지 않습니다. 따로 끌어 놓으세요.")

        tr = self.model.project.tracks  # 🔒 잠긴 트랙에는 넣지 않음
        if plan.media and tr.clips.locked:
            notes.append(f"🔒 클립 트랙이 잠겨 있어 이미지·동영상 {len(plan.media)}개를 넣지 않았습니다.")
            plan.media, plan.media_given = [], []
        if plan.subtitles and tr.subtitle.locked:
            notes.append("🔒 자막 트랙이 잠겨 있어 자막 파일을 넣지 않았습니다.")
            plan.subtitles = []
        if plan.audio and tr.bgm.locked:
            notes.append("🔒 배경음악 트랙이 잠겨 있어 음악 파일을 넣지 않았습니다.")
            plan.audio = []

        p = self.model.project
        if not p.clips and self.model.path is None and not p.subtitle.file and not p.bgm.file:
            base = guess_base_dir(plan)  # 처음 추가하는 미디어의 폴더 = 작업 폴더
            if base is not None:
                p.base_dir = base
        changed = False
        if len(plan.media) >= 2 and not self.image_defaults.skip_order.isChecked():
            order = self.ask_media_order(plan.media, plan.media_given)
            if order is None:
                self.statusBar().showMessage("넣기를 취소했습니다", 4000)
                return
            plan.media = order
        if plan.media:
            inserted = insert_media(p, plan.media, index, self.image_defaults.image_duration.value(),
                                    self.image_defaults.image_motion.currentData())
            where = "끝에" if index is None or index >= len(p.clips) - len(inserted) else f"{inserted[0] + 1}번 자리에"
            notes.insert(0, f"클립 {len(inserted)}개를 {where} 추가했습니다.")
            changed = True
        if plan.subtitles:
            sub = plan.subtitles[0]
            p.subtitle.file = rel_path(p, sub)
            p.subtitle.items = None
            notes.append(f"자막을 {sub.name} 로 설정했습니다.")
            if len(plan.subtitles) > 1:
                notes.append(f"자막은 하나만 쓸 수 있어 나머지 {len(plan.subtitles) - 1}개는 무시했습니다.")
            changed = True
        if plan.audio:
            music = plan.audio[0]
            rel = rel_path(p, music)
            if p.bgm.file and p.bgm.file != rel:
                ok = self.confirm("배경음악", f"이미 배경음악({p.bgm.file})이 있습니다.\n{music.name} 로 바꿀까요?")
            else:
                ok = True
            if ok:
                p.bgm.file = rel
                notes.append(f"배경음악을 {music.name} 로 설정했습니다.")
                changed = True
            else:
                notes.append("배경음악은 그대로 두었습니다.")
            if len(plan.audio) > 1:
                notes.append(f"배경음악은 하나만 쓸 수 있어 나머지 {len(plan.audio) - 1}개는 무시했습니다.")
        if plan.unsupported:
            notes.append(describe_unsupported(plan.unsupported))
        if plan.empty and not plan.unsupported:
            notes.append("추가할 수 있는 파일이 없습니다.")
        if changed:
            self.model.commit()  # 한 번의 드롭 = 한 번의 실행 취소
            if plan.media:
                self.model.select("clip", inserted[0])
        self.notify("\n".join(notes), warning=bool(plan.unsupported) or (plan.empty and not changed))

    def validate_current(self):
        issues = validate_project(self.model.project.to_dict(), self.model.project.base_dir)
        QMessageBox.information(self, "검사 결과", "문제 없음 ✔" if not issues else "\n".join(map(str, issues[:30])))

    # ------------------------------------------------------------------ 렌더
    def _check_renderable(self) -> bool:
        if not self.model.project.clips:
            QMessageBox.information(self, "내보내기", "클립이 없습니다. 이미지·동영상을 먼저 넣으세요.")
            return False
        issues = [i for i in validate_project(self.model.project.to_dict(), self.model.project.base_dir)
                  if i.severity == "error"]
        if issues:
            QMessageBox.warning(self, "아직 만들 수 없습니다", "\n".join(map(str, issues[:20])))
            return False
        return True

    # ------------------------------------------------------------------ 자동 미리보기
    def render_preview(self):
        """F5 / '🔄 미리보기 새로 만들기': 지금 바로 백그라운드로 다시 만듭니다."""
        if not self._check_renderable():
            return
        if self._direct_source():
            self._load_preview_for_project()
            return
        self.auto.request(force=True)

    def _preview_ready_to_render(self) -> bool:
        p = self.model.project
        if not p.clips or self._direct_source():
            return False
        return not any(i.severity == "error" for i in validate_project(p.to_dict(), p.base_dir))

    def _start_preview_worker(self, key):
        """AutoPreview 가 호출: 360p 초안을 백그라운드로 렌더 (바뀐 클립만 다시 만듦)."""
        if not self._preview_ready_to_render():
            n = len([i for i in validate_project(self.model.project.to_dict(), self.model.project.base_dir)
                     if i.severity == "error"])
            if n:
                self.player.set_notice(f"미리보기를 만들 수 없습니다: 문제 {n}개 (편집 ▸ 검사하기)")
            QTimer.singleShot(0, lambda: self.auto.on_finished(key, False))
            return
        proj = self.model.project.copy()
        proj.dubbing.takes = []  # 더빙은 미리보기에서 실시간으로 재생
        proj.subtitle = SubtitleSettings()  # 자막은 화면 위에 실시간으로 그림
        proj.bgm.file = None  # 배경음악은 따로 (bgm_live)
        for ts in (proj.tracks.clips, proj.tracks.subtitle, proj.tracks.dubbing, proj.tracks.bgm):
            ts.muted = ts.solo = False  # 소리 켜고 끄기는 재생 볼륨으로
        proj.tracks.subtitle.hidden = False
        self._preview_seq += 1
        out = cache_dir(proj) / f"preview_{self._preview_seq % 2}.mp4"
        if self.player.media_path is not None and Path(self.player.media_path) == out:
            self._preview_seq += 1
            out = cache_dir(proj) / f"preview_{self._preview_seq % 2}.mp4"
        worker = RenderWorker(proj, out, RenderSettings(draft=True, encoder="auto"))
        self._preview_worker = worker
        worker.progress.connect(lambda f, m: self._preview_progress(f))
        worker.finished_ok.connect(lambda res, k=key, o=out, w=worker: self._preview_done(w, k, o, True))
        worker.failed.connect(lambda msg, log, k=key, o=out, w=worker: self._preview_failed(w, k, msg))
        worker.cancelled.connect(lambda k=key, o=out, w=worker: self._preview_done(w, k, o, False))
        self._preview_progress(0.0)
        worker.start()

    def _cancel_preview_worker(self):
        w = getattr(self, "_preview_worker", None)
        if w is not None and w.isRunning():
            w.cancel()

    def _preview_progress(self, f: float):
        self.player.set_progress(f"미리보기 만드는 중… {int(f * 100)}%", f)

    def _preview_done(self, worker, key, out: Path, ok: bool):
        worker.wait(3000)
        if getattr(self, "_preview_worker", None) is worker:
            self._preview_worker = None
        self.player.set_progress("", None)
        if ok and out.exists():  # 조금 지난 결과라도 예전 것보다는 최신 → 바꾸고, 필요하면 AutoPreview 가 다시 만듦
            old = self.player.media_path
            self.preview_is_direct = False
            self.preview_revision = key
            self.player.load(out, keep_position=True, resume=True)  # 재생 위치·재생 상태 유지하며 교체
            if old and Path(old) != out and Path(old).name.startswith("preview_"):
                QTimer.singleShot(1500, lambda p=Path(old): p.unlink(missing_ok=True) if self.player.media_path != p else None)
            self.statusBar().showMessage("미리보기를 새로 만들었습니다", 3000)
        self.auto.on_finished(key, ok and out.exists())
        self._update_notice()

    def _preview_failed(self, worker, key, msg: str):
        worker.wait(3000)
        if getattr(self, "_preview_worker", None) is worker:
            self._preview_worker = None
        self.player.set_progress("", None)
        self.statusBar().showMessage(f"미리보기 실패: {msg}", 10000)
        self.auto.on_finished(key, False)
        self.player.set_notice("미리보기를 만들지 못했습니다 — 🔄 미리보기 새로 만들기 로 다시 시도 (자세한 내용은 상태 표시줄)")

    def _on_auto_toggled(self, on: bool):
        self.auto.enabled = on
        self.settings.setValue("view/auto_preview", on)
        if on:
            self.auto.notify_changed()
        self._update_notice()

    def _overlap_ok(self) -> bool:
        """겹친 더빙 조각이 있으면 한 번 묻기 (계속 = True)."""
        from ..edit_ops import take_overlaps
        p = self.model.project
        if p.tracks.dubbing.muted:
            return True
        spans, who = take_overlaps(p, self.model.take_file_len)
        if not who:
            return True
        where = ", ".join(f"{fmt_time(a)}~{fmt_time(b)}" for a, b in spans[:3]) + (" …" if len(spans) > 3 else "")
        return self.confirm("더빙 조각이 겹칩니다",
                            f"더빙 조각 {len(who)}개가 겹쳐 있어 그 부분은 두 목소리가 동시에 들립니다.\n"
                            f"겹친 곳: {where} (타임라인 빨간 빗금)\n\n그래도 내보낼까요?")

    def render_final(self):
        if not self._check_renderable():
            return
        if not self._overlap_ok():
            return
        default = self.model.project.resolve(self.model.project.output.file or "output.mp4")
        o = self.model.project.output
        from ..tracks import off_tracks
        opt = FinalRenderOptions(default, self, o.width, o.height, o.fps, warnings=off_tracks(self.model.project))
        if not self.exec_export_options(opt):
            return
        w, h, fps = opt.output_size()
        if (w, h, fps) != (o.width, o.height, float(o.fps)):  # 내보내기 창에서 바꾼 크기·fps → 프로젝트에 반영(실행 취소 1회)
            def fn(p):
                p.output.width, p.output.height, p.output.fps = w, h, fps
            self.model.edit(fn)
        out = Path(opt.path.text())
        worker = RenderWorker(self.model.project, out, opt.settings())
        dlg = ProgressDialog(worker, "영상 내보내기", self)
        if dlg.exec_worker():
            res = dlg.result_obj
            r = QMessageBox.question(self, "내보내기 완료", f"내보내기 완료!\n{out}\n길이 {res.duration:.2f}초 · 새로 만든 클립 "
                                                   f"{res.clips_rendered}개 · 캐시 사용 {res.clips_cached}개\n\n폴더를 열까요?")
            if r == QMessageBox.Yes:
                open_folder(out.parent)

    def open_script_dialog(self):
        """자막으로 대본 만들기 (인쇄 / PDF / Word / TXT / 클립보드)."""
        from .script_dialog import ScriptDialog
        name = self.model.project.title or (self.model.path.stem if self.model.path else "")
        dlg = ScriptDialog(self.model.project, name, self)
        self.exec_script_dialog(dlg)
        dlg.deleteLater()

    def exec_script_dialog(self, dlg):
        """(테스트에서 바꿔 끼울 수 있음)"""
        return dlg.exec()

    def exec_export_options(self, opt) -> bool:
        """내보내기 설정 창 (테스트에서 바꿔 끼울 수 있음)."""
        return bool(opt.exec())

    def export_voice(self):
        if not self.model.project.dubbing.takes:
            QMessageBox.information(self, "내보내기", "녹음된 테이크가 없습니다.")
            return
        if not self._overlap_ok():
            return
        f, _ = QFileDialog.getSaveFileName(self, "목소리만 WAV로 내보내기", str(self.model.project.base_dir / "voice.wav"), "WAV (*.wav)")
        if not f:
            return
        worker = RenderWorker(self.model.project, Path(f), RenderSettings(), kind="voice")
        if ProgressDialog(worker, "목소리만 WAV로 내보내기", self).exec_worker():
            self.statusBar().showMessage(f"저장됨: {f}", 5000)

    def clear_cache(self):
        self.player.load(None)
        clear_cache(self.model.project)
        self.preview_revision = -1
        self._load_preview_for_project()
        self.statusBar().showMessage("캐시를 비웠습니다.", 4000)


def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
