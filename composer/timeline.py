"""타임라인 계산: 클립 길이, 속도, fit_duration, 전환, 더빙 배치.

전환(프리미어 방식, 프로젝트 version 2): 클립들은 겹치지 않고 맞닿아 놓이며(전체 길이 = 클립 길이의 합),
전환은 두 클립의 경계 가운데에 걸칩니다. 길이 d 인 전환은 앞 클립의 끝 뒤 d/2 와 뒤 클립의 시작 앞 d/2
'여분(핸들)'을 씁니다. 이미지는 여분이 무한하고, 동영상은 자른 바깥 원본을 쓰되 모자라면 첫/끝 프레임을 멈춰서 채웁니다.
(version 1 은 앞뒤 클립을 겹쳐 놓던 방식 — 열 때 시간이 똑같도록 자동으로 바꿉니다: migrate_v1)"""
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
    transition_in: Transition  # 이전 클립과의 전환(첫 클립은 none) — 경계(start) 가운데에 걸침
    pre: float = 0.0  # 앞쪽 여분(출력 초) = 들어오는 전환 길이 / 2
    post: float = 0.0  # 뒤쪽 여분(출력 초) = 나가는 전환 길이 / 2
    source_len: Optional[float] = None  # 동영상 원본 길이(모르면 None)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def render_len(self) -> float:
        """중간 파일 길이 = 여분 + 보이는 길이 + 여분."""
        return self.pre + self.duration + self.post

    def handle_source(self) -> tuple[float, float, float, float]:
        """동영상: (원본에서 읽을 시작, 끝, 앞에서 멈춰 채울 초, 뒤에서 멈춰 채울 초) — 출력 초 기준 채움."""
        sp = self.speed or 1.0
        want_in = self.source_in - self.pre * sp
        want_out = self.source_out + self.post * sp
        s_in = max(0.0, want_in)
        s_out = min(self.source_len, want_out) if self.source_len else self.source_out
        s_out = max(s_out, self.source_out)
        return s_in, s_out, (s_in - want_in) / sp, max(0.0, want_out - s_out) / sp


@dataclass
class Timeline:
    clips: list[ClipTiming]
    total: float

    def clip_at(self, t: float) -> Optional[ClipTiming]:
        for c in reversed(self.clips):
            if c.start <= t < c.end:
                return c
        return None

    def transition_span(self, index: int) -> tuple[float, float]:
        """index 번째 클립으로 들어오는 전환의 (시작, 끝) — 경계 가운데에 걸침."""
        c = self.clips[index]
        d = c.transition_in.duration
        return c.start - d / 2, c.start + d / 2


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


def transition_limit(prev_dur: float, dur: float) -> float:
    """전환은 앞뒤 클립 중 짧은 쪽 길이를 넘을 수 없습니다(경계 가운데에 걸치므로 각 클립에 절반씩)."""
    return min(prev_dur, dur)


def build_timeline(project: Project, duration_fn: DurationFn) -> Timeline:
    timings: list[ClipTiming] = []
    cursor = 0.0
    for i, clip in enumerate(project.clips):
        src = duration_fn(clip) if clip.type == "video" else None
        dur, speed, s_in, s_out = clip_timing(clip, src)
        tr = effective_transition(project, i)
        if i > 0 and tr.duration > 0:
            tr = Transition(tr.type, round(min(tr.duration, transition_limit(timings[-1].duration, dur)), 4))
        if i > 0 and tr.duration > 0:
            timings[-1].post = tr.duration / 2
        timings.append(ClipTiming(i, cursor, dur, speed, s_in, s_out, tr, tr.duration / 2 if i > 0 else 0.0, 0.0,
                                  float(src) if src else None))
        cursor += dur  # 맞닿게: 전체 길이 = 클립 길이의 합
    return Timeline(timings, cursor)


def migrate_v1(project: Project, duration_fn: DurationFn) -> bool:
    """version 1(겹쳐 놓기) → 2(경계 가운데 전환). 화면에 보이는 시간과 그림이 그대로 되도록
    각 클립을 앞뒤 전환 길이의 절반씩 줄입니다(동영상은 그만큼 자른 부분이 전환 여분이 됨). 바꿨으면 True."""
    from .schema import SCHEMA_VERSION
    if (project.version or 1) >= 2:
        return False
    # 예전 방식의 실제 전환 길이(짧은 클립 절반 제한)
    durs, trs = [], []
    for i, clip in enumerate(project.clips):
        src = duration_fn(clip) if clip.type == "video" else None
        dur, speed, s_in, s_out = clip_timing(clip, src)
        tr = effective_transition(project, i).duration if i > 0 else 0.0
        if i > 0 and tr > 0:
            tr = min(tr, min(durs[-1][0], dur) * 0.5)
        durs.append((dur, speed, s_in, s_out, src))
        trs.append(tr)
    for i, clip in enumerate(project.clips):
        a = trs[i] / 2
        b = (trs[i + 1] / 2) if i + 1 < len(trs) else 0.0
        if a <= 0 and b <= 0:
            continue
        dur, speed, s_in, s_out, src = durs[i]
        new = max(MIN_CLIP, dur - a - b)
        if clip.type == "image":
            if clip.fit_duration:
                clip.fit_duration = round(new, 4)
            else:
                clip.duration = round(new, 4)
            continue
        if clip.fit_duration:
            clip.fit_duration = round(new, 4)
        clip.trim_start = round(s_in + a * speed, 4)
        if b > 0:
            clip.trim_end = round(s_out - b * speed, 4)
        # 전환 길이가 '짧은 쪽 절반'으로 줄어 있었다면 그 값을 클립에 적어 둠(새 제한은 더 느슨해서 길어지지 않게)
    for i, clip in enumerate(project.clips):
        if i == 0:
            continue
        want = effective_transition(project, i).duration
        if want > 0 and trs[i] + 1e-6 < want:
            base = clip.transition or project.transition
            clip.transition = Transition(base.type, round(trs[i], 4))
    project.version = SCHEMA_VERSION
    return True


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
