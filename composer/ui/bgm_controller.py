"""배경음악 추가 및 미리보기 동기화.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog

from .inspector import set_bgm_file


class BgmController:
    """배경음악 파일 추가와 미리보기 동기화를 담당합니다."""

    BGM_PREP = "♪ 배경음악 준비 중… (잠시 뒤 함께 들려요)"

    def __init__(self, window):
        self.win = window

    def ask_bgm_file(self) -> str:
        f, _ = QFileDialog.getOpenFileName(
            self.win,
            "배경음악",
            str(self.win.model.project.base_dir),
            "오디오 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus)",
        )
        return f

    def add_bgm(self, f: str = "") -> bool:
        if self.win._locked_msg("bgm"):
            return False
        f = f or self.ask_bgm_file()
        if not f:
            return False
        set_bgm_file(self.win.model, f)
        self.win.model.select("bgm", 0)
        self.win.statusBar().showMessage(
            f"♪ 배경음악: {Path(f).name} — 오른쪽 🛠 속성에서 볼륨·페이드 (Ctrl+Z 로 되돌리기)", 6000
        )
        return True

    def remove_bgm(self) -> bool:
        if not self.win.model.project.bgm.file or self.win._locked_msg("bgm"):
            return False
        if not self.win.confirm("배경음악 제거", "프로젝트에서 배경음악을 뺄까요? 원본 음악 파일은 지워지지 않습니다."):
            return False
        self.win.model.edit(lambda p: setattr(p.bgm, "file", None))
        if self.win.model.selection.kind == "bgm":
            self.win.model.select("")
        self.win.statusBar().showMessage("배경음악을 뺐습니다 (Ctrl+Z 로 되돌리기)", 8000)
        return True

    def apply_volume(self):
        from ..tracks import audible
        if not hasattr(self.win, "bgm_live"):
            return
        self.win.bgm_live.set_volume(
            self.win.player.vol.value() / 100.0 * self.win.player._base_volume
            * (1.0 if audible(self.win.model.project, "bgm") else 0.0)
        )

    def on_building(self, on: bool):
        if on and self.win.player.is_playing():
            self.win.statusBar().showMessage(self.BGM_PREP)
        elif not on and self.win.statusBar().currentMessage() == self.BGM_PREP:
            self.win.statusBar().clearMessage()

    def on_failed(self, err: str):
        first = (err or "").strip().splitlines()[0] if (err or "").strip() else "알 수 없는 오류"
        self.win.statusBar().showMessage(
            f"⚠ 배경음악을 미리보기에서 틀 준비를 하지 못했어요: {first} "
            "— 재생을 다시 누르면 한 번 더 해 봐요 (내보내기에는 영향 없음)",
            12000,
        )

    def on_play(self, playing: bool):
        if not playing:
            return
        self.win.bgm_live.retry_if_failed(self.win.model.project, self.win.model.timeline.total)
        if self.win.bgm_live.building:
            self.win.statusBar().showMessage(self.BGM_PREP)

    def sync(self):
        self.win.bgm_live.sync(self.win.player.position(), self.win.player.is_playing())

    def apply_track_audio(self):
        from ..tracks import audible
        p = self.win.model.project
        self.win.player.set_track_gain(1.0 if audible(p, "clips") else 0.0)
        self.win.bgm_live.update(p, self.win.model.timeline.total)
        self.sync()
