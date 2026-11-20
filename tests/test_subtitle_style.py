"""자막 스타일: ASS 생성, 효과 계산, 검증·스키마·AI 프롬프트, UI 탭, 실시간 오버레이, 실제 렌더."""
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from composer.probe import ffmpeg_path
from composer.render.ass import ass_color, ass_time, build_ass, escape_text
from composer.render.ffmpeg_builder import RenderSettings, build_assembly
from composer.schema import JSON_SCHEMA, Project, SubtitleStyle
from composer.subtitle_style import appear_state, typing_time
from composer.subtitles import Cue
from composer.timeline import build_timeline
from composer.validate import validate_project


def _proj(**style):
    return Project.from_dict({"output": {"width": 1280, "height": 720, "fps": 30},
                              "subtitle": {"style": style}})


CUE = Cue(1, 1.0, 3.0, "안녕하세요\n둘째 줄")


def _events(ass):
    return [l for l in ass.splitlines() if l.startswith("Dialogue:")]


# ------------------------------------------------------------------ ASS
def test_ass_basics():
    assert ass_color("#FF8000") == "&H000080FF"
    assert ass_color("#000000", 0.6) == "&H66000000"
    assert ass_time(3661.256) == "1:01:01.26"
    assert escape_text("a{b}\\c\nd") == "a｛b｝＼c\\Nd"
    a = build_ass(_proj(), [CUE, Cue(2, 4, 4, "길이 0 은 건너뜀")])
    assert "PlayResX: 1280" in a and "PlayResY: 720" in a
    text_style = next(l for l in a.splitlines() if l.startswith("Style: Text"))
    f = text_style.split(",")
    assert f[1] == "Malgun Gothic" and f[2] == "48" and f[15] == "1" and f[18] == "2" and f[21] == "60"
    ev = _events(a)
    assert len(ev) == 1 and ev[0] == "Dialogue: 1,0:00:01.00,0:00:03.00,Text,,0,0,0,,안녕하세요\\N둘째 줄"


@pytest.mark.parametrize("pos,align", [("bottom", "2"), ("center", "5"), ("top", "8")])
def test_ass_position(pos, align):
    a = build_ass(_proj(position=pos, margin_v=90), [CUE])
    f = next(l for l in a.splitlines() if l.startswith("Style: Text")).split(",")
    assert f[18] == align and f[21] == "90"


def test_ass_box_layer():
    a = build_ass(_proj(box=True, box_color="#102030", box_opacity=0.25, box_padding=15, outline_color="#FF0000"), [CUE])
    box = next(l for l in a.splitlines() if l.startswith("Style: Box")).split(",")
    assert box[5] == "&HBF302010" and box[15] == "3" and box[16] == "15"  # 상자 색·불투명도, BorderStyle=3, 여백
    assert box[3].startswith("&HFF")  # 상자 층 글자는 투명
    ev = _events(a)
    assert ev[0].startswith("Dialogue: 0,") and ",Box," in ev[0]
    assert ev[1].startswith("Dialogue: 1,") and ",Text," in ev[1]
    # 불투명도 0 이면 상자 없음
    assert len(_events(build_ass(_proj(box=True, box_opacity=0), [CUE]))) == 1


def test_ass_effects():
    fade = _events(build_ass(_proj(appear="fade", appear_duration=0.4), [CUE]))[0]
    assert "{\\fad(400,150)}" in fade
    slide = _events(build_ass(_proj(appear="slide_up", appear_duration=0.5, position="bottom", margin_v=60, size=50), [CUE]))[0]
    assert "\\move(640,690,640,660,0,500)" in slide  # 아래에서 글자 크기*0.6 만큼 올라옴
    top = _events(build_ass(_proj(appear="slide_up", position="top", margin_v=40, size=50), [CUE]))[0]
    assert "\\move(640,70,640,40," in top
    pop = _events(build_ass(_proj(appear="pop", appear_duration=0.4), [CUE]))[0]
    assert "\\fscx40\\fscy40\\t(0,260,\\fscx115\\fscy115)\\t(260,400,\\fscx100\\fscy100)" in pop
    # 효과 시간은 자막 길이의 절반을 넘지 않음
    short = _events(build_ass(_proj(appear="fade", appear_duration=3), [Cue(1, 0, 1, "짧음")]))[0]
    assert "\\fad(500," in short


