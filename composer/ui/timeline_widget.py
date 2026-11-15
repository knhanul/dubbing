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
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
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
    context_requested = Signal(object, float)  # (화면 좌표, 타임라인 초) — 오른쪽 클릭 메뉴
    files_dropped = Signal(list, int)  # 탐색기에서 끌어온 경로들, 클립 삽입 위치
    zoom_changed = Signal(float)  # 초당 픽셀 (툴바 확대 슬라이더와 맞춤)
    razor_requested = Signal(str, int, float)  # ✂ 자르기 도구로 누른 (종류, 번호, 타임라인 초)
    add_requested = Signal(str)  # 빈 트랙의 '＋ 음악 넣기'(bgm) / '＋ 자막 파일'(subtitle)
    status_requested = Signal(str)  # 상태 표시줄 안내 (더빙 조각을 옆 끝에 붙였을 때 등)

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self.pps = 60.0  # 초당 픽셀
        self.playhead = 0.0
        self.setMouseTracking(True)
        self.setMinimumHeight(RULER_H + sum(t[2] for t in TRACKS) + 8)
        self._drag: Optional[dict] = None
        self._hover: Optional[Hit] = None  # 마우스가 올라간 가장자리(강조 표시)
        self.mark_in: Optional[float] = None  # 구간 잘라내기 In / Out (눈금자에 표시)
        self.mark_out: Optional[float] = None
        self.empty_hint = ""  # 클립이 없을 때 가운데 안내
        self.drop_hint: Optional[dict] = None  # 드롭 위치 표시 {"index": n, "x": px, "text": str}
        self.tool = "select"  # "select" ↖ 선택 / "razor" ✂ 누른 곳을 자름
        self.magnet = True  # 🧲 클립 끝·재생 위치·자막 경계·In/Out 에 붙기 (Shift = 끔)
        self._razor_x: Optional[float] = None
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
        pps = max(5.0, min(800.0, pps))
        changed = abs(pps - self.pps) > 1e-9
        self.pps = pps
        self._relayout()
        if changed:
            self.zoom_changed.emit(pps)

    def set_tool(self, tool: str):
        self.tool = tool if tool in ("select", "razor") else "select"
        self._razor_x = None
        self.setCursor(Qt.ArrowCursor)
        self.update()

    # --- 🧲 자석 ---
    MAGNET_PX = 8

    def magnet_targets(self, skip_kind: str = "", skip_index: int = -1) -> list[float]:
        """붙을 수 있는 시각: 클립 경계 · 재생 위치 · 자막 시작/끝 · 더빙 시작/끝 · In/Out (끌고 있는 항목 제외)."""
        m = self.model
        ts = [0.0, self.playhead]
        for c in m.timeline.clips:
            if skip_kind == "clip" and c.index >= skip_index:  # 끄는 클립과 그 뒤(같이 움직임)는 제외
                break
            ts += [c.start, c.end]
        for it in m.cues:
            if not (skip_kind == "subtitle" and it.index == skip_index):
                ts += [it.start, it.end]
        for k, (_, _, r) in enumerate(x for x in self._items() if x[0] == "take"):
            if not (skip_kind == "take" and k == skip_index):
                ts += [self.t_of(r.left()), self.t_of(r.right())]
        ts += [t for t in (self.mark_in, self.mark_out) if t is not None]
        return ts

    def snap_time(self, t: float, fine: bool, skip_kind: str = "", skip_index: int = -1) -> tuple[float, bool]:
        """자석이 켜져 있고 Shift 가 아니면 가까운(8px) 시각에 붙이고, 아니면 0.1초(Shift 0.01초) 단위."""
        if self.magnet and not fine:
            tol = self.MAGNET_PX / self.pps
            best = min(self.magnet_targets(skip_kind, skip_index), key=lambda x: abs(x - t), default=None)
            if best is not None and abs(best - t) <= tol:
                return round(best, 3), True
        return snap(t, fine), False

    def snap_span(self, start: float, end: float, fine: bool, kind: str, index: int) -> tuple[float, bool]:
        """옮기는 항목: 시작이나 끝 중 더 가까운 쪽을 붙임 → 옮길 양(초)."""
        a, sa = self.snap_time(start, fine, kind, index)
        b, sb = self.snap_time(end, fine, kind, index)
        if sa and (not sb or abs(a - start) <= abs(b - end)):
            return a - start, True
        if sb:
            return b - end, True
        return snap(start, fine) - start, False

    # --- 빈 트랙 ＋ 단추 ---
    def add_buttons(self) -> list[tuple[str, QRectF, str]]:
        out = []
        pr = self.model.project
        if not pr.bgm.file:
            r = self.track_rect("bgm")
            out.append(("bgm", QRectF(self.x_of(0) + 4, r.top() + 4, 150, r.height() - 8), "＋ 음악 넣기"))
        if not self.model.cues:
            r = self.track_rect("subtitle")
            out.append(("subtitle", QRectF(self.x_of(0) + 4, r.top() + 5, 170, r.height() - 10), "＋ 자막 파일 불러오기"))
        return out

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
        for c in m.timeline.clips:  # 전환 = 경계 가운데에 걸친 작은 블록 (트랙 아래쪽 일부 높이만)
            if c.index > 0 and c.transition_in.duration > 0:
                yield "transition", c.index, self.transition_rect(c.start, c.transition_in.duration)
        r = self.track_rect("subtitle")
        for cue in m.cues:
            yield "subtitle", cue.index, QRectF(self.x_of(cue.start), r.top() + 3, max(2, cue.duration * self.pps),
                                                r.height() - 6)
        r = self.track_rect("take")
        for i, (take, pos) in enumerate(zip(m.project.dubbing.takes, m.take_positions())):
            ts = float(take.trim_start or 0.0)  # 자르기로 나눈 더빙은 파일 안 일부 구간만
            te = take.trim_end if take.trim_end is not None else self._take_len(take)
            yield "take", i, QRectF(self.x_of(pos + ts), r.top() + 3, max(4, (te - ts) * self.pps), r.height() - 6)
        if m.project.bgm.file:
            r = self.track_rect("bgm")
            b = m.project.bgm
            end = m.timeline.total
            yield "bgm", 0, QRectF(self.x_of(b.start_offset), r.top() + 3,
                                   max(4, (end - b.start_offset) * self.pps), r.height() - 6)

    def transition_rect(self, boundary: float, d: float) -> QRectF:
        r = self.track_rect("clip")
        h = (r.height() - 6) * 0.42
        w = max(14.0, d * self.pps)
        return QRectF(self.x_of(boundary) - w / 2, r.bottom() - 3 - h, w, h)

    def _paint_take_wave(self, p: QPainter, rect: QRectF, take, visible: QRectF) -> None:
        """더빙 클립 안에 녹음 파형(자르기로 쓴 구간만)."""
        from ..dubbing.levels import file_peaks

        path = self.model.project.resolve(take.file)
        peaks = file_peaks(path) if path else None
        if peaks is None or not len(peaks):
            return
        per = 100.0
        ts = float(take.trim_start or 0.0)
        te = take.trim_end if take.trim_end is not None else len(peaks) / per
        x0, x1 = max(rect.left() + 1, float(visible.left())), min(rect.right() - 1, float(visible.right()))
        if x1 <= x0 or te <= ts:
            return
        mid, amp = rect.center().y(), rect.height() / 2 - 2
        p.save()
        p.setPen(QPen(QColor(220, 255, 230, 150), 1))
        step = 1.0  # 화면 1px 마다 한 줄
        x = x0
        sec_per_px = (te - ts) / max(1.0, rect.width())
        while x < x1:
            a = ts + (x - rect.left()) * sec_per_px
            i0, i1 = int(a * per), int((a + sec_per_px * step) * per) + 1
            seg = peaks[max(0, i0):min(len(peaks), i1)]
            if len(seg):
                h = min(1.0, float(seg.max()) ** 0.6) * amp  # 작은 소리도 보이게 살짝 키움
                if h >= 0.5:
                    p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
            x += step
        p.restore()

    def clip_has_fx(self, i: int) -> bool:
        """색 필터·밝기·분위기 중 하나라도 바뀐 클립 (타임라인 ✨ 표시)."""
        from ..effects import has_effects
        clips = self.model.project.clips
        return 0 <= i < len(clips) and has_effects(clips[i])

    def take_overlap_rects(self) -> list[QRectF]:
        """더빙 조각끼리 겹친 구간 (화면 좌표)."""
        from ..edit_ops import take_overlaps
        pr = self.model.project
        if len(pr.dubbing.takes) < 2:
            return []
        lat = pr.dubbing.latency_ms / 1000.0
        r = self.track_rect("take")
        return [QRectF(self.x_of(a - lat), r.top() + 1, max(2.0, (b - a) * self.pps), r.height() - 2)
                for a, b in take_overlaps(pr, self._file_len)[0]]

    def _paint_take_overlaps(self, p: QPainter) -> None:
        for rect in self.take_overlap_rects():  # 겹친 곳 = 빨간 빗금 (두 소리가 동시에 들림)
            p.fillRect(rect, QColor(229, 57, 53, 90))
            p.fillRect(rect, QBrush(QColor("#ff5252"), Qt.BDiagPattern))
            p.setPen(QPen(QColor("#ff5252"), 1))
            p.drawRect(rect)

    def _take_len(self, take) -> float:
        return self._file_len(take.file)

    def _file_len(self, rel: str) -> float:
        return self.model.take_file_len(rel)

    def hit_test(self, pos: QPointF) -> Optional[Hit]:
        """가장자리(길이 조절)를 몸통보다 먼저 봅니다 — 전환으로 겹친 클립도 앞 클립 끝을 잡을 수 있게.
        가장자리가 여럿이면 마우스에 가장 가까운 것, 몸통은 나중 항목(위에 그려진 것) 우선."""
        body, edges = None, []
        x = pos.x()
        items = list(self._items())
        # 전환 블록(트랙 아래쪽)이 먼저: 양 끝 = 길이, 가운데 = 고르기. 위쪽 절반에서는 클립 끝을 잡음
        for kind, idx, rect in items:
            if kind == "transition" and rect.adjusted(-3, 0, 3, 0).contains(pos):
                ew = min(EDGE, rect.width() / 3)
                if x <= rect.left() + ew:
                    return Hit(kind, idx, "left")
                if x >= rect.right() - ew:
                    return Hit(kind, idx, "right")
                return Hit(kind, idx, "body")
        for kind, idx, rect in items:
            if kind == "transition" or not (rect.top() <= pos.y() <= rect.bottom()):
                continue
            if kind in ("clip", "subtitle", "take"):
                ew = min(EDGE, max(2.0, rect.width() / 3))  # 아주 짧은 막대는 가운데를 몸통으로 남김
                if rect.right() - ew <= x <= rect.right() + 3:
                    edges.append((abs(x - rect.right()), Hit(kind, idx, "right")))
                if rect.left() - 3 <= x <= rect.left() + ew:
                    edges.append((abs(x - rect.left()), Hit(kind, idx, "left")))
            if rect.adjusted(-2, 0, 2, 0).contains(pos):
                body = Hit(kind, idx, "body")
        if edges:
            return min(edges, key=lambda e: e[0])[1]
        return body

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
            if kind == "transition":
                continue
            p.setOpacity(0.32 if dimmed(pr, KIND_TRACK[kind]) else 1.0)
            selected = sel.kind == kind and sel.index == idx
            base = QColor(colors[kind])
            if kind == "clip" and m.project.clips[idx].type == "image":
                base = QColor("#4b7f9c")
            p.setBrush(QBrush(base.lighter(135) if selected else base))
            p.setPen(QPen(QColor("#ffffff") if selected else base.darker(150), 2 if selected else 1))
            p.drawRoundedRect(rect, 4, 4)
            if kind == "take":
                self._paint_take_wave(p, rect, m.project.dubbing.takes[idx], ev.rect())
            p.setPen(QColor("#ffffff"))
            text = ""
            if kind == "clip":
                c = m.project.clips[idx]
                tm = m.timeline.clips[idx]
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
            if kind == "clip" and self.clip_has_fx(idx):  # ✨ 효과 표시 (오른쪽 위)
                badge = QRectF(rect.right() - 20, rect.top() + 2, 17, 15)
                p.setBrush(QColor("#ffd54f"))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(badge, 3, 3)
                p.setPen(QColor("#202124"))
                p.drawText(badge, Qt.AlignCenter, "✨")
            p.restore()
        p.setOpacity(1.0)
        self._paint_take_overlaps(p)
        self._paint_transitions(p)
        if self._hover is not None:  # 잡을 수 있는 가장자리 강조
            for kind, idx, rect in self._items():
                if kind == self._hover.kind and idx == self._hover.index:
                    x = rect.right() if self._hover.part == "right" else rect.left()
                    p.setPen(QPen(QColor("#ffd54f"), 3))
                    p.drawLine(QPointF(x, rect.top() - 2), QPointF(x, rect.bottom() + 2))
                    p.setBrush(QColor("#ffd54f"))
                    p.setPen(Qt.NoPen)
                    my = rect.center().y()
                    for sgn in (-1, 1):  # ◀▶ 작은 화살표
                        tip = x + sgn * 7
                        p.drawPolygon(QPolygonF([QPointF(tip, my), QPointF(tip - sgn * 4, my - 4),
                                                 QPointF(tip - sgn * 4, my + 4)]))
                    break

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

        # 구간 잘라내기 In/Out
        if self.mark_in is not None or self.mark_out is not None:
            a = self.mark_in if self.mark_in is not None else 0.0
            b = self.mark_out if self.mark_out is not None else max(a, self.model.timeline.total)
            if self.mark_in is not None and self.mark_out is not None:
                p.fillRect(QRectF(self.x_of(a), RULER_H, (b - a) * self.pps, self.height() - RULER_H),
                           QColor(255, 82, 82, 38))
                p.fillRect(QRectF(self.x_of(a), RULER_H - 5, (b - a) * self.pps, 5), QColor("#ff5252"))
            f4 = QFont(self.font()); f4.setPointSizeF(8); f4.setBold(True)
            p.setFont(f4)
            for t, lab in ((self.mark_in, "In"), (self.mark_out, "Out")):
                if t is None:
                    continue
                x = self.x_of(t)
                p.setPen(QPen(QColor("#ff5252"), 2))
                p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ff5252"))
                w = 22 if lab == "In" else 28
                p.drawRect(QRectF(x if lab == "In" else x - w, 0, w, 13))
                p.setPen(QColor("#ffffff"))
                p.drawText(QRectF(x if lab == "In" else x - w, 0, w, 13), Qt.AlignCenter, lab)

        # 빈 트랙의 ＋ 단추
        f5 = QFont(self.font()); f5.setPointSizeF(8.5)
        p.setFont(f5)
        for _, r, text in self.add_buttons():
            p.setPen(QPen(QColor("#8ab4f8"), 1, Qt.DashLine))
            p.setBrush(QColor(138, 180, 248, 30))
            p.drawRoundedRect(r, 4, 4)
            p.setPen(QColor("#c6dafc"))
            p.drawText(r, Qt.AlignCenter, text)
        # ✂ 자르기 도구: 마우스 위치에 자를 선
        if self.tool == "razor" and self._razor_x is not None:
            p.setPen(QPen(QColor("#ffd54f"), 1, Qt.DashLine))
            p.drawLine(QPointF(self._razor_x, RULER_H), QPointF(self._razor_x, self.height()))

        # 재생 위치
        x = self.x_of(self.playhead)
        p.setPen(QPen(QColor("#ff4d4d"), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        p.end()

    def _paint_transitions(self, p: QPainter):
        """전환 블록: 경계 가운데에 걸친 작은 상자(이름 + 길이). 클립들은 맞닿게 그려짐."""
        m = self.model
        pr = m.project
        p.setOpacity(0.32 if dimmed(pr, "clips") else 1.0)
        f = QFont(self.font()); f.setPointSizeF(7.5)
        p.setFont(f)
        short = {"fade": "페이드", "crossfade": "크로스", "none": ""}
        for kind, idx, rect in self._items():
            if kind != "transition":
                continue
            tr = m.timeline.clips[idx].transition_in
            sel = m.selection.kind == "transition" and m.selection.index == idx
            p.setBrush(QColor("#f1f3f4") if sel else QColor("#c7cbd1"))
            p.setPen(QPen(QColor("#ffd54f") if sel else QColor("#5f6368"), 2 if sel else 1))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QColor("#202124"))
            name = short.get(tr.type, tr.type)
            text = f"{name} {tr.duration:.1f}" if rect.width() > 52 else (f"{tr.duration:.1f}" if rect.width() > 22 else "")
            p.drawText(rect, Qt.AlignCenter, text)
        p.setOpacity(1.0)

    # --- 마우스 ---
    def mousePressEvent(self, ev):
        pos = ev.position()
        if ev.button() != Qt.LeftButton:
            return
        if pos.y() < RULER_H or pos.x() < LABEL_W:
            self.seek_requested.emit(self.t_of(pos.x()))
            self._drag = {"kind": "seek"}
            return
        for kind, r, _ in self.add_buttons():
            if r.contains(pos):
                self.add_requested.emit(kind)
                return
        hit = self.hit_test(pos)
        if self.tool == "razor":  # ✂ 누른 곳을 자름 (클립·자막·더빙)
            if hit is not None and hit.kind in ("clip", "subtitle", "take"):
                self.razor_requested.emit(hit.kind, hit.index, self.t_of(pos.x()))
            else:
                self.seek_requested.emit(self.t_of(pos.x()))
            return
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
                    "src_out": self.model.timeline.clips[hit.index].source_out,
                    "src_in": self.model.timeline.clips[hit.index].source_in}
        if hit.kind == "transition":
            from ..timeline import effective_transition
            return {"d": self.model.timeline.clips[hit.index].transition_in.duration,
                    "type": effective_transition(pr, hit.index).type}
        if hit.kind == "subtitle":
            items = ensure_items(pr)
            it = next(i for i in items if i.index == hit.index)
            return {"start": it.start, "end": it.end}
        if hit.kind == "take":
            r = next(r for k, i, r in self._items() if k == "take" and i == hit.index)
            tk = pr.dubbing.takes[hit.index]
            return {"offset": tk.offset, "span": (self.t_of(r.left()), self.t_of(r.right())),
                    "ts": float(tk.trim_start or 0.0),
                    "te": tk.trim_end if tk.trim_end is not None else self._take_len(tk),
                    "flen": self._take_len(tk)}
        if hit.kind == "bgm":
            return {"start": pr.bgm.start_offset}
        return {}

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        d = self._drag
        if d is None and self.tool == "razor":
            self._razor_x = pos.x() if pos.y() >= RULER_H else None
            self.setCursor(Qt.SplitHCursor if pos.y() >= RULER_H else Qt.ArrowCursor)
            self.update()
            return
        if d is None and any(r.contains(pos) for _, r, _ in self.add_buttons()):
            self.setCursor(Qt.PointingHandCursor)
            return
        if d is None:
            hit = self.hit_test(pos)
            edge = hit if hit and hit.part in ("left", "right") and not locked(self.model.project, hit.kind) else None
            self.setCursor(Qt.SizeHorCursor if edge else Qt.OpenHandCursor if hit else Qt.ArrowCursor)
            if edge != self._hover:
                self._hover = edge
                self.update()
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
            st = self.model.timeline.clips[d["index"]].start
            end, stuck = self.snap_time(st + o["out"] + dt, fine, "clip", d["index"])
            new_out = max(0.1, round(end - st, 3) if stuck else snap(o["out"] + dt, fine))
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
        elif k == "transition":
            if d["part"] == "body":
                return
            from ..schema import Transition
            from ..timeline import transition_limit
            i = d["index"]
            tl = self.model.timeline.clips
            lim = transition_limit(tl[i - 1].duration, tl[i].duration)
            nd = o["d"] + (2 * dt if d["part"] == "right" else -2 * dt)  # 가운데 고정, 양쪽으로 같이 늘고 줄음
            nd = round(min(lim, max(0.05, snap(nd, fine))), 3)
            pr.clips[i].transition = Transition(o["type"], nd)
            tip = f"전환 길이 {nd:.2f}초" + (" (최대: 짧은 쪽 클립 길이)" if nd >= lim - 1e-6 else "")
        elif k == "clip" and d["part"] == "left":
            # 프리미어처럼 왼쪽 끝 잡기: 동영상 = 시작 부분 자르기/되살리기, 이미지 = 길이
            c = pr.clips[d["index"]]
            new_out = max(0.1, snap(o["out"] - dt, fine))
            if c.type == "image" or c.fit_duration:
                if c.type == "image" and not c.fit_duration:
                    c.duration = new_out
                else:
                    c.fit_duration = new_out
                tip = f"길이 {new_out:.2f}초"
            else:
                src_in = o["src_in"] + (o["out"] - new_out) * o["speed"]
                src_in = round(min(max(0.0, src_in), o["src_out"] - 0.1 * o["speed"]), 3)
                c.trim_start = src_in
                tip = f"시작 자르기 {src_in:.2f}초 · 길이 {(o['src_out'] - src_in) / o['speed']:.2f}초"
                if src_in <= 0.0 and o["out"] - dt > o["out"] + o["src_in"] / o["speed"] + 1e-3:
                    tip += " (원본 처음까지)"
        elif k == "clip":
            self.setCursor(Qt.ClosedHandCursor)
            d["drop_x"] = pos.x()
            tip = "놓으면 이 위치로 순서 이동"
        elif k == "subtitle":
            items = ensure_items(pr)
            it = next(i for i in items if i.index == d["index"])
            if d["part"] == "left":
                off = pr.subtitle.offset  # 자막 값은 오프셋 전, 붙을 곳은 화면(타임라인) 시각
                it.start = min(round(self.snap_time(max(0.0, o["start"] + dt) + off, fine, "subtitle", it.index)[0] - off, 3),
                               it.end - 0.1)
            elif d["part"] == "right":
                off = pr.subtitle.offset
                it.end = max(round(self.snap_time(o["end"] + dt + off, fine, "subtitle", it.index)[0] - off, 3),
                             it.start + 0.1)
            else:
                off = pr.subtitle.offset
                dd = max(dt, -o["start"])
                delta, stuck = self.snap_span(o["start"] + dd + off, o["end"] + dd + off, fine, "subtitle", it.index)
                delta = max(dd + delta, -o["start"]) if stuck else snap(dd, fine)
                it.start, it.end = round(o["start"] + delta, 3), round(o["end"] + delta, 3)
            tip = f"{it.start:.2f} → {it.end:.2f} ({it.end - it.start:.2f}초)"
        elif k == "take" and d["part"] in ("left", "right"):
            # 더빙 조각 끝 끌기 = 길이 조절(파일 안 들리는 구간). 옆 조각에 닿으면 멈춤 (Shift = 겹쳐도 됨)
            from ..edit_ops import take_trim_limit
            i = d["index"]
            t = pr.dubbing.takes[i]
            a0, b0 = o["span"]
            lim = None if fine else take_trim_limit(pr, i, d["part"], self._file_len)
            lat = pr.dubbing.latency_ms / 1000.0
            if d["part"] == "right":
                end = self.snap_time(b0 + dt, fine, "take", i)[0]
                te = o["te"] + (end - b0)
                if lim is not None:
                    te = min(te, o["te"] + (lim - lat - b0))
                t.trim_end = round(min(o["flen"], max(o["ts"] + 0.1, te)), 3)
                if abs(t.trim_end - o["flen"]) < 1e-3:
                    t.trim_end = None
            else:
                st = self.snap_time(a0 + dt, fine, "take", i)[0]
                ts = o["ts"] + (st - a0)
                if lim is not None:
                    ts = max(ts, o["ts"] + (lim - lat - a0))
                t.trim_start = round(max(0.0, min(o["te"] - 0.1, ts)), 3)
            te_now = t.trim_end if t.trim_end is not None else o["flen"]
            tip = f"들리는 길이 {te_now - (t.trim_start or 0.0):.2f}초"
            if lim is not None and ((d["part"] == "right" and te_now >= o["te"] + (lim - lat - b0) - 1e-3)
                                    or (d["part"] == "left" and (t.trim_start or 0) <= o["ts"] + (lim - lat - a0) + 1e-3)):
                tip += "\n옆 조각에 닿음 (Shift = 겹쳐도 됨)"
        elif k == "take":
            t = pr.dubbing.takes[d["index"]]
            a0, b0 = o["span"]
            delta, stuck = self.snap_span(a0 + dt, b0 + dt, fine, "take", d["index"])
            t.offset = round(o["offset"] + dt + delta, 3) if stuck else snap(o["offset"] + dt, fine)
            tip = f"오프셋 {t.offset:+.2f}초"
            from ..edit_ops import take_overlaps
            if d["index"] in take_overlaps(pr, self._file_len)[1]:
                tip += ("\n⚠ 겹쳐 놓기 — 두 소리가 동시에 들립니다" if fine
                        else "\n놓으면 옆 조각 끝에 붙습니다 (Shift 를 누른 채 놓으면 겹쳐 놓기)")
        elif k == "bgm":
            pr.bgm.start_offset = max(0.0, self.snap_time(o["start"] + dt, fine, "bgm")[0])
            tip = f"시작 {pr.bgm.start_offset:.2f}초"
        if k not in ("clip", "transition") or d["part"] in ("left", "right"):
            self.model.recompute()
            if k == "clip":
                tip += self._transition_note(d["index"])
        QToolTip.showText(ev.globalPosition().toPoint(), tip, self)
        self.update()

    def _transition_note(self, i: int) -> str:
        """전환은 앞뒤 클립 중 짧은 쪽 길이까지만(자동 제한) — 줄었으면 알려 줌."""
        from ..timeline import effective_transition
        tl = self.model.timeline.clips
        notes = []
        for j in (i, i + 1):
            if 0 < j < len(tl):
                want = effective_transition(self.model.project, j).duration
                got = tl[j].transition_in.duration
                if want > got + 1e-3:
                    notes.append(f"전환 {want:.2f}→{got:.2f}초로 자동 줄임")
        return ("\n" + " · ".join(notes)) if notes else ""

    def contextMenuEvent(self, ev):
        pos = QPointF(ev.pos())
        t = self.t_of(pos.x())
        hit = self.hit_test(pos) if pos.y() >= RULER_H else None
        if hit is not None:
            self.model.select(hit.kind, hit.index)
        self.context_requested.emit(ev.globalPos(), t)

    def set_marks(self, a: Optional[float], b: Optional[float]):
        self.mark_in, self.mark_out = a, b
        self.update()

    def leaveEvent(self, ev):
        if self._razor_x is not None:
            self._razor_x = None
            self.update()
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(ev)

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
        if d["kind"] == "take" and d["part"] == "body" and not (ev.modifiers() & Qt.ShiftModifier):
            from ..edit_ops import settle_take
            if settle_take(self.model.project, d["index"], self._file_len):
                self.model.recompute()
                self.status_requested.emit("더빙 조각이 겹쳐서 옆 조각 끝에 붙였습니다 (Shift 를 누른 채 놓으면 겹쳐 놓기)")
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
