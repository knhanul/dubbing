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

        self.right = QTabWidget()
        self.right.addTab(self.dub, "🎤 더빙 / 자막")
        self.right.addTab(self.inspector, "🛠 미세 조정")
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
        self.inspector.take_page.play_requested.connect(self.dub.play_take)
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
        self.statusBar().showMessage("파일 ▸ 'AI 프롬프트로 새 프로젝트' 또는 '동영상+자막으로 더빙 시작'으로 시작하세요.")
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
            "Home : 처음으로", "F5 : 미리보기 렌더", "Ctrl+E : 최종 렌더", "Ctrl+Z / Ctrl+Y : 실행 취소 / 다시 실행",
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
        name = self.model.path.name if self.model.path else "제목 없음"
        self.setWindowTitle(f"{APP_NAME} — {name}{' *' if self.model.dirty else ''}")

    def _on_changed(self):
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
        if not self.model.dirty:
            return True
        r = QMessageBox.question(self, "저장", "변경 내용을 저장할까요?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

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

    def set_project(self, project: Project, path: Optional[Path] = None):
        self.dub.stop_live()
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
        if not self.maybe_save():
            return
        path = folder / "project.json"
        if path.exists():
            r = QMessageBox.question(self, "저장", f"{path.name} 이(가) 이미 있습니다. 덮어쓸까요?\n(아니요 = 다른 이름으로)")
            if r != QMessageBox.Yes:
                p, _ = QFileDialog.getSaveFileName(self, "프로젝트 저장", str(folder / "project_ai.json"), "프로젝트 (*.json)")
                if not p:
                    self.set_project(project, None)
                    return
                path = Path(p)
        self.set_project(project, None)
        self.model.save(path)
        self.statusBar().showMessage(f"AI 프로젝트를 열고 {path.name} 으로 저장했습니다. F5 로 미리보기를 만드세요.", 8000)

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
        f, _ = QFileDialog.getSaveFileName(self, "다른 이름으로 저장", str(self.model.project.base_dir / "project.json"),
                                           "프로젝트 (*.json)")
        if not f:
            return False
        self.model.save(Path(f))
        self.statusBar().showMessage(f"저장됨: {f}", 4000)
        return True

    def add_clips(self):
        files, _ = QFileDialog.getOpenFileNames(self, "클립 추가", str(self.model.project.base_dir), MEDIA_FILTER)
        if not files:
            return
        p = self.model.project
        if not p.clips and not self.model.path:
            p.base_dir = Path(files[0]).parent
        for f in files:
            kind = guess_type(f)
            if kind == "image":
                p.clips.append(Clip(file=_rel(p, f), type="image", duration=4.0, motion="zoom_in"))
            elif kind == "video":
                p.clips.append(Clip(file=_rel(p, f), type="video"))
        self.model.commit()

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