def test_ass_typing():
    st = SubtitleStyle(appear="typing", appear_duration=0.5)
    ev = _events(build_ass(_proj(appear="typing", appear_duration=0.5), [CUE]))[0]
    ks = [int(x) for x in re.findall(r"\\ko(\d+)", ev)]
    n = len("안녕하세요둘째 줄")
    assert len(ks) == n
    assert sum(ks) == round(typing_time(CUE.text, st, 2.0) * 100)
    assert "\\N" in ev and "SecondaryColour" not in ev
    text_style = next(l for l in build_ass(_proj(appear="typing"), [CUE]).splitlines() if l.startswith("Style: Text"))
    assert text_style.split(",")[4].startswith("&HFF")  # 아직 안 친 글자는 투명


def test_assembly_uses_ass_file():
    p = Project.from_dict({"output": {"width": 640, "height": 360, "fps": 30},
                           "clips": [{"file": "a.png", "duration": 2}]})
    tl = build_timeline(p, lambda c: None)
    plan = build_assembly(p, tl, [Path("c0.mkv")], RenderSettings(fonts_dir=None), Path("o.mp4"), [CUE], "subs.srt",
                          ass_name="subs.ass")
    assert "subtitles=filename=subs.ass[vsub]" in plan.filter_graph and "force_style" not in plan.filter_graph
    # Windows 기본값: 설치된 글꼴 폴더도 찾게 (드라이브 ':' 는 필터 문법에 맞게 '\:')
    fd = build_assembly(p, tl, [Path("c0.mkv")], RenderSettings(fonts_dir="C:\\Windows\\Fonts"), Path("o.mp4"), [CUE],
                        "subs.srt", ass_name="subs.ass")
    assert "subtitles=filename=subs.ass:fontsdir='C\\:/Windows/Fonts'[vsub]" in fd.filter_graph
    soft = build_assembly(p, tl, [Path("c0.mkv")], RenderSettings(burn_subtitles=False), Path("o.mp4"), [CUE],
                          "subs.srt", ass_name="subs.ass")
    assert "subtitles=" not in soft.filter_graph and "mov_text" in soft.args


# ------------------------------------------------------------------ 효과 계산 (미리보기)
def test_appear_state():
    st = SubtitleStyle(appear="fade", appear_duration=0.4)
    assert appear_state(st, "x", 0.0, 2, 2).opacity == 0
    assert appear_state(st, "x", 0.2, 2, 2).opacity == pytest.approx(0.5)
    assert appear_state(st, "x", 1.0, 2, 2).opacity == 1
    assert appear_state(st, "x", 1.9, 0.05, 2).opacity < 0.5  # 사라질 때 살짝 페이드아웃
    ty = SubtitleStyle(appear="typing", appear_duration=0.5)
    assert appear_state(ty, "가나다라마바사아자차", 0.0, 3, 3).chars == 1
    assert appear_state(ty, "가나다라마바사아자차", 0.25, 3, 3).chars == 6
    assert appear_state(ty, "가나다라마바사아자차", 2.0, 1, 3).chars == 10
    po = SubtitleStyle(appear="pop", appear_duration=0.4)
    scales = [appear_state(po, "x", t, 2, 2).scale for t in (0.0, 0.26, 0.4, 1.0)]
    assert scales[0] == pytest.approx(0.4) and scales[1] == pytest.approx(1.15, abs=0.01) and scales[3] == 1
    sl = SubtitleStyle(appear="slide_up", appear_duration=0.4)
    rises = [appear_state(sl, "x", t, 2, 2).rise for t in (0.0, 0.1, 0.4)]
    assert rises[0] == pytest.approx(0.6) and rises[0] > rises[1] > rises[2] == 0
    assert appear_state(SubtitleStyle(), "x", 0, 1, 1) == appear_state(SubtitleStyle(), "x", 5, 1, 1)


