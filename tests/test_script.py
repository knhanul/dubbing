"""대본 출력: 대본 만들기(오프셋·미세 조정·녹음 상태·클립 구간·읽기 속도), PDF/DOCX/TXT 파일, 창, 메뉴."""
import shutil
import subprocess
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.schema import Project
from composer.script import ScriptOptions, build_script, cell, fmt_time, script_from_file, script_from_project, \
    summary, to_docx, to_html, to_text, to_txt_file
from composer.subtitles import Cue

ALL = ["index", "start", "end", "duration", "text", "recorded", "clip", "speed", "memo"]


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "takes").mkdir()
    (tmp_path / "takes" / "t1.wav").write_bytes(b"x")
    return Project.from_dict({
        "title": "여행 브이로그",
        "clips": [{"file": "a.png", "duration": 3}, {"file": "b.png", "duration": 5, "label": "바다"}],
        "subtitle": {"offset": 0.5, "items": [
            {"index": 1, "start": 0.2, "end": 2.0, "text": "안녕하세요, 대본 출력 테스트입니다"},
            {"index": 2, "start": 3.0, "end": 3.5, "text": "아주 빠르게 읽어야 하는\n긴 문장입니다"},
            {"index": 3, "start": 5.0, "end": 7.0, "text": "천천히"}]},
        "dubbing": {"takes": [{"file": "takes/t1.wav", "subtitle": 1}, {"file": "takes/없음.wav", "subtitle": 3}]},
    }, base_dir=tmp_path)


def test_time_formats():
    assert fmt_time(83.44) == "01:23.4" and fmt_time(83.46, "short") == "01:23.5"
    assert fmt_time(3723.456, "srt") == "01:02:03,456"
    assert fmt_time(-1) == "00:00.0"


def test_script_model_from_project(proj):
    s = script_from_project(proj)
    assert s.title == "여행 브이로그" and s.source == "프로젝트 자막"
    l1, l2, l3 = s.lines
    assert (l1.start, l1.end) == (0.7, 2.5)  # 전체 오프셋 반영
    assert l1.recorded is True and l2.recorded is False and l3.recorded is False  # 파일 없는 테이크는 미녹음
    assert l1.clip == "1. a.png" and l2.clip == "2. 바다" and l2.clip_index == 1
    assert l2.chars == len("아주빠르게읽어야하는긴문장입니다")
    o = ScriptOptions(columns=ALL, speed_limit=7)
    assert l2.too_fast(7) and not l3.too_fast(7)
    assert cell(l2, "speed", o).endswith("⚠ 빠름") and cell(l3, "speed", o) == "1.5자/초"
    assert cell(l1, "recorded", o) == "✔" and cell(l2, "recorded", o) == ""
    assert "녹음 1/3" in summary(s, o) and "빠른 줄 2개" in summary(s, o)
    # 미세 조정한 내용 반영
    proj.subtitle.items[2].text = "고친 대본"
    assert script_from_project(proj).lines[2].text == "고친 대본"


def test_external_srt(tmp_path, proj):
    srt = tmp_path / "외부.srt"
    srt.write_bytes("1\r\n00:00:01,000 --> 00:00:03,000\r\n외부 자막 첫 줄\r\n".encode("cp949"))
    s = script_from_file(srt, project=proj)
    assert s.title == "외부" and s.source == "외부.srt"
    assert s.lines[0].text == "외부 자막 첫 줄" and s.lines[0].recorded is None and s.lines[0].clip == "1. a.png"
    assert cell(s.lines[0], "recorded", ScriptOptions()) == "-"


def test_text_and_html(proj):
    s = script_from_project(proj)
    o = ScriptOptions(title="제목", columns=ALL, time_format="srt")
    t = to_text(s, o)
    assert t.startswith("여행 브이로그") or "제목" not in t  # 제목은 script.title
    assert "[1]  00:00:00,700 →  00:00:02,500" in t and "    긴 문장입니다" in t and "메모: ____" in t
    h = to_html(s, o, "Noto Sans CJK KR")
    assert "<th" in h and "대본 내용" in h and "▼ 클립 2. 바다 시작" in h and "#ffe1e1" in h
    # 칸 끄기
    o2 = ScriptOptions(columns=["text"])
    h2 = to_html(s, o2)
    assert "시작" not in h2 and "안녕하세요" in h2
    assert "00:00" not in to_text(s, o2)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext 필요")
