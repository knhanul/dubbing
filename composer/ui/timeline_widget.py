"""타임라인: 클립/자막/더빙/BGM 트랙 표시와 마우스 미세 조정.

* 클릭: 선택 + 재생 위치 이동(눈금 영역)
* 클립 끌기: 순서 바꾸기 / 클립 오른쪽 끝 끌기: 길이 조절
* 자막 끌기: 이동 / 자막 양 끝 끌기: 시작·끝 조절
* 더빙 테이크·BGM 끌기: 위치 이동
* 기본 0.1초 단위로 맞춰지며, Shift 를 누르고 끌면 0.01초 단위
* Ctrl + 휠: 확대/축소
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QToolTip, QWidget

from ..schema import MOTION_LABELS
from ..subtitles import ensure_items
from .model import ProjectModel

RULER_H = 22
TRACKS = [("clip", "클립", 54), ("subtitle", "자막", 38), ("take", "더빙", 30), ("bgm", "배경음악", 24)]
LABEL_W = 70
EDGE = 6


def snap(v: float, fine: bool) -> float:
    step = 0.01 if fine else 0.1
    return round(round(v / step) * step, 3)


@dataclass
class Hit:
    kind: str
    index: int  # clip idx / subtitle number / take list idx / 0
    part: str  # body | left | right


class TimelineCanvas(QWidget):
    seek_requested = Signal(float)
    files_dropped = Signal(list, int)  # 탐색기에서 끌어온 경로들, 클립 삽입 위치

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.pps = 60.0  # 초당 픽셀
        self.playhead = 0.0
        self.setMouseTracking(True)
        self.setMinimumHeight(RULER_H + sum(t[2] for t in TRACKS) + 8)
        self._drag: Optional[dict] = None
        self.empty_hint = ""  # 클립이 없을 때 가운데 안내
        self.drop_hint: Optional[dict] = None  # 드롭 위치 표시 {"index": n, "x": px, "text": str}
        self.setAcceptDrops(True)
        model.changed.connect(self._relayout)
        model.selection_changed.connect(self.update)
        self._relayout()

    # --- 좌표 ---
    def x_of(self, t: float) -> float:
        return LABEL_W + t * self.pps

    def t_of(self, x: float) -> float:
        return max(0.0, (x - LABEL_W) / self.pps)

    def track_rect(self, kind: str) -> QRectF:
        y = RULER_H
        for k, _, h in TRACKS:
            if k == kind:
                return QRectF(0, y, self.width(), h)
            y += h
        return QRectF()

    def _relayout(self):
        total = max(self.model.timeline.total, 10.0)
        if self.model.project.bgm.file:
            total = max(total, self.model.project.bgm.start_offset + 2)
        self.setMinimumWidth(int(LABEL_W + (total + 5) * self.pps))
        vp = self.parentWidget()
        self.resize(max(self.minimumWidth(), vp.width() if vp else 0), self.height())
        self.update()

    def set_zoom(self, pps: float):
        self.pps = max(5.0, min(800.0, pps))
        self._relayout()

    def set_playhead(self, t: float):
        old = self.x_of(self.playhead)
        self.playhead = t
        new = self.x_of(t)
        self.update(int(min(old, new)) - 2, 0, int(abs(new - old)) + 4, self.height())

    # --- 항목 위치 ---
    def _items(self):
        m = self.model
        r = self.track_rect("clip")
        for c in m.timeline.clips:
            yield "clip", c.index, QRectF(self.x_of(c.start), r.top() + 3, c.duration * self.pps, r.height() - 6)
        r = self.track_rect("subtitle")
        for cue in m.cues:
            yield "subtitle", cue.index, QRectF(self.x_of(cue.start), r.top() + 3, max(2, cue.duration * self.pps),
                                                r.height() - 6)
        r = self.track_rect("take")
        for i, (take, pos) in enumerate(zip(m.project.dubbing.takes, m.take_positions())):
            length = self._take_len(take)
            yield "take", i, QRectF(self.x_of(pos), r.top() + 3, max(4, length * self.pps), r.height() - 6)
        if m.project.bgm.file:
            r = self.track_rect("bgm")
            b = m.project.bgm
            end = m.timeline.total
            yield "bgm", 0, QRectF(self.x_of(b.start_offset), r.top() + 3,
                                   max(4, (end - b.start_offset) * self.pps), r.height() - 6)

    def _take_len(self, take) -> float:
        from ..probe import probe

        p = self.model.project.resolve(take.file)
        try:
            return probe(p).duration if p and p.exists() else 1.0
        except Exception:
            return 1.0

    def hit_test(self, pos: QPointF) -> Optional[Hit]:
        found = None
        for kind, idx, rect in self._items():
            if rect.adjusted(-2, 0, 2, 0).contains(pos):
                part = "body"
                if kind in ("clip", "subtitle"):
                    if pos.x() >= rect.right() - EDGE:
                        part = "right"
                    elif kind == "subtitle" and pos.x() <= rect.left() + EDGE:
                        part = "left"
                found = Hit(kind, idx, part)  # 나중 항목(위에 그려진 것) 우선
        return found

    # --- 그리기 ---
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1e1f24"))
        m = self.model
        sel = m.selection
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        p.setFont(font)

        # 눈금
        p.fillRect(QRectF(0, 0, self.width(), RULER_H), QColor("#2a2c33"))
        step = 1.0
        for s in (0.5, 1, 2, 5, 10, 30, 60):
            if s * self.pps >= 40:
                step = s
                break
        t = 0.0
        p.setPen(QColor("#9aa0aa"))
        while self.x_of(t) < self.width():
            x = self.x_of(t)
            p.drawLine(QPointF(x, RULER_H - 6), QPointF(x, RULER_H))
            mm, ss = divmod(t, 60)
            p.drawText(QPointF(x + 2, 13), f"{int(mm)}:{ss:04.1f}" if step < 1 else f"{int(mm)}:{int(ss):02d}")
            t += step

        # 트랙 배경과 이름
        for kind, label, h in TRACKS:
            r = self.track_rect(kind)
            p.fillRect(r.adjusted(0, 1, 0, 0), QColor("#25272d"))
            p.setPen(QColor("#c8ccd4"))
            p.drawText(QRectF(4, r.top(), LABEL_W - 8, r.height()), Qt.AlignVCenter, label)

        colors = {"clip": "#3d6fb6", "subtitle": "#c98a2b", "take": "#3fa36b", "bgm": "#8a5bc2"}
        for kind, idx, rect in self._items():
            selected = sel.kind == kind and sel.index == idx
            base = QColor(colors[kind])
            if kind == "clip" and m.project.clips[idx].type == "image":
                base = QColor("#4b7f9c")
            p.setBrush(QBrush(base.lighter(135) if selected else base))
            p.setPen(QPen(QColor("#ffffff") if selected else base.darker(150), 2 if selected else 1))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QColor("#ffffff"))
            text = ""
            if kind == "clip":
                c = m.project.clips[idx]
                tm = m.timeline.clips[idx]
                tr = tm.transition_in
                if tr.duration > 0:
                    ov = QRectF(rect.left(), rect.top(), tr.duration * self.pps, rect.height())
                    p.fillRect(ov, QBrush(QColor(255, 255, 255, 50), Qt.BDiagPattern))
                text = f"{idx + 1}. {c.label or c.file}\n{tm.duration:.2f}초"
                if abs(tm.speed - 1) > 1e-3:
                    text += f"  ×{tm.speed:.2f}"
                if c.type == "image" and c.motion != "none":
                    text += f"  {MOTION_LABELS.get(c.motion, c.motion)}"
            elif kind == "subtitle":
                cue = m.cue_by_index(idx)
                text = f"{idx}. {cue.text.replace(chr(10), ' ')}" if cue else ""
            elif kind == "take":
                tk = m.project.dubbing.takes[idx]
                text = f"🎤 {tk.subtitle if tk.subtitle is not None else '세션'}"
            elif kind == "bgm":
                text = f"♪ {m.project.bgm.file}"
            p.save()
            p.setClipRect(rect.adjusted(3, 1, -3, -1))
            p.drawText(rect.adjusted(5, 2, -3, -2), Qt.AlignLeft | Qt.AlignVCenter, text)
            p.restore()

        if self.empty_hint and not self.model.project.clips:
            f3 = QFont(self.font())
            f3.setPointSizeF(11)
            f3.setBold(True)
            p.setFont(f3)
            p.setPen(QColor("#8a8f99"))
            vis = self.visibleRegion().boundingRect()
            p.drawText(QRectF(vis.left() + LABEL_W, RULER_H, max(200, vis.width() - LABEL_W), self.height() - RULER_H),
                       Qt.AlignCenter, self.empty_hint)

        # 드롭 위치 표시
        if self.drop_hint:
            h = self.drop_hint
            p.fillRect(self.rect(), QColor(80, 160, 255, 28))
            if h.get("x") is not None:
                cr = self.track_rect("clip")
                p.setPen(QPen(QColor("#4dd2ff"), 3))
                p.drawLine(QPointF(h["x"], cr.top()), QPointF(h["x"], cr.bottom()))
                p.setBrush(QColor("#4dd2ff"))
                p.drawEllipse(QPointF(h["x"], cr.top() + 2), 4, 4)
            f2 = QFont(self.font())
            f2.setBold(True)
            p.setFont(f2)
            tx = (h["x"] + 8) if h.get("x") is not None else LABEL_W + 8
            tw = p.fontMetrics().horizontalAdvance(h["text"]) + 12
            box = QRectF(tx - 6, 2, tw, RULER_H - 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 40, 60, 230))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#4dd2ff"))
            p.drawText(box, Qt.AlignCenter, h["text"])

        # 재생 위치
        x = self.x_of(self.playhead)
        p.setPen(QPen(QColor("#ff4d4d"), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        p.end()

    # --- 마우스 ---
    def mousePressEvent(self, ev):
        pos = ev.position()
        if ev.button() != Qt.LeftButton:
            return
        if pos.y() < RULER_H or pos.x() < LABEL_W:
            self.seek_requested.emit(self.t_of(pos.x()))
            self._drag = {"kind": "seek"}
            return
        hit = self.hit_test(pos)
        if hit is None:
            self.seek_requested.emit(self.t_of(pos.x()))
            self.model.select("")
            return
        self.model.select(hit.kind, hit.index)
        self._drag = {"kind": hit.kind, "index": hit.index, "part": hit.part, "x0": pos.x(), "moved": False,
                      "orig": self._orig_values(hit)}

    def _orig_values(self, hit: Hit):
        pr = self.model.project
        if hit.kind == "clip":
            c = pr.clips[hit.index]
            return {"duration": c.duration, "fit_duration": c.fit_duration, "trim_end": c.trim_end,
                    "out": self.model.timeline.clips[hit.index].duration,
                    "speed": self.model.timeline.clips[hit.index].speed,
                    "src_out": self.model.timeline.clips[hit.index].source_out}
        if hit.kind == "subtitle":
            items = ensure_items(pr)
            it = next(i for i in items if i.index == hit.index)
            return {"start": it.start, "end": it.end}
        if hit.kind == "take":
            return {"offset": pr.dubbing.takes[hit.index].offset}
        if hit.kind == "bgm":
            return {"start": pr.bgm.start_offset}
        return {}

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self._drag
        if d is None:
            hit = self.hit_test(pos)
            self.setCursor(Qt.SizeHorCursor if hit and hit.part in ("left", "right") else
                           Qt.OpenHandCursor if hit else Qt.ArrowCursor)
            return
        if d["kind"] == "seek":
            self.seek_requested.emit(self.t_of(pos.x()))
            return
        dt = (pos.x() - d["x0"]) / self.pps
        if abs(pos.x() - d["x0"]) < 3 and not d["moved"]:
            return
        d["moved"] = True
        fine = bool(ev.modifiers() & Qt.ShiftModifier)
        pr = self.model.project
        o = d["orig"]
        k = d["kind"]
        tip = ""
        if k == "clip" and d["part"] == "right":
            c = pr.clips[d["index"]]
            new_out = max(0.1, snap(o["out"] + dt, fine))
            if c.type == "image":
                if c.fit_duration:
                    c.fit_duration = new_out
                else:
                    c.duration = new_out
            elif c.fit_duration:
                c.fit_duration = new_out
            else:
                c.trim_end = round(max(c.trim_start + 0.1, o["src_out"] + (new_out - o["out"]) * o["speed"]), 3)
            tip = f"길이 {new_out:.2f}초"
        elif k == "clip":
            self.setCursor(Qt.ClosedHandCursor)
            d["drop_x"] = pos.x()
            tip = "놓으면 이 위치로 순서 이동"
        elif k == "subtitle":
            items = ensure_items(pr)
            it = next(i for i in items if i.index == d["index"])
            if d["part"] == "left":
                it.start = min(snap(max(0.0, o["start"] + dt), fine), it.end - 0.1)
            elif d["part"] == "right":
                it.end = max(snap(o["end"] + dt, fine), it.start + 0.1)
            else:
                delta = snap(max(dt, -o["start"]), fine)
                it.start, it.end = round(o["start"] + delta, 3), round(o["end"] + delta, 3)
            tip = f"{it.start:.2f} → {it.end:.2f} ({it.end - it.start:.2f}초)"
        elif k == "take":
            t = pr.dubbing.takes[d["index"]]
            t.offset = snap(o["offset"] + dt, fine)
            tip = f"오프셋 {t.offset:+.2f}초"
        elif k == "bgm":
            pr.bgm.start_offset = max(0.0, snap(o["start"] + dt, fine))
            tip = f"시작 {pr.bgm.start_offset:.2f}초"
        if k != "clip" or d["part"] == "right":
            self.model.recompute()
        QToolTip.showText(ev.globalPosition().toPoint(), tip, self)
        self.update()

    def mouseReleaseEvent(self, ev):
        d, self._drag = self._drag, None
        self.setCursor(Qt.ArrowCursor)
        if not d or d["kind"] == "seek" or not d.get("moved"):
            return
        if d["kind"] == "clip" and d["part"] == "body":
            t = self.t_of(d.get("drop_x", d["x0"]))
            clips = self.model.timeline.clips
            target = sum(1 for c in clips if c.index != d["index"] and (c.start + c.duration / 2) < t)
            src = d["index"]
            if target != src:
                pr = self.model.project
                clip = pr.clips.pop(src)
                pr.clips.insert(target, clip)
                self.model.select("clip", target)
        self.model.commit()

    # --- 탐색기 드래그 앤 드롭 ---
    def drop_index_at(self, x: float) -> int:
        """x 위치에 놓으면 몇 번째 자리에 삽입되는지 (클립 가운데 기준)."""
        t = self.t_of(x)
        return sum(1 for c in self.model.timeline.clips if (c.start + c.duration / 2) < t)

    def insert_x(self, index: int) -> float:
        clips = self.model.timeline.clips
        if not clips:
            return self.x_of(0)
        if index >= len(clips):
            return self.x_of(clips[-1].end)
        return self.x_of(clips[index].start)

    def _update_drop_hint(self, ev) -> bool:
        from ..importer import classify_paths
        from .dnd import paths_from_mime

        paths = paths_from_mime(ev.mimeData())
        if not paths:
            self.drop_hint = None
            self.update()
            return False
        plan = classify_paths(paths)
        idx = self.drop_index_at(ev.position().x())
        parts = []
        if plan.media:
            parts.append(f"클립 {len(plan.media)}개를 {idx + 1}번 자리에 삽입")
        if plan.subtitles:
            parts.append("자막 설정")
        if plan.audio:
            parts.append("배경음악 설정")
        if plan.projects and not parts:
            parts.append("프로젝트 열기")
        if not parts:
            parts.append("추가할 수 있는 파일이 없습니다")
        self.drop_hint = {"index": idx, "x": self.insert_x(idx) if plan.media else None, "text": " · ".join(parts)}
        self.update()
        return True

    def dragEnterEvent(self, ev):
        if self._update_drop_hint(ev):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        if self._update_drop_hint(ev):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()

    def dragLeaveEvent(self, ev):
        self.drop_hint = None
        self.update()

    def dropEvent(self, ev):
        from .dnd import paths_from_mime

        paths = paths_from_mime(ev.mimeData())
        idx = self.drop_index_at(ev.position().x())
        self.drop_hint = None
        self.update()
        if paths:
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            self.files_dropped.emit(paths, idx)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            factor = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.set_zoom(self.pps * factor)
            ev.accept()
        else:
            ev.ignore()


class TimelineView(QScrollArea):
    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.canvas = TimelineCanvas(model)
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setMinimumHeight(self.canvas.minimumHeight() + 24)
        self.follow = True

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.canvas.resize(max(self.canvas.minimumWidth(), self.viewport().width()), self.viewport().height())

    def set_playhead(self, t: float):
        self.canvas.set_playhead(t)
        if self.follow:
            x = self.canvas.x_of(t)
            bar = self.horizontalScrollBar()
            vw = self.viewport().width()
            if x < bar.value() + LABEL_W or x > bar.value() + vw - 40:
                bar.setValue(int(max(0, x - LABEL_W - 20)))
