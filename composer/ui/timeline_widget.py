"""타임라인: 클립/자막/더빙/BGM 트랙 표시와 마우스 미세 조정.

* 클릭: 선택 + 재생 위치 이동(눈금 영역)
* 클립 끌기: 순서 바꾸기 / 클립 오른쪽 끝 끌기: 길이 조절
* 자막 끌기: 이동 / 자막 양 끝 끌기: 시작·끝 조절
* 더빙 테이크·BGM 끌기: 위치 이동
* 기본 0.1초 단위로 맞춰지며, Shift 를 누르고 끌면 0.01초 단위
* Ctrl + 휠: 확대/축소
* 왼쪽 트랙 머리: 👁 감추기 · 🔇 소리 끄기 · S 솔로 · 🔒 잠금 (가로로 스크롤해도 늘 보임)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QScrollArea, QToolButton, QToolTip, QWidget

from ..schema import MOTION_LABELS
from ..subtitles import ensure_items
from ..tracks import KIND_TRACK, TRACK_FLAGS, audible, dimmed, locked, visible
from .model import ProjectModel

RULER_H = 22
TRACKS = [("clip", "클립", 54), ("subtitle", "자막", 38), ("take", "더빙", 32), ("bgm", "배경음악", 30)]
LABEL_W = 8  # 캔버스 왼쪽 여백 (트랙 이름·단추는 TrackHeader 에)
HEADER_W = 152
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
        pr = m.project
        for kind, label, h in TRACKS:
            r = self.track_rect(kind)
            key = KIND_TRACK[kind]
            p.fillRect(r.adjusted(0, 1, 0, 0), QColor("#1c1d21") if dimmed(pr, key) else QColor("#25272d"))
            if pr.tracks.get(key).locked:  # 🔒 빗금
                p.fillRect(r.adjusted(0, 1, 0, 0), QBrush(QColor(255, 255, 255, 18), Qt.BDiagPattern))

        colors = {"clip": "#3d6fb6", "subtitle": "#c98a2b", "take": "#3fa36b", "bgm": "#8a5bc2"}
        for kind, idx, rect in self._items():
            p.setOpacity(0.32 if dimmed(pr, KIND_TRACK[kind]) else 1.0)
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
        p.setOpacity(1.0)

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
        if locked(self.model.project, hit.kind):  # 🔒 고르기만 되고 끌기는 안 됨
            self._drag = {"kind": "locked"}
            QToolTip.showText(ev.globalPosition().toPoint(), "🔒 잠긴 트랙입니다 — 왼쪽 트랙 머리의 🔒 를 눌러 풀 수 있습니다", self)
            return
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
        if d["kind"] == "locked":
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
        if not d or d["kind"] in ("seek", "locked") or not d.get("moved"):
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


FLAG_COL = {"hidden": 0, "muted": 1, "solo": 2, "locked": 3}
BTN_W, BTN_H = 22, 20
_BTN_CSS = """
QToolButton { background:#30333b; color:#c8ccd4; border:1px solid #474b55; border-radius:3px; font-weight:bold;
              padding:0; }
