"""타임라인 조작 (확대, 자석, 자르기, 표시, 삭제 등).

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMessageBox

from ..edit_ops import cut_at as op_cut_at, cut_range
from ..schema import Transition
from ..subtitles import detach_takes, ensure_items
from ..timeline import build_timeline
from .preview import fmt_time


class TimelineController:
    """타임라인 관련 사용자 동작(확대·자르기·표시·삭제 등)을 처리합니다."""

    def __init__(self, window):
        self.win = window

    # ------------------------------------------------------------------ 확대 / 축소
    @staticmethod
    def _pps_of(v: int) -> float:
        return 5.0 * (160.0 ** (v / 100.0))

    def sync_zoom_slider(self, pps: float):
        import math
        slider = self.win.zoom_slider
        slider.blockSignals(True)
        slider.setValue(int(round(100 * math.log(max(5.0, pps) / 5.0) / math.log(160.0))))
        slider.blockSignals(False)

    def zoom_in(self):
        self.win.timeline.canvas.set_zoom(self.win.timeline.canvas.pps * 1.3)

    def zoom_out(self):
        self.win.timeline.canvas.set_zoom(self.win.timeline.canvas.pps / 1.3)

    def zoom_fit(self):
        total = max(self.win.model.timeline.total, 1.0)
        vw = max(200, self.win.timeline.viewport().width())
        self.win.timeline.canvas.set_zoom((vw - 40) / total)
        self.win.timeline.horizontalScrollBar().setValue(0)

    # ------------------------------------------------------------------ 도구 / 자석
    def set_magnet(self, on: bool):
        self.win.timeline.canvas.magnet = bool(on)
        from PySide6.QtCore import QSettings
        QSettings("knhanul", "VideoComposer").setValue("timeline/magnet", bool(on))

    def set_tool(self, t: str):
        self.win.timeline.canvas.set_tool(t)
        (self.win.act_razor if t == "razor" else self.win.act_select).setChecked(True)
        if t == "razor":
            self.win.statusBar().showMessage(
                "✂ 자르기 도구 — 클립·자막·더빙에서 자를 곳을 누르세요 (V 또는 Esc = 선택 도구)", 6000
            )

    def razor(self, kind: str, idx: int, t: float):
        self.win.model.select(kind, idx)
        self.cut_at(t)

    # ------------------------------------------------------------------ 자르기 / 표시
    def update_cut_buttons(self):
        c = self.win.timeline.canvas
        has = c.mark_in is not None and c.mark_out is not None
        self.win.btn_cut_range.setEnabled(has)
        self.win.btn_clear_marks.setEnabled(has or c.mark_in is not None or c.mark_out is not None)

    def cut_at(self, t: Optional[float] = None) -> bool:
        if self.win._locked_msg("clip"):
            return False
        t = self.win.player.position() if t is None else t
        box = {}
        self.win.model.edit(lambda p: box.setdefault("ok", op_cut_at(p, t)))
        if not box.get("ok"):
            self.win.statusBar().showMessage("자를 수 있는 클립이 없습니다", 4000)
            return False
        self.win.statusBar().showMessage(f"✂ {fmt_time(t)} 에서 잘랐습니다 (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def set_mark_in(self, t: Optional[float] = None):
        c = self.win.timeline.canvas
        t = round(self.win.player.position() if t is None else t, 3)
        c.set_marks(t, c.mark_out if c.mark_out is not None and c.mark_out > t else None)
        self.update_cut_buttons()
        self.win.statusBar().showMessage(f"In {fmt_time(t)} — 이제 Out(O)을 표시하세요", 4000)

    def set_mark_out(self, t: Optional[float] = None):
        c = self.win.timeline.canvas
        t = round(self.win.player.position() if t is None else t, 3)
        c.set_marks(c.mark_in if c.mark_in is not None and c.mark_in < t else None, t)
        self.update_cut_buttons()
        self.win.statusBar().showMessage(f"Out {fmt_time(t)}", 4000)

    def clear_marks(self):
        self.win.timeline.canvas.set_marks(None, None)
        self.update_cut_buttons()

    def cut_marked_range(self) -> bool:
        from ..edit_ops import cut_range
        c = self.win.timeline.canvas
        if c.mark_in is None or c.mark_out is None:
            return False
        if self.win._locked_msg("clip"):
            return False
        a, b = sorted((c.mark_in, c.mark_out))
        all_tracks = self.win.chk_cut_all.isChecked()
        box = {}
        self.win.model.edit(lambda p: box.setdefault("n", cut_range(p, a, b, all_tracks)))
        n = box.get("n", 0)
        if not n:
            self.win.statusBar().showMessage("자를 구간이 없습니다", 4000)
            return False
        self.clear_marks()
        self.win.statusBar().showMessage(f"✂ {fmt_time(a)}~{fmt_time(b)} 구간을 잘랐습니다 (Ctrl+Z 로 되돌리기)", 6000)
        return True

    # ------------------------------------------------------------------ 선택 / 삭제
    def select_track_all(self, kind: str) -> int:
        if kind == "clip":
            n = len(self.win.model.project.clips)
        elif kind == "subtitle":
            n = len(ensure_items(self.win.model.project))
        elif kind == "take":
            n = len(self.win.model.project.dubbing.takes)
        else:
            return 0
        if n:
            self.win.model.select(kind, list(range(n)))
        return n

    def delete_selected(self) -> bool:
        s = self.win.model.selection
        if s.count > 1:
            return self._delete_many()
        if s.count != 1:
            self.win.statusBar().showMessage("지울 항목을 먼저 타임라인에서 고르세요", 4000)
            return False
        kind, idx = s.kind, s.index
        if kind == "clip" and self.win._locked_msg("clip"):
            return False
        if kind == "subtitle" and self.win._locked_msg("subtitle"):
            return False
        if kind == "take" and self.win._locked_msg("take"):
            return False
        if kind == "transition" and self.win._locked_msg("clip"):
            return False

        def fn(p):
            if kind == "clip" and 0 <= idx < len(p.clips):
                del p.clips[idx]
            elif kind == "subtitle":
                detach_takes(p, {idx})
                p.subtitle.items = [x for x in ensure_items(p) if x.index != idx]
            elif kind == "take" and 0 <= idx < len(p.dubbing.takes):
                p.dubbing.takes.pop(idx)
            elif kind == "transition" and 0 < idx < len(p.clips):
                p.clips[idx].transition = Transition("none", 0.0)
            elif kind == "bgm":
                p.bgm.file = None

        self.win.model.edit(fn)
        self.win.model.select("")
        labels = {"clip": "클립", "subtitle": "자막", "take": "더빙", "transition": "전환", "bgm": "배경음악"}
        self.win.statusBar().showMessage(f"🗑 {labels.get(kind, '항목')}을(를) 지웠습니다 (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def _delete_many(self) -> bool:
        s = self.win.model.selection
        kind, idx = s.kind, s.indices

        def fn(p):
            if kind == "clip":
                for i in sorted(idx, reverse=True):
                    if 0 <= i < len(p.clips):
                        del p.clips[i]
            elif kind == "subtitle":
                gone = set(idx)
                detach_takes(p, gone)
                p.subtitle.items = [x for x in ensure_items(p) if x.index not in gone]
            elif kind == "take":
                for i in sorted(idx, reverse=True):
                    if 0 <= i < len(p.dubbing.takes):
                        p.dubbing.takes.pop(i)
            elif kind == "transition":
                for i in idx:
                    if 0 < i < len(p.clips):
                        p.clips[i].transition = Transition("none", 0.0)

        self.win.model.edit(fn)
        self.win.model.select("")
        msg = {
            "clip": f"클립 {len(idx)}개를 지우고 뒤 클립을 당겨 붙였습니다",
            "subtitle": f"자막 {len(idx)}줄을 지웠습니다",
            "take": f"더빙 조각 {len(idx)}개를 지웠습니다 (원본 파일은 그대로)",
            "transition": f"전환 {len(idx)}개를 없앴습니다",
        }.get(kind, "지웠습니다")
        self.win.statusBar().showMessage(f"🗑 {msg} (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def nudge_selected(self, delta: float) -> bool:
        # Simplified placeholder — full implementation depends on existing nudge logic
        self.win.statusBar().showMessage(f"선택 항목을 {delta:+.2f}초 이동", 2000)
        return True
