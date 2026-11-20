"""AI 응답에서 JSON 추출 + 프로젝트 검증 + 수정 요청 프롬프트 생성."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .schema import EASINGS, FITS, LOOKS, MOTIONS, SCHEMA_VERSION, TRANSITIONS, guess_type


class JsonExtractError(ValueError):
    pass


_FENCE_RE = re.compile(r"```(?:json|JSON|jsonc)?\s*\n?(.*?)```", re.S)


def _balanced_objects(text: str):
    """문자열 안의 균형 잡힌 {...} 덩어리를 차례로 돌려줍니다(문자열 내부 괄호 무시)."""
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:i + 1]
                start = None


def _relaxed(s: str) -> str:
    """흔한 AI 실수 보정: // 주석, 끝 쉼표, 스마트 따옴표."""
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    out, in_str, esc, i = [], False, False, 0
    while i < len(s):
        ch = s[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif s.startswith("//", i):
            while i < len(s) and s[i] != "\n":
                i += 1
            continue
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = len(s) if j < 0 else j + 2
            continue
        else:
            out.append(ch)
        i += 1
    s = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def extract_json(text: str) -> dict:
    """AI 응답 전체를 붙여넣어도 프로젝트 JSON 객체를 찾아냅니다."""
    if not text or not text.strip():
        raise JsonExtractError("붙여넣은 내용이 비어 있습니다.")
    candidates: list[str] = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)
    objs: list[str] = []
    for c in candidates:
        objs.extend(_balanced_objects(c))
    # 가장 큰 덩어리(보통 전체 프로젝트)부터 시도
    objs.sort(key=len, reverse=True)
    last_err: Optional[Exception] = None
    for o in objs:
        for attempt in (o, _relaxed(o)):
            try:
                data = json.loads(attempt)
            except json.JSONDecodeError as e:
                last_err = e
                continue
            if isinstance(data, dict):
                return data
    if last_err:
        raise JsonExtractError(f"JSON 형식 오류: {last_err.msg} (줄 {last_err.lineno}, 칸 {last_err.colno})")
    raise JsonExtractError("응답에서 {...} 형태의 JSON 을 찾지 못했습니다.")


@dataclass
class Issue:
    path: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        tag = "오류" if self.severity == "error" else "경고"
        return f"[{tag}] {self.path}: {self.message}"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _V:
    def __init__(self):
        self.issues: list[Issue] = []

    def err(self, path, msg):
        self.issues.append(Issue(path, msg, "error"))

    def warn(self, path, msg):
        self.issues.append(Issue(path, msg, "warning"))

    def num(self, obj: dict, key: str, path: str, lo=None, hi=None, *, required=False,
            nullable=False, exclusive_lo=False, integer=False):
        if key not in obj:
            if required:
                self.err(f"{path}.{key}", "값이 필요합니다.")
            return None
        v = obj[key]
        if v is None:
            if not nullable:
                self.err(f"{path}.{key}", "null 일 수 없습니다. 숫자를 넣어 주세요.")
            return None
        if not _is_num(v):
            self.err(f"{path}.{key}", f"숫자여야 합니다 (현재: {json.dumps(v, ensure_ascii=False)}).")
            return None
        if integer and int(v) != v:
            self.err(f"{path}.{key}", f"정수여야 합니다 (현재: {v}).")
        if lo is not None and (v <= lo if exclusive_lo else v < lo):
            self.err(f"{path}.{key}", f"{'초과' if exclusive_lo else '이상'} {lo} 이어야 합니다 (현재: {v}).")
        if hi is not None and v > hi:
            self.err(f"{path}.{key}", f"{hi} 이하여야 합니다 (현재: {v}).")
        return v

    def enum(self, obj, key, path, allowed, nullable=False):
        if key not in obj or (obj[key] is None and nullable):
            return
        if obj[key] not in allowed:
            self.err(f"{path}.{key}", f"허용되지 않는 값 '{obj[key]}'. 가능한 값: {', '.join(map(str, allowed))}")

    def boolean(self, obj, key, path):
        if key in obj and not isinstance(obj[key], bool):
            self.err(f"{path}.{key}", "true 또는 false 여야 합니다.")

    def unknown(self, obj, allowed, path):
        for k in obj:
            if k not in allowed:
                self.warn(f"{path}.{k}", "알 수 없는 항목이라 무시됩니다.")

    def file(self, rel, path, base_dir: Optional[Path], kinds: tuple):
        if not isinstance(rel, str) or not rel.strip():
            self.err(path, "파일 이름(문자열)이 필요합니다.")
            return
        kind = guess_type(rel)
        if kind not in kinds:
            self.err(path, f"'{rel}' 은(는) 지원하지 않는 형식입니다 ({'/'.join(kinds)} 필요).")
        if base_dir is not None:
            p = Path(rel)
            if not p.is_absolute():
                p = base_dir / p
            if not p.exists():
                self.err(path, f"파일 '{rel}' 이(가) 폴더에 없습니다.")