# ------------------------------------------------------------------ 검증 / 스키마 / 프롬프트
def test_validation_of_style():
    good = {"clips": [], "subtitle": {"style": {"font": "Nanum Gothic", "position": "top", "appear": "typing",
                                                "box": True, "box_color": "#112233", "box_opacity": 0.5,
                                                "appear_duration": 0.4, "box_padding": 10}}}
    assert not [i for i in validate_project(good, "/x") if i.path.startswith("subtitle")]
    bad = {"clips": [], "subtitle": {"style": {"position": "left", "appear": "spin", "color": "red",
                                               "box_opacity": 2, "box": "yes", "font": ""}}}
    iss = {i.path: i.message for i in validate_project(bad, "/x") if i.severity == "error"}
    assert "가능한 값" in iss["subtitle.style.position"] and "top" in iss["subtitle.style.position"]
    assert "typing" in iss["subtitle.style.appear"]
    assert "#RRGGBB" in iss["subtitle.style.color"]
    assert "subtitle.style.box_opacity" in iss and "subtitle.style.box" in iss and "subtitle.style.font" in iss
    props = JSON_SCHEMA["properties"]["subtitle"]["properties"]["style"]["properties"]
    assert props["appear"]["enum"] == ["none", "fade", "typing", "slide_up", "pop"]
    assert props["position"]["enum"] == ["bottom", "center", "top"]
    shipped = json.loads((Path(__file__).parent.parent / "schema" / "project.schema.json").read_text(encoding="utf-8"))
    assert shipped == json.loads(json.dumps(JSON_SCHEMA))


def test_prompt_mentions_style():
    from composer.prompt_template import EXAMPLE, build_prompt
    t = build_prompt([])
    for word in ("subtitle.style", '"typing"(타이핑)', '"slide_up"(아래에서 올라오기)', '"pop"(톡 튀어나오기)',
                 '"center"(가운데)', "box_opacity", "Malgun Gothic"):
        assert word in t
    assert not [i for i in validate_project(EXAMPLE, "/x") if "style" in i.path]


def test_style_roundtrip(tmp_path):
    from composer.schema import load_project, save_project
    p = _proj(position="center", appear="pop", box=True, box_color="#ABCDEF", font="Gothic A1")
    save_project(p, tmp_path / "p.json")
    q = load_project(tmp_path / "p.json")
    assert q.subtitle.style == p.subtitle.style


