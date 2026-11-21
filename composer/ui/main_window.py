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
from .preview_controller import PreviewController
from .timeline_controller import TimelineController
from .media_drop_controller import MediaDropController
from .project_controller import ProjectController
from .render_controller import RenderController
from .ai_controller import AiController
from .bgm_controller import BgmController
from .recording_controller import RecordingController
from .preview_helpers import PreviewHelpers
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
        # callbacks will be set after helpers are created
        self._thumbs: dict = {}
        self._play_without_preview_ok = False
        self.dub = DubbingPanel(self.model, self.player)
        self.inspector = Inspector(self.model)
        self.timeline = TimelineView(self.model)

        self.image_defaults = ImageDefaultsDialog(self)  # ➕ 미디어 추가 ▾ ▸ 새 이미지 기본값
        # 오른쪽: 작업 순서대로 5개 탭 — 🛠 선택(고른 것에 따라 클립/자막/더빙…) · 🎤 녹음 · 🅰 자막 · ✨ 효과 · 📤 내보내기
        from .widgets import PANEL_MIN_W, scroll_page
        self.right = QTabWidget()
        # 오른쪽 패널 내용(자막 글 길이 등)이 바뀌어도 위쪽 영역 높이·미리보기 크기에 영향 주지 않게
        self.right.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        # 최소 폭 고정: 이보다 좁게는 못 줄임 (넓히는 건 됨 — 720px 이상이면 섹션이 2단)
        self.right.setMinimumWidth(PANEL_MIN_W)
        self.right.tabBar().setExpanding(True)
        self.right.setUsesScrollButtons(False)

        self.dub_scroll = scroll_page(self.dub, footer=self.dub.footer)  # 창이 낮아도 녹음 탭이 찌그러지지 않게 (스크롤)
        self.style_panel = SubtitleStylePanel(self.model)
        self.sub_tab = QWidget()
        sl = QVBoxLayout(self.sub_tab)
        sl.setContentsMargins(8, 8, 8, 8)
        sl.setSpacing(10)
        sl.addWidget(self.inspector.sub_file_panel)
        sl.addWidget(self.style_panel, 1)
        self.sub_scroll = scroll_page(self.sub_tab)
        self.output_scroll = scroll_page(self.inspector.output_page, footer=self.inspector.output_page.footer)
        from .effects_panel import EffectsPanel
        self.effects = EffectsPanel(self.model)
        self.effects_scroll = scroll_page(self.effects, footer=self.effects.footer)
        self.right.addTab(self.inspector, "🛠 선택")
        self.right.addTab(self.dub_scroll, "🎤 녹음")
        self.right.addTab(self.sub_scroll, "🅰 자막")
        self.right.addTab(self.effects_scroll, "✨ 효과")
        self.right.addTab(self.output_scroll, "📤 내보내기")
        self.right.setTabToolTip(0, "타임라인에서 고른 클립·전환·자막·더빙·배경음악의 설정 (아무것도 안 고르면 시작하기 단계)")
        self.right.setTabToolTip(1, "마이크 녹음 — 자막 보며 녹음 / 자유롭게 녹음(대본 먼저) · 녹음본 고르기")
        self.right.setTabToolTip(2, "자막 파일 불러오기·저장 · 자막 모양")
        self.right.setTabToolTip(3, "고른 클립의 움직임 · 색 필터 · 밝기·색 · 분위기")
        self.right.setTabToolTip(4, "영상 모양·화질 · 자막 넣는 방식 · 장면 바뀔 때 · 처음/끝 · 영상 내보내기")
        self.inspector.title_changed.connect(lambda t: self.right.setTabText(self.right.indexOf(self.inspector), t))
        self.inspector.clip_page.effects_requested.connect(lambda: self.right.setCurrentWidget(self.effects_scroll))
        self.inspector.style_requested.connect(self.show_style_tab)
        self.right.currentChanged.connect(
            lambda i: self.dub.ensure_devices() if self.right.widget(i) is self.dub_scroll else None)
        self.right.currentChanged.connect(self._on_right_tab)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.player)
        top.addWidget(self.right)
        top.setSizes([900, 560])
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        # 설정은 컨트롤러들이 사용하므로 먼저 만듦
        self.settings = QSettings("knhanul", "VideoComposer")
        # 미리보기 조율은 PreviewController 로 분리
        self.preview = PreviewController(self)
        self.auto = self.preview.auto  # 기존 코드 호환용
        self.timeline_ctrl = TimelineController(self)
        self.media_ctrl = MediaDropController(self)
        self.project_ctrl = ProjectController(self)
        self.render_ctrl = RenderController(self)
        self.ai_ctrl = AiController(self)
        self.bgm_ctrl = BgmController(self)
        self.rec_ctrl = RecordingController(self)
        self.preview_helpers = PreviewHelpers(self)
        self.player.subtitle_fn = self.preview_helpers.subtitle_at
        self.player.cue_fn = self.preview_helpers.cue_at
        self.player.placeholder_fn = self.preview_helpers.placeholder_at
        self.player.before_play = self.preview_helpers.before_play
        self.ask_play_without_preview = self.preview_helpers.ask_play_without_preview
        self.add_subtitle_here = self.rec_ctrl.add_subtitle_here
        self.record_from_timeline = self.rec_ctrl.record_from_timeline
        self.start_next_recording = self.rec_ctrl.start_next_recording
        self.rerecord_take = self.rec_ctrl.rerecord_take
        self.add_subtitle_file = self.rec_ctrl.add_subtitle_file
        self.delete_take_file = self.rec_ctrl.delete_take_file
        self.remove_all_subtitles = self.rec_ctrl.remove_all_subtitles
        self.add_bgm = self.bgm_ctrl.add_bgm
        self.remove_bgm = self.bgm_ctrl.remove_bgm
        self._apply_bgm_volume = self.bgm_ctrl.apply_volume
        self._bgm_building = self.bgm_ctrl.on_building
        self._bgm_failed = self.bgm_ctrl.on_failed
        self._bgm_on_play = self.bgm_ctrl.on_play
        self._sync_bgm = self.bgm_ctrl.sync
        self._apply_track_audio = self.bgm_ctrl.apply_track_audio
        self.open_prompt_dialog = self.ai_ctrl.open_prompt_dialog
        self.apply_ai_result = self.ai_ctrl.apply_ai_result
        # 렌더/내보내기 위임
        self.render_final = self.render_ctrl.render_final
        self.export_voice = self.render_ctrl.export_voice
        self.clear_cache = self.render_ctrl.clear_cache
        self.open_script_dialog = self.render_ctrl.open_script_dialog
        self._check_renderable = self.render_ctrl.check_renderable
        self._overlap_ok = self.render_ctrl.overlap_ok
        # 프로젝트 파일 관리 위임
        self.maybe_save = self.project_ctrl.maybe_save
        self.new_project = self.project_ctrl.new_project
        self.open_project = self.project_ctrl.open_project
        self.save = self.project_ctrl.save
        self.save_as = self.project_ctrl.save_as
        self.load_path = self.project_ctrl.load_path
        self.add_recent = self.project_ctrl.add_recent
        self.recent_files = self.project_ctrl.recent_files
        self.open_recent = self.project_ctrl.open_recent
        # 미디어 드롭/추가 위임
        self.add_clips = self.media_ctrl.add_clips
        self.handle_drop = self.media_ctrl.handle_drop
        self.ask_media_order = self.media_ctrl.ask_media_order
        self.notify = self.media_ctrl.notify
        self.confirm = self.media_ctrl.confirm
        # 기존 메서드 일부를 컨트롤러로 위임 (점진적 마이그레이션)
        self.zoom_in = self.timeline_ctrl.zoom_in
        self.zoom_out = self.timeline_ctrl.zoom_out
        self.zoom_fit = self.timeline_ctrl.zoom_fit
        self.cut_at = self.timeline_ctrl.cut_at
        self.delete_selected = self.timeline_ctrl.delete_selected
        self.clear_marks = self.timeline_ctrl.clear_marks
        self.set_tool = self.timeline_ctrl.set_tool
        self.set_mark_in = self.timeline_ctrl.set_mark_in
        self.set_mark_out = self.timeline_ctrl.set_mark_out
        self.cut_marked_range = self.timeline_ctrl.cut_marked_range
        self.select_track_all = self.timeline_ctrl.select_track_all
        self._set_magnet = self.timeline_ctrl.set_magnet
        self._update_cut_buttons = self.timeline_ctrl.update_cut_buttons
        self.nudge_selected = self.timeline_ctrl.nudge_selected
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
        ins.take_page.play_file_requested.connect(lambda rel: self.dub.play_version(self.model.selection.index, rel))
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
        ins.empty.record_requested.connect(self.start_next_recording)
        ins.empty.export_requested.connect(lambda: self.right.setCurrentWidget(self.output_scroll))
        self.dub.script_requested.connect(self.open_script_dialog)
        self.timeline.canvas.razor_requested.connect(self.timeline_ctrl.razor)
        # ... existing connections ...
        self.timeline.canvas.status_requested.connect(lambda t: self.statusBar().showMessage(t, 6000))
        self.timeline.canvas.add_requested.connect(
            lambda k: self.add_bgm() if k == "bgm" else self.add_subtitle_file())
        self.dub.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.model.changed.connect(self._on_changed)
        self.model.selection_changed.connect(self._on_selection)
        self.model.dirty_changed.connect(lambda _: self._title())
        self.model.path_changed.connect(self._title)
        self.player.playing_changed.connect(lambda p: None if p else self.dub.stop_live())

        self.preview_revision = self.preview.preview_revision
        self.preview_is_direct = self.preview.preview_is_direct
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
        self.player.btn_refresh.clicked.connect(self.preview.render_now)

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
        self._act(f, "🔁 녹음에 맞춰 다시 구성 (AI)…", self.ai_ctrl.open_realign_dialog)
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
        self.act_auto.toggled.connect(self.preview.on_auto_toggled)
        v.addAction(self.act_auto)
        self._act(v, "🔄 미리보기 새로 만들기 (360p)", self.preview.render_now, "F5")

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
            "여러 개 고르기(같은 트랙끼리): Ctrl+클릭 = 더하기/빼기 · Shift+클릭 = 범위 · 빈 곳에서 끌기 = 상자 · "
            "Ctrl+A = 고른 트랙(없으면 클립) 모두 — 끌기 · [ ] · Delete 가 모두에 적용",
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
        if k == Qt.Key_A and ctrl and not shift:
            from PySide6.QtWidgets import QAbstractItemView
            if isinstance(QApplication.focusWidget(), QAbstractItemView):
                return False  # 표(목록) 안에서는 표의 '모두 고르기'
            self.select_track_all(self.model.selection.kind or "clip")
            return True
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

    def _on_right_tab(self, i: int):
        """✨ 효과 탭에 있는 동안은 미리보기 영상 다시 만들기를 미룸 (값을 바꿀 때마다 영상 전체를 만들면 느린 PC 가 버벅임)."""
        if hasattr(self, "auto"):
            self.auto.set_hold(self.right.widget(i) is self.effects_scroll)
            self._update_notice()

    # --- 넣기 (배경음악 · 자막 파일) ---
    def show_image_defaults(self):
        self.image_defaults.show()
        self.image_defaults.raise_()

    def _file_len(self, rel: str) -> float:
        from ..probe import probe
        p = self.model.project.resolve(rel)
        try:
            return float(probe(p).duration or 0.0) if p and p.exists() else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def _timeline_menu(self, gpos, t: float):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        m.addAction(f"✂ 여기서 자르기 ({fmt_time(t)})", lambda: self.cut_at(t))
        sel = self.model.selection
        m.addAction(f"🗑 삭제 ({sel.count}개)" if sel.count > 1 else "🗑 삭제", self.delete_selected).setEnabled(bool(sel.kind))
        kind = sel.kind if sel.kind in ("clip", "subtitle", "take") else "clip"
        from .timeline_widget import KIND_NAMES
        m.addAction(f"☑ {KIND_NAMES[kind]} 모두 고르기 (Ctrl+A)", lambda: self.select_track_all(kind))
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

    BGM_PREP = "♪ 배경음악 준비 중… (잠시 뒤 함께 들려요)"

    def _locked_msg(self, kind: str) -> bool:
        """잠긴 트랙이면 상태 표시줄에 알리고 True."""
        from ..tracks import KIND_TRACK, TRACK_LABELS, locked
        if kind in KIND_TRACK and locked(self.model.project, kind):
            self.statusBar().showMessage(f"🔒 {TRACK_LABELS[KIND_TRACK[kind]]} 트랙이 잠겨 있습니다 — "
                                         "타임라인 왼쪽 트랙 머리의 🔒 를 눌러 푸세요", 6000)
            return True
        return False

    # ------------------------------------------------------------------ 상태
    def show_output_tab(self):
        self.right.setCurrentWidget(self.output_scroll)

    def show_style_tab(self, scope: str = "all"):
        self.style_panel.set_scope(scope)
        self.right.setCurrentWidget(self.sub_scroll)
        self.style_panel.sample.replay()

    # ------------------------------------------------------------------ 미리보기 없는 재생
    def _title(self):
        name = self.model.path.name if self.model.path else getattr(self, "untitled_name", "제목 없음")
        self.setWindowTitle(f"{name}{' *' if self.model.dirty else ''} — {APP_NAME}")

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
        if self.preview.preview_is_direct and not self.preview._direct_source():
            self.preview.preview_is_direct = False
            self.preview.preview_revision = -1
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
        elif self.preview.preview_revision != self.preview._render_revision_key():
            if auto and self.auto.hold and not busy:
                self.player.set_notice("효과를 고르는 중 — 다른 탭으로 가거나 ▶ 재생하면 미리보기 영상을 새로 만들어요 (위 그림이 효과 미리보기)")
            elif busy:
                self.player.set_notice("편집 내용을 반영한 미리보기를 만드는 중… (준비되면 자동으로 바뀜)")
            else:
                self.player.set_notice("화면이 최신 편집과 다릅니다 — 🔄 미리보기 새로 만들기 (자막·더빙은 바로 반영됨)")
        else:
            self.player.set_notice("")

    def _on_selection(self):
        s = self.model.selection
        if s.count > 1:
            from .timeline_widget import selection_text
            self.statusBar().showMessage(f"{selection_text(s)} — 끌기 · [ ] · Delete 가 모두에 적용됩니다 "
                                         "(속성 탭은 마지막에 고른 것)", 4000)
        # 고르면 🛠 속성 탭으로. 단 녹음 탭에서 자막·더빙을 고를 때는 녹음 흐름을 지키려고 그대로
        recording_tab = self.right.currentWidget() is self.dub_scroll
        effects_tab = self.right.currentWidget() is self.effects_scroll
        if s.kind == "clip" and effects_tab:
            pass  # ✨ 효과 탭에서 다른 클립을 고르면 효과 탭 그대로 (그 클립 효과로)
        elif s.kind in ("clip", "transition", "bgm") or (s.kind in ("subtitle", "take") and not recording_tab):
            self.right.setCurrentWidget(self.inspector)
        if s.kind == "clip" and s.count == 1 and 0 <= s.index < len(self.model.timeline.clips) and not self.player.is_playing():
            self.player.seek(self.model.timeline.clips[s.index].start)

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
        if hasattr(self, "preview"):
            self.preview.shutdown()
        if hasattr(self, "bgm_live"):
            self.bgm_live.shutdown()
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
        self.preview.load_for_project()

    # ------------------------------------------------------------------ 최근 파일
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

    # ------------------------------------------------------------------ 드래그 앤 드롭
    def dragEnterEvent(self, ev):
        self.media_ctrl.drag_enter(ev)

    def dragMoveEvent(self, ev):
        self.media_ctrl.drag_move(ev)

    def dropEvent(self, ev):
        self.media_ctrl.drop(ev)

    def _drop_later(self, paths, index):
        self.media_ctrl._drop_later(paths, index)

    def validate_current(self):
        issues = validate_project(self.model.project.to_dict(), self.model.project.base_dir)
        QMessageBox.information(self, "검사 결과", "문제 없음 ✔" if not issues else "\n".join(map(str, issues[:30])))

    # ------------------------------------------------------------------ 렌더
    # ------------------------------------------------------------------ 자동 미리보기
    def exec_script_dialog(self, dlg):
        """(테스트에서 바꿔 끼울 수 있음)"""
        return dlg.exec()

    def exec_export_options(self, opt) -> bool:
        """내보내기 설정 창 (테스트에서 바꿔 끼울 수 있음)."""
        return bool(opt.exec())
