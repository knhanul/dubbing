"""대본 출력: 자막(미세 조정·전체 오프셋 반영)을 표 형태의 대본으로 만듭니다 (Qt 없음).

* 열: 번호, 시작, 끝, 길이, 대본 내용, 녹음 상태(✔), 클립(어느 클립 구간인지), 읽기 속도(글자/초), 메모 칸
* 내보내기: 텍스트(TXT/클립보드), HTML(미리보기·인쇄·PDF), DOCX(python-docx)
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .schema import Project
from .subtitles import Cue

COLUMNS = ("index", "start", "end", "duration", "text", "recorded", "clip", "speed", "memo")
COLUMN_LABELS = {
    "index": "번호", "start": "시작", "end": "끝", "duration": "길이", "text": "대본 내용",
    "recorded": "녹음", "clip": "클립", "speed": "읽기 속도", "memo": "메모",
}
TIME_FORMATS = {"short": "mm:ss.0", "srt": "hh:mm:ss,ms"}


@dataclass
class ScriptOptions:
    title: str = "대본"
    columns: list[str] = field(default_factory=lambda: ["index", "start", "end", "duration", "text", "recorded"])
    font_size: int = 11  # pt
    time_format: str = "short"  # short | srt
    speed_limit: float = 7.0  # 글자/초 넘으면 '빠름'
    memo_lines: int = 1  # 메모 칸 줄 수 (TXT/DOCX)

    def has(self, col: str) -> bool:
        return col in self.columns

    def ordered_columns(self) -> list[str]:
        return [c for c in COLUMNS if c in self.columns]


@dataclass
class ScriptLine:
    index: int
    start: float
    end: float
    text: str
    recorded: Optional[bool] = None  # None = 알 수 없음(외부 SRT)
    clip: str = ""
    clip_index: Optional[int] = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def chars(self) -> int:
        return len("".join(self.text.split()))  # 공백·줄바꿈 제외

    @property
    def cps(self) -> float:
        return self.chars / self.duration if self.duration > 0 else float("inf")

    def too_fast(self, limit: float) -> bool:
        return self.cps > limit


@dataclass
class Script:
    title: str
    lines: list[ScriptLine]
    source: str = ""  # "프로젝트 자막" / 파일 이름

    @property
    def total(self) -> float:
        return max((l.end for l in self.lines), default=0.0)


# ------------------------------------------------------------------ 만들기
def fmt_time(t: float, fmt: str = "short") -> str:
    t = max(0.0, t)
    if fmt == "srt":
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    ds = int(round(t * 10))
    m, ds = divmod(ds, 600)
    return f"{m:02d}:{ds // 10:02d}.{ds % 10}"


def fmt_duration(d: float) -> str:
    return f"{d:.1f}초"


def build_script(cues: list[Cue], title: str, project: Optional[Project] = None, source: str = "") -> Script:
    """cues 는 이미 오프셋이 적용된 최종 자막. project 가 있으면 녹음 상태·클립 구간도 채움."""
    timeline = None
    if project is not None and project.clips:
        from .timeline import build_timeline
        try:
            timeline = build_timeline(project, lambda c: None)
        except Exception:  # noqa: BLE001  (영상 길이를 몰라도 대본은 만들 수 있게)
            timeline = None
    lines = []
    for c in sorted(cues, key=lambda c: (c.start, c.index)):
        rec = _recorded(project, c.index) if project is not None else None
        clip, ci = "", None
        if timeline is not None:
            ci = _clip_at(timeline, c.start)
            if ci is not None:
                cl = project.clips[ci]
                clip = f"{ci + 1}. {cl.label or Path(cl.file).name}"
        lines.append(ScriptLine(c.index, c.start, c.end, c.text.strip(), rec, clip, ci))
    return Script(title or "대본", lines, source)


def _recorded(project: Project, index: int) -> bool:
    for t in project.dubbing.takes:
        if t.subtitle == index:
            p = project.resolve(t.file)
            return bool(p and p.exists())
    return False


def _clip_at(timeline, t: float) -> Optional[int]:
    """t 초에 화면에 나오는 클립(전환 겹침에서는 뒤 클립)."""
    found = None
    for i, ct in enumerate(timeline.clips):
        if ct.start <= t + 1e-6:
            found = i
        else:
            break
    return found


def script_from_project(project: Project, title: str = "") -> Script:
    from .subtitles import project_cues
    return build_script(project_cues(project), title or project.title or "대본", project, "프로젝트 자막")


def script_from_file(path: Path, title: str = "", project: Optional[Project] = None) -> Script:
    from .subtitles import load_subtitles
    cues = load_subtitles(path)
    s = build_script(cues, title or Path(path).stem, None, Path(path).name)
    if project is not None and project.clips:  # 클립 구간은 현재 프로젝트 기준으로
        s2 = build_script(cues, s.title, project)
        for a, b in zip(s.lines, s2.lines):
            a.clip, a.clip_index = b.clip, b.clip_index
    return s


# ------------------------------------------------------------------ 칸 값
def cell(line: ScriptLine, col: str, opts: ScriptOptions) -> str:
    if col == "index":
        return str(line.index)
    if col == "start":
        return fmt_time(line.start, opts.time_format)
    if col == "end":
        return fmt_time(line.end, opts.time_format)
    if col == "duration":
        return fmt_duration(line.duration)
    if col == "text":
        return line.text
    if col == "recorded":
        return "✔" if line.recorded else ("" if line.recorded is not None else "-")
    if col == "clip":
        return line.clip
    if col == "speed":
        if line.duration <= 0:
            return "!"
        s = f"{line.cps:.1f}자/초"
        return s + (" ⚠ 빠름" if line.too_fast(opts.speed_limit) else "")
    if col == "memo":
        return ""
    return ""


def summary(script: Script, opts: ScriptOptions) -> str:
    n = len(script.lines)
    parts = [f"자막 {n}줄", f"전체 {fmt_time(script.total, opts.time_format)}"]
    if any(l.recorded is not None for l in script.lines):
        parts.append(f"녹음 {sum(1 for l in script.lines if l.recorded)}/{n}")
    if opts.has("speed"):
        fast = sum(1 for l in script.lines if l.too_fast(opts.speed_limit))
        if fast:
            parts.append(f"빠른 줄 {fast}개(>{opts.speed_limit:g}자/초)")
    if script.source:
        parts.append(f"출처: {script.source}")
    return " · ".join(parts)


# ------------------------------------------------------------------ 텍스트
def to_text(script: Script, opts: ScriptOptions) -> str:
    cols = opts.ordered_columns()
    out = [script.title, summary(script, opts), ""]
    meta_cols = [c for c in cols if c not in ("text", "memo")]
    for l in script.lines:
        head = "  ".join(
            (f"[{cell(l, c, opts)}]" if c == "index" else
             f"{cell(l, c, opts)} →" if c == "start" and "end" in cols else
             f"{COLUMN_LABELS[c]} {cell(l, c, opts)}" if c in ("recorded", "clip") and cell(l, c, opts) else
             cell(l, c, opts))
            for c in meta_cols if cell(l, c, opts) or c == "index")
        if head:
            out.append(head)
        if "text" in cols:
            out.extend("    " + t for t in l.text.split("\n"))
        if "memo" in cols:
            out.extend(["    메모: " + "_" * 40] + ["          " + "_" * 40] * (max(1, opts.memo_lines) - 1))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ HTML (미리보기·인쇄·PDF)
def to_html(script: Script, opts: ScriptOptions, font_family: str = "") -> str:
    cols = opts.ordered_columns()
    esc = html.escape
    fs = max(6, int(opts.font_size))
    ff = f"font-family:'{font_family}';" if font_family else ""
    base = {"index": 5, "start": 10, "end": 10, "duration": 7, "recorded": 6, "clip": 14, "speed": 11, "memo": 20}
    if opts.time_format == "srt":
        base["start"] = base["end"] = 14
    fixed = sum(base.get(c, 0) for c in cols if c != "text")
    if fixed > 70:  # 칸이 많으면 대본 내용이 최소 30% 는 되게 비율로 줄임
        k = 70 / fixed
        base = {c: v * k for c, v in base.items()}
        fixed = 70
    widths = {c: f"{base[c]:.0f}%" for c in base}
    widths["text"] = f"{max(30, 100 - fixed):.0f}%"
    nowrap = ' style="white-space:nowrap;"'
    rows = []
    head = "".join(f'<th bgcolor="#e8eaef" align="center" width="{widths.get(c, "")}"{nowrap if c != "text" else ""}>'
                   f'{COLUMN_LABELS[c]}</th>' for c in cols)
    prev_clip = None
    for l in script.lines:
        if opts.has("clip") and l.clip_index is not None and l.clip_index != prev_clip and prev_clip is not None:
            rows.append(f'<tr><td colspan="{len(cols)}" bgcolor="#f3f4f8"><small>▼ 클립 {esc(l.clip)} 시작</small></td></tr>')
        prev_clip = l.clip_index if l.clip_index is not None else prev_clip
        tds = []
        fast = l.too_fast(opts.speed_limit)
        for c in cols:
            v = esc(cell(l, c, opts)).replace("\n", "<br>")
            if c == "text":
                tds.append(f'<td>{v}</td>')
            elif c == "memo":
                tds.append('<td>&nbsp;<br>&nbsp;</td>')
            elif c == "speed" and fast and opts.has("speed"):
                tds.append(f'<td align="center" bgcolor="#ffe1e1"><font color="#b3261e"><b>{v}</b></font></td>')
            elif c == "recorded" and l.recorded:
                tds.append(f'<td align="center"><font color="#1e8e3e"><b>{v}</b></font></td>')
            elif c == "clip":
                tds.append(f'<td>{v}</td>')
            else:
                tds.append(f'<td align="center"{nowrap}>{v}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<html><head><meta charset="utf-8"><title>{esc(script.title)}</title></head>'
            f'<body style="{ff}font-size:{fs}pt;color:#000;background:#fff;">'
            f'<h2 style="{ff}">{esc(script.title)}</h2>'
            f'<p style="color:#555;font-size:{max(6, fs - 2)}pt;">{esc(summary(script, opts))}</p>'
            f'<table border="1" cellspacing="0" cellpadding="4" width="100%" style="border-collapse:collapse;">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'</body></html>')


# ------------------------------------------------------------------ DOCX
DOCX_FONT = "Malgun Gothic"


def to_docx(script: Script, opts: ScriptOptions, path: Path, font: str = DOCX_FONT) -> Path:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    cols = opts.ordered_columns()
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    if len(cols) >= 7:  # 열이 많으면 가로
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Mm(15))

    def set_font(run, size=None, bold=None, color=None):
        run.font.name = font
        rpr = run._element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        for k in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rfonts.set(qn(k), font)
        if size:
            run.font.size = Pt(size)
        if bold is not None:
            run.font.bold = bold
        if color:
            run.font.color.rgb = RGBColor.from_string(color)

    st = doc.styles["Normal"]
    st.font.name = font
    st.font.size = Pt(opts.font_size)
    st.element.get_or_add_rPr()
    set_font(doc.add_paragraph().add_run(script.title), opts.font_size + 7, True)
    set_font(doc.add_paragraph().add_run(summary(script, opts)), max(6, opts.font_size - 2), color="555555")

    table = doc.add_table(rows=1, cols=len(cols))
    table.style = "Table Grid"
    for i, c in enumerate(cols):
        cell_ = table.rows[0].cells[i]
        set_font(cell_.paragraphs[0].add_run(COLUMN_LABELS[c]), opts.font_size, True)
    for l in script.lines:
        row = table.add_row().cells
        for i, c in enumerate(cols):
            p = row[i].paragraphs[0]
            if c == "memo":
                for _ in range(max(1, opts.memo_lines) - 1):
                    row[i].add_paragraph("")
                continue
            v = cell(l, c, opts)
            fast = c == "speed" and l.too_fast(opts.speed_limit)
            for j, part in enumerate(v.split("\n")):
                para = p if j == 0 else row[i].add_paragraph()
                set_font(para.add_run(part), opts.font_size, bold=True if fast or (c == "recorded" and l.recorded) else None,
                         color="B3261E" if fast else ("1E8E3E" if c == "recorded" and l.recorded else None))
    # 열 너비
    width_mm = {"index": 12, "start": 22, "end": 22, "duration": 16, "recorded": 12, "clip": 30, "speed": 24, "memo": 45}
    total = (sec.page_width - sec.left_margin - sec.right_margin) / Mm(1)
    fixed = sum(width_mm.get(c, 0) for c in cols if c != "text")
    for i, c in enumerate(cols):
        w = width_mm.get(c, max(30, total - fixed))
        for r in table.rows:
            r.cells[i].width = Mm(w)
    path = Path(path)
    doc.save(str(path))
    return path


def to_txt_file(script: Script, opts: ScriptOptions, path: Path) -> Path:
    path = Path(path)
    path.write_text(to_text(script, opts), encoding="utf-8-sig")  # 메모장에서도 한글이 깨지지 않게 BOM
    return path
