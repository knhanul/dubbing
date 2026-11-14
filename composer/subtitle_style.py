"""자막 스타일 공통 계산 (Qt 없음).

영상 내보내기(ASS)와 실시간 미리보기 오버레이가 같은 규칙을 쓰도록 위치·효과 계산을 한곳에 둡니다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .schema import SubtitleStyle

POSITION_LABELS = {"bottom": "아래", "center": "가운데", "top": "위"}
APPEAR_LABELS = {
    "none": "그냥",
    "fade": "페이드",
    "typing": "타이핑",
    "slide_up": "아래에서 올라오기",
    "pop": "톡 튀어나오기",
}
ASS_ALIGN = {"bottom": 2, "center": 5, "top": 8}
TYPING_MIN_PER_CHAR = 0.04  # 글자당 최소 시간(초)


def visible_len(text: str) -> int:
    return len(text.replace("\n", ""))


def typing_time(text: str, style: SubtitleStyle, cue_duration: float) -> float:
    """타이핑 효과로 한 자막을 다 치는 데 걸리는 시간."""
    n = max(1, visible_len(text))
    return max(0.05, min(max(style.appear_duration, TYPING_MIN_PER_CHAR * n), 0.8 * max(0.1, cue_duration)))


def effect_duration(style: SubtitleStyle, cue_duration: float) -> float:
    """효과 시간은 자막 길이의 절반을 넘지 않습니다."""
    return max(0.0, min(style.appear_duration, 0.5 * max(0.0, cue_duration)))


def _ease_out(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


@dataclass
class AppearState:
    opacity: float = 1.0  # 0~1
    scale: float = 1.0
    rise: float = 0.0  # 글자 크기 대비 아래로 내려가 있는 정도(0 = 제자리)
    chars: int = -1  # 보이는 글자 수 (-1 = 전부)


def appear_state(style: SubtitleStyle, text: str, elapsed: float, remaining: float, cue_duration: float) -> AppearState:
    """자막이 나타난 뒤 elapsed 초, 사라지기 remaining 초 전의 모습 (미리보기용 근사)."""
    mode = style.appear
    st = AppearState()
    if mode == "none":
        return st
    if mode == "typing":
        tt = typing_time(text, style, cue_duration)
        n = visible_len(text)
        st.chars = n if elapsed >= tt else int(n * max(0.0, elapsed) / tt) + 1
        return st
    d = effect_duration(style, cue_duration)
    if d <= 0:
        return st
    p = max(0.0, elapsed) / d
    if mode == "fade":
        st.opacity = min(1.0, p)
        out = min(d / 2, 0.15)
        if out > 0 and remaining < out:
            st.opacity = min(st.opacity, max(0.0, remaining / out))
    elif mode == "slide_up":
        st.opacity = min(1.0, p / 0.6) if p < 0.6 else 1.0
        st.rise = 0.6 * (1 - _ease_out(p))
    elif mode == "pop":
        st.opacity = min(1.0, max(0.0, elapsed) / min(d, 0.12)) if d > 0 else 1.0
        a = 0.65
        if p < a:
            st.scale = 0.4 + (1.15 - 0.4) * (p / a)
        elif p < 1:
            st.scale = 1.15 - 0.15 * ((p - a) / (1 - a))
        else:
            st.scale = 1.0
    return st
