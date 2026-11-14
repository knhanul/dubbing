"""SRT / WebVTT 자막 읽기·쓰기 및 시간 계산."""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .schema import Project, SubtitleItem

_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def decode_bytes(raw: bytes) -> str:
    """UTF-8(BOM 포함) → UTF-16 → CP949 순으로 시도합니다."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _secs(h, m, s, ms) -> float:
    ms = (ms or "0").ljust(3, "0")
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_subtitles(text: str) -> list[Cue]:
    """SRT 와 VTT 를 모두 처리합니다. 번호가 없거나 틀려도 순서대로 다시 매깁니다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [l for l in block.split("\n")]
        tline = None
        for i, line in enumerate(lines):
            m = _TIME_RE.search(line)
            if m:
                tline = i
                break
        if tline is None:
            continue  # WEBVTT 헤더, NOTE, STYLE 등
        g = m.groups()
        start, end = _secs(*g[0:4]), _secs(*g[4:8])
        body = "\n".join(l.rstrip() for l in lines[tline + 1:]).strip()
        body = _TAG_RE.sub("", body)
        cues.append(Cue(len(cues) + 1, start, end, body))
    cues.sort(key=lambda c: (c.start, c.end))
    for i, c in enumerate(cues, 1):
        c.index = i
    return cues


def load_subtitles(path: Path | str) -> list[Cue]:
    return parse_subtitles(decode_bytes(Path(path).read_bytes()))


def format_srt_time(t: float) -> str:
    t = max(0.0, t)
    ms_total = int(round(t * 1000))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: Iterable[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{format_srt_time(c.start)} --> {format_srt_time(c.end)}\n{c.text}\n")
    return "\n".join(out)


def apply_offset(cues: Iterable[Cue], offset: float) -> list[Cue]:
    res = []
    for c in cues:
        s, e = c.start + offset, c.end + offset
        if e <= 0:
            continue
        res.append(Cue(c.index, round(max(0.0, s), 4), round(e, 4), c.text))
    res.sort(key=lambda c: (c.start, c.end))
    return res


def project_cues(project: Project, apply_global_offset: bool = True) -> list[Cue]:
    """프로젝트의 최종 자막 목록(미세 조정 반영, 전역 오프셋 적용)."""
    st = project.subtitle
    if st.items is not None:
        cues = [Cue(i.index, i.start, i.end, i.text) for i in st.items]
    elif st.file:
        path = project.resolve(st.file)
        cues = load_subtitles(path) if path and path.exists() else []
    else:
        cues = []
    return apply_offset(cues, st.offset) if apply_global_offset else cues


def ensure_items(project: Project) -> list[SubtitleItem]:
    """미세 조정을 시작할 때 파일 자막을 project.subtitle.items 로 복사합니다."""
    st = project.subtitle
    if st.items is None:
        st.items = [SubtitleItem(c.index, c.start, c.end, c.text) for c in project_cues(project, False)]
    return st.items


def nudge_item(project: Project, index: int, d_start: float = 0.0, d_end: float = 0.0,
               min_len: float = 0.1) -> SubtitleItem:
    """자막 하나의 시작/끝을 이동합니다. 시작만 바꾸면 길이 유지 X(끝 고정)."""
    items = ensure_items(project)
    it = next(i for i in items if i.index == index)
    ns, ne = round(it.start + d_start, 3), round(it.end + d_end, 3)
    ns = max(0.0, ns)
    if ne - ns < min_len:
        if d_start:
            ns = ne - min_len
        else:
            ne = ns + min_len
    it.start, it.end = ns, ne
    return it


def move_item(project: Project, index: int, delta: float) -> SubtitleItem:
    items = ensure_items(project)
    it = next(i for i in items if i.index == index)
    delta = max(delta, -it.start)
    it.start, it.end = round(it.start + delta, 3), round(it.end + delta, 3)
    return it


class CueTimeline:
    """재생 위치에서 현재/다음 자막과 카운트다운을 빠르게 계산합니다."""

    def __init__(self, cues: list[Cue]):
        self.cues = sorted(cues, key=lambda c: c.start)
        self._starts = [c.start for c in self.cues]

    def current(self, t: float) -> Optional[Cue]:
        """t 시점에 표시 중인 자막(겹치면 가장 늦게 시작한 것)."""
        i = bisect_right(self._starts, t) - 1
        for j in range(i, max(-1, i - 50), -1):
            if self.cues[j].end > t:
                return self.cues[j]
        return None

    def next(self, t: float) -> Optional[Cue]:
        i = bisect_right(self._starts, t)
        return self.cues[i] if i < len(self.cues) else None

    def status(self, t: float) -> dict:
        cur, nxt = self.current(t), self.next(t)
        return {
            "current": cur,
            "next": nxt,
            "until_next": (nxt.start - t) if nxt else None,
            "until_end": (cur.end - t) if cur else None,
            "progress": ((t - cur.start) / cur.duration if cur and cur.duration else None),
        }


def has_subtitles(project: Project) -> bool:
    st = project.subtitle
    return bool(st.file) or bool(st.items)


def detach_takes(project: Project, indices: Optional[set] = None) -> int:
    """자막에 연결된 더빙 테이크를 현재 타임라인 위치 그대로 '절대 위치'로 바꿉니다.
    (자막을 지워도 녹음이 엉뚱한 곳으로 옮겨지지 않도록) indices 가 None 이면 전부."""
    from .timeline import take_position

    cues = {c.index: c for c in project_cues(project)}
    n = 0
    for t in project.dubbing.takes:
        if t.subtitle is not None and (indices is None or t.subtitle in indices):
            pos = take_position(t, cues, 0.0)
            t.subtitle = None
            t.offset = round(pos, 3)
            n += 1
    return n


def clear_subtitles(project: Project) -> int:
    """프로젝트에서 자막을 모두 뺍니다(디스크의 SRT 파일은 건드리지 않음). 위치를 고정한 테이크 수를 돌려줍니다."""
    n = detach_takes(project)
    project.subtitle.file = None
    project.subtitle.items = None
    return n
