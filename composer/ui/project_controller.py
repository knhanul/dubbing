"""프로젝트 파일 관리 (새로 만들기, 열기, 저장, 최근 파일).

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..schema import Project
from ..validate import has_errors, validate_project
from ..ai_apply import is_blank


class ProjectController:
    """프로젝트 열기/저장/최근 파일 및 저장 확인을 담당합니다."""

    def __init__(self, window):
        self.win = window

    def maybe_save(self) -> bool:
        """저장 안 한 변경이 있으면 한 번만 묻습니다."""
        if not self.win.model.dirty:
            return True
        if is_blank(self.win.model.project, self.win.model.path is not None):
            return True
        box = QMessageBox(
            QMessageBox.Question,
            "저장",
            "현재 프로젝트의 변경 내용을 저장할까요?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            self.win,
        )
        box.button(QMessageBox.Save).setText("저장")
        box.button(QMessageBox.Discard).setText("저장 안 함")
        box.button(QMessageBox.Cancel).setText("취소")
        box.setDefaultButton(QMessageBox.Save)
        r = self.win.ask_save_changes(box)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def new_project(self):
        if not self.maybe_save():
            return
        self.win.set_project(Project(base_dir=Path.home()), None)
        from .main_window import EMPTY_HINT
        self.win.statusBar().showMessage(EMPTY_HINT.replace("\n", " "), 8000)

    def recent_files(self) -> list:
        v = self.win.settings.value("file/recent", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or [])]

    def add_recent(self, path):
        items = [str(Path(path))] + [x for x in self.recent_files() if Path(x) != Path(path)]
        self.win.settings.setValue("file/recent", items[:10])

    def open_recent(self, f: str):
        p = Path(f)
        if not p.exists():
            QMessageBox.information(self.win, "최근 파일", f"파일을 찾을 수 없어 목록에서 뺍니다:\n{p}")
            self.win.settings.setValue("file/recent", [x for x in self.recent_files() if Path(x) != p])
            return
        if self.maybe_save():
            self.load_path(p)

    def open_project(self):
        if not self.maybe_save():
            return
        f, _ = QFileDialog.getOpenFileName(self.win, "열기", "", "프로젝트 (*.json)")
        if f:
            self.load_path(Path(f))

    def load_path(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            QMessageBox.critical(self.win, "열기 실패", f"JSON 을 읽을 수 없습니다.\n{e}")
            return
        issues = validate_project(data, path.parent)
        if has_errors(issues):
            QMessageBox.warning(
                self.win, "프로젝트 확인", "문제가 있지만 열어 둡니다:\n\n" + "\n".join(map(str, issues[:20]))
            )
        self.win.set_project(Project.from_dict(data, base_dir=path.parent), path)
        self.add_recent(path)

    def save(self) -> bool:
        if self.win.model.path is None:
            return self.save_as()
        self.win.model.save()
        self.add_recent(self.win.model.path)
        self.win.statusBar().showMessage(f"저장됨: {self.win.model.path}", 4000)
        return True

    def save_as(self) -> bool:
        default = self.win.model.path or (Path(self.win.model.project.base_dir) / "project.json")
        title = "프로젝트 저장" if self.win.model.path is None else "다른 이름으로 저장"
        f, _ = QFileDialog.getSaveFileName(self.win, title, str(default), "프로젝트 (*.json)")
        if not f:
            return False
        self.win.model.save(Path(f))
        self.add_recent(Path(f))
        self.win.statusBar().showMessage(f"저장됨: {f}", 4000)
        return True
