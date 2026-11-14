"""타임라인 자르기·삭제 (프리미어의 자르기 도구 / 리플 삭제처럼). Qt 없음.

- split_clip: 재생 위치에서 클립을 둘로 (동영상 = 같은 파일을 trim 으로 나눔(속도 반영), 이미지 = 길이 나눔 + 움직임 이어짐)
- split_cue / split_take: 자막 한 줄 / 더빙 한 개를 둘로 (더빙은 파일 안 구간만 나눠 가리킴 — 원본 파일은 그대로)
- cut_range: In~Out 구간을 클립에서 빼고 뒤를 당겨 붙임(선택: 자막·더빙도 함께)
클립 트랙은 원래 차례로 이어 붙는 구조라 클립을 지우면 뒤 클립이 저절로 당겨집니다(리플 삭제).
"""
from __future__ import annotations

import copy
from typing import Callable, Optional

from .schema import Project, SubtitleItem, Transition
from .timeline import MIN_CLIP, Timeline, take_position

EDGE_TOL = 0.1  # 경계에서 이보다 가까우면 자르지 않음(아주 짧은 조각 방지)


# ------------------------------------------------------------------ 클립
def split_clip(project: Project, timeline: Timeline, i: int, t: float) -> bool:
    """i 번째 클립을 타임라인 t 초에서 둘로. 앞 조각은 원래 전환 유지, 새 경계에는 전환 없음."""
    if not (0 <= i < len(timeline.clips)):
        return False
    tm = timeline.clips[i]
    u = t - tm.start
    if not (EDGE_TOL <= u <= tm.duration - EDGE_TOL):
        return False
    c = project.clips[i]
    c2 = copy.deepcopy(c)
    c2.transition = Transition("none", 0.0)
    rest = tm.duration - u
    if c.type == "image":
        if c.fit_duration:
            c.fit_duration, c2.fit_duration = round(u, 4), round(rest, 4)
        else:
            c.duration, c2.duration = round(u, 4), round(rest, 4)
        a, b = c.motion_from, c.motion_to
        mid = round(a + (b - a) * u / tm.duration, 5)
        c.motion_to, c2.motion_from = mid, mid
    else:
        s = round(tm.source_in + u * tm.speed, 4)  # 원본에서 자르는 곳 (속도 반영)
        c.trim_end, c2.trim_start = s, s
        # 뒤 조각의 끝(trim_end)은 원래 그대로(None 이면 파일 끝까지)
        if c.fit_duration:
            c.fit_duration, c2.fit_duration = round(u, 4), round(rest, 4)
    project.clips.insert(i + 1, c2)
    return True


def delete_clip(project: Project, i: int) -> bool:
    """리플 삭제: 지우면 뒤 클립들이 당겨짐(클립은 차례로 이어 붙으므로)."""
    if not (0 <= i < len(project.clips)):
        return False
    del project.clips[i]
    return True


# ------------------------------------------------------------------ 자막
def _renumber(project: Project, items: list[SubtitleItem]):
    """번호를 1..n 으로 다시 매기고, 자막에 묶인 더빙의 번호도 함께 바꿈."""
    mapping = {}
    items.sort(key=lambda x: (x.start, x.end))
    for k, it in enumerate(items, start=1):
        mapping[id(it)] = (it.index, k)
    old_to_new = {}
    for it in items:
        old, new = mapping[id(it)]
        old_to_new.setdefault(old, new)
        it.index = new
    for tk in project.dubbing.takes:
        if tk.subtitle is not None:
            tk.subtitle = old_to_new.get(tk.subtitle, tk.subtitle)
    project.subtitle.items = items


def split_cue(project: Project, index: int, t: float, cursor: Optional[int] = None) -> bool:
    """index 번 자막을 타임라인 t 초에서 둘로 (글은 시간 비율에 가까운 띄어쓰기에서)."""
    from .cue_edit import split_text
    from .subtitles import ensure_items
    items = ensure_items(project)
    off = project.subtitle.offset
    it = next((x for x in items if x.index == index), None)
    if it is None:
        return False
    lt = t - off
    if not (it.start + EDGE_TOL <= lt <= it.end - EDGE_TOL):
        return False
    a, b = split_text(it.text.replace("\n", " "), (lt - it.start) / (it.end - it.start), cursor)
    if not a or not b:
        a, b = it.text, it.text  # 한 글자짜리면 같은 글로
    new = SubtitleItem(0, round(lt, 3), it.end, b)
    it.end, it.text = round(lt, 3), a
    items = list(items) + [new]
    _renumber(project, items)
    return True


# ------------------------------------------------------------------ 더빙
def take_span(project: Project, k: int, file_len: Callable[[str], float]) -> tuple[float, float]:
    """k 번째 더빙이 실제로 들리는 (시작, 끝) 타임라인 초 (녹음 지연 보정 전 위치)."""
    from .subtitles import project_cues
    tk = project.dubbing.takes[k]
    pos = take_position(tk, {c.index: c for c in project_cues(project)}, 0.0)
    te = tk.trim_end if tk.trim_end is not None else file_len(tk.file)
    return pos + (tk.trim_start or 0.0), pos + te