QToolButton:hover { border-color:#8ab4f8; }
QToolButton[flag="hidden"]:checked, QToolButton[flag="muted"]:checked { background:#c0392b; color:#fff;
              border-color:#ff8a80; }
QToolButton[flag="solo"]:checked { background:#f2c200; color:#000; border-color:#fff3b0; }
QToolButton[flag="locked"]:checked { background:#5f6b7a; color:#fff; border-color:#b0bec5; }
"""


def flag_text(flag: str, on: bool) -> str:
    return {"hidden": "🚫" if on else "👁", "muted": "🔇" if on else "🔊", "solo": "S",
            "locked": "🔒" if on else "🔓"}[flag]


def flag_tip(track: str, flag: str, on: bool, project) -> str:
    name = {"clips": "클립", "subtitle": "자막", "dubbing": "더빙", "bgm": "배경음악"}[track]
    if flag == "hidden":
        what = "화면(영상)" if track == "clips" else "자막"
        return (f"{name}: {what} 감춤 — 눌러서 다시 보이기" if on else
                f"{name} 감추기 — 미리보기·내보내기에서 " + ("화면을 검게" if track == "clips" else "자막을 빼고") + " 합니다")
    if flag == "muted":
        what = "원본 소리" if track == "clips" else "소리"
        return f"{name}: {what} 꺼짐 — 눌러서 켜기" if on else f"{name} {what} 끄기 (미리보기·내보내기)"
    if flag == "solo":
        tip = (f"{name}: 솔로 켜짐 — 솔로 켠 트랙 소리만 들립니다. 눌러서 끄기" if on else
               f"{name} 솔로 — 이 트랙(과 솔로 켠 다른 트랙) 소리만 듣기")
        if not on and not audible(project, track) and not project.tracks.get(track).muted:
            tip += "\n(지금은 다른 트랙이 솔로라 안 들림)"
        return tip
    return f"{name}: 잠김 — 편집·끌기 막힘. 눌러서 풀기" if on else f"{name} 잠그기 — 실수로 바뀌지 않게 편집·끌기를 막습니다"


class TrackHeader(QWidget):
    """트랙 이름과 켜고 끄기 단추 (프리미어처럼). 누를 때마다 되돌리기 한 단계."""

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.setFixedWidth(HEADER_W)
        self.setStyleSheet(_BTN_CSS)
        self.buttons: dict[tuple[str, str], QToolButton] = {}
        self.labels: dict[str, QLabel] = {}
        y = RULER_H
        for kind, label, h in TRACKS:
            key = KIND_TRACK[kind]
            lb = QLabel(label, self)
            lb.setGeometry(6, y, HEADER_W - 4 * (BTN_W + 2) - 8, h)
            lb.setStyleSheet("color:#c8ccd4; background:transparent;")
            self.labels[key] = lb
            for flag in TRACK_FLAGS[key]:
                b = QToolButton(self)
                b.setCheckable(True)
                b.setProperty("flag", flag)
                b.setFocusPolicy(Qt.NoFocus)
                b.setGeometry(HEADER_W - (4 - FLAG_COL[flag]) * (BTN_W + 2) - 2, y + (h - BTN_H) // 2, BTN_W, BTN_H)
                b.toggled.connect(lambda on, k=key, f=flag: self.set_flag(k, f, on))
                self.buttons[(key, flag)] = b
            y += h
        model.changed.connect(self.refresh)
        self.refresh()

    def set_flag(self, key: str, flag: str, on: bool):
        ts = self.model.project.tracks.get(key)
        if getattr(ts, flag) == on:
            return
        self.model.edit(lambda p: setattr(p.tracks.get(key), flag, on))

    def refresh(self):
        pr = self.model.project
        for (key, flag), b in self.buttons.items():
            on = getattr(pr.tracks.get(key), flag)
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)
            b.setText(flag_text(flag, on))
            b.setToolTip(flag_tip(key, flag, on, pr))
        for key, lb in self.labels.items():
            off = dimmed(pr, key)
            lb.setStyleSheet(f"color:{'#6b707a' if off else '#c8ccd4'}; background:transparent;"
                             f"{'text-decoration:line-through;' if not visible(pr, key) else ''}")
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1f24"))
        p.fillRect(QRectF(0, 0, self.width(), RULER_H), QColor("#2a2c33"))
        y = RULER_H
        pr = self.model.project
        for kind, _, h in TRACKS:
            key = KIND_TRACK[kind]
            p.fillRect(QRectF(0, y + 1, self.width(), h - 1), QColor("#1c1d21") if dimmed(pr, key) else QColor("#2b2d34"))
            y += h
        p.setPen(QColor("#3a3d45"))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        p.end()


class TimelineView(QScrollArea):
    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.canvas = TimelineCanvas(model)
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setMinimumHeight(self.canvas.minimumHeight() + 24)
        self.follow = True
        self.header = TrackHeader(model, self)  # 가로 스크롤과 상관없이 왼쪽에 고정
        self.setViewportMargins(HEADER_W, 0, 0, 0)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._place_header())

    def _place_header(self):
        vp = self.viewport().geometry()
        self.header.setGeometry(vp.left() - HEADER_W, vp.top() - self.verticalScrollBar().value(), HEADER_W,
                                max(vp.height(), self.canvas.height()))
        self.header.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.canvas.resize(max(self.canvas.minimumWidth(), self.viewport().width()), self.viewport().height())
        self._place_header()

    def set_playhead(self, t: float):
        self.canvas.set_playhead(t)
        if self.follow:
            x = self.canvas.x_of(t)
            bar = self.horizontalScrollBar()
            vw = self.viewport().width()
            if x < bar.value() + LABEL_W or x > bar.value() + vw - 40:
                bar.setValue(int(max(0, x - LABEL_W - 20)))
