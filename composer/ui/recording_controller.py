"""녹음 트리거 및 자막 관련 동작.

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from pathlib import Path

from ..edit_ops import insert_cue
from ..dubbing.takes import find_take


class RecordingController:
    """자막 삽입, 녹음 시작, 다시 녹음 등을 담당합니다."""

    def __init__(self, window):
        self.win = window

    def add_subtitle_here(self) -> bool:
        if self.win._locked_msg("subtitle"):
            return False
        t = self.win.player.position()
        box = {}
        self.win.model.edit(lambda p: box.setdefault("i", insert_cue(p, t)))
        idx = box.get("i")
        if idx is None:
            self.win.statusBar().showMessage("여기에는 자막을 넣을 자리가 없습니다 (다른 자막과 겹침)", 5000)
            return False
        self.win.model.select("subtitle", idx)
        self.win.right.setCurrentWidget(self.win.inspector)
        te = self.win.inspector.sub_page.text
        te.setFocus()
        te.selectAll()
        self.win.statusBar().showMessage("＋ 자막을 넣었습니다 — 오른쪽 '내용'에 글을 쓰세요 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    def record_from_timeline(self):
        if self.win.dub.mode:
            self.win.dub.stop_recording()
            return
        self.win.right.setCurrentWidget(self.win.dub_scroll)
        if self.win.model.cues:
            self.win.dub.mode_tabs.setCurrentIndex(0)
            if self.win.model.selection.kind != "subtitle":
                c = self.win.model.cue_timeline.current(self.win.player.position())
                if c is not None:
                    self.win.model.select("subtitle", c.index)
            self.win.dub.record_line()
        else:
            self.win.dub.mode_tabs.setCurrentIndex(1)
            self.win.dub.free_page.toggle_record()

    def start_next_recording(self) -> bool:
        self.win.right.setCurrentWidget(self.win.dub_scroll)
        cues = self.win.model.cues
        if not cues:
            self.win.dub.mode_tabs.setCurrentIndex(1)
            return False
        self.win.dub.mode_tabs.setCurrentIndex(0)
        c = next((c for c in cues if find_take(self.win.model.project, c.index) is None), cues[0])
        self.win.model.select("subtitle", c.index)
        self.win.player.seek(max(0.0, c.start - 0.05))
        self.win.statusBar().showMessage(f"{c.index}번 줄을 골랐어요 — ● 이 줄 녹음 (R) 을 누르면 시작합니다", 6000)
        return True

    def rerecord_take(self, i: int):
        takes = self.win.model.project.dubbing.takes
        if not (0 <= i < len(takes)) or self.win._locked_msg("take"):
            return
        tk = takes[i]
        self.win.right.setCurrentWidget(self.win.dub_scroll)
        if tk.subtitle is not None and self.win.model.cue_by_index(tk.subtitle) is not None:
            self.win.dub.mode_tabs.setCurrentIndex(0)
            self.win.model.select("subtitle", tk.subtitle)
            self.win.dub.record_line()
        else:
            self.win.dub.mode_tabs.setCurrentIndex(1)
            self.win.player.seek(self.win.model.take_positions()[i] + (tk.trim_start or 0.0))
            self.win.dub.free_page.toggle_record()
            self.win.statusBar().showMessage(
                "새 녹음이 더해집니다 — 예전 녹음은 타임라인에서 고르고 Delete 로 지울 수 있어요", 8000
            )

    def add_subtitle_file(self, f: str = "") -> bool:
        if self.win._locked_msg("subtitle"):
            return False
        if not self.win.inspector.sub_file_panel.pick_sub(f):
            return False
        self.win.statusBar().showMessage(f"📝 자막 {len(self.win.model.cues)}줄을 불러왔습니다 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    def delete_take_file(self, rel: str) -> bool:
        p = self.win.model.project
        if self.win._locked_msg("take"):
            return False
        norm = rel.replace("\\", "/").lower()
        if any(t.file.replace("\\", "/").lower() == norm for t in p.dubbing.takes):
            self.win.statusBar().showMessage("지금 쓰는 녹음은 지울 수 없습니다 — 다른 녹음을 '이걸로 쓰기' 한 뒤 지우세요", 6000)
            return False
        path = p.resolve(rel)
        if not path or not path.exists():
            return False
        if not self.win.confirm(
            "녹음 파일 삭제",
            f"{Path(rel).name} 파일을 지울까요?\n지운 파일은 되돌릴 수 없습니다(Ctrl+Z 로도 안 됨).",
        ):
            return False
        try:
            path.unlink()
        except OSError as e:
            self.win.statusBar().showMessage(f"지울 수 없습니다: {e}", 6000)
            return False
        self.win.statusBar().showMessage(f"삭제됨: {Path(rel).name}", 4000)
        return True

    def remove_all_subtitles(self) -> bool:
        from ..subtitles import clear_subtitles, has_subtitles
        if not has_subtitles(self.win.model.project) or self.win._locked_msg("subtitle"):
            return False
        if not self.win.confirm("자막 전체 제거", "프로젝트에서 자막을 모두 뺄까요? 원본 SRT 파일은 지워지지 않습니다."):
            return False
        holder = {}
        self.win.model.edit(lambda p: holder.setdefault("n", clear_subtitles(p)))
        if self.win.model.selection.kind == "subtitle":
            self.win.model.select("")
        self.win.player.refresh_overlay()
        n = holder.get("n", 0)
        self.win.statusBar().showMessage(
            "자막을 모두 뺐습니다 (Ctrl+Z 로 되돌리기)" + (f" · 더빙 {n}개는 지금 위치에 고정" if n else ""), 8000
        )
        return True