# ------------------------------------------------------------------ UI
@pytest.fixture
def win(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    p = Project.from_dict({"output": {"width": 640, "height": 360, "fps": 30},
                           "clips": [{"file": "a.png", "duration": 5}],
                           "subtitle": {"items": [{"index": 1, "start": 1, "end": 3, "text": "안녕하세요"}]}},
                          base_dir=tmp_path)
    w.set_project(p, None)
    yield w
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def test_style_tab_always_present_and_menu(win):
    from PySide6.QtWidgets import QFontComboBox
    w = win
    tabs = [w.right.tabText(i) for i in range(w.right.count())]
    assert "🅰 자막" in tabs
    sp = w.style_panel
    assert isinstance(sp.font_combo, QFontComboBox)
    assert w.sub_scroll.widget().isAncestorOf(sp) and w.sub_scroll.widget().isAncestorOf(w.inspector.sub_file_panel)
    # '직접 바꾸기' 한 곳에 글꼴·크기·위치·색·상자·나타나기가 한 줄에 2~3개씩 (접지 않음)
    assert not hasattr(sp, "more")
    for wdg in (sp.font_combo, sp.size, sp.position, sp.color, sp.box, sp.appear):
        assert sp.g_edit.isAncestorOf(wdg)
    # 프리셋 이름 바꾸기·삭제 등은 '내 모양 관리 ▾' 메뉴
    assert sp.btn_more_preset.menu() is sp.preset_menu and len(sp.preset_actions) >= 2
    w.show_style_tab()
    assert w.right.currentWidget() is w.sub_scroll
    w.right.setCurrentIndex(0)
    w.model.select("subtitle", 1)
    w.inspector.sub_page.btn_style.click()  # 자막 속성의 버튼도 이 탭으로
    assert w.right.currentWidget() is w.sub_scroll


def test_style_edits_write_json_update_overlay_and_skip_rerender(win):
    from PySide6.QtGui import QFont
    w = win
    sp = w.style_panel
    w.auto.enabled = True
    key = w._render_revision_key()
    w.auto.rendered_key = key  # 미리보기가 최신인 상태에서 시작
    # 글꼴
    fam = QFont("DejaVu Sans").family()
    sp.korean_only.setChecked(False)
    sp.font_combo.setCurrentFont(QFont(fam))
    assert w.model.project.subtitle.style.font == sp.font_combo.currentFont().family()
    assert sp.font_preview.font().family() == sp.font_combo.currentFont().family()
    # 색 (색 고르기 창 대신 가짜)
    sp.pick_color = lambda cur, title: "#ff0000" if title == "글자 색" else "#00ff00"
    sp.color.click()
    st = w.model.project.subtitle.style
    assert st.color == "#FF0000" and sp.color.text() == "#FF0000"
    # 상자, 위치, 효과
    assert not sp.box_color.isEnabled()
    sp.box.setChecked(True)
    assert st.box is True and sp.box_color.isEnabled()
    sp.box_color.click()
    assert st.box_color == "#00FF00"
    sp.position.setCurrentIndex(sp.position.findData("center"))
    assert st.position == "center" and not sp.margin.isEnabled()
    sp.position.setCurrentIndex(sp.position.findData("top"))
    sp.margin.setValue(33)
    sp.appear.setCurrentIndex(sp.appear.findData("typing"))
    sp.appear_duration.setValue(0.8)
    assert (st.position, st.margin_v, st.appear, st.appear_duration) == ("top", 33, "typing", 0.8)
    assert [sp.appear.itemText(i) for i in range(sp.appear.count())] == \
        ["그냥", "페이드", "타이핑", "아래에서 올라오기", "톡 튀어나오기"]
    assert [sp.position.itemText(i) for i in range(sp.position.count())] == ["아래", "가운데", "위"]
    # 미리보기 오버레이에 바로 반영, 미리보기 다시 렌더 안 함
    assert w.player.view.style.appear == "typing" and w.player.view.style.position == "top"
    assert w._render_revision_key() == key and not w.auto.busy
    # JSON 에 기록
    d = json.loads(w.model.project.to_json())["subtitle"]["style"]
    assert d["appear"] == "typing" and d["box"] is True and d["position"] == "top"
    # 실행 취소 / 기본값
    w.model.undo()
    assert w.model.project.subtitle.style.appear_duration != 0.8
    sp.btn_reset.click()
    assert w.model.project.subtitle.style == SubtitleStyle()
    assert sp.appear.currentData() == "none"


def test_soft_subtitle_note(win):
    w = win
    w.model.edit(lambda p: setattr(p.subtitle, "burn_in", False))
    assert "소프트 자막" in w.style_panel.note.text()


def _grab_view(w, style, elapsed=5.0, text="가나다라마바사"):
    from PySide6.QtCore import QCoreApplication
    w.resize(1200, 800); w.show()
    for _ in range(5):
        QCoreApplication.processEvents()
    v = w.player.view
    w.player.set_subtitle_style(style, 360)
    v.text, v.cue_elapsed, v.cue_remaining, v.cue_duration = text, elapsed, 2.0, 3.0
    img = v.viewport().grab().toImage()
    # .copy(): QImage 가 함수 끝에서 사라져도 배열이 지워진 메모리를 가리키지 않게 (예전엔 access violation)
    arr = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :img.width()].copy()
    b, g, r = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
    fr = v.mapFromScene(v.sceneRect()).boundingRect()
    arr = arr[max(0, fr.top()):fr.bottom(), max(0, fr.left()):fr.right()]  # 영상이 보이는 영역만
    b, g, r = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
    return (r > 200) & (g > 200) & (b < 80), arr  # 노란 글자


