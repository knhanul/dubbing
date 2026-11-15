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
    else:
        s = round(tm.source_in + u * tm.speed, 4)  # 원본에서 자르는 곳 (속도 반영)
        c.trim_end, c2.trim_start = s, s
        # 뒤 조각의 끝(trim_end)은 원래 그대로(None 이면 파일 끝까지)
        if c.fit_duration:
            c.fit_duration, c2.fit_duration = round(u, 4), round(rest, 4)
    # 움직임 경로를 나눠 이어지게 (이미지·영상 모두). 효과(색·밝기·분위기)는 deepcopy 로 그대로 복사됨
    a, b = c.motion_from, c.motion_to
    mid = round(a + (b - a) * u / tm.duration, 5)
    c.motion_to, c2.motion_from = mid, mid
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


OVERLAP_TOL = 0.005  # 이보다 짧게 닿는 것은 겹침이 아님 (끝에 붙인 것)


def take_spans(project: Project, file_len: Callable[[str], float]) -> list[tuple[float, float]]:
    return [take_span(project, k, file_len) for k in range(len(project.dubbing.takes))]


def take_overlaps(project: Project, file_len: Callable[[str], float]) -> tuple[list[tuple[float, float]], set]:
    """더빙 조각끼리 겹친 구간 [(시작, 끝)] (합쳐서) 과 겹친 조각 번호들 (녹음 지연 보정 전 시각)."""
    spans = take_spans(project, file_len)
    raw, who = [], set()
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a, b = max(spans[i][0], spans[j][0]), min(spans[i][1], spans[j][1])
            if b - a > OVERLAP_TOL:
                raw.append((a, b))
                who.update((i, j))
    raw.sort()
    out: list[tuple[float, float]] = []
    for a, b in raw:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out, who


def _hits(spans, k, s, e) -> bool:
    return any(j != k and min(e, b) - max(s, a) > OVERLAP_TOL for j, (a, b) in enumerate(spans))


def settle_take(project: Project, k: int, file_len: Callable[[str], float]) -> bool:
    """k 번째 더빙이 다른 조각과 겹치면, 겹친 조각의 앞 끝/뒤 끝 중 가까운 쪽에 붙여 놓음
    (그 자리도 다른 조각과 겹치면 다음으로 가까운 끝). 옮겼으면 True."""
    takes = project.dubbing.takes
    if not (0 <= k < len(takes)):
        return False
    spans = take_spans(project, file_len)
    s, e = spans[k]
    if not _hits(spans, k, s, e):
        return False
    ln = e - s
    cands = []
    for j, (a, b) in enumerate(spans):
        if j != k:
            cands += [a - ln, b]
    best = None
    for c in sorted(cands, key=lambda c: (abs(c - s), c)):
        if c >= -1e-6 and not _hits(spans, k, c, c + ln):
            best = c
            break
    if best is None:
        return False
    takes[k].offset = round(float(takes[k].offset or 0.0) + (best - s), 4)
    return True


def take_trim_limit(project: Project, k: int, side: str, file_len: Callable[[str], float]) -> Optional[float]:
    """k 번째 더빙의 왼쪽(side='left') 끝을 늘릴 수 있는 가장 이른 시각 / 오른쪽 끝의 가장 늦은 시각
    (옆 조각에 닿는 곳). 막는 조각이 없으면 None."""
    spans = take_spans(project, file_len)
    s, e = spans[k]
    if side == "left":
        ends = [b for j, (a, b) in enumerate(spans) if j != k and b <= s + OVERLAP_TOL]
        return max(ends) if ends else None
    starts = [a for j, (a, b) in enumerate(spans) if j != k and a >= e - OVERLAP_TOL]
    return min(starts) if starts else None


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


# ------------------------------------------------------------------ 타임라인 툴바: ＋ 자막 · ＋ 전환
NEW_CUE_LEN = 2.0


def insert_cue(project: Project, t: float, text: str = "새 자막", length: float = NEW_CUE_LEN) -> Optional[int]:
    """재생 위치 t 에 자막 한 줄을 넣고 새 번호를 돌려줌. 다른 자막과 겹치지 않게(자리가 0.3초도 없으면 None)."""
    from .subtitles import ensure_items
    items = list(ensure_items(project))
    lt = max(0.0, t - project.subtitle.offset)
    for x in sorted(items, key=lambda x: x.start):
        if x.start - 1e-6 <= lt < x.end:  # 자막 위면 그 자막 끝에서 시작
            lt = x.end
    nxt = min((x.start for x in items if x.start > lt + 1e-6), default=float("inf"))
    end = min(lt + length, nxt)
    if end - lt < 0.3:
        return None
    new = SubtitleItem(0, round(lt, 3), round(end, 3), text)
    _renumber(project, items + [new])
    return new.index


def nearest_boundary(timeline: Timeline, t: float) -> Optional[int]:
    """t 에 가장 가까운 클립 경계(뒤 클립 번호, 1 이상)."""
    n = len(timeline.clips)
    if n < 2:
        return None
    return min(range(1, n), key=lambda j: abs(timeline.clips[j].start - t))


def add_transition(project: Project, timeline: Timeline, t: float) -> Optional[int]:
    """t 에 가장 가까운 경계에 기본 전환을 넣음(기본이 '없음'이면 크로스페이드, 길이 0이면 0.5초). 길이는 자동 제한."""
    from .timeline import transition_limit
    i = nearest_boundary(timeline, t)
    if i is None:
        return None
    g = project.transition
    base = Transition(g.type if g.type != "none" else "crossfade", g.duration if g.duration > 0 else 0.5)
    lim = transition_limit(timeline.clips[i - 1].duration, timeline.clips[i].duration)
    project.clips[i].transition = Transition(base.type, round(min(base.duration, lim), 3))
    return i


def apply_default_transition_all(project: Project) -> int:
    """모든 경계가 기본 전환을 쓰게(클립별 지정·없애기를 지움). 바뀐 경계 수."""
    n = 0
    for c in project.clips[1:]:
        if c.transition is not None:
            c.transition = None
            n += 1
    return n
