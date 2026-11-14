"""자막 스타일 모음: 기본 제공 스타일 + 내 스타일(저장/이름 바꾸기/삭제/가져오기/내보내기).

* 크기·외곽선·여백 값은 기준 화면(짧은 변 ref 픽셀, 기본 1080)에 맞춘 값이며, 적용할 때 현재 출력 크기의 짧은 변에 맞춰 늘이고 줄입니다.
  (그래서 가로 1920×1080 이든 세로 1080×1920 이든 비슷한 비율로 보임)
* 글꼴은 후보 목록 중 설치된 첫 글꼴을 씁니다(없으면 맑은 고딕).
* 내 스타일은 %LOCALAPPDATA%/VideoComposer/subtitle_presets.json 에 저장됩니다.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .schema import SubtitleStyle, _filter_kwargs

DEFAULT_FONT = "Malgun Gothic"
SANS = ["Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "Noto Sans KR", "Nanum Gothic", "NanumGothic", "나눔고딕",
        "Apple SD Gothic Neo", "Gothic A1"]
SERIF = ["NanumMyeongjo", "나눔명조", "Nanum Myeongjo", "Batang", "바탕", "Noto Serif CJK KR", "Noto Serif KR",
         "AppleMyungjo", "UnBatang", "Gungsuh", "궁서"]
SCALED = ("size", "outline", "margin_v", "box_padding")


@dataclass
class Preset:
    name: str
    style: dict
    fonts: list[str] = field(default_factory=list)  # 글꼴 후보(앞에서부터). 비면 style["font"]
    ref: int = 1080  # 이 값들이 맞춰진 화면 짧은 변(px)
    margin_frac: Optional[float] = None  # 있으면 margin_v = 화면 높이 × 이 값
    builtin: bool = False
    description: str = ""

    def to_json(self) -> dict:
        d = {"name": self.name, "style": dict(self.style), "ref": self.ref}
        if self.fonts:
            d["fonts"] = list(self.fonts)
        if self.margin_frac is not None:
            d["margin_frac"] = self.margin_frac
        return d


def _p(name, desc, fonts, **style) -> Preset:
    mf = style.pop("margin_frac", None)
    return Preset(name, style, fonts, 1080, mf, True, desc)


BUILTIN: list[Preset] = [
    _p("유튜브 기본", "흰 글자 + 검은 외곽선, 아래", SANS,
       size=52, bold=True, color="#FFFFFF", outline_color="#000000", outline=3.0, position="bottom", margin_v=60,
       box=False, appear="none", appear_duration=0.3),
    _p("쇼츠 강조", "크고 굵은 노란 글자, 두꺼운 외곽선, 화면 아래쪽 가운데, 톡 튀어나오기", SANS,
       size=76, bold=True, color="#FFE14D", outline_color="#000000", outline=6.0, position="bottom", margin_frac=0.24,
       box=False, appear="pop", appear_duration=0.3),
    _p("뉴스 자막", "아래 반투명 어두운 상자 + 흰 글자", SANS,
       size=46, bold=True, color="#FFFFFF", outline_color="#000000", outline=0.0, position="bottom", margin_v=48,
       box=True, box_color="#101418", box_opacity=0.72, box_padding=16, appear="fade", appear_duration=0.2),
    _p("영화 자막", "작은 흰 글자, 얇은 외곽선, 페이드", SANS,
       size=40, bold=False, color="#FFFFFF", outline_color="#000000", outline=1.5, position="bottom", margin_v=54,
       box=False, appear="fade", appear_duration=0.4),
    _p("감성 시", "가운데, 명조 글꼴, 타이핑", SERIF,
       size=56, bold=False, color="#FFF8E7", outline_color="#2A2118", outline=1.2, position="center", margin_v=60,
       box=False, appear="typing", appear_duration=1.2),
    _p("깔끔 박스", "흰 상자 + 검은 글자", SANS,
       size=46, bold=True, color="#111111", outline_color="#FFFFFF", outline=0.0, position="bottom", margin_v=70,
       box=True, box_color="#FFFFFF", box_opacity=1.0, box_padding=14, appear="none", appear_duration=0.3),
]


# ------------------------------------------------------------------ 적용
def resolve_font(candidates: Iterable[str], available: Optional[Iterable[str]]) -> str:
    cands = [c for c in candidates if c]
    if available is None:
        return cands[0] if cands else DEFAULT_FONT
    av = set(available)
    lower = {a.lower(): a for a in av}
    for c in cands:
        if c in av:
            return c
        if c.lower() in lower:
            return lower[c.lower()]
    return DEFAULT_FONT


def style_from_preset(preset: Preset, out_w: int, out_h: int, available_fonts: Optional[Iterable[str]] = None,
                      base: Optional[SubtitleStyle] = None) -> SubtitleStyle:
    """현재 출력 크기에 맞춘 SubtitleStyle. 프리셋에 없는 값은 기본값(base 가 아니라 SubtitleStyle 기본)."""
    d = asdict(SubtitleStyle())
    d.update({k: v for k, v in preset.style.items() if k in d})
    k = min(out_w, out_h) / max(1, preset.ref)
    for key in SCALED:
        if key in preset.style:
            v = preset.style[key] * k
            d[key] = round(v, 2) if key == "outline" else int(round(v))
    if preset.margin_frac is not None:
        d["margin_v"] = int(round(out_h * preset.margin_frac))
    d["size"] = max(8, min(300, d["size"]))
    d["font"] = resolve_font(preset.fonts or [preset.style.get("font", DEFAULT_FONT)], available_fonts)
    return SubtitleStyle(**_filter_kwargs(SubtitleStyle, d))


def preset_from_style(name: str, style: SubtitleStyle, out_w: int, out_h: int) -> Preset:
    d = asdict(style)
    return Preset(name, d, [style.font], int(min(out_w, out_h)), None, False, "")


def matches(preset: Preset, style: SubtitleStyle, out_w: int, out_h: int, available_fonts=None) -> bool:
    return style_from_preset(preset, out_w, out_h, available_fonts) == style


# ------------------------------------------------------------------ 내 스타일 저장소
def data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "VideoComposer"


def presets_path() -> Path:
    return data_dir() / "subtitle_presets.json"


class PresetError(ValueError):
    pass


def _validate_style(d: dict) -> list[str]:
    from .validate import validate_project
    return [f"{i.path}: {i.message}" for i in validate_project({"clips": [], "subtitle": {"style": d}}, ".")
            if i.severity == "error" and i.path.startswith("subtitle.style")]


def parse_presets(data) -> list[Preset]:
    """가져오기: {"presets":[...]} / [ ... ] / {"name","style"} / 스타일 dict 하나 모두 허용."""
    if isinstance(data, dict) and "presets" in data:
        items = data["presets"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "style" in data:
        items = [data]
    elif isinstance(data, dict):
        items = [{"name": "가져온 스타일", "style": data}]
    else:
        raise PresetError("스타일 JSON 형식이 아닙니다.")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("style"), dict):
            raise PresetError(f"{i + 1}번째 항목에 style 이 없습니다.")
        st = {k: v for k, v in it["style"].items() if k in asdict(SubtitleStyle())}
        errs = _validate_style(st)
        if errs:
            raise PresetError(f"'{it.get('name', i + 1)}' 스타일이 올바르지 않습니다:\n" + "\n".join(errs))
        name = str(it.get("name") or f"가져온 스타일 {i + 1}").strip()[:60]
        fonts = [str(f) for f in it.get("fonts", []) if isinstance(f, str)]
        ref = int(it.get("ref", 1080)) if isinstance(it.get("ref", 1080), (int, float)) and it.get("ref", 1080) > 0 else 1080
        mf = it.get("margin_frac")
        out.append(Preset(name, st, fonts, ref, float(mf) if isinstance(mf, (int, float)) else None))
    return out


class PresetStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else presets_path()
        self.user: list[Preset] = []
        self.load_error = ""
        self.load()

    def load(self):
        self.user = []
        if not self.path.exists():
            return
        try:
            self.user = parse_presets(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001  (깨진 파일이어도 프로그램은 계속)
            self.load_error = f"내 스타일 파일을 읽지 못했습니다: {e}"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "presets": [p.to_json() for p in self.user]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[Preset]:
        return BUILTIN + self.user

    def names(self) -> set[str]:
        return {p.name for p in self.all()}

    def unique_name(self, name: str) -> str:
        name = name.strip() or "내 스타일"
        if name not in self.names():
            return name
        n = 2
        while f"{name} ({n})" in self.names():
            n += 1
        return f"{name} ({n})"

    def find(self, name: str) -> Optional[Preset]:
        return next((p for p in self.all() if p.name == name), None)

    def add(self, preset: Preset, replace: bool = False) -> Preset:
        existing = next((p for p in self.user if p.name == preset.name), None)
        if existing and replace:
            self.user[self.user.index(existing)] = preset
        else:
            preset.name = self.unique_name(preset.name)
            self.user.append(preset)
        self.save()
        return preset

    def rename(self, old: str, new: str) -> Preset:
        p = next((p for p in self.user if p.name == old), None)
        if p is None:
            raise PresetError("기본 제공 스타일은 이름을 바꿀 수 없습니다.")
        new = new.strip()
        if not new:
            raise PresetError("이름을 입력하세요.")
        if new != old and new in self.names():
            raise PresetError(f"'{new}' 이름이 이미 있습니다.")
        p.name = new
        self.save()
        return p

    def delete(self, name: str):
        before = len(self.user)
        self.user = [p for p in self.user if p.name != name]
        if len(self.user) == before:
            raise PresetError("기본 제공 스타일은 지울 수 없습니다.")
        self.save()

    def export(self, path: Path, names: Optional[list[str]] = None) -> Path:
        items = [p for p in self.all() if names is None or p.name in names]
        if names is None:
            items = self.user
        Path(path).write_text(json.dumps({"version": 1, "presets": [p.to_json() for p in items]},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
        return Path(path)

    def import_file(self, path: Path) -> list[Preset]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise PresetError(f"JSON 을 읽지 못했습니다: {e}") from e
        added = [self.add(p) for p in parse_presets(data)]
        return added
