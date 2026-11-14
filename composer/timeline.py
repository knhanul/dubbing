"""타임라인 계산: 클립 길이, 속도, fit_duration, 전환 겹침, 더빙 배치."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .schema import Clip, Project, Take, Transition

DEFAULT_IMAGE_DURATION = 4.0
MIN_CLIP = 0.1

# 소스 파일 길이를 돌려주는 함수(테스트에서는 가짜 함수를 넣습니다).
DurationFn = Callable[[Clip], Optional[float]]


@dataclass
class ClipTiming:
    index: int
    start: float  # 타임라인 시작 시각 (이전 클립과 겹치는 전환 포함)
    duration: float  # 출력 길이
    speed: float  # 실제 적용되는 재생 속도
    source_in: float  # 소스에서 사용하는 구간 시작
    source_out: float  # 소스에서 사용하는 구간 끝
    transition_in: Transition  # 이전 클립과의 전환(첫 클립은 none)

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Timeline:
    clips: list[ClipTiming]
    total: float

    def clip_at(self, t: float) -> Optional[ClipTiming]:
        for c in reversed(self.clips):
            if c.start <= t < c.end:
                return c
        return None


def clip_timing(clip: Clip, source_duration: Optional[float]) -> tuple[float, float, float, float]:
    """(출력 길이, 실제 속도, 소스 시작, 소스 끝)을 계산합니다."""
    if clip.type == "image":
        d = clip.fit_duration or clip.duration or DEFAULT_IMAGE_DURATION
        d = max(MIN_CLIP, float(d))
        return d, 1.0, 0.0, d
    src = float(source_duration or 0.0)
    t_in = max(0.0, float(clip.trim_start or 0.0))
    t_out = float(clip.trim_end) if clip.trim_end else src
    if src:
        t_out = min(t_out, src)
    used = max(MIN_CLIP, t_out - t_in)
    if clip.fit_duration:
        out = max(MIN_CLIP, float(clip.fit_duration))
        speed = used / out
    else:
        speed = float(clip.speed or 1.0)
        out = used / speed
    return out, speed, t_in, t_in + used


def effective_transition(project: Project, index: int) -> Transition:
    if index == 0:
        return Transition("none", 0.0)
    tr = project.clips[index].transition or project.transition or Transition("none", 0.0)
    if tr.type == "none":
        return Transition("none", 0.0)
    return Transition(tr.type, max(0.0, float(tr.duration or 0.0)))


def build_timeline(project: Project, duration_fn: DurationFn) -> Timeline:
    timings: list[ClipTiming] = []
    cursor = 0.0
    for i, clip in enumerate(project.clips):
        src = duration_fn(clip) if clip.type == "video" else None
        dur, speed, s_in, s_out = clip_timing(clip, src)
        tr = effective_transition(project, i)
        if i > 0 and tr.duration > 0:
            # 전환은 앞뒤 클립 길이의 절반을 넘을 수 없습니다.
            limit = min(timings[-1].duration, dur) * 0.5
            tr = Transition(tr.type, round(min(tr.duration, limit), 4))
        start = cursor - (tr.duration if i > 0 else 0.0)
        timings.append(ClipTiming(i, start, dur, speed, s_in, s_out, tr))
        cursor = start + dur
    return Timeline(timings, cursor)


def take_position(take: Take, cues_by_index: dict, latency_ms: float = 0.0) -> float:
    """더빙 테이크 파일의 0초가 놓이는 타임라인 시각."""
    base = 0.0
    if take.subtitle is not None and take.subtitle in cues_by_index:
        base = cues_by_index[take.subtitle].start
    return base + float(take.offset or 0.0) - latency_ms / 1000.0


def atempo_chain(speed: float) -> list[str]:
    """atempo 는 0.5~2.0 범위만 정확하므로 여러 번 이어 붙입니다."""
    if abs(speed - 1.0) < 1e-6:
        return []
    parts = []
    s = speed
    while s > 2.0:
        parts.append(2.0)
        s /= 2.0
    while s < 0.5:
        parts.append(0.5)
        s /= 0.5
    parts.append(s)
    return [f"atempo={p:.6f}".rstrip("0").rstrip(".") for p in parts]
