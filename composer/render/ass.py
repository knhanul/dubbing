"""자막 → ASS(Advanced SubStation Alpha) 파일 생성. 영상 내보내기에서 libass 로 영상에 새깁니다.

* 좌표계(PlayResX/Y)는 출력 크기 그대로라 글자 크기·여백이 픽셀 값과 같습니다(초안 렌더는 libass 가 비율대로 축소).
* 배경 상자는 별도 층(Layer 0, BorderStyle=3)으로 그려서 글자 외곽선 색과 상자 색을 따로 쓸 수 있습니다.
* 나타나기 효과: 페이드(\\fad), 타이핑(\\ko 글자별 카라오케), 아래에서 올라오기(\\move), 톡 튀어나오기(\\t 크기 변화).
"""
from __future__ import annotations

from ..schema import Project, SubtitleStyle
from ..subtitle_style import ASS_ALIGN, effect_duration, typing_time
from ..subtitles import Cue


def ass_color(hex_color: str, opacity: float = 1.0) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    a = max(0, min(255, int(round((1 - opacity) * 255))))
    return f"&H{a:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_text(text: str) -> str:
    t = text.replace("\r", "").replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return t.replace("\n", "\\N")


def _anchor(style: SubtitleStyle, w: int, h: int) -> tuple[int, int]:
    x = w // 2
    if style.position == "top":
        return x, style.margin_v
    if style.position == "center":
        return x, h // 2
    return x, h - style.margin_v


def effect_tags(style: SubtitleStyle, cue: Cue, w: int, h: int, *, box: bool = False) -> str:
    """효과 태그 (텍스트 앞에 붙임). 타이핑의 글자별 태그는 typing_text() 에서."""
    dur = cue.end - cue.start
    mode = style.appear
    d = int(round(effect_duration(style, dur) * 1000))
    if mode == "fade" and d > 0:
        return f"\\fad({d},{min(d // 2, 150)})"
    if mode == "typing":
        return f"\\fad({min(150, int(typing_time(cue.text, style, dur) * 500))},0)" if box else ""
    if mode == "slide_up" and d > 0:
        x, y = _anchor(style, w, h)
        rise = int(round(style.size * 0.6))
        return f"\\move({x},{y + rise},{x},{y},0,{d})\\fad({int(d * 0.6)},0)"
    if mode == "pop" and d > 0:
        a = int(d * 0.65)
        return (f"\\fscx40\\fscy40\\t(0,{a},\\fscx115\\fscy115)\\t({a},{d},\\fscx100\\fscy100)"
                f"\\fad({min(d, 120)},0)")
    return ""


def typing_text(text: str, style: SubtitleStyle, cue_duration: float) -> str:
    """글자마다 \\ko 를 붙여 차례로 나타나게 합니다(SecondaryColour 를 투명하게 둠)."""
    chars = [c for c in text.replace("\r", "")]
    n = max(1, len([c for c in chars if c != "\n"]))
    total_cs = typing_time(text, style, cue_duration) * 100
    out, acc, done = [], 0.0, 0
    for c in chars:
        if c == "\n":
            out.append("\\N")
            continue
        done += 1
        nxt = int(round(total_cs * done / n))
        k = max(0, nxt - int(round(acc)))
        acc = nxt
        out.append(f"{{\\ko{k}}}{escape_text(c)}")
    return "".join(out)


def build_ass(project: Project, cues: list[Cue]) -> str:
    st = project.subtitle.style
    w, h = int(project.output.width), int(project.output.height)
    align = ASS_ALIGN.get(st.position, 2)
    bold = -1 if st.bold else 0
    ml = max(10, int(w * 0.05))
    font = (st.font or "Malgun Gothic").replace(",", " ")
    fields = "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, " \
             "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, " \
             "MarginL, MarginR, MarginV, Encoding"
    text_style = (f"Style: Text,{font},{st.size},{ass_color(st.color)},{ass_color(st.color, 0)},"
                  f"{ass_color(st.outline_color)},&H00000000,{bold},0,0,0,100,100,0,0,1,{st.outline:g},0,"
                  f"{align},{ml},{ml},{st.margin_v},1")
    box_style = (f"Style: Box,{font},{st.size},&HFF000000,&HFF000000,{ass_color(st.box_color, st.box_opacity)},"
                 f"&HFF000000,{bold},0,0,0,100,100,0,0,3,{st.box_padding},0,{align},{ml},{ml},{st.margin_v},1")
    lines = [
        "[Script Info]",
        "; 영상 합성기에서 만든 자막",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        f"Format: {fields}",
        text_style,
        box_style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    use_box = st.box and st.box_opacity > 0
    for c in cues:
        if c.end <= c.start or not c.text.strip():
            continue
        t0, t1 = ass_time(c.start), ass_time(c.end)
        dur = c.end - c.start
        if use_box:
            tags = effect_tags(st, c, w, h, box=True)
            lines.append(f"Dialogue: 0,{t0},{t1},Box,,0,0,0,,{{{tags}}}{escape_text(c.text)}" if tags
                         else f"Dialogue: 0,{t0},{t1},Box,,0,0,0,,{escape_text(c.text)}")
        tags = effect_tags(st, c, w, h)
        body = typing_text(c.text, st, dur) if st.appear == "typing" else escape_text(c.text)
        lines.append(f"Dialogue: 1,{t0},{t1},Text,,0,0,0,,{{{tags}}}{body}" if tags
                     else f"Dialogue: 1,{t0},{t1},Text,,0,0,0,,{body}")
    return "\n".join(lines) + "\n"
