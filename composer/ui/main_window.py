"""메인 창."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..render.ffmpeg_builder import RenderSettings
from ..render.runner import cache_dir, clear_cache
from ..schema import Clip, Project, SubtitleSettings, Transition, guess_type
from ..validate import has_errors, validate_project
from .dnd import ClipListPanel, paths_from_mime
from .dubbing_panel import DubbingPanel
from .inspector import Inspector, _rel
from .model import ProjectModel
from .preview import PreviewPlayer
from .prompt_dialog import PromptDialog
from .render_dialog import FinalRenderOptions, ProgressDialog, RenderWorker
from .timeline_widget import TimelineView

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
        self.dub = DubbingPanel(self.model, self.player)
        self.inspector = Inspector(self.model)
        self.timeline = TimelineView(self.model)

        self.clip_panel = ClipListPanel(self.model)
        self.right = QTabWidget()
        self.right.addTab(self.dub, "🎤 더빙 / 자막")
        self.right.addTab(self.inspector, "🛠 미세 조정")
        self.right.addTab(self.clip_panel, "🎞 클립 목록")
        self.right.currentChanged.connect(lambda i: self.dub.ensure_devices() if i == 0 else None)
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.player)
        top.addWidget(self.right)
        top.setSizes([900, 560])
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.timeline)
        main.setSizes([650, 260])
        self.setCentralWidget(main)

        self.player.position_changed.connect(self.timeline.set_playhead)
        self.timeline.canvas.seek_requested.connect(self.player.seek)
        self.timeline.canvas.files_dropped.connect(self.handle_drop)
        self.clip_panel.files_dropped.connect(self.handle_drop)
        # 탐색기에서 창 어디에 놓아도 받기 (미리보기 화면은 부모 창으로 넘김)
        self.setAcceptDrops(True)
        self.player.view.setAcceptDrops(False)
        self.player.view.viewport().setAcceptDrops(False)
        self.last_drop_report = ""
        self.inspector.take_page.play_requested.connect(self.dub.play_take)
        self.inspector.project_page.remove_subtitles_requested.connect(self.remove_all_subtitles)
        self.dub.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.model.changed.connect(self._on_changed)
        self.model.selection_changed.connect(self._on_selection)
        self.model.dirty_changed.connect(lambda _: self._title())
        self.model.path_changed.connect(self._title)
        self.player.playing_changed.connect(lambda p: None if p else self.dub.stop_live())

        self._build_menus()
        self.key_filter = KeyFilter(self)
        QApplication.instance().installEventFilter(self.key_filter)
        self._title()
        self.statusBar().showMessage("파일 ▸ 'AI 프롬프트로 새 프로젝트' 또는 '동영상+자막으로 더빙 시작'으로 시작하거나, "
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

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("파일(&F)")
        self._act(f, "AI 프롬프트로 새 프로젝트…", self.open_prompt_dialog, "Ctrl+N")
        self._act(f, "빈 프로젝트 (폴더 선택)…", self.new_empty)
        self._act(f, "동영상 + 자막으로 더빙 시작…", self.open_video_for_dubbing, "Ctrl+Shift+O")
        f.addSeparator()
        self._act(f, "프로젝트 열기…", self.open_project, "Ctrl+O")
        self._act(f, "저장", self.save, "Ctrl+S")
        self._act(f, "다른 이름으로 저장…", self.save_as, "Ctrl+Shift+S")
        f.addSeparator()
        self._act(f, "끝내기", self.close, "Alt+F4")

        e = mb.addMenu("편집(&E)")
        self._act(e, "실행 취소", lambda: self.model.undo(), "Ctrl+Z")
        self._act(e, "다시 실행", lambda: self.model.redo(), "Ctrl+Y")
        e.addSeparator()
        self._act(e, "클립 추가…", self.add_clips, "Ctrl+I")
        self._act(e, "AI 답변(JSON) 붙여넣기…", lambda: self.open_prompt_dialog(tab=1), "Ctrl+Shift+V")
        self._act(e, "검사하기 (파일·값 확인)", self.validate_current)
        e.addSeparator()
        self.act_clear_subs = self._act(e, "자막 전체 제거", self.remove_all_subtitles,
                                        tip="프로젝트에서 자막을 모두 뺍니다(원본 SRT 파일은 그대로)")

        v = mb.addMenu("보기(&V)")
        self._act(v, "타임라인 확대", lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.pps * 1.3), "Ctrl+=")
        self._act(v, "타임라인 축소", lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.pps / 1.3), "Ctrl+-")
        a = self._act(v, "재생 위치 따라가기", lambda c: setattr(self.timeline, "follow", c))
        a.setCheckable(True); a.setChecked(True)

        r = mb.addMenu("렌더(&R)")
        self._act(r, "미리보기 렌더 (저해상도, 빠름)", self.render_preview, "F5")
        self._act(r, "최종 렌더 (MP4)…", self.render_final, "Ctrl+E")
        self._act(r, "목소리만 WAV 로 내보내기…", self.export_voice)
        r.addSeparator()
        self._act(r, "렌더 캐시 비우기", self.clear_cache)

        h = mb.addMenu("도움말(&H)")
        self._act(h, "단축키", self.show_shortcuts, "F1")
        self._act(h, "정보", lambda: QMessageBox.about(self, APP_NAME, f"{APP_NAME} {__version__}\nAI JSON 으로 영상 자동 조립 + 더빙"))

        tb = self.addToolBar("주요 기능")
        tb.setMovable(False)
        for text, slot in [("🤖 AI 프롬프트", self.open_prompt_dialog), ("📂 열기", self.open_project),
                           ("💾 저장", self.save), ("➕ 클립", self.add_clips),
                           ("👁 미리보기 렌더(F5)", self.render_preview), ("🎬 최종 렌더", self.render_final)]:
            a = QAction(text, self)
            a.triggered.connect(slot)
            tb.addAction(a)

    def show_shortcuts(self):
        QMessageBox.information(self, "단축키", "\n".join([
            "Space : 재생 / 일시정지", "R : 선택한 자막 줄 녹음 (녹음 중이면 정지)", "Shift+R : 연속 녹음 시작/정지",
            "← / → : 이전 / 다음 자막 줄", "Ctrl+← / → : 0.1초 뒤로/앞으로 (Shift 를 더하면 1초)",
            "[ / ] : 선택한 자막·테이크를 0.1초 앞당기기/늦추기 (Shift = 0.01초)",
            "Delete : 선택한 클립 / 자막 줄 삭제", "Home : 처음으로", "F5 : 미리보기 렌더", "Ctrl+E : 최종 렌더", "Ctrl+Z / Ctrl+Y : 실행 취소 / 다시 실행",
            "Ctrl+S : 저장", "타임라인: 끌어서 이동, 끝을 끌어서 길이, Shift 끌기 = 0.01초, Ctrl+휠 = 확대",
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
        if k == Qt.Key_Delete and self.model.selection.kind == "clip":
            self.inspector.clip_page._delete()
            return True
        if k == Qt.Key_Delete and self.model.selection.kind == "subtitle":
            if self.model.cue_by_index(self.model.selection.index) is not None:
                self.inspector.sub_page._delete()
                self.statusBar().showMessage("자막 한 줄을 지웠습니다 (Ctrl+Z 로 되돌리기)", 5000)
            return True
        return False

    def nudge_selected(self, d: float):
        from ..subtitles import move_item

        s = self.model.selection
        if s.kind == "subtitle":
            self.model.edit(lambda p: move_item(p, s.index, d), "nudge")
        elif s.kind == "take":
            t = self.model.project.dubbing.takes[s.index]
            self.model.edit(lambda p: setattr(t, "offset", round(t.offset + d, 3)), "nudge")
        elif s.kind == "bgm":
            self.model.edit(lambda p: setattr(p.bgm, "start_offset", max(0.0, round(p.bgm.start_offset + d, 3))), "nudge")

    # ------------------------------------------------------------------ 상태
    def _subtitle_at(self, t: float) -> str:
        c = self.model.cue_timeline.current(t)
        return c.text if c else ""

    def _title(self):
        name = self.model.path.name if self.model.path else getattr(self, "untitled_name", "제목 없음")
        self.setWindowTitle(f"{APP_NAME} — {name}{' *' if self.model.dirty else ''}")

    def remove_all_subtitles(self) -> bool:
        """프로젝트에서 자막을 모두 뺍니다. 디스크의 자막 파일은 절대 지우지 않습니다."""
        from ..subtitles import clear_subtitles, has_subtitles

        if not has_subtitles(self.model.project):
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

    def _on_changed(self):
        from ..subtitles import has_subtitles

        if hasattr(self, "act_clear_subs"):
            self.act_clear_subs.setEnabled(has_subtitles(self.model.project))
        self.player.refresh_overlay()
        if self.preview_is_direct and not self._direct_source():
            self.preview_is_direct = False
            self.preview_revision = -1
        self._update_notice()

    def _update_notice(self):
        if not self.model.project.clips:
            self.player.set_notice("클립이 없습니다 — AI 프롬프트나 '클립 추가'로 시작하세요")
        elif self.preview_is_direct:
            self.player.set_notice("")
        elif self.player.media_path is None:
            self.player.set_notice("F5 를 눌러 미리보기 렌더를 만드세요")
        elif self.preview_revision != self._render_revision_key():
            self.player.set_notice("화면이 최신 편집과 다릅니다 — F5 로 미리보기 다시 렌더 (자막·더빙은 바로 반영됨)")
        else:
            self.player.set_notice("")

    def _render_revision_key(self):
        """미리보기 영상에 영향을 주는 값(자막·더빙 제외)만으로 비교합니다."""
        d = self.model.project.to_dict()
        d.pop("subtitle", None)
        d.pop("dubbing", None)
        return hash(str(d))

    def _on_selection(self):
        s = self.model.selection
        if s.kind in ("clip", "take", "bgm"):
            self.right.setCurrentWidget(self.inspector)
        if s.kind == "clip" and 0 <= s.index < len(self.model.timeline.clips) and not self.player.is_playing():
            self.player.seek(self.model.timeline.clips[s.index].start)

    def _direct_source(self) -> Optional[Path]:
        """영상 하나뿐이고 변형이 없으면 렌더 없이 원본을 바로 미리보기."""
        p = self.model.project
        if len(p.clips) != 1 or p.bgm.file:
            return None
        c = p.clips[0]
        if c.type != "video" or c.trim_start or c.trim_end or c.fit_duration or abs(c.speed - 1) > 1e-6:
            return None
        return p.resolve(c.file)

    def _load_preview_for_project(self):
        src = self._direct_source()
        self.preview_is_direct = bool(src and src.exists())
        if self.preview_is_direct:
            self.player.load(src, keep_position=False)
        else:
            cached = cache_dir(self.model.project) / "preview.mp4"
            self.player.load(None)
            self.preview_revision = -1
            if cached.exists():
                self.player.load(cached, keep_position=False)
        self._update_notice()

    # ------------------------------------------------------------------ 파일
    def maybe_save(self) -> bool:
        """저장 안 한 변경이 있으면 한 번만 묻습니다(저장 / 저장 안 함 / 취소).
        클립도 없는 제목 없는 빈 프로젝트는 묻지 않습니다."""
        if not self.model.dirty:
            return True
        if self.model.path is None and not self.model.project.clips:
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
        self.dub.recorder.close()
        self.dub.stop_live()
        self.player.player.stop()
        self.player.player.setVideoOutput(None)
        self.player.player.setSource(QUrl())

    def set_project(self, project: Project, path: Optional[Path] = None, untitled_name: str = "제목 없음"):
        self.dub.stop_live()
        self.untitled_name = untitled_name
        self.model.set_project(project, path)
        self._load_preview_for_project()

    def open_prompt_dialog(self, tab: int = 0):
        if not isinstance(tab, int):
            tab = 0
        folder = self.model.project.base_dir if self.model.path or self.model.project.clips else None
        dlg = PromptDialog(folder, self)
        dlg.tabs.setCurrentIndex(tab)
        dlg.project_ready.connect(self._project_from_ai)
        dlg.exec()

    def _project_from_ai(self, project: Project, folder: Path):
        """AI 프로젝트를 저장 창 없이 '저장 안 된 새 프로젝트'로 바로 엽니다. 저장은 Ctrl+S 로."""
        if not self.maybe_save():
            return
        project.base_dir = Path(folder)
        self.set_project(project, None, untitled_name="AI 프로젝트")
        self.model.mark_modified()
        self.statusBar().showMessage("AI 프로젝트를 열었습니다. F5 로 미리보기를 만드세요. 저장하려면 Ctrl+S", 15000)

    def new_empty(self):
        if not self.maybe_save():
            return
        d = QFileDialog.getExistingDirectory(self, "작업 폴더 선택 (미디어 파일이 있는 곳)")
        if d:
            p = Project(base_dir=Path(d))
            self.set_project(p, None)

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
        self.right.setCurrentWidget(self.dub)
        self.dub.ensure_devices()
        self.model.save(vp.parent / f"{vp.stem}_dubbing.json")

    def open_project(self):
        if not self.maybe_save():
            return
        f, _ = QFileDialog.getOpenFileName(self, "프로젝트 열기", "", "프로젝트 (*.json)")
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

    def save(self) -> bool:
        if self.model.path is None:
            return self.save_as()
        self.model.save()
        self.statusBar().showMessage(f"저장됨: {self.model.path}", 4000)
        return True

    def save_as(self) -> bool:
        # 저장된 적 없는 프로젝트는 미디어 폴더의 project.json 을 기본값으로 제안
        default = self.model.path or (Path(self.model.project.base_dir) / "project.json")
        title = "프로젝트 저장" if self.model.path is None else "다른 이름으로 저장"
        f, _ = QFileDialog.getSaveFileName(self, title, str(default), "프로젝트 (*.json)")
        if not f:
            return False
        self.model.save(Path(f))
        self.statusBar().showMessage(f"저장됨: {f}", 4000)
        return True

    def add_clips(self):
        files, _ = QFileDialog.getOpenFileNames(self, "클립 추가", str(self.model.project.base_dir), MEDIA_FILTER)
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
            self.handle_drop(paths, None)

    def confirm(self, title: str, text: str) -> bool:
        """예/아니요 질문 (테스트에서 바꿔 끼울 수 있음)."""
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def notify(self, text: str, warning: bool = False) -> None:
        self.last_drop_report = text
        self.statusBar().showMessage(text.split("\n")[0], 8000)
        if warning:
            QMessageBox.information(self, "가져오기", text)

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

        p = self.model.project
        if not p.clips and self.model.path is None:
            base = guess_base_dir(plan)
            if base is not None:
                p.base_dir = base
        changed = False
        if plan.media:
            inserted = insert_media(p, plan.media, index, self.clip_panel.image_duration.value(),
                                    self.clip_panel.image_motion.currentData())
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
            QMessageBox.information(self, "렌더", "클립이 없습니다.")
            return False
        issues = [i for i in validate_project(self.model.project.to_dict(), self.model.project.base_dir)
                  if i.severity == "error"]
        if issues:
            QMessageBox.warning(self, "렌더할 수 없습니다", "\n".join(map(str, issues[:20])))
            return False
        return True

    def render_preview(self):
        if not self._check_renderable():
            return
        self.dub.stop_live()
        proj = self.model.project.copy()
        proj.dubbing.takes = []  # 더빙은 미리보기에서 실시간으로 재생
        proj.subtitle = SubtitleSettings()  # 자막은 화면 위에 실시간으로 그림
        out = cache_dir(proj) / "preview.mp4"
        self.player.load(None)
        worker = RenderWorker(proj, out, RenderSettings(draft=True, encoder="auto"))
        dlg = ProgressDialog(worker, "미리보기 렌더", self)
        key = self._render_revision_key()
        if dlg.exec_worker():
            self.preview_is_direct = False
            self.preview_revision = key
            self.player.load(out)
            self.statusBar().showMessage("미리보기 준비 완료 (Space 로 재생)", 5000)
        elif out.exists():
            self.player.load(out)
        self._update_notice()

    def render_final(self):
        if not self._check_renderable():
            return
        default = self.model.project.resolve(self.model.project.output.file or "output.mp4")
        opt = FinalRenderOptions(default, self)
        if not opt.exec():
            return
        out = Path(opt.path.text())
        worker = RenderWorker(self.model.project, out, opt.settings())
        dlg = ProgressDialog(worker, "최종 렌더", self)
        if dlg.exec_worker():
            res = dlg.result_obj
            r = QMessageBox.question(self, "완료", f"렌더 완료!\n{out}\n길이 {res.duration:.2f}초 · 새로 만든 클립 "
                                                   f"{res.clips_rendered}개 · 캐시 사용 {res.clips_cached}개\n\n폴더를 열까요?")
            if r == QMessageBox.Yes:
                open_folder(out.parent)

    def export_voice(self):
        if not self.model.project.dubbing.takes:
            QMessageBox.information(self, "내보내기", "녹음된 테이크가 없습니다.")
            return
        f, _ = QFileDialog.getSaveFileName(self, "목소리 WAV", str(self.model.project.base_dir / "voice.wav"), "WAV (*.wav)")
        if not f:
            return
        worker = RenderWorker(self.model.project, Path(f), RenderSettings(), kind="voice")
        if ProgressDialog(worker, "목소리 내보내기", self).exec_worker():
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
