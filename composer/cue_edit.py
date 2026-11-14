"""'자막 맞추기' 화면의 편집 규칙 (Qt 없음 → 테스트하기 쉬움).

- 무음 경계 자석(스냅): 자막 시작은 '말 시작', 끝은 '말 끝'(= 무음 시작)에 붙음
- 나누기 / 합치기 / 문구 고치기 / 이동 / 가장자리 조절 — 모두 되돌리기(↶)·다시 하기(↷)
- 탭으로 맞추기: 음성을 들으며 누를 때마다 '다음 자막의 시작'을 찍음
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from .subtitles import Cue

LOW_CONF = 0.6  # 이보다 낮으면 노란색(확인 필요)
MIN_LEN = 0.2  # 자막 하나의 최소 길이(초)


@dataclass
class EditCue:
    start: float
    end: float
    text: str
    heard: str = ""  # 음성 인식이 알아들은 글
    conf: Optional[float] = None  # 0~1 (None = 모름)
    reason: str = ""  # 노란색인 까닭

    @property
    def low(self) -> bool:
        return self.conf is not None and self.conf < LOW_CONF


# ------------------------------------------------------------------ 자석
def snap(t: float, points, tol: float) -> float:
    """t 에서 tol 안에 있는 가장 가까운 점으로. 없으면 그대로."""
    best, bd = t, tol + 1e-9
    for p in points:
        d = abs(p - t)
        if d <= bd:
            best, bd = p, d
    return best


def speech_starts(regions) -> list[float]:
    return [s for s, _ in regions]


def speech_ends(regions) -> list[float]:
    return [e for _, e in regions]


# ------------------------------------------------------------------ 글 나누기/합치기
def split_text(text: str, frac: float = 0.5, cursor: Optional[int] = None) -> tuple[str, str]:
    """문구를 둘로: 커서 위치가 있으면 거기서, 없으면 시간 비율(frac)에 가장 가까운 띄어쓰기에서."""
    text = text.strip()
    n = len(text)
    if n < 2:
        return text, ""
    if cursor is not None and 0 < cursor < n:
        cut = cursor
    else:
        target = min(n - 1, max(1, round(n * frac)))
        spaces = [m.start() for m in re.finditer(r"\s", text)]
        cut = min(spaces, key=lambda k: abs(k - target)) if spaces else target
        if spaces and abs(cut - target) > max(3, n * 0.25):  # 너무 멀면 글자 기준
            cut = target
    a, b = text[:cut].strip(), text[cut:].strip()
    return a, b


def join_text(a: str, b: str, max_chars: int = 20) -> str:
    from .autosub import _wrap_lines
    flat = re.sub(r"\s+", " ", f"{a} {b}").strip()
    return "\n".join(_wrap_lines(flat, max_chars)) if flat else ""


def _min_conf(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


# ------------------------------------------------------------------ 목록 + 되돌리기
class CueList:
    def __init__(self, cues: list[EditCue], regions=(), gap: float = 0.05, max_chars: int = 20):
        self.cues = [replace(c) for c in cues]
        self.regions = sorted((float(s), float(e)) for s, e in regions)
        self.gap = gap
        self.max_chars = max_chars
        self._undo: list[list[EditCue]] = []
        self._redo: list[list[EditCue]] = []
        self._key = None

    def __len__(self):
        return len(self.cues)

    # --- 되돌리기
    def _push(self, key=None):
        """바꾸기 직전에 부름. 같은 key 가 이어지면(한 번의 끌기, 이어서 타자) 되돌리기 한 번으로 묶음."""
        if key is not None and key == self._key:
            return
        self._undo.append([replace(c) for c in self.cues])
        self._redo.clear()
        self._key = key

    def end_group(self):
        self._key = None

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append([replace(c) for c in self.cues])
        self.cues = self._undo.pop()
        self._key = None
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append([replace(c) for c in self.cues])
        self.cues = self._redo.pop()
        self._key = None
        return True

    @property
    def dirty(self) -> bool:
        return bool(self._undo)

    # --- 자석
    def snap_start(self, t: float, tol: float) -> float:
        return snap(t, speech_starts(self.regions), tol)

    def snap_end(self, t: float, tol: float) -> float:
        return snap(t, speech_ends(self.regions), tol)

    def bounds(self, i: int) -> tuple[float, float]:
        lo = self.cues[i - 1].end + self.gap if i > 0 else 0.0
        hi = self.cues[i + 1].start - self.gap if i + 1 < len(self.cues) else float("inf")
        return lo, hi

    # --- 시간 바꾸기
    def set_start(self, i: int, t: float, snap_on: bool = True, tol: float = 0.15, key=None) -> float:
        c = self.cues[i]
        if snap_on:
            t = self.snap_start(t, tol)
        lo, _ = self.bounds(i)
        t = min(max(lo, t), c.end - MIN_LEN)
        self._push(key)
        c.start = round(t, 3)
        return c.start

    def set_end(self, i: int, t: float, snap_on: bool = True, tol: float = 0.15, key=None) -> float:
        c = self.cues[i]
        if snap_on:
            t = self.snap_end(t, tol)
        _, hi = self.bounds(i)
        t = max(min(hi, t), c.start + MIN_LEN)
        self._push(key)
        c.end = round(t, 3)
        return c.end

    def move(self, i: int, new_start: float, snap_on: bool = True, tol: float = 0.15, key=None) -> float:
        """길이는 그대로 옮기기. 자석: 시작이나 끝 중 더 가까운 경계에 붙음."""
        c = self.cues[i]
        d = c.end - c.start
        s = new_start
        if snap_on:
            s1 = self.snap_start(s, tol)
            e1 = self.snap_end(s + d, tol)
            cand = [(abs(s1 - s), s1) if s1 != s else None, (abs(e1 - (s + d)), e1 - d) if e1 != s + d else None]
            cand = [x for x in cand if x]
            if cand:
                s = min(cand)[1]
        lo, hi = self.bounds(i)
        s = max(lo, min(s, hi - d))
        self._push(key)
        c.start, c.end = round(s, 3), round(s + d, 3)
        return c.start

    # --- 나누기/합치기/문구
    def split(self, i: int, t: float, cursor: Optional[int] = None) -> bool:
        c = self.cues[i]
        if not (c.start + MIN_LEN <= t <= c.end - MIN_LEN):
            return False
        frac = (t - c.start) / max(1e-6, c.end - c.start)
        a, b = split_text(c.text, frac, cursor)
        if not a or not b:
            return False
        ha, hb = split_text(c.heard, frac) if c.heard else ("", "")
        self._push()
        g = self.gap / 2
        first = replace(c, end=round(t - g, 3), text=a, heard=ha)
        second = replace(c, start=round(t + g, 3), text=b, heard=hb)
        self.cues[i:i + 1] = [first, second]
        return True

    def merge_next(self, i: int) -> bool:
        if not (0 <= i < len(self.cues) - 1):
            return False
        a, b = self.cues[i], self.cues[i + 1]
        self._push()
        reason = " / ".join(x for x in (a.reason, b.reason) if x)
        self.cues[i:i + 2] = [EditCue(a.start, b.end, join_text(a.text, b.text, self.max_chars),
                                      f"{a.heard} {b.heard}".strip(), _min_conf(a.conf, b.conf), reason)]
        return True

    def set_text(self, i: int, text: str, key=None):
        if self.cues[i].text == text:
            return
        self._push(key)
        self.cues[i].text = text

    def mark_checked(self, i: int):
        """'확인했음' — 노란색 끄기."""
        c = self.cues[i]
        if c.low:
            self._push()
            c.conf, c.reason = 1.0, ""

    # --- 찾기
    def at(self, t: float) -> Optional[int]:
        for k, c in enumerate(self.cues):
            if c.start <= t <= c.end:
                return k
        return None

    def next_low(self, i: Optional[int]) -> Optional[int]:
        n = len(self.cues)
        start = -1 if i is None else i
        for k in range(1, n + 1):
            j = (start + k) % n
            if self.cues[j].low:
                return j
        return None

    def low_count(self) -> int:
        return sum(1 for c in self.cues if c.low)

    def to_cues(self) -> list[Cue]:
        return [Cue(k + 1, round(c.start, 3), round(c.end, 3), c.text.strip())
                for k, c in enumerate(self.cues) if c.text.strip()]


# ------------------------------------------------------------------ 탭으로 맞추기
class TapSession:
    """누를 때마다: 지금 시각 = 다음 자막 시작. 이전 자막 끝 = 다음 시작 직전(그 사이에 말이 끝난 곳(무음 시작)이 있으면 거기 + linger).
    뒤의 자막들은 같은 만큼 함께 밀려서 순서·간격이 유지됩니다. 탭 한 번 모음 = 되돌리기 한 번."""

    def __init__(self, cl: CueList, start_index: int = 0, linger: float = 0.3):
        self.cl = cl
        self.i = max(0, min(start_index, len(cl.cues)))
        self.linger = linger
        self._key = ("tap", id(self), start_index)

    @property
    def done(self) -> bool:
        return self.i >= len(self.cl.cues)

    def current_text(self) -> str:
        return self.cl.cues[self.i].text if not self.done else ""

    def next_text(self) -> str:
        return self.cl.cues[self.i + 1].text if self.i + 1 < len(self.cl.cues) else ""

    def prev_text(self) -> str:
        return self.cl.cues[self.i - 1].text if 0 < self.i <= len(self.cl.cues) else ""

    def restart(self, index: int):
        self.i = max(0, min(index, len(self.cl.cues)))
        self._key = ("tap", id(self), index)

    def tap(self, t: float) -> Optional[int]:
        """t 초에 누름 → 몇 번째 자막의 시작을 찍었는지(없으면 None)."""
        cl, i = self.cl, self.i
        if self.done:
            return None
        cues = cl.cues
        gap = cl.gap
        if i > 0:
            prev = cues[i - 1]
            t = max(t, prev.start + MIN_LEN + gap)  # 이전 자막보다 앞으로는 못 감
        cl._push(self._key)
        if i > 0:
            prev = cues[i - 1]
            # 이전 자막 시작 이후 ~ 지금 사이에 말이 끝난 곳(무음 시작)이 있으면 그 직후에서 끝냄
            ends = [e for e in speech_ends(cl.regions) if prev.start + MIN_LEN <= e <= t - gap]
            pe = min(t - gap, ends[-1] + self.linger) if ends else t - gap
            prev.end = round(max(prev.start + MIN_LEN, pe), 3)
        delta = t - cues[i].start
        for c in cues[i:]:  # 뒤 자막들도 함께 이동 (겹치지 않게)
            c.start = round(c.start + delta, 3)
            c.end = round(c.end + delta, 3)
        self.i += 1
        return i