def _transition(v: _V, tr, path):
    if tr is None:
        return
    if isinstance(tr, str):
        tr = {"type": tr}
    if not isinstance(tr, dict):
        v.err(path, "전환은 {\"type\": ..., \"duration\": ...} 객체여야 합니다.")
        return
    v.unknown(tr, {"type", "duration"}, path)
    v.enum(tr, "type", path, TRANSITIONS)
    v.num(tr, "duration", path, 0, 10)


STYLE_KEYS = {"font", "size", "color", "outline_color", "outline", "bold", "margin_v", "position",
              "box", "box_color", "box_opacity", "box_padding", "appear", "appear_duration"}


def _check_style(v, sty: dict, sp: str) -> None:
    """subtitle.style 또는 subtitle.items[i].style (자막 모양) 검사."""
    v.unknown(sty, STYLE_KEYS, sp)
    v.num(sty, "size", sp, 8, 300)
    v.num(sty, "outline", sp, 0, 20)
    v.num(sty, "margin_v", sp, 0, 2000)
    v.num(sty, "box_opacity", sp, 0, 1)
    v.num(sty, "box_padding", sp, 0, 200)
    v.num(sty, "appear_duration", sp, 0, 5)
    v.enum(sty, "position", sp, ["bottom", "center", "top"])
    v.enum(sty, "appear", sp, ["none", "fade", "typing", "slide_up", "pop"])
    v.boolean(sty, "bold", sp)
    v.boolean(sty, "box", sp)
    for ck in ("color", "outline_color", "box_color"):
        if ck in sty and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(sty[ck])):
            v.err(f"{sp}.{ck}", "#RRGGBB 형식의 색이어야 합니다. 예: \"#FFFFFF\"")
    if "font" in sty and (not isinstance(sty["font"], str) or not sty["font"].strip()):
        v.err(f"{sp}.font", "글꼴 이름(문자열)이어야 합니다. 예: \"Malgun Gothic\"")