def test_overlay_follows_position_and_effects(win):
    w = win
    ys = {}
    for pos in ("top", "center", "bottom"):
        mask, arr = _grab_view(w, SubtitleStyle(color="#FFFF00", outline=0, position=pos))
        assert mask.sum() > 50
        ys[pos] = np.nonzero(mask)[0].mean() / arr.shape[0]
    assert ys["top"] < 0.3 and 0.4 < ys["center"] < 0.6 and ys["bottom"] > 0.7
    # 타이핑: 처음엔 일부 글자만
    early, _ = _grab_view(w, SubtitleStyle(color="#FFFF00", outline=0, appear="typing", appear_duration=1.0), 0.2)
    late, _ = _grab_view(w, SubtitleStyle(color="#FFFF00", outline=0, appear="typing", appear_duration=1.0), 2.0)
    assert 0 < early.sum() < late.sum() * 0.6
    # 페이드: 시작 직후엔 거의 안 보임
    fade0, _ = _grab_view(w, SubtitleStyle(color="#FFFF00", outline=0, appear="fade", appear_duration=0.5), 0.0)
    assert fade0.sum() == 0
    # 배경 상자
    mask, arr = _grab_view(w, SubtitleStyle(color="#FFFF00", outline=0, box=True, box_color="#0000FF", box_opacity=1.0))
    blue = (arr[..., 0] > 200) & (arr[..., 2] < 60)
    assert blue.sum() > mask.sum()
    # 견본 위젯도 오류 없이 그려짐
    w.style_panel.sample.resize(400, 225)
    w.style_panel.sample.grab()


# ------------------------------------------------------------------ 실제 렌더
def test_burn_in_render_with_style(tmp_path):
    from composer.render.runner import render_project
    ff = ffmpeg_path()
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=640x360", "-frames:v", "1",
                    str(tmp_path / "bg.png")], check=True)
    (tmp_path / "s.srt").write_text("1\n00:00:00,500 --> 00:00:02,500\n가나다라마바사아자차카타파하\n",
                                    encoding="utf-8")
    p = Project.from_dict({
        "output": {"width": 640, "height": 360, "fps": 25},
        "clips": [{"file": "bg.png", "duration": 3}],
        "subtitle": {"file": "s.srt", "style": {"font": "Gothic A1", "size": 36, "color": "#FFFF00", "outline": 0,
                                                "position": "top", "margin_v": 30, "box": True,
                                                "box_color": "#0000FF", "box_opacity": 1.0,
                                                "appear": "typing", "appear_duration": 1.0}},
    }, base_dir=tmp_path)
    res = render_project(p, tmp_path / "out.mp4", RenderSettings())
    work_ass = list(tmp_path.rglob("subs.ass"))
    assert work_ass and "\\ko" in work_ass[0].read_text(encoding="utf-8")

    def frame(t):
        raw = subprocess.run([ff, "-v", "error", "-ss", str(t), "-i", str(res.output), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(360, 640, 3).astype(int)

    def yellow(a):
        return (a[..., 0] > 180) & (a[..., 1] > 180) & (a[..., 2] < 90)

    def blue(a):
        return (a[..., 2] > 180) & (a[..., 0] < 70) & (a[..., 1] < 70)

    f0, f_early, f_late = frame(0.2), frame(0.8), frame(2.0)
    assert yellow(f0).sum() == 0 and blue(f0).sum() == 0  # 자막 전
    ys = np.nonzero(yellow(f_late))[0]
    assert ys.size > 100 and ys.mean() < 360 * 0.3  # 위쪽
    assert blue(f_late).sum() > 1000  # 배경 상자
    assert 0 < yellow(f_early).sum() < yellow(f_late).sum() * 0.7  # 타이핑 중


def test_windows_fonts_dir_escaped():
    p = Project.from_dict({"clips": [{"file": "a.png", "duration": 2}]})
    tl = build_timeline(p, lambda c: None)
    g = build_assembly(p, tl, [Path("c.mkv")], RenderSettings(fonts_dir=r"C:\Windows\Fonts"), Path("o.mp4"),
                       [CUE], "subs.srt", ass_name="subs.ass").filter_graph
    assert "subtitles=filename=subs.ass:fontsdir='C\\:/Windows/Fonts'[vsub]" in g