def split_take(project: Project, k: int, t: float, file_len: Callable[[str], float]) -> bool:
    """k 번째 더빙을 t 초에서 둘로 — 같은 파일의 앞/뒤 구간을 가리키는 두 개(원본 파일은 안 바뀜)."""
    if not (0 <= k < len(project.dubbing.takes)):
        return False
    s, e = take_span(project, k, file_len)
    if not (s + EDGE_TOL <= t <= e - EDGE_TOL):
        return False
    tk = project.dubbing.takes[k]
    cut = round((tk.trim_start or 0.0) + (t - s), 4)
    t2 = copy.deepcopy(tk)
    tk.trim_end, t2.trim_start = cut, cut
    project.dubbing.takes.insert(k + 1, t2)
    return True


# ------------------------------------------------------------------ 구간 잘라내기
def cut_range(project: Project, rebuild: Callable[[], Timeline], a: float, b: float, all_tracks: bool,
              file_len: Callable[[str], float], skip: Optional[set] = None) -> tuple[float, str]:
    """In(a)~Out(b) 구간을 클립에서 빼고 뒤를 당김. all_tracks 면 자막·더빙도 같은 만큼 빼고 당김.
    skip: 건드리지 않을 트랙 {"clips", "subtitle", "dubbing"} (잠긴 트랙). (실제로 뺀 초, 안내 글)"""
    skip = skip or set()
    a, b = min(a, b), max(a, b)
    if b - a < EDGE_TOL or "clips" in skip:
        return 0.0, ""
    tl = rebuild()
    for t in (b, a):  # 뒤부터 잘라야 앞 번호가 안 바뀜
        ci = next((c.index for c in tl.clips if c.start < t < c.end), None)
        if ci is not None:
            split_clip(project, tl, ci, t)
            tl = rebuild()
    inside = [c for c in tl.clips if c.start >= a - EDGE_TOL and c.end <= b + EDGE_TOL]
    if not inside:
        return 0.0, ""
    a2 = inside[0].start
    removed = sum(c.duration for c in inside)
    b2 = a2 + removed
    first_after = inside[-1].index + 1
    if first_after < len(project.clips):  # 새로 생긴 자른 경계 → 전환 없음
        project.clips[first_after].transition = Transition("none", 0.0)
    for c in reversed(inside):
        del project.clips[c.index]
    notes = [f"클립 {len(inside)}조각"]
    if all_tracks:
        if "subtitle" not in skip:
            n = _cut_subtitles(project, a2, b2, removed)
            if n:
                notes.append(f"자막 {n}줄")
        if "dubbing" not in skip:
            n = _cut_takes(project, a2, b2, removed, file_len)
            if n:
                notes.append(f"더빙 {n}개")
    return removed, ", ".join(notes)


def _cut_subtitles(project: Project, a: float, b: float, removed: float) -> int:
    from .subtitles import detach_takes, ensure_items
    st = project.subtitle
    if st.items is None and not st.file:
        return 0
    items = ensure_items(project)
    off = st.offset
    la, lb = a - off, b - off
    touched = [it for it in items if it.end > la and it.start < lb]
    # 잘리거나 지워지는 자막에 묶인 더빙은 지금 위치에 고정(그다음 더빙 쪽 규칙으로 함께 잘림)
    detach_takes(project, {it.index for it in touched})
    out, shifted = [], 0
    for it in items:
        if it.end <= la:
            out.append(it)
        elif it.start >= lb:
            it.start, it.end = round(it.start - removed, 3), round(it.end - removed, 3)
            out.append(it)
            shifted += 1
        elif it.start >= la and it.end <= lb:
            continue  # 구간 안 → 지움
        else:  # 걸침 → 구간만큼 짧게
            ns = it.start if it.start < la else la
            ne = it.end - removed if it.end > lb else la
            if ne - ns >= 0.1:
                it.start, it.end = round(ns, 3), round(ne, 3)
                out.append(it)
    _renumber(project, out)
    return len(touched) + shifted


def _cut_takes(project: Project, a: float, b: float, removed: float, file_len) -> int:
    n = 0
    takes = project.dubbing.takes
    k = 0
    while k < len(takes):
        tk = takes[k]
        if tk.subtitle is not None:  # 자막에 묶인 것은 자막과 함께 움직임
            k += 1
            continue
        s, e = take_span(project, k, file_len)
        if e <= a:
            k += 1
            continue
        n += 1
        if s >= b:
            tk.offset = round(tk.offset - removed, 4)
            k += 1
            continue
        if s >= a and e <= b:  # 구간 안 → 뺌
            del takes[k]
            continue
        pieces = []
        if s < a:  # 앞 조각
            p1 = copy.deepcopy(tk)
            p1.trim_end = round((tk.trim_start or 0.0) + (a - s), 4)
            pieces.append(p1)
        if e > b:  # 뒤 조각 → 당겨서 a 에
            p2 = copy.deepcopy(tk)
            p2.trim_start = round((tk.trim_start or 0.0) + (b - s), 4)
            p2.offset = round(tk.offset - removed, 4)
            pieces.append(p2)
        takes[k:k + 1] = pieces
        k += len(pieces)
    return n


def clip_index_at(timeline: Timeline, t: float) -> Optional[int]:
    c = timeline.clip_at(t)
    if c is None and timeline.clips and abs(t - timeline.total) < 1e-6:
        c = timeline.clips[-1]
    return c.index if c else None


__all__ = ["split_clip", "delete_clip", "split_cue", "split_take", "take_span", "cut_range", "clip_index_at",
           "MIN_CLIP"]
