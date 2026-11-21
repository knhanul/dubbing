"""최종 렌더링 및 내보내기 조율.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..render.ffmpeg_builder import RenderSettings
from ..render.runner import clear_cache
from ..tracks import off_tracks
from ..validate import validate_project
from .render_dialog import FinalRenderOptions, ProgressDialog, RenderWorker
from .preview import fmt_time


def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class RenderController:
    """최종 영상 내보내기, 목소리만 내보내기, 캐시 비우기를 담당합니다."""

    def __init__(self, window):
        self.win = window

    def check_renderable(self) -> bool:
        if not self.win.model.project.clips:
            QMessageBox.information(self.win, "내보내기", "클립이 없습니다. 이미지·동영상을 먼저 넣으세요.")
            return False
        issues = [
            i
            for i in validate_project(self.win.model.project.to_dict(), self.win.model.project.base_dir)
            if i.severity == "error"
        ]
        if issues:
            QMessageBox.warning(self.win, "아직 만들 수 없습니다", "\n".join(map(str, issues[:20])))
            return False
        return True

    def overlap_ok(self) -> bool:
        from ..edit_ops import take_overlaps
        p = self.win.model.project
        if p.tracks.dubbing.muted:
            return True
        spans, who = take_overlaps(p, self.win.model.take_file_len)
        if not who:
            return True
        where = ", ".join(f"{fmt_time(a)}~{fmt_time(b)}" for a, b in spans[:3]) + (" …" if len(spans) > 3 else "")
        return self.win.confirm(
            "더빙 조각이 겹칩니다",
            f"더빙 조각 {len(who)}개가 겹쳐 있어 그 부분은 두 목소리가 동시에 들립니다.\n"
            f"겹친 곳: {where} (타임라인 빨간 빗금)\n\n그래도 내보낼까요?",
        )

    def render_final(self):
        if not self.check_renderable():
            return
        if not self.overlap_ok():
            return
        default = self.win.model.project.resolve(self.win.model.project.output.file or "output.mp4")
        o = self.win.model.project.output
        opt = FinalRenderOptions(default, self.win, o.width, o.height, o.fps, warnings=off_tracks(self.win.model.project))
        if not self.win.exec_export_options(opt):
            return
        w, h, fps = opt.output_size()
        if (w, h, fps) != (o.width, o.height, float(o.fps)):
            def fn(p):
                p.output.width, p.output.height, p.output.fps = w, h, fps
            self.win.model.edit(fn)
        out = Path(opt.path.text())
        worker = RenderWorker(self.win.model.project, out, opt.settings())
        dlg = ProgressDialog(worker, "영상 내보내기", self.win)
        if dlg.exec_worker():
            res = dlg.result_obj
            r = QMessageBox.question(
                self.win,
                "내보내기 완료",
                f"내보내기 완료!\n{out}\n길이 {res.duration:.2f}초 · 새로 만든 클립 "
                f"{res.clips_rendered}개 · 캐시 사용 {res.clips_cached}개\n\n폴더를 열까요?",
            )
            if r == QMessageBox.Yes:
                open_folder(out.parent)

    def export_voice(self):
        if not self.win.model.project.dubbing.takes:
            QMessageBox.information(self.win, "내보내기", "녹음된 테이크가 없습니다.")
            return
        if not self.overlap_ok():
            return
        f, _ = QFileDialog.getSaveFileName(
            self.win, "목소리만 WAV로 내보내기", str(self.win.model.project.base_dir / "voice.wav"), "WAV (*.wav)"
        )
        if not f:
            return
        worker = RenderWorker(self.win.model.project, Path(f), RenderSettings(), kind="voice")
        if ProgressDialog(worker, "목소리만 WAV로 내보내기", self.win).exec_worker():
            self.win.statusBar().showMessage(f"저장됨: {f}", 5000)

    def clear_cache(self):
        self.win.player.load(None)
        clear_cache(self.win.model.project)
        self.win.preview.preview_revision = -1
        self.win.preview.load_for_project()
        self.win.statusBar().showMessage("캐시를 비웠습니다.", 4000)

    def open_script_dialog(self):
        from .script_dialog import ScriptDialog
        name = self.win.model.project.title or (self.win.model.path.stem if self.win.model.path else "")
        dlg = ScriptDialog(self.win.model.project, name, self.win)
        self.win.exec_script_dialog(dlg)
        dlg.deleteLater()
