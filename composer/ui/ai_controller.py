"""AI 프롬프트 대화상자와 결과 적용.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from pathlib import Path

from ..ai_apply import apply_ai_result, is_blank
from .prompt_dialog import PromptDialog


class AiController:
    """AI로 구성하기 대화상자와 결과 적용을 담당합니다."""

    def __init__(self, window):
        self.win = window

    def open_prompt_dialog(self, tab: int = 0, mode: str = "initial"):
        if not isinstance(tab, int):
            tab = 0
        folder = self.win.model.project.base_dir if self.win.model.path or self.win.model.project.clips else None
        dlg = PromptDialog(folder, self.win, project=self.win.model.project, mode=mode)
        dlg.tabs.setCurrentIndex(tab)
        dlg.apply_requested.connect(self.apply_ai_result)
        dlg.exec()

    def open_realign_dialog(self):
        self.open_prompt_dialog(mode="realign")

    def apply_ai_result(self, data: dict, folder: Path):
        """검사를 통과한 AI 결과를 현재 타임라인에 반영합니다."""
        self.win.dub.stop_live()
        blank = is_blank(self.win.model.project, self.win.model.path is not None)
        new = apply_ai_result(self.win.model.project, data, folder, keep_base=not blank)
        if blank:
            self.win.untitled_name = "AI 프로젝트"
        self.win.model.replace_project(new)
        self.win.model.select("")
        self.win.preview.load_for_project()
        self.win._title()
        msg = "AI 구성을 타임라인에 반영했습니다. 미리보기를 자동으로 만드는 중입니다. 되돌리려면 Ctrl+Z"
        if self.win.model.path is None:
            msg += " · 저장하려면 Ctrl+S"
        self.win.statusBar().showMessage(msg, 15000)