def validate_project(data: Any, base_dir: Path | str | None = None) -> list[Issue]:
    """문제 목록을 돌려줍니다. severity == 'error' 가 없으면 렌더 가능."""
    v = _V()
    base = Path(base_dir) if base_dir is not None else None
    if not isinstance(data, dict):
        v.err("$", "최상위는 JSON 객체 {...} 여야 합니다.")
        return v.issues
    v.unknown(data, {"version", "title", "output", "transition", "clips", "subtitle", "bgm", "dubbing", "tracks"}, "$")
    if "version" in data and data["version"] == 1:
        v.warn("$.version", "예전 형식(version 1: 전환이 클립을 겹쳐 놓음)입니다. 열 때 보이는 시간이 같도록 "
                            f"version {SCHEMA_VERSION}(전환을 경계 가운데에 걸침)으로 자동으로 바꿉니다.")
    elif "version" in data and data["version"] != SCHEMA_VERSION:
        v.warn("$.version", f"버전 {SCHEMA_VERSION} 형식으로 해석합니다.")

    out = data.get("output", {})
    if not isinstance(out, dict):
        v.err("output", "객체여야 합니다.")
        out = {}
    v.unknown(out, {"width", "height", "fps", "fit", "background", "original_volume", "file", "fade_in", "fade_out"}, "output")
    for k, hi in (("width", 7680), ("height", 4320)):
        val = v.num(out, k, "output", 16, hi, integer=True)
        if _is_num(val) and int(val) % 2:
            v.err(f"output.{k}", f"짝수여야 합니다 (현재: {val}).")
    v.num(out, "fps", "output", 1, 120)
    v.enum(out, "fit", "output", FITS)
    v.num(out, "original_volume", "output", 0, 4)
    v.num(out, "fade_in", "output", 0, 30)
    v.num(out, "fade_out", "output", 0, 30)
    if "background" in out and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(out["background"])):
        v.err("output.background", "#RRGGBB 형식의 색이어야 합니다.")

    _transition(v, data.get("transition"), "transition")

    clips = data.get("clips")
    if not isinstance(clips, list) or not clips:
        v.err("clips", "클립이 하나 이상 있는 배열이 필요합니다.")
        clips = []
    allowed_clip = {"file", "type", "duration", "motion", "motion_strength", "motion_easing", "motion_from", "motion_to", "speed",
                    "fit_duration", "trim_start", "trim_end", "mute", "volume", "fit", "transition", "label",
                    "look", "brightness", "contrast", "saturation", "vignette", "blur", "grain"}
    for i, c in enumerate(clips):
        p = f"clips[{i}]"
        if not isinstance(c, dict):
            v.err(p, "객체여야 합니다.")
            continue
        v.unknown(c, allowed_clip, p)
        v.file(c.get("file"), f"{p}.file", base, ("image", "video"))
        ctype = c.get("type") or guess_type(str(c.get("file", "")))
        if c.get("type") is not None:
            v.enum(c, "type", p, ["image", "video"])
            g = guess_type(str(c.get("file", "")))
            if g in ("image", "video") and g != c["type"]:
                v.err(f"{p}.type", f"파일 확장자로는 '{g}' 인데 type 이 '{c['type']}' 입니다.")
        if ctype == "image":
            if c.get("duration") is None and c.get("fit_duration") is None:
                v.err(f"{p}.duration", "이미지는 duration(초)이 필요합니다.")
            v.num(c, "duration", p, 0, 3600, nullable=True, exclusive_lo=True)
        if c.get("motion") is not None:  # 움직임은 이미지·영상 모두
            v.enum(c, "motion", p, MOTIONS)
        v.num(c, "motion_strength", p, 0, 1)
        v.enum(c, "motion_easing", p, EASINGS)
        if c.get("look") is not None:
            v.enum(c, "look", p, LOOKS)
        for k, lo, hi in (("brightness", -0.5, 0.5), ("contrast", 0.5, 2), ("saturation", 0, 3),
                          ("vignette", 0, 1), ("blur", 0, 1), ("grain", 0, 1)):
            v.num(c, k, p, lo, hi)
        v.num(c, "speed", p, 0.1, 16)
        v.num(c, "fit_duration", p, 0, 3600, nullable=True, exclusive_lo=True)
        ts = v.num(c, "trim_start", p, 0)
        te = v.num(c, "trim_end", p, 0, nullable=True, exclusive_lo=True)
        if _is_num(ts) and _is_num(te) and te <= ts:
            v.err(f"{p}.trim_end", f"trim_end({te})는 trim_start({ts})보다 커야 합니다.")
        v.boolean(c, "mute", p)
        v.num(c, "volume", p, 0, 4)
        v.enum(c, "fit", p, FITS, nullable=True)
        _transition(v, c.get("transition"), f"{p}.transition")
        tr = c.get("transition") if isinstance(c.get("transition"), dict) else None
        dur = c.get("fit_duration") or c.get("duration")
        if tr and _is_num(tr.get("duration")) and _is_num(dur) and tr["duration"] > dur:
            v.warn(f"{p}.transition.duration", "전환이 클립 길이보다 길어서 자동으로 줄어듭니다(앞뒤 클립 중 짧은 쪽 길이까지).")

    st = data.get("subtitle")
    if st is not None:
        if isinstance(st, str):
            st = {"file": st}
        if not isinstance(st, dict):
            v.err("subtitle", "객체여야 합니다.")
        else:
            v.unknown(st, {"file", "offset", "burn_in", "style", "items"}, "subtitle")
            if st.get("file"):
                v.file(st["file"], "subtitle.file", base, ("subtitle",))
            v.num(st, "offset", "subtitle", -3600, 3600)
            v.boolean(st, "burn_in", "subtitle")
            if isinstance(st.get("style"), dict):
                _check_style(v, st["style"], "subtitle.style")
            if isinstance(st.get("items"), list):
                for j, it in enumerate(st["items"]):
                    pp = f"subtitle.items[{j}]"
                    if not isinstance(it, dict):
                        v.err(pp, "객체여야 합니다.")
                        continue
                    s = v.num(it, "start", pp, 0, required=True)
                    e = v.num(it, "end", pp, 0, required=True)
                    if _is_num(s) and _is_num(e) and e <= s:
                        v.err(pp, "end 는 start 보다 커야 합니다.")
                    if it.get("style") is not None:  # 이 자막만 다른 모양 (바꾼 칸만)
                        if isinstance(it["style"], dict):
                            _check_style(v, it["style"], f"{pp}.style")
                        else:
                            v.err(f"{pp}.style", "객체여야 합니다. 예: {\"color\": \"#FFD400\"}")

    bgm = data.get("bgm")
    if bgm is not None:
        if isinstance(bgm, str):
            bgm = {"file": bgm}
        if not isinstance(bgm, dict):
            v.err("bgm", "객체여야 합니다.")
        else:
            v.unknown(bgm, {"file", "volume", "start_offset", "file_start", "fade_in", "fade_out", "loop",
                            "duck_under_voice", "duck_ratio"}, "bgm")
            if bgm.get("file"):
                v.file(bgm["file"], "bgm.file", base, ("audio", "video"))
            v.num(bgm, "volume", "bgm", 0, 4)
            for k in ("start_offset", "file_start", "fade_in", "fade_out"):
                v.num(bgm, k, "bgm", 0, 36000)
            v.boolean(bgm, "loop", "bgm")
            v.boolean(bgm, "duck_under_voice", "bgm")
            v.num(bgm, "duck_ratio", "bgm", 1, 20)

    dub = data.get("dubbing")
    if dub is not None:
        if not isinstance(dub, dict):
            v.err("dubbing", "객체여야 합니다.")
        else:
            v.unknown(dub, {"folder", "volume", "latency_ms", "takes"}, "dubbing")
            v.num(dub, "volume", "dubbing", 0, 4)
            for j, t in enumerate(dub.get("takes") or []):
                pp = f"dubbing.takes[{j}]"
                if not isinstance(t, dict):
                    v.err(pp, "객체여야 합니다.")
                    continue
                v.file(t.get("file"), f"{pp}.file", base, ("audio",))
                v.num(t, "offset", pp)
                v.num(t, "volume", pp, 0, 4)
                v.num(t, "trim_start", pp, 0)
                if t.get("trim_end") is not None:
                    v.num(t, "trim_end", pp, 0)

    tr = data.get("tracks")
    if tr is not None:
        if not isinstance(tr, dict):
            v.err("tracks", "객체여야 합니다.")
        else:
            v.unknown(tr, {"clips", "subtitle", "dubbing", "bgm"}, "tracks")
            for k, ts in tr.items():
                if k not in ("clips", "subtitle", "dubbing", "bgm"):
                    continue
                if not isinstance(ts, dict):
                    v.err(f"tracks.{k}", "객체여야 합니다.")
                    continue
                v.unknown(ts, {"hidden", "muted", "solo", "locked"}, f"tracks.{k}")
                for f in ("hidden", "muted", "solo", "locked"):
                    v.boolean(ts, f, f"tracks.{k}")
    return v.issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


