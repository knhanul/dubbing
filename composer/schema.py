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

SCHEMA_VERSION = 1

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
    "none": "없음(바로 전환)", "fade": "페이드(검은 화면)", "crossfade": "크로스페이드(겹침)",
}
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


@dataclass
class Clip:
    file: str = ""
    type: str = "image"  # image | video
    duration: Optional[float] = None  # 이미지 표시 시간
    motion: str = "none"
    motion_strength: float = 0.15
    motion_easing: str = "ease"
    speed: float = 1.0
    fit_duration: Optional[float] = None  # 지정하면 이 길이에 맞도록 속도 자동 계산
    trim_start: float = 0.0
    trim_end: Optional[float] = None
    mute: bool = False
    volume: float = 1.0
    fit: Optional[str] = None  # None 이면 output.fit
    transition: Optional[Transition] = None  # 이전 클립 -> 이 클립 전환 (None 이면 전역 기본값)
    label: str = ""

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
    margin_v: int = 60


@dataclass
class SubtitleItem:
    index: int
    start: float
    end: float
    text: str


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


@dataclass
class Dubbing:
    folder: str = "takes"
    volume: float = 1.0
    latency_ms: float = 0.0  # 녹음 지연 보정
    takes: list[Take] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> "Dubbing":
        if d is None:
            return cls()
        kw = _filter_kwargs(cls, d)
        kw["takes"] = [Take(**_filter_kwargs(Take, t)) for t in kw.get("takes") or []]
        return cls(**kw)


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
        )
        if base_dir is not None:
            p.base_dir = Path(base_dir)
        return p

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("base_dir", None)
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
JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/knhanul/dubbing/schema/project.schema.json",
    "title": "영상 합성기 프로젝트",
    "type": "object",
    "required": ["clips"],
    "properties": {
        "version": {"type": "integer", "const": SCHEMA_VERSION},
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
                    "speed": {"type": "number", "minimum": 0.1, "maximum": 16},
                    "fit_duration": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "trim_start": {"type": "number", "minimum": 0},
                    "trim_end": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "mute": {"type": "boolean"},
                    "volume": {"type": "number", "minimum": 0, "maximum": 4},
                    "fit": {"type": ["string", "null"], "enum": FITS + [None]},
                    "transition": {"oneOf": [_transition, {"type": "null"}]},
                    "label": {"type": "string"},
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
                    "properties": {
                        "font": {"type": "string"},
                        "size": {"type": "integer", "minimum": 8, "maximum": 300},
                        "color": {"type": "string"},
                        "outline_color": {"type": "string"},
                        "outline": {"type": "number", "minimum": 0},
                        "bold": {"type": "boolean"},
                        "margin_v": {"type": "integer", "minimum": 0},
                    },
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
        "dubbing": {
            "type": "object",
            "properties": {
                "folder": {"type": "string"},
                "volume": {"type": "number", "minimum": 0, "maximum": 4},
                "latency_ms": _num,
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
