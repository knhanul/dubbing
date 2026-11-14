"""자막을 Qt 로 그리기 — 미리보기 오버레이와 '🅰 자막 스타일' 탭의 견본이 함께 씁니다.

최종 렌더(libass, composer/render/ass.py)와 같은 규칙(크기·외곽선·배경 상자·위치·여백·나타나기 효과)을
화면 크기에 맞춰 비슷하게 흉내 냅니다. 글꼴 렌더링 차이로 몇 픽셀 다를 수 있습니다.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from ..schema import SubtitleStyle
from ..subtitle_style import AppearState


def make_font(style: SubtitleStyle, px: float) -> QFont:
    f = QFont(style.font or "Malgun Gothic")
    f.setPixelSize(max(4, int(round(px))))
    f.setBold(bool(style.bold))
    return f


def wrap_lines(text: str, fm: QFontMetricsF, max_w: float) -> list[str]:
    """libass(WrapStyle 0)처럼 너무 긴 줄은 단어(없으면 글자) 단위로 나눕니다."""
    out: list[str] = []
    for raw in text.split("\n"):
        if fm.horizontalAdvance(raw) <= max_w or not raw:
            out.append(raw)
            continue
        cur = ""
        tokens = raw.split(" ")
        for i, tok in enumerate(tokens):
            cand = tok if not cur else cur + " " + tok
            if fm.horizontalAdvance(cand) <= max_w:
                cur = cand
                continue
            if cur:
                out.append(cur)
            cur = ""
            for ch in tok:  # 한 단어가 너무 길면 글자 단위
                if fm.horizontalAdvance(cur + ch) > max_w and cur:
                    out.append(cur)
                    cur = ""
                cur += ch
        out.append(cur)
    return out


def paint_subtitle(painter: QPainter, frame: QRectF, text: str, style: SubtitleStyle, out_h: float,
                   state: AppearState | None = None) -> QRectF:
    """frame(출력 영상이 보이는 영역) 안에 자막을 그립니다. 그린 영역을 돌려줍니다."""
    if not text:
        return QRectF()
    state = state or AppearState()
    k = frame.height() / max(1.0, float(out_h))
    font = make_font(style, style.size * k)
    fm = QFontMetricsF(font)
    lines = wrap_lines(text, fm, frame.width() * 0.9)
    lh = fm.height()
    block_h = lh * len(lines)
    margin = style.margin_v * k
    if style.position == "top":
        top = frame.top() + margin
        anchor = QPointF(frame.center().x(), top)
    elif style.position == "center":
        top = frame.center().y() - block_h / 2
        anchor = QPointF(frame.center().x(), frame.center().y())
    else:
        top = frame.bottom() - margin - block_h
        anchor = QPointF(frame.center().x(), frame.bottom() - margin)
    top += state.rise * style.size * k

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setOpacity(max(0.0, min(1.0, state.opacity)))
    if abs(state.scale - 1.0) > 1e-3:
        painter.translate(anchor)
        painter.scale(state.scale, state.scale)
        painter.translate(-anchor)

    remaining_chars = state.chars
    pad = style.box_padding * k
    drawn = QRectF()
    pen = QPen(QColor(style.outline_color), max(0.0, style.outline * 2 * k), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    for i, line in enumerate(lines):
        w = fm.horizontalAdvance(line)
        x = frame.center().x() - w / 2
        y = top + lh * i
        r = QRectF(x, y, w, lh)
        drawn = drawn.united(r)
        if style.box and style.box_opacity > 0 and line.strip():
            c = QColor(style.box_color)
            c.setAlphaF(max(0.0, min(1.0, style.box_opacity)))
            painter.fillRect(r.adjusted(-pad, -pad * 0.5, pad, pad * 0.5), c)
        shown = line
        if remaining_chars >= 0:
            shown = line[:max(0, remaining_chars)]
            remaining_chars = max(0, remaining_chars - len(line))
        if not shown:
            continue
        path = QPainterPath()
        path.addText(x, y + fm.ascent(), font, shown)
        if style.outline > 0:
            painter.strokePath(path, pen)
        painter.fillPath(path, QColor(style.color))
    painter.restore()
    return drawn