def test_pdf_has_korean_and_embedded_font(app, proj, tmp_path):
    from composer.ui.script_dialog import korean_font, save_pdf
    s = script_from_project(proj)
    out = save_pdf(s, ScriptOptions(columns=ALL), tmp_path / "대본.pdf")
    txt = subprocess.run(["pdftotext", "-layout", str(out), "-"], capture_output=True, text=True, check=True).stdout
    assert "여행 브이로그" in txt and "안녕하세요, 대본 출력 테스트입니다" in txt and "00:00.7" in txt
    fonts = subprocess.run(["pdffonts", str(out)], capture_output=True, text=True).stdout
    lines = [l for l in fonts.splitlines()[2:] if l.strip()]
    assert lines and all(" yes " in l for l in lines)  # 글꼴 모두 내장
    assert korean_font() in ("Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "Noto Sans KR", "Gothic A1",
                             "Nanum Gothic", "NanumGothic") or korean_font()


def test_docx_and_txt(proj, tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    s = script_from_project(proj)
    o = ScriptOptions(columns=ALL)
    d = to_docx(s, o, tmp_path / "대본.docx")
    doc = Document(str(d))
    assert doc.paragraphs[0].text == "여행 브이로그"
    t = doc.tables[0]
    assert [c.text for c in t.rows[0].cells] == ["번호", "시작", "끝", "길이", "대본 내용", "녹음", "클립", "읽기 속도", "메모"]
    assert t.rows[1].cells[4].text == "안녕하세요, 대본 출력 테스트입니다" and t.rows[1].cells[5].text == "✔"
    assert t.rows[2].cells[4].text == "아주 빠르게 읽어야 하는\n긴 문장입니다"
    run = t.rows[1].cells[4].paragraphs[0].runs[0]
    assert run._element.rPr.find(qn("w:rFonts")).get(qn("w:eastAsia")) == "Malgun Gothic"  # 한글 글꼴 지정
    assert len(t.rows) == 4
    p = to_txt_file(s, o, tmp_path / "대본.txt")
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and "안녕하세요" in raw.decode("utf-8-sig")


# ------------------------------------------------------------------ 창
def test_dialog_preview_options_exports_and_clipboard(app, proj, tmp_path):
    from composer.ui.script_dialog import ScriptDialog
    dlg = ScriptDialog(proj, "여행 브이로그")
    assert dlg.source.currentData() == "project"
    html = dlg.preview.toPlainText()
    assert "여행 브이로그" in html and "안녕하세요, 대본 출력 테스트입니다" in html and "00:00.7" in html
    # 옵션 → 미리보기 바로 반영
    dlg.col_checks["start"].setChecked(False)
    dlg.time_format.setCurrentIndex(dlg.time_format.findData("srt"))
    assert "00:00:02,500" in dlg.preview.toPlainText() and "00:00:00,700" not in dlg.preview.toPlainText()
    dlg.col_checks["speed"].setChecked(True)
    assert "⚠ 빠름" in dlg.preview.toPlainText() and "빠른 줄" in dlg.lbl_summary.text()
    dlg.title.setText("바뀐 제목")
    dlg.refresh()
    assert "바뀐 제목" in dlg.preview.toPlainText()
    # 파일로 저장
    dlg.info = lambda t: None
    saved = {}
    for kind in ("pdf", "docx", "txt"):
        dlg.ask_save_path = lambda k, kind=kind: tmp_path / f"out.{k}"
        saved[kind] = dlg.export(kind)
        assert saved[kind].exists() and saved[kind].stat().st_size > 500 if kind != "txt" else saved[kind].exists()
    assert "바뀐 제목" in saved["txt"].read_text(encoding="utf-8-sig")
    # 클립보드
    dlg.copy_to_clipboard()
    md = QApplication.clipboard().mimeData()
    assert "안녕하세요" in md.text() and "<table" in md.html()
    dlg.deleteLater()


def test_dialog_without_project_subtitles_uses_external_file(app, tmp_path):
    from composer.ui.script_dialog import ScriptDialog
    p = Project(base_dir=tmp_path)
    dlg = ScriptDialog(p)
    assert dlg.source.currentData() == "file" and "없음" in dlg.source.itemText(0)
    assert not dlg.btn_pdf.isEnabled() and "SRT" in dlg.preview.toPlainText()
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n파일 자막\n", encoding="utf-8")
    dlg.ask_open_srt = lambda: srt
    dlg.btn_srt.click()
    assert dlg.btn_pdf.isEnabled() and "파일 자막" in dlg.preview.toPlainText() and dlg.title.text() == "s"
    assert not dlg.col_checks["recorded"].isEnabled()
    dlg.deleteLater()


def test_menu_shortcut_and_toolbar(app, proj):
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    try:
        f = w.menuBar().actions()[0].menu()
        act = next(a for a in f.actions() if a.text() == "📄 대본 출력…")
        assert act.shortcut().toString() == "Ctrl+P"
        tb = [a.text() for a in w.findChildren(type(act)) if a.text() == "📄 대본"]
        assert tb
        w.set_project(proj, None)
        seen = []
        w.exec_script_dialog = lambda dlg: seen.append(dlg.preview.toPlainText())
        act.trigger()
        assert seen and "안녕하세요" in seen[0]
    finally:
        w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()
