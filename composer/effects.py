"""클립 효과: 색 필터(룩)·밝기/대비/채도·분위기(비네팅·흐림·필름 노이즈) → ffmpeg 필터.

클립마다 중간 파일을 만들 때 출력 크기로 맞춘 뒤 붙입니다(캐시 키에 클립 값이 모두 들어가므로
효과를 바꾼 클립만 다시 만듭니다). 미리보기(360p)도 같은 필터를 씁니다.
"""
from __future__ import annotations

from typing import Optional

LOOKS = ["none", "bw", "sepia", "vintage", "warm", "cool", "vivid", "cinematic"]
LOOK_LABELS = {
    "none": "원본", "bw": "흑백", "sepia": "세피아", "vintage": "빈티지",
    "warm": "따뜻하게", "cool": "차갑게", "vivid": "선명하게", "cinematic": "영화톤",
}
LOOK_FILTERS = {
    "bw": ["hue=s=0"],
    "sepia": ["colorchannelmixer=rr=0.393:rg=0.769:rb=0.189:gr=0.349:gg=0.686:gb=0.168:br=0.272:bg=0.534:bb=0.131"],
    "vintage": ["curves=preset=vintage", "eq=saturation=0.85"],
    "warm": ["colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.06:gm=0.01:bm=-0.06:rh=0.03:bh=-0.03"],
    "cool": ["colorbalance=rs=-0.08:bs=0.08:rm=-0.05:bm=0.06:rh=-0.02:bh=0.03"],
    "vivid": ["eq=contrast=1.08:saturation=1.35", "unsharp=5:5:0.6:5:5:0"],
    "cinematic": ["curves=preset=increase_contrast", "colorbalance=rs=-0.05:bs=0.07:rh=0.06:bh=-0.05",
                  "eq=saturation=0.9"],
}

# (속성, 기본값, 최소, 최대, 이름)
ADJUST = [
    ("brightness", 0.0, -0.5, 0.5, "밝기"),
    ("contrast", 1.0, 0.5, 2.0, "또렷함"),
    ("saturation", 1.0, 0.0, 3.0, "색 진하기"),
]
MOOD = [
    ("vignette", 0.0, 0.0, 1.0, "모서리 어둡게"),
    ("blur", 0.0, 0.0, 1.0, "흐림"),
    ("grain", 0.0, 0.0, 1.0, "필름 노이즈"),
]
EFFECT_FIELDS = ["look"] + [a[0] for a in ADJUST + MOOD]
DEFAULTS = {"look": "none", **{a[0]: a[1] for a in ADJUST + MOOD}}
MOTION_FIELDS = ["motion", "motion_strength", "motion_easing"]


def _f(v: float) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def has_effects(clip) -> bool:
    """색·밝기·분위기 중 기본값이 아닌 것이 있으면 True (✨ 표시)."""
    for k, d in DEFAULTS.items():
        v = getattr(clip, k, d)
        if k == "look":
            if (v or "none") != "none":
                return True
        elif abs(float(v if v is not None else d) - d) > 1e-4:
            return True
    return False


def effect_filters(clip, height: int = 1080) -> list[str]:
    """색 필터 → 밝기·대비·채도 → 흐림 → 비네팅 → 노이즈 순서의 필터 목록 (없으면 [])."""
    out: list[str] = []
    out += LOOK_FILTERS.get(getattr(clip, "look", "none") or "none", [])
    b = float(getattr(clip, "brightness", 0.0) or 0.0)
    c = float(getattr(clip, "contrast", 1.0) if getattr(clip, "contrast", None) is not None else 1.0)
    s = float(getattr(clip, "saturation", 1.0) if getattr(clip, "saturation", None) is not None else 1.0)
    eq = []
    if abs(b) > 1e-4:
        eq.append(f"brightness={_f(b)}")
    if abs(c - 1) > 1e-4:
        eq.append(f"contrast={_f(c)}")
    if abs(s - 1) > 1e-4:
        eq.append(f"saturation={_f(s)}")
    if eq:
        out.append("eq=" + ":".join(eq))
    blur = max(0.0, min(1.0, float(getattr(clip, "blur", 0.0) or 0.0)))
    if blur > 1e-4:  # 화면 높이 기준(1080p 에서 최대 sigma 12) → 미리보기(360p)도 같은 느낌
        out.append(f"gblur=sigma={_f(max(0.1, blur * 12.0 * height / 1080.0))}")
    vig = max(0.0, min(1.0, float(getattr(clip, "vignette", 0.0) or 0.0)))
    if vig > 1e-4:
        out.append(f"vignette=angle={_f(0.15 + vig * 0.85)}")
    grain = max(0.0, min(1.0, float(getattr(clip, "grain", 0.0) or 0.0)))
    if grain > 1e-4:
        out.append(f"noise=alls={int(round(4 + grain * 36))}:allf=t+u")
    return out


def copy_effects(src, dst, motion: bool = True) -> None:
    """효과(와 움직임) 값을 복사 — '모든 클립에 적용'."""
    for k in EFFECT_FIELDS + (MOTION_FIELDS if motion else []):
        setattr(dst, k, getattr(src, k))


def clear_effects(clip, motion: bool = False) -> None:
    for k, v in DEFAULTS.items():
        setattr(clip, k, v)
    if motion:
        clip.motion = "none"


def fade_filters(total: float, fade_in: float, fade_out: float) -> tuple[list[str], list[str]]:
    """영상 시작/끝 검은 화면 페이드 (영상 필터, 소리 필터)."""
    v: list[str] = []
    a: list[str] = []
    fi = max(0.0, min(float(fade_in or 0.0), total / 2))
    fo = max(0.0, min(float(fade_out or 0.0), total / 2))
    if fi > 1e-3:
        v.append(f"fade=t=in:st=0:d={_f(fi)}:color=black")
        a.append(f"afade=t=in:st=0:d={_f(fi)}")
    if fo > 1e-3:
        st = max(0.0, total - fo)
        v.append(f"fade=t=out:st={_f(st)}:d={_f(fo)}:color=black")
        a.append(f"afade=t=out:st={_f(st)}:d={_f(fo)}")
    return v, a


def clamp_value(name: str, v) -> Optional[float]:
    for k, d, lo, hi, _ in ADJUST + MOOD:
        if k == name:
            try:
                return max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                return d
    return None
