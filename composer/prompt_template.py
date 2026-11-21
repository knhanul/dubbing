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
   - label: 장면 이름(예: S04). 선택 사항이며 타임라인에 표시된다.
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
6. 자막은 두 방법 중 하나로 넣는다. (a) subtitle.file: 폴더에 있는 .srt/.vtt 파일. (b) subtitle.items: 자막을 JSON 안에 직접 넣는다. 각 항목은 index(1부터 차례대로 정수), start, end(초, end > start, 서로 겹치지 않음), text(줄바꿈은 \\n)이다. 폴더에 자막 파일이 없으면 (b)를 쓴다. 음악 파일은 bgm.file에 넣고, 없으면 해당 항목을 생략한다.
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
              json.dumps(EXAMPLE, ensure_ascii=False, indent=2), "```",
              "", "자막을 파일 없이 직접 넣는 경우 예시 (subtitle.items):",
              "```json",
              json.dumps({
                  "subtitle": {
                      "items": [
                          {"index": 1, "start": 0.0, "end": 3.5, "text": "첫 번째 자막\\n두 줄"},
                          {"index": 2, "start": 3.5, "end": 7.0, "text": "두 번째 자막"}
                      ]
                  }
              }, ensure_ascii=False, indent=2),
              "```"]
    if schema_text:
        lines += ["", "정확한 JSON Schema:", "```json", schema_text.strip(), "```"]
    lines += ["", "이제 위 규칙대로 JSON 만 출력해."]
    return "\n".join(lines) + "\n"


def build_realign_prompt(project, entries: list[FileEntry], request: str = "",
                         width: int = 1920, height: int = 1080, fps: float = 30) -> str:
    """녹음 완료 후 클립 길이를 낭독 시간에 맞게 다시 구성하도록 요청하는 프롬프트.

    clips와 transition(필요시 output.fade)만 출력하도록 안내한다.
    """
    from .subtitles import project_cues
    from .timeline import build_timeline, take_position
    from .probe import probe
    from .autosub import detect_speech

    lines = [
        "너는 영상 편집 도우미야. 이미 구성된 프로젝트의 클립 길이를 실제 낭독 시간에 맞게 다시 조정해 줘.",
        "자막과 더빙 녹음은 이미 되어 있으니, **클립 길이·전환만** 조정한 JSON을 출력해.",
        "",
        "## 내가 원하는 조정",
        request.strip() or "(낭독이 쉬는 구간의 가운데에 장면이 바뀌고, 말소리와 전환이 겹치지 않게 해 줘)",
        "",
        f"출력 해상도: {width}x{height}, {fps:g}fps",
        "",
        "## 현재 클립 목록",
    ]

    # 클립 타이밍
    def _dur(clip):
        if clip.type != "video":
            return None
        p = project.resolve(clip.file)
        return probe(p).duration if p and p.exists() else None

    tl = build_timeline(project, _dur)
    for i, ct in enumerate(tl.clips):
        c = project.clips[i]
        src = _dur(c)
        parts = [f"{i+1}. file={c.file!r} type={c.type}"]
        if c.label:
            parts.append(f"label={c.label!r}")
        parts.append(f"timeline={ct.start:.2f}~{ct.end:.2f}s")
        if c.type == "image":
            parts.append(f"duration={c.duration or 4.0:.2f}")
            if c.motion and c.motion != "none":
                parts.append(f"motion={c.motion} strength={c.motion_strength:.2f}")
        else:
            parts.append(f"src_duration={src or 0:.2f}s")
            if c.trim_start:
                parts.append(f"trim_start={c.trim_start:.2f}")
            if c.trim_end:
                parts.append(f"trim_end={c.trim_end:.2f}")
            if abs(c.speed - 1) > 1e-6:
                parts.append(f"speed={c.speed:.2f}")
            if c.fit_duration:
                parts.append(f"fit_duration={c.fit_duration:.2f}")
            if c.mute:
                parts.append("mute=true")
        if ct.transition_in and ct.transition_in.duration > 0:
            parts.append(f"transition_in={ct.transition_in.type}/{ct.transition_in.duration:.2f}s")
        lines.append("- " + " | ".join(parts))

    # 자막
    lines += ["", "## 현재 자막 목록 (offset 적용된 최종 시간)"]
    cues = project_cues(project, apply_global_offset=True)
    cues_by_idx = {c.index: c for c in cues}
    for c in cues:
        text_preview = (c.text or "").replace("\n", " / ")[:60]
        lines.append(f"- index={c.index} {c.start:.2f}~{c.end:.2f}s : {text_preview}")

    # 더빙
    lines += ["", "## 더빙 녹음 (자막 index별 실제 말소리 구간)"]
    takes_by_sub = {}
    for take in project.dubbing.takes:
        if take.subtitle is not None:
            takes_by_sub.setdefault(take.subtitle, []).append(take)

    for c in cues:
        takes = takes_by_sub.get(c.index, [])
        if not takes:
            lines.append(f"- index={c.index}: 녹음 없음")
            continue
        for take in takes:
            pos = take_position(take, cues_by_idx, project.dubbing.latency_ms)
            # 말소리 구간 추정
            file_path = project.resolve(take.file) if take.file else None
            speech = None
            note = "무음 포함"
            if file_path and file_path.exists():
                try:
                    regions = detect_speech(file_path)
                    if regions:
                        # 테이크 파일 기준 말소리 → 타임라인 절대 시간으로 변환
                        trim_s = take.trim_start or 0.0
                        speech_start = pos + (regions[0][0] - trim_s)
                        speech_end = pos + (regions[-1][1] - trim_s)
                        speech = (max(0, speech_start), speech_end)
                        note = "무음 제거"
                except Exception:
                    pass
            if speech:
                lines.append(f"- index={c.index}: 말소리 {speech[0]:.2f}~{speech[1]:.2f}s ({note})")
            else:
                # 테이크 구간 그대로
                end = pos + (take.trim_end or 0) - (take.trim_start or 0) if take.trim_end else pos + 5.0
                lines.append(f"- index={c.index}: 테이크 {pos:.2f}~{end:.2f}s ({note})")

    # 파일 목록
    lines += ["", "## 사용 가능한 파일"]
    labels = {"image": "이미지", "video": "영상", "audio": "음악/소리", "subtitle": "자막"}
    if not entries:
        lines.append("(파일 없음)")
    for e in entries:
        extra = f" ({e.info})" if e.info else ""
        lines.append(f"- {e.name} — {labels.get(e.kind, e.kind)}{extra}")

    # 규칙
    lines += [
        "",
        "## 규칙",
        "a. clips와 transition(필요하면 output.fade_in/fade_out)만 출력한다. subtitle, bgm, dubbing은 출력하지 않는다.",
        "b. 클립 순서와 file은 바꾸지 않는다. 길이(duration, trim, speed, fit_duration)와 전환만 조정한다.",
        "c. 장면 경계는 낭독이 쉬는 구간의 가운데에 둔다. 전환이 말소리와 겹치지 않게 한다.",
        "d. 전체 길이(클립 길이의 합)는 마지막 말소리 끝 + 여운 이상이어야 한다.",
        "e. 영상 클립은 실제 파일 길이 안에서만 trim한다. 늘려야 하면 fit_duration을 쓰고, 0.75배속보다 느려지면 앞 이미지 클립을 늘린다.",
        "f. JSON 하나만 출력한다. 설명·주석 없이.",
        "",
        "이제 위 규칙대로 JSON만 출력해.",
    ]
    return "\n".join(lines) + "\n"
