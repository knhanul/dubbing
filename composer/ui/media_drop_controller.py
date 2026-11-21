"""드래그앤드롭 및 미디어 추가 처리.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..importer import classify_paths, describe_unsupported, guess_base_dir, insert_media, rel_path
from .dnd import paths_from_mime


class MediaDropController:
    """파일/폴더 드래그앤드롭과 미디어 추가를 처리합니다."""

    def __init__(self, window):
        self.win = window

    def add_clips(self):
        files, _ = QFileDialog.getOpenFileNames(
            self.win,
            "클립 추가 (여러 개 선택 가능 — 다음 창에서 순서를 정함)",
            str(self.win.model.project.base_dir),
            self.win.MEDIA_FILTER if hasattr(self.win, "MEDIA_FILTER") else
            "미디어 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 *.mov *.mkv *.webm *.avi *.m4v)",
        )
        if files:
            self.handle_drop([Path(f) for f in files], None)

    def drag_enter(self, ev):
        if paths_from_mime(ev.mimeData()):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def drag_move(self, ev):
        self.drag_enter(ev)

    def drop(self, ev):
        paths = paths_from_mime(ev.mimeData())
        if paths:
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            self._drop_later(paths, None)

    def _drop_later(self, paths, index):
        QTimer.singleShot(0, lambda: self.handle_drop(list(paths), index))

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self.win, title, text) == QMessageBox.Yes

    def notify(self, text: str, warning: bool = False) -> None:
        self.win.last_drop_report = text
        self.win.statusBar().showMessage(text.split("\n")[0], 8000)
        if warning:
            QMessageBox.information(self.win, "가져오기", text)

    def ask_media_order(self, files: list, dropped: list):
        from .order_dialog import OrderDialog
        dlg = OrderDialog(files, dropped, self.win)
        ok = dlg.exec() == OrderDialog.Accepted
        if ok and dlg.skip_next.isChecked():
            self.win.image_defaults.skip_order.setChecked(True)
        order = dlg.result_order if ok else None
        dlg.deleteLater()
        return order

    def handle_drop(self, paths: list, index: Optional[int] = None) -> None:
        """탐색기에서 끌어온 파일/폴더 처리. index 는 클립 삽입 위치(None = 끝에 추가)."""
        plan = classify_paths(paths)
        notes = []
        if plan.unsupported:
            notes.append(describe_unsupported(plan.unsupported))
            plan.audio = []

        p = self.win.model.project
        if not p.clips and self.win.model.path is None and not p.subtitle.file and not p.bgm.file:
            base = guess_base_dir(plan)
            if base is not None:
                p.base_dir = base

        changed = False
        if len(plan.media) >= 2 and not self.win.image_defaults.skip_order.isChecked():
            order = self.ask_media_order(plan.media, plan.media_given)
            if order is None:
                self.win.statusBar().showMessage("넣기를 취소했습니다", 4000)
                return
            plan.media = order

        if plan.media:
            inserted = insert_media(
                p,
                plan.media,
                index,
                self.win.image_defaults.image_duration.value(),
                self.win.image_defaults.image_motion.currentData(),
            )
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
            self.win.model.commit()
            if plan.media:
                self.win.model.select("clip", inserted[0])
        self.notify("\n".join(notes), warning=bool(plan.unsupported) or (plan.empty and not changed))
