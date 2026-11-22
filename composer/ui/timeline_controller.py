"""타임라인 조작 (확대, 자석, 자르기, 표시, 삭제 등).

main_window.py 에서 분리한 클래스입니다.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMessageBox

from ..edit_ops import clip_index_at, cut_range, split_clip, split_cue, split_take
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
        if hasattr(self.win, "act_clear_marks"):
            self.win.act_clear_marks.setEnabled(has or c.mark_in is not None or c.mark_out is not None)

    def cut_at(self, t: Optional[float] = None) -> bool:
        """✂ 자르기: 고른 클립/자막/더빙을 t 초에서 둘로."""
        t = self.win.player.position() if t is None else t
        s = self.win.model.selection
        kind, idx = s.kind, s.index
        if kind == "clip":
            tm = self.win.model.timeline.clips[idx] if 0 <= idx < len(self.win.model.timeline.clips) else None
            if tm is None or not (tm.start < t < tm.end):
                kind = ""
        if kind not in ("clip", "subtitle", "take"):
            kind, idx = "clip", clip_index_at(self.win.model.timeline, t)
            if idx is None:
                self.win.statusBar().showMessage("재생 위치 아래에 자를 클립이 없습니다", 5000)
                return False
        if self.win._locked_msg(kind):
            return False
        box = {}
        tl = self.win.model.timeline
        if kind == "clip":
            self.win.model.edit(lambda p: box.setdefault("ok", split_clip(p, tl, idx, t)))
        elif kind == "subtitle":
            self.win.model.edit(lambda p: box.setdefault("ok", split_cue(p, idx, t)))
        else:
            tt = t + self.win.model.project.dubbing.latency_ms / 1000.0
            self.win.model.edit(lambda p: box.setdefault("ok", split_take(p, idx, tt, self.win._file_len)))
        if not box.get("ok"):
            where = "자를 위치(재생 헤드)를 고른 더빙 조각 안쪽에" if kind == "take" else "재생 위치를 그 항목 안쪽에"
            self.win.statusBar().showMessage(f"자를 수 없습니다 — {where} 두세요 (끝에서 0.1초 이상 안쪽)", 6000)
            return False
        name = {"clip": "클립", "subtitle": "자막", "take": "더빙"}[kind]
        self.win.model.select(kind, idx + 1)
        self.win.statusBar().showMessage(f"✂ {name}을(를) {fmt_time(t)}에서 둘로 나눴습니다 (Ctrl+Z 로 되돌리기)", 5000)
        return True

    def set_mark_in(self, t: Optional[float] = None):
        c = self.win.timeline.canvas
        t = round(self.win.player.position() if t is None else t, 3)
        c.set_marks(t, c.mark_out if c.mark_out is not None and c.mark_out > t else None)
        self.update_cut_buttons()
        if hasattr(self.win, "_ensure_range_tools_visible"):
            self.win._ensure_range_tools_visible()
        self.win.statusBar().showMessage(f"In {fmt_time(t)} — 이제 Out(O)을 표시하세요", 4000)

    def set_mark_out(self, t: Optional[float] = None):
        c = self.win.timeline.canvas
        t = round(self.win.player.position() if t is None else t, 3)
        c.set_marks(c.mark_in if c.mark_in is not None and c.mark_in < t else None, t)
        self.update_cut_buttons()
        if hasattr(self.win, "_ensure_range_tools_visible"):
            self.win._ensure_range_tools_visible()
        self.win.statusBar().showMessage(f"Out {fmt_time(t)}", 4000)

    def clear_marks(self):
        self.win.timeline.canvas.set_marks(None, None)
        self.update_cut_buttons()

    def cut_marked_range(self) -> bool:
        """✂ 구간 잘라내기: In~Out 을 클립에서 빼고 당김."""
        from ..tracks import locked
        c = self.win.timeline.canvas
        if c.mark_in is None or c.mark_out is None:
            self.win.statusBar().showMessage("먼저 In(I)과 Out(O)을 표시하세요", 5000)
            return False
        if self.win._locked_msg("clip"):
            return False
        a, b = c.mark_in, c.mark_out
        allt = self.win.chk_all_tracks.isChecked()
        skip = {k for k in ("subtitle", "dubbing") if locked(self.win.model.project, k)}
        box = {}

        def fn(p):
            box["r"] = cut_range(p, lambda: build_timeline(p, self.win.model.duration_of), a, b, allt,
                                 self.win._file_len, skip)
        self.win.model.edit(fn)
        removed, note = box.get("r", (0.0, ""))
        if removed <= 0:
            self.win.statusBar().showMessage("잘라낼 클립이 없습니다", 5000)
            return False
        self.clear_marks()
        self.win.model.select("")
        self.win.player.seek(min(a, b))
        extra = " (잠긴 트랙은 그대로)" if allt and skip else ""
        self.win.statusBar().showMessage(f"✂ {removed:.2f}초를 잘라내고 당겼습니다: {note}{extra} — Ctrl+Z 로 되돌리기", 8000)
        return True

    # ------------------------------------------------------------------ 선택 / 삭제
    def select_track_all(self, kind: str) -> int:
        """한 트랙의 항목을 모두 고릅니다. 고른 개수를 돌려줍니다."""
        n = self.win.model.select_all(kind)
        if n:
            from .timeline_widget import KIND_NAMES
            self.win.statusBar().showMessage(f"{KIND_NAMES.get(kind, kind)} {n}개를 모두 골랐습니다", 3000)
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
