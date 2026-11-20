"""프로젝트 JSON 데이터 모델.

모든 시간 단위는 초(float)입니다. ``from_dict`` 는 관대하게 읽어서 누락된 값은
기본값으로 채우고, 검증은 :mod:`composer.validate` 에서 별도로 합니다.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 2  # 2: 전환을 경계 가운데에 걸침(클립 맞닿음). 1: 겹쳐 놓기(열 때 자동 변환)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
SUBTITLE_EXTS = {".srt", ".vtt"}

MOTIONS = [
    "none", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down",
    "zoom_in_left", "zoom_in_right", "ken_burns",
]
MOTION_LABELS = {
    "none": "움직임 없음", "zoom_in": "확대(줌 인)", "zoom_out": "축소(줌 아웃)",
    "pan_left": "왼쪽으로 이동", "pan_right": "오른쪽으로 이동",
    "pan_up": "위로 이동", "pan_down": "아래로 이동",
    "zoom_in_left": "확대하며 왼쪽", "zoom_in_right": "확대하며 오른쪽",
    "ken_burns": "켄 번즈(확대+대각 이동)",
}
EASINGS = ["linear", "ease"]
# "fade" = 검은 화면을 거치는 페이드, "crossfade" = 두 장면이 겹치는 디졸브.
# 그 외 이름은 ffmpeg xfade 전환 이름을 그대로 사용합니다.
TRANSITIONS = [
    "none", "fade", "crossfade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "circleopen", "circleclose",
    "dissolve", "smoothleft", "smoothright", "radial", "zoomin",
]
TRANSITION_LABELS = {
    "none": "없음(바로 전환)", "fade": "페이드(검은 화면)", "crossfade": "크로스페이드(서서히 바뀜)",
}
from .effects import LOOK_LABELS, LOOKS  # noqa: E402,F401  (색 필터 목록)

FITS = ["cover", "contain"]  # cover = 꽉 채우고 잘라냄, contain = 레터박스(여백)


def _filter_kwargs(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass
class Transition:
    type: str = "none"
    duration: float = 0.5

    @classmethod
    def from_dict(cls, d: Any) -> Optional["Transition"]:
        if d is None:
            return None
        if isinstance(d, str):
            return cls(type=d)
        return cls(**_filter_kwargs(cls, d))


@dataclass
class OutputSettings:
    width: int = 1920
    height: int = 1080
    fps: float = 30
    fit: str = "cover"
    background: str = "#000000"
    original_volume: float = 1.0  # 모든 클립 원본 소리에 곱해지는 전체 볼륨
    file: str = "output.mp4"
    fade_in: float = 0.0  # 영상 시작: 검은 화면에서 서서히 (소리도 함께), 초
    fade_out: float = 0.0  # 영상 끝: 검은 화면으로 서서히 (소리도 함께), 초


@dataclass
class Clip:
    file: str = ""
    type: str = "image"  # image | video
    duration: Optional[float] = None  # 이미지 표시 시간
    motion: str = "none"
    motion_strength: float = 0.15
    motion_easing: str = "ease"
    motion_from: float = 0.0  # 움직임 경로 중 이 클립이 쓰는 구간(0~1). 자르기로 나눈 이미지가 이어지게
    motion_to: float = 1.0
    speed: float = 1.0
    fit_duration: Optional[float] = None  # 지정하면 이 길이에 맞도록 속도 자동 계산
    trim_start: float = 0.0
    trim_end: Optional[float] = None
    mute: bool = False
    volume: float = 1.0
    fit: Optional[str] = None  # None 이면 output.fit
    transition: Optional[Transition] = None  # 이전 클립 -> 이 클립 전환 (None 이면 전역 기본값)
    label: str = ""
    # ✨ 효과 (composer/effects.py) — 기본값이면 아무 필터도 안 붙음
    look: str = "none"  # 색 필터: none | bw | sepia | vintage | warm | cool | vivid | cinematic
    brightness: float = 0.0  # -0.5 ~ 0.5
    contrast: float = 1.0  # 0.5 ~ 2
    saturation: float = 1.0  # 0 ~ 3
    vignette: float = 0.0  # 0 ~ 1 (가장자리 어둡게)
    blur: float = 0.0  # 0 ~ 1 (흐림)
    grain: float = 0.0  # 0 ~ 1 (필름 노이즈)

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        kw = _filter_kwargs(cls, d)
        if "transition" in kw:
            kw["transition"] = Transition.from_dict(kw["transition"])
        c = cls(**kw)
        if not d.get("type") and c.file:
            c.type = guess_type(c.file) or "image"
        return c


@dataclass
class SubtitleStyle:
    font: str = "Malgun Gothic"
    size: int = 48  # 출력 영상 높이 기준 픽셀
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline: float = 2.0
    bold: bool = True
    margin_v: int = 60  # 아래/위 위치일 때 화면 가장자리에서 떨어진 거리(px). 가운데면 위(-)/아래(+) 이동은 없음
    position: str = "bottom"  # bottom(아래) | center(가운데) | top(위)
    box: bool = False  # 글자 뒤 배경 상자
    box_color: str = "#000000"
    box_opacity: float = 0.6  # 0~1
    box_padding: int = 12  # 상자 여백(px)
    appear: str = "none"  # none(그냥) | fade(페이드) | typing(타이핑) | slide_up(아래에서 올라오기) | pop(톡 튀어나오기)
    appear_duration: float = 0.3  # 나타나는 효과 시간(초). 타이핑은 한 줄 전체를 치는 시간(최소 글자당 0.04초)


SUB_POSITIONS = ("bottom", "center", "top")
SUB_APPEARS = ("none", "fade", "typing", "slide_up", "pop")


def _style_override(d: Any) -> Optional[dict]:
    """자막 한 줄의 모양 덮어쓰기: SubtitleStyle 칸 이름만 남김(빈 것은 None)."""
    if not isinstance(d, dict):
        return None
    names = {f.name for f in fields(SubtitleStyle)}
    out = {k: v for k, v in d.items() if k in names}
    return out or None


@dataclass
class SubtitleItem:
    index: int
    start: float
    end: float
    text: str
    # 이 자막만 다르게 할 모양 — 전체 스타일(subtitle.style)과 다른 칸만 담음. 예: {"color": "#FFD400", "size": 64}
    # None 이면 전체 스타일을 그대로 따름.
    style: Optional[dict] = None


@dataclass
class SubtitleSettings:
    file: Optional[str] = None
    offset: float = 0.0
    burn_in: bool = True  # False 면 소프트 자막(mov_text)
    style: SubtitleStyle = field(default_factory=SubtitleStyle)
    # 미세 조정한 자막 목록. 있으면 파일 대신 이것을 사용합니다(offset 적용 전 값).
    items: Optional[list[SubtitleItem]] = None

    @classmethod
    def from_dict(cls, d: Any) -> "SubtitleSettings":
        if d is None:
            return cls()
        if isinstance(d, str):
            return cls(file=d)
        kw = _filter_kwargs(cls, d)
        if isinstance(kw.get("style"), dict):
            kw["style"] = SubtitleStyle(**_filter_kwargs(SubtitleStyle, kw["style"]))
        elif "style" in kw:
            kw["style"] = SubtitleStyle()
        if kw.get("items") is not None:
            kw["items"] = [SubtitleItem(**_filter_kwargs(SubtitleItem, i)) for i in kw["items"]]
            for it in kw["items"]:
                it.style = _style_override(it.style)
        return cls(**kw)


@dataclass
class BGM:
    file: Optional[str] = None
    volume: float = 0.3
    start_offset: float = 0.0  # 타임라인에서 음악이 시작되는 시점
    file_start: float = 0.0  # 음악 파일 안에서 재생을 시작할 위치
    fade_in: float = 1.0
    fade_out: float = 2.0
    loop: bool = True
    duck_under_voice: bool = True  # 더빙 목소리가 나올 때 자동으로 음악 줄이기
    duck_ratio: float = 6.0

    @classmethod
    def from_dict(cls, d: Any) -> "BGM":
        if d is None:
            return cls()
        if isinstance(d, str):
            return cls(file=d)
        return cls(**_filter_kwargs(cls, d))


@dataclass
class Take:
    file: str
    subtitle: Optional[int] = None  # 자막 번호. None 이면 offset 이 절대 시간
    offset: float = 0.0  # 자막 시작 기준 상대 위치(초). 음수 = 프리롤 포함 녹음
    volume: float = 1.0
    trim_start: float = 0.0  # 파일 안에서 쓰는 구간 시작(초) — 자르기로 나눈 더빙(원본 파일은 그대로)
    trim_end: Optional[float] = None  # 파일 안에서 쓰는 구간 끝(초), None = 파일 끝까지


@dataclass
class Dubbing:
    folder: str = "takes"
    volume: float = 1.0
    latency_ms: float = 0.0  # 녹음 지연 보정
    takes: list[Take] = field(default_factory=list)
    # 녹음본(파일)별 이름·★ — {"takes/line_0001_v2.wav": {"name": "밝게", "star": true}}
    notes: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Any) -> "Dubbing":
        if d is None:
            return cls()
        kw = _filter_kwargs(cls, d)
        kw["takes"] = [Take(**_filter_kwargs(Take, t)) for t in kw.get("takes") or []]
        notes = kw.get("notes")
        kw["notes"] = {str(k): {kk: v[kk] for kk in ("name", "star") if kk in v}
                       for k, v in notes.items() if isinstance(v, dict)} if isinstance(notes, dict) else {}
        return cls(**kw)


@dataclass
class TrackState:
    """타임라인 트랙(행) 머리의 켜고 끄기. 자막에는 muted/solo 가, 더빙·배경음악에는 hidden 이 쓰이지 않습니다."""
    hidden: bool = False  # 👁 감추기 (클립: 화면 검게, 자막: 표시·새기기 안 함)
    muted: bool = False  # 🔇 소리 끄기
    solo: bool = False  # S 이 트랙 소리만 듣기 (솔로 켠 오디오 트랙끼리만 들림)
    locked: bool = False  # 🔒 잠금 (편집·끌기 막기)


TRACK_KEYS = ("clips", "subtitle", "dubbing", "bgm")


@dataclass
class Tracks:
    clips: TrackState = field(default_factory=TrackState)
    subtitle: TrackState = field(default_factory=TrackState)
    dubbing: TrackState = field(default_factory=TrackState)
    bgm: TrackState = field(default_factory=TrackState)

    @classmethod
    def from_dict(cls, d: Any) -> "Tracks":
        if not isinstance(d, dict):
            return cls()
        return cls(**{k: TrackState(**_filter_kwargs(TrackState, d[k])) for k in TRACK_KEYS
                       if isinstance(d.get(k), dict)})

    def get(self, key: str) -> TrackState:
        return getattr(self, key)


@dataclass
class Project:
    version: int = SCHEMA_VERSION
    title: str = ""
    output: OutputSettings = field(default_factory=OutputSettings)
    transition: Transition = field(default_factory=lambda: Transition("crossfade", 0.5))
    clips: list[Clip] = field(default_factory=list)
    subtitle: SubtitleSettings = field(default_factory=SubtitleSettings)
    bgm: BGM = field(default_factory=BGM)
    dubbing: Dubbing = field(default_factory=Dubbing)
    tracks: Tracks = field(default_factory=Tracks)

    # 저장 위치(직렬화하지 않음). 상대 경로 해석 기준.
    base_dir: Path = field(default_factory=Path.cwd, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict, base_dir: Path | str | None = None) -> "Project":
        out = d.get("output") or {}
        p = cls(
            version=int(d.get("version", SCHEMA_VERSION) or SCHEMA_VERSION),
            title=str(d.get("title", "") or ""),
            output=OutputSettings(**_filter_kwargs(OutputSettings, out)),
            transition=Transition.from_dict(d.get("transition")) or Transition("none", 0.0),
            clips=[Clip.from_dict(c) for c in d.get("clips") or [] if isinstance(c, dict)],
            subtitle=SubtitleSettings.from_dict(d.get("subtitle")),
            bgm=BGM.from_dict(d.get("bgm")),
            dubbing=Dubbing.from_dict(d.get("dubbing")),
            tracks=Tracks.from_dict(d.get("tracks")),
        )
        if base_dir is not None:
            p.base_dir = Path(base_dir)
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("base_dir", None)
        for it in (d.get("subtitle") or {}).get("items") or []:
            if not it.get("style"):
                it.pop("style", None)  # 전체 모양을 따르는 자막은 칸을 쓰지 않음(예전 파일과 같은 모양)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def resolve(self, rel: Optional[str]) -> Optional[Path]:
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def copy(self) -> "Project":
        return copy.deepcopy(self)


def guess_type(filename: str) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    return None


def load_project(path: Path | str) -> Project:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return Project.from_dict(data, base_dir=path.parent)


def save_project(project: Project, path: Path | str) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(project.to_json(), encoding="utf-8")
    tmp.replace(path)
    project.base_dir = path.parent


# ---------------------------------------------------------------------------
# JSON Schema (draft 2020-12). schema/project.schema.json 으로도 배포됩니다.
# ---------------------------------------------------------------------------
_num = {"type": "number"}
_transition = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": TRANSITIONS},
        "duration": {"type": "number", "minimum": 0, "maximum": 10},
    },
    "required": ["type"],
    "additionalProperties": False,
}
_STYLE_PROPS: dict = {
    "font": {"type": "string"},
    "size": {"type": "integer", "minimum": 8, "maximum": 300},
    "color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
    "outline_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
    "outline": {"type": "number", "minimum": 0},
    "bold": {"type": "boolean"},
    "margin_v": {"type": "integer", "minimum": 0},
    "position": {"enum": ["bottom", "center", "top"]},
    "box": {"type": "boolean"},
    "box_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
    "box_opacity": {"type": "number", "minimum": 0, "maximum": 1},
    "box_padding": {"type": "integer", "minimum": 0, "maximum": 200},
    "appear": {"enum": ["none", "fade", "typing", "slide_up", "pop"]},
    "appear_duration": {"type": "number", "minimum": 0, "maximum": 5},
}

JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/knhanul/dubbing/schema/project.schema.json",
    "title": "영상 합성기 프로젝트",
    "type": "object",
    "required": ["clips"],
    "properties": {
        "version": {"type": "integer", "enum": [1, SCHEMA_VERSION],
                    "description": "2 = 전환을 클립 경계 가운데에 걸침(클립은 맞닿음, 전체 길이 = 클립 길이 합). 1 = 예전 겹쳐 놓기(열 때 자동 변환)"},
        "title": {"type": "string"},
        "output": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 16, "maximum": 7680, "multipleOf": 2},
                "height": {"type": "integer", "minimum": 16, "maximum": 4320, "multipleOf": 2},
                "fps": {"type": "number", "minimum": 1, "maximum": 120},
                "fit": {"type": "string", "enum": FITS},
                "background": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                "original_volume": {"type": "number", "minimum": 0, "maximum": 4},
                "fade_in": {"type": "number", "minimum": 0, "maximum": 30, "description": "영상 시작 페이드 인(초, 검은 화면에서)"},
                "fade_out": {"type": "number", "minimum": 0, "maximum": 30, "description": "영상 끝 페이드 아웃(초, 검은 화면으로)"},
                "file": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "transition": _transition,
        "clips": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {"type": "string"},
                    "type": {"type": "string", "enum": ["image", "video"]},
                    "duration": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "motion": {"type": "string", "enum": MOTIONS},
                    "motion_strength": {"type": "number", "minimum": 0, "maximum": 1},
                    "motion_easing": {"type": "string", "enum": EASINGS},
                    "motion_from": {"type": "number", "minimum": 0, "maximum": 1},
                    "motion_to": {"type": "number", "minimum": 0, "maximum": 1},
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 16},
                    "fit_duration": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "trim_start": {"type": "number", "minimum": 0},
                    "trim_end": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "mute": {"type": "boolean"},
                    "volume": {"type": "number", "minimum": 0, "maximum": 4},
                    "fit": {"type": ["string", "null"], "enum": FITS + [None]},
                    "transition": {"oneOf": [_transition, {"type": "null"}]},
                    "label": {"type": "string"},
                    "look": {"type": "string", "enum": LOOKS, "description": "색 필터"},
                    "brightness": {"type": "number", "minimum": -0.5, "maximum": 0.5},
                    "contrast": {"type": "number", "minimum": 0.5, "maximum": 2},
                    "saturation": {"type": "number", "minimum": 0, "maximum": 3},
                    "vignette": {"type": "number", "minimum": 0, "maximum": 1},
                    "blur": {"type": "number", "minimum": 0, "maximum": 1},
                    "grain": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "additionalProperties": False,
            },
        },
        "subtitle": {
            "type": "object",
            "properties": {
                "file": {"type": ["string", "null"]},
                "offset": _num,
                "burn_in": {"type": "boolean"},
                "style": {
                    "type": "object",
                    "properties": _STYLE_PROPS,
                    "additionalProperties": False,
                },
                "items": {
                    "type": ["array", "null"],
                    "items": {
                        "type": "object",
                        "required": ["index", "start", "end", "text"],
                        "properties": {
                            "index": {"type": "integer"},
                            "start": _num, "end": _num, "text": {"type": "string"},
                            "style": {
                                "type": ["object", "null"],
                                "description": "이 자막만 다르게 할 모양 (subtitle.style 과 같은 칸 중 바꿀 것만). "
                                               "생략하면 subtitle.style 을 따름",
                                "properties": _STYLE_PROPS,
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            },
            "additionalProperties": False,
        },
        "bgm": {
            "type": "object",
            "properties": {
                "file": {"type": ["string", "null"]},
                "volume": {"type": "number", "minimum": 0, "maximum": 4},
                "start_offset": {"type": "number", "minimum": 0},
                "file_start": {"type": "number", "minimum": 0},
                "fade_in": {"type": "number", "minimum": 0},
                "fade_out": {"type": "number", "minimum": 0},
                "loop": {"type": "boolean"},
                "duck_under_voice": {"type": "boolean"},
                "duck_ratio": {"type": "number", "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
        "tracks": {
            "type": "object",
            "description": "타임라인 트랙 켜고 끄기 (프로그램이 관리, AI 는 쓰지 않아도 됨)",
            "properties": {k: {"type": "object", "additionalProperties": False, "properties": {
                "hidden": {"type": "boolean"}, "muted": {"type": "boolean"},
                "solo": {"type": "boolean"}, "locked": {"type": "boolean"}}} for k in ("clips", "subtitle", "dubbing", "bgm")},
            "additionalProperties": False,
        },
        "dubbing": {
            "type": "object",
            "properties": {
                "folder": {"type": "string"},
                "volume": {"type": "number", "minimum": 0, "maximum": 4},
                "latency_ms": _num,
                "notes": {
                    "type": "object",
                    "description": "녹음본(파일)별 이름·★ (프로그램이 관리, AI 는 쓰지 않아도 됨)",
                    "additionalProperties": {"type": "object", "properties": {
                        "name": {"type": "string"}, "star": {"type": "boolean"}}},
                },
                "takes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string"},
                            "subtitle": {"type": ["integer", "null"]},
                            "offset": _num,
                            "volume": {"type": "number", "minimum": 0, "maximum": 4},
                            "trim_start": {"type": "number", "minimum": 0},
                            "trim_end": {"type": ["number", "null"], "exclusiveMinimum": 0},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}


def write_json_schema(path: Path | str) -> None:
    Path(path).write_text(json.dumps(JSON_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
