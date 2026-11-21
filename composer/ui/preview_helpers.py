"""미리보기 오버레이 및 플레이스홀더 헬퍼.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QMessageBox

from ..probe import ffmpeg_path, popen_kwargs
from ..render.runner import cache_dir


class PreviewHelpers:
    """자막/더빙 오버레이와 미리보기 없는 재생 시 플레이스홀더를 담당합니다."""

    def __init__(self, window):
        self.win = window

    def cue_at(self, t: float):
        if self.win.model.project.tracks.subtitle.hidden:
            return None
        c = self.win.model.cue_timeline.current(t)
        if not c:
            return None
        return c.text, t - c.start, c.end - t, c.end - c.start, c.style

    def subtitle_at(self, t: float) -> str:
        if self.win.model.project.tracks.subtitle.hidden:
            return ""
        c = self.win.model.cue_timeline.current(t)
        return c.text if c else ""

    def placeholder_at(self, t: float):
        if self.win.model.project.tracks.clips.hidden:
            return ("hidden",), None
        tm = self.win.model.timeline.clip_at(t)
        if tm is None and self.win.model.timeline.clips:
            tm = (
                self.win.model.timeline.clips[-1]
                if t >= self.win.model.timeline.total
                else self.win.model.timeline.clips[0]
            )
        if tm is None:
            return None, None
        clip = self.win.model.project.clips[tm.index]
        path = self.win.model.project.resolve(clip.file)
        if not path or not path.exists():
            return ("missing", tm.index), None
        st = path.stat()
        key = (str(path), st.st_mtime, round(tm.source_in, 2))
        if key not in self.win._thumbs:
            pix = QPixmap()
            if clip.type == "image":
                pix.load(str(path))
            else:
                thumb = self.video_thumbnail(
                    path, tm.source_in + min(0.5, max(0.0, tm.source_out - tm.source_in) / 2)
                )
                if thumb:
                    pix.load(str(thumb))
            self.win._thumbs[key] = pix
        return key, self.win._thumbs[key]

    def video_thumbnail(self, path: Path, at: float) -> Optional[Path]:
        d = cache_dir(self.win.model.project) / "thumbs"
        d.mkdir(parents=True, exist_ok=True)
        st = path.stat()
        h = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}|{at:.2f}".encode()).hexdigest()[:16]
        out = d / f"{h}.jpg"
        if not out.exists():
            try:
                subprocess.run(
                    [
                        ffmpeg_path(),
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{at:.3f}",
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=960:-2",
                        str(out),
                    ],
                    timeout=15,
                    capture_output=True,
                    **popen_kwargs(),
                )
            except Exception:
                return None
        return out if out.exists() else None

    def ask_play_without_preview(self) -> str:
        box = QMessageBox(
            QMessageBox.Question,
            "미리보기 없음",
            "아직 미리보기 영상이 없습니다.\n\n"
            "• 미리보기 만들기(F5): 전환·움직임·소리까지 실제 결과로 확인\n"
            "• 영상 없이 재생: 클립 그림과 자막·카운트다운만 타임라인 시간에 맞춰 재생(더빙 연습용)",
            QMessageBox.NoButton,
            self.win,
        )
        b_render = box.addButton("미리보기 만들기 (F5)", QMessageBox.AcceptRole)
        b_play = box.addButton("영상 없이 재생", QMessageBox.ActionRole)
        box.addButton("취소", QMessageBox.RejectRole)
        box.setDefaultButton(b_render)
        box.exec()
        return "render" if box.clickedButton() is b_render else "play" if box.clickedButton() is b_play else "cancel"

    def before_play(self) -> bool:
        if self.win._play_without_preview_ok or not self.win.model.project.clips:
            return True
        if self.win.auto.enabled and (self.win.auto.busy or self.win.preview._preview_ready_to_render()):
            if not self.win.auto.busy:
                self.win.auto.request()
            return True
        choice = self.ask_play_without_preview()
        if choice == "render":
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self.win.preview.render_now)
            return False
        if choice == "play":
            self.win._play_without_preview_ok = True
            self.win.statusBar().showMessage(
                "미리보기 영상 없이 타임라인 시간으로 재생합니다 (F5 = 미리보기 만들기)", 6000
            )
            return True
        return False