def correction_prompt(issues: list[Issue], available_files: Optional[list[str]] = None,
                      parse_error: Optional[str] = None) -> str:
    """AI 에게 다시 보낼 수정 요청 문장."""
    lines = ["방금 보내준 영상 프로젝트 JSON 에 문제가 있어. 아래 문제를 모두 고친 **전체 JSON 하나만** 다시 보내줘.",
             "설명 문장, 주석, 코드블록 밖의 텍스트는 넣지 말고 JSON 만 출력해.", ""]
    if parse_error:
        lines += ["JSON 을 읽을 수 없었어:", f"- {parse_error}",
                  "- 큰따옴표만 사용하고, 마지막 항목 뒤 쉼표와 주석을 빼줘.", ""]
    errs = [i for i in issues if i.severity == "error"]
    warns = [i for i in issues if i.severity == "warning"]
    if errs:
        lines.append("고쳐야 할 오류:")
        lines += [f"- {i.path}: {i.message}" for i in errs]
        lines.append("")
    if warns:
        lines.append("참고(가능하면 고쳐줘):")
        lines += [f"- {i.path}: {i.message}" for i in warns]
        lines.append("")
    if available_files is not None:
        lines.append("사용할 수 있는 파일은 정확히 이것뿐이야 (이름을 그대로 써줘):")
        lines += [f"- {f}" for f in available_files]
    return "\n".join(lines).strip() + "\n"
