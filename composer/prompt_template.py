"""AI(Grok 등)에게 붙여넣을 프로젝트 생성 프롬프트를 만듭니다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .subtitle_style import APPEAR_LABELS, POSITION_LABELS
from .schema import LOOK_LABELS, MOTIONS, TRANSITIONS, guess_type


@dataclass
class FileEntry:
    name: str  # 폴더 기준 상대 경로(/ 구분)
    kind: str  # image | video | audio | subtitle
    info: str = ""


def scan_folder(folder: Path | str, info_fn: Optional[Callable[[Path], str]] = None,
                recursive: bool = True) -> list[FileEntry]:
    folder = Path(folder)
    entries = []
    it = folder.rglob("*") if recursive else folder.glob("*")
    for p in sorted(it, key=lambda x: str(x).lower()):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        if rel.startswith((".composer_cache/", "takes/", "render/")) or p.name.startswith("."):
            continue
        kind = guess_type(p.name)
        if not kind:
            continue
        info = ""
        if info_fn is not None:
            try:
                info = info_fn(p)
            except Exception:
                info = ""
        entries.append(FileEntry(rel, kind, info))
    return entries


def media_info_text(path: Path) -> str:
    from .probe import probe

    kind = guess_type(path.name)
    if kind not in ("video", "audio", "image"):
        return ""
    mi = probe(path)
    parts = []
    if mi.width:
        parts.append(f"{mi.width}x{mi.height}")
    if kind in ("video", "audio") and mi.duration:
        parts.append(f"{mi.duration:.2f}초")
    if kind == "video":
        parts.append("소리 있음" if mi.has_audio else "소리 없음")
    return ", ".join(parts)


EXAMPLE = {
    "version": 2,
    "title": "예시 영상",
    "output": {"width": 1920, "height": 1080, "fps": 30, "fit": "cover", "file": "output.mp4"},
    "transition": {"type": "crossfade", "duration": 0.5},
    "clips": [
        {"file": "scene1.png", "type": "image", "duration": 4.0, "motion": "zoom_in", "motion_strength": 0.15},
        {"file": "scene2.mp4", "type": "video", "trim_start": 0.0, "trim_end": 5.0, "speed": 1.0,
         "mute": True, "transition": {"type": "fade", "duration": 0.8}},
        {"file": "scene3.png", "type": "image", "duration": 3.5, "motion": "pan_right", "motion_strength": 0.2},
    ],
    "subtitle": {"file": "subtitle.srt", "offset": 0.0, "burn_in": True,
                 "style": {"font": "Malgun Gothic", "size": 52, "bold": True, "color": "#FFFFFF",
                           "outline_color": "#000000", "outline": 2.5, "position": "bottom", "margin_v": 70,
                           "box": True, "box_color": "#000000", "box_opacity": 0.5, "box_padding": 12,
                           "appear": "fade", "appear_duration": 0.3}},
    "bgm": {"file": "music.mp3", "volume": 0.3, "fade_in": 1.0, "fade_out": 2.0, "loop": True,
            "duck_under_voice": True},
}

RULES = f"""규칙:
1. 반드시 JSON 하나만 출력한다. 설명, 인사말, 주석(//)은 절대 넣지 않는다.
2. file 에는 아래 '사용 가능한 파일' 목록의 이름을 글자 그대로 쓴다. 목록에 없는 파일은 만들지 않는다.
3. 시간 단위는 모두 초(소수점 가능)이다.
4. clips 는 영상에 나올 순서대로 나열한다.
   - type: "image" 또는 "video"
   - 이미지: duration(표시 시간) 필수. motion 으로 움직임을 준다(영상 클립에도 쓸 수 있음).
     motion 값: {", ".join(MOTIONS)}
     motion_strength: 0~1 (0.1~0.25 권장, 클수록 많이 움직임)
   - 효과(생략하면 원본 그대로, 꼭 필요할 때만): look(색 필터) 값: {", ".join(f'"{k}"({v})' for k, v in LOOK_LABELS.items())},
     brightness(-0.5~0.5, 0 = 그대로), contrast(0.5~2, 1 = 그대로), saturation(0~3, 1 = 그대로),
     vignette(0~1, 가장자리 어둡게), blur(0~1, 흐림), grain(0~1, 필름 노이즈)
   - 영상: trim_start/trim_end 로 구간 자르기, speed(0.1~16)로 속도 조절,
     fit_duration 을 주면 그 길이에 맞게 속도를 자동 계산한다(speed 무시).
     mute: true 면 원본 소리 제거, volume 은 원본 소리 크기(1.0 = 그대로).
   - transition: 이전 클립에서 이 클립으로 넘어가는 전환. 생략하면 최상위 transition 사용.
     type 값: {", ".join(TRANSITIONS)}  (duration 은 0.3~1.5 권장)
     전환은 두 클립의 경계 가운데에 걸쳐지고 클립들은 맞닿아 이어지므로 **전체 길이 = 클립 길이(duration)의 합** 이다
     (전환이 길이를 줄이지 않는다). "version": 2 로 쓴다.
5. output: width/height 는 짝수, fps 는 24/30/60 중 하나 권장. fit 은 "cover"(꽉 채움) 또는 "contain"(여백).
   output.fade_in / output.fade_out: 영상 시작/끝을 검은 화면에서·으로 서서히(초, 소리도 함께, 생략하면 0).
6. subtitle.file 에는 .srt 파일, bgm.file 에는 음악 파일을 넣는다. 없으면 해당 항목을 생략한다.
7. subtitle.style (자막 모양, 생략 가능 — 영상 분위기에 맞게 골라도 됨):
   - font: Windows 글꼴 이름 (한글이 되는 글꼴: "Malgun Gothic", "Nanum Gothic", "NanumMyeongjo", "Gulim" 등)
   - size: 출력 높이 기준 픽셀 (1080p 에서 40~64 권장), bold: true/false
   - color / outline_color / box_color: "#RRGGBB", outline: 외곽선 두께 px (0~6 권장)
   - position: {" / ".join(f'"{k}"({v})' for k, v in POSITION_LABELS.items())}, margin_v: 아래/위 가장자리 여백 px
   - box: true 면 글자 뒤에 배경 상자, box_opacity: 0~1, box_padding: 상자 여백 px
   - appear: {" / ".join(f'"{k}"({v})' for k, v in APPEAR_LABELS.items())}, appear_duration: 효과 시간(초, 0.2~0.6 권장)
8. 숫자에 따옴표를 붙이지 않는다. true/false/null 은 소문자로 쓴다."""


def build_prompt(entries: list[FileEntry], request: str = "", width: int = 1920, height: int = 1080,
                 fps: float = 30, target_length: Optional[float] = None,
                 schema_text: Optional[str] = None) -> str:
    lines = [
        "너는 영상 편집 도우미야. 내 폴더의 이미지·영상·음악·자막 파일로 영상을 자동 조립하는 프로그램이 있어.",
        "이 프로그램이 읽을 수 있는 **프로젝트 JSON** 을 만들어줘.",
        "",
        "## 내가 원하는 영상",
        request.strip() or "(파일 순서와 내용에 어울리게 자연스럽게 구성해줘)",
        "",
        f"출력 해상도: {width}x{height}, {fps:g}fps",
    ]
    if target_length:
        lines.append(f"목표 전체 길이: 약 {target_length:g}초 (= 클립 길이의 합, 전환은 길이를 줄이지 않음)")
    lines += ["", "## 사용 가능한 파일"]
    labels = {"image": "이미지", "video": "영상", "audio": "음악/소리", "subtitle": "자막"}
    if not entries:
        lines.append("(파일 없음)")
    for e in entries:
        extra = f" ({e.info})" if e.info else ""
        lines.append(f"- {e.name} — {labels.get(e.kind, e.kind)}{extra}")
    lines += ["", "## 출력 형식", RULES, "", "형식 예시:", "```json",
              json.dumps(EXAMPLE, ensure_ascii=False, indent=2), "```"]
    if schema_text:
        lines += ["", "정확한 JSON Schema:", "```json", schema_text.strip(), "```"]
    lines += ["", "이제 위 규칙대로 JSON 만 출력해."]
    return "\n".join(lines) + "\n"
