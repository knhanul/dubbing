"""자막 한 줄만 다른 모양(subtitle.items[].style): 기본은 모두 같은 모양, 고른 자막만 바꾼 칸을 덮어씀.

데이터·JSON 왕복 · 실제 모양 계산 · ASS(줄마다 스타일) · 나누기 · 검증/스키마 · 🅰 탭 '고른 자막만' · 🛠 선택 탭 ·
미리보기 오버레이 · 실제 렌더."""
import json
import os
import subprocess

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.render.ass import build_ass  # noqa: E402
from composer.schema import JSON_SCHEMA, Project, SubtitleStyle  # noqa: E402
from composer.subtitle_style import (clean_override, describe_override, effective_style,  # noqa: E402
                                     override_from_style, set_override)
from composer.subtitles import project_cues  # noqa: E402
from composer.validate import validate_project  # noqa: E402


def _data(**extra):
    d = {
        "output": {"width": 640, "height": 360, "fps": 25},
        "subtitle": {"style": {"color": "#FFFFFF", "size": 40, "position": "bottom"},
                     "items": [{"index": 1, "start": 0.5, "end": 1.5, "text": "하나"},
                               {"index": 2, "start": 2.0, "end": 3.0, "text": "둘",
                                "style": {"color": "#FFD400", "size": 64, "position": "top"}},
                               {"index": 3, "start": 4.0, "end": 5.0, "text": "셋"}]},
    }
    d.update(extra)
    return d


# ------------------------------------------------------------------ 모양 계산
def test_effective_and_clean_override():
    base = SubtitleStyle(color="#FFFFFF", size=40)
    assert effective_style(base, None) is base
    st = effective_style(base, {"color": "#FF0000", "nope": 1})
    assert st.color == "#FF0000" and st.size == 40 and base.color == "#FFFFFF"
    # 전체와 같은 칸·모르는 칸은 빠지고, 남는 게 없으면 None
    assert clean_override(base, {"color": "#FFFFFF", "size": 50, "x": 1}) == {"size": 50}
    assert clean_override(base, {"color": "#FFFFFF"}) is None
    assert set_override(base, {"size": 50}, "size", 40) is None  # 전체와 같아지면 따르게
    assert set_override(base, None, "appear", "pop") == {"appear": "pop"}
    assert override_from_style(base, SubtitleStyle(color="#FFFFFF", size=40, bold=False)) == {"bold": False}
    assert describe_override({"size": 1, "color": "#000000"}) == "크기, 글자 색"


def test_json_roundtrip_and_old_files_unchanged():
    p = Project.from_dict(_data())
    items = p.subtitle.items
    assert items[0].style is None and items[1].style == {"color": "#FFD400", "size": 64, "position": "top"}
    out = json.loads(p.to_json())["subtitle"]["items"]
    assert "style" not in out[0] and "style" not in out[2]  # 전체 모양을 따르는 자막은 예전 모양 그대로
    assert out[1]["style"]["size"] == 64
    assert Project.from_dict(json.loads(p.to_json())).subtitle.items[1].style == items[1].style
    # 모르는 칸은 버리고, 객체가 아니면 None
    q = Project.from_dict({"subtitle": {"items": [{"index": 1, "start": 0, "end": 1, "text": "a",
                                                   "style": {"color": "#000000", "zzz": 1}},
                                                  {"index": 2, "start": 1, "end": 2, "text": "b", "style": "x"}]}})
    assert q.subtitle.items[0].style == {"color": "#000000"} and q.subtitle.items[1].style is None
    # 자막 목록(Cue)에도 실려 감 (전역 앞당기기 적용 뒤에도)
    p.subtitle.offset = 0.25
    cues = project_cues(p)
    assert cues[1].style["color"] == "#FFD400" and cues[1].start == pytest.approx(2.25) and cues[0].style is None


# ------------------------------------------------------------------ ASS
def test_ass_gives_each_look_its_own_style():
    p = Project.from_dict(_data())
    p.subtitle.items[2].style = {"color": "#FFD400", "size": 64, "position": "top"}  # 2번과 같은 모양
    a = build_ass(p, project_cues(p))
    styles = [l for l in a.splitlines() if l.startswith("Style:")]
    assert [s.split(",")[0] for s in styles] == ["Style: Text", "Style: Box", "Style: Text2", "Style: Box2"]
    t2 = styles[2].split(",")
    assert t2[2] == "64" and t2[3] == "&H0000D4FF" and t2[18] == "8"  # 크기 · 색(BGR) · 위(8)
    ev = [l for l in a.splitlines() if l.startswith("Dialogue:")]
    assert [e.split(",")[3] for e in ev] == ["Text", "Text2", "Text2"]  # 같은 모양은 스타일 하나를 같이 씀


def test_ass_per_line_box_and_effect():
    p = Project.from_dict(_data())
    p.subtitle.items[1].style = {"box": True, "box_color": "#0000FF", "box_opacity": 1.0, "appear": "pop"}
    p.subtitle.items[2].style = {"appear": "typing", "appear_duration": 0.5}
    ev = [l for l in build_ass(p, project_cues(p)).splitlines() if l.startswith("Dialogue:")]
    assert len(ev) == 4  # 2번만 배경 상자 층이 하나 더
    assert ev[0].split(",")[3] == "Text" and "\\" not in ev[0]  # 1번: 효과 없음
    assert ev[1].startswith("Dialogue: 0,") and ",Box2," in ev[1]
    assert ",Text2," in ev[2] and "\\fscx40" in ev[2]  # 톡 튀어나오기
    assert ",Text3," in ev[3] and "\\ko" in ev[3]  # 타이핑


# ------------------------------------------------------------------ 편집
def test_split_keeps_look_on_both_halves():
    from composer.edit_ops import split_cue
    p = Project.from_dict(_data())
    p.subtitle.items[1].text = "둘 더하기 셋"
    assert split_cue(p, 2, 2.5)
    a, b = [i for i in p.subtitle.items if i.start >= 2.0 and i.end <= 3.0]
    assert a.style == b.style == {"color": "#FFD400", "size": 64, "position": "top"}
    a.style["size"] = 10
    assert b.style["size"] == 64  # 따로 복사됨


# ------------------------------------------------------------------ 검증 · 스키마
def test_validation_and_schema():
    assert not [i for i in validate_project(_data(), "/x") if i.path.startswith("subtitle")]
    bad = _data()
    bad["subtitle"]["items"][0]["style"] = {"color": "red", "position": "left", "size": 2, "what": 1}
    bad["subtitle"]["items"][2]["style"] = "yellow"
    iss = {i.path: i.message for i in validate_project(bad, "/x")}
    for k in ("color", "position", "size", "what"):
        assert f"subtitle.items[0].style.{k}" in iss
    assert "subtitle.items[2].style" in iss
    item = JSON_SCHEMA["properties"]["subtitle"]["properties"]["items"]["items"]["properties"]["style"]
    assert item["properties"] == JSON_SCHEMA["properties"]["subtitle"]["properties"]["style"]["properties"]
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(Project.from_dict(_data(clips=[{"file": "a.png"}])).to_dict(), JSON_SCHEMA)


# ------------------------------------------------------------------ UI
@pytest.fixture
def win(tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.confirm = lambda title, text: True
    w.notify = lambda text, warning=False: None
    w.show()
    w.set_project(Project.from_dict(_data(), base_dir=tmp_path), None)
    app.processEvents()
    yield w
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _items(w):
    return {i.index: i for i in w.model.project.subtitle.items}


def test_style_tab_scope_edits_only_selected(win):
    w, sp = win, win.style_panel
    assert sp.scope == "all" and not sp.btn_scope_sel.isEnabled()
    # 아무것도 안 골랐으면 모든 자막(기본 모양)을 바꿈 — 2번이 바꾼 칸은 그대로
    sp.size.setValue(44)
    assert w.model.project.subtitle.style.size == 44 and _items(w)[2].style["size"] == 64
    # 1·3번을 골라 '고른 자막만'
    w.model.select_many("subtitle", [1, 3])
    assert sp.btn_scope_sel.isEnabled() and "2개" in sp.btn_scope_sel.text()
    sp.btn_scope_sel.click()
    assert sp.scope == "selected" and sp.btn_scope_sel.isChecked() and sp.btn_reset.text() == "전체 모양으로"
    sp.appear.setCurrentIndex(sp.appear.findData("fade"))
    sp.size.setValue(50)
    sp.size.setValue(52)  # 연달아 바꾼 크기는 되돌리기 한 번
    it = _items(w)
    assert it[1].style == {"appear": "fade", "size": 52} == it[3].style
    assert it[2].style == {"color": "#FFD400", "size": 64, "position": "top"}
    assert w.model.project.subtitle.style.size == 44 and w.model.project.subtitle.style.appear == "none"
    assert "크기" in sp.scope_info.text() and "나타나기" in sp.scope_info.text()
    w.model.undo()
    assert _items(w)[1].style == {"appear": "fade"}
    # 전체와 같은 값으로 돌리면 그 칸은 빠짐(다시 전체를 따름)
    sp.appear.setCurrentIndex(sp.appear.findData("none"))
    assert _items(w)[1].style is None and _items(w)[3].style is None
    # 스타일 모음 → 고른 자막에만
    sp.apply_preset("쇼츠 강조")
    assert _items(w)[1].style and _items(w)[1].style == _items(w)[3].style
    assert w.model.project.subtitle.style.size == 44
    # '전체 모양으로'
    sp.btn_reset.click()
    assert _items(w)[1].style is None and _items(w)[3].style is None
    # 자막이 아닌 것을 고르면 모든 자막으로 돌아감
    w.model.select("")
    assert sp.scope == "all" and not sp.btn_scope_sel.isEnabled()


def test_style_tab_shows_selected_look(win):
    w, sp = win, win.style_panel
    w.model.select("subtitle", 2)
    sp.set_scope("selected")
    assert sp.size.value() == 64 and sp.position.currentData() == "top"
    assert sp.sample.text == "둘"
    assert "글자 색" in sp.scope_info.text() and not sp.btn_clear_item.isHidden()
    sp.set_scope("all")
    assert sp.size.value() == 40 and sp.position.currentData() == "bottom"


def test_inspector_subtitle_page_look_section(win):
    w = win
    pg = w.inspector.sub_page
    w.model.select("subtitle", 2)
    assert "크기" in pg.look_info.text() and pg.btn_item_reset.isEnabled()
    pg.btn_item_style.click()  # 🅰 탭을 '고른 자막만'으로
    assert w.right.currentWidget() is w.sub_scroll and w.style_panel.scope == "selected"
    pg.btn_style.click()  # 모든 자막 모양
    assert w.style_panel.scope == "all"
    pg.btn_item_reset.click()
    assert _items(w)[2].style is None and "같은 모양" in pg.look_info.text() and not pg.btn_item_reset.isEnabled()
    assert w.model.undo() and _items(w)[2].style["size"] == 64
    # 타임라인 블록에 ✨ 표시는 그림 그리기에서 — 오류 없이 그려지는지
    w.timeline.canvas.grab()


def test_preview_overlay_uses_line_look(win):
    w = win
    v = w.player.view
    assert w._cue_at(1.0)[4] is None
    info = w._cue_at(2.5)
    assert info[4]["size"] == 64
    w.player._tick(force=True)
    v.cue_style = info[4]
    assert v.look().size == 64 and v.look().position == "top" and v.look().color == "#FFD400"
    v.cue_style = None
    assert v.look() is v.style


def test_real_render_uses_each_line_look(tmp_path):
    from composer.probe import ffmpeg_path
    from composer.render.ffmpeg_builder import RenderSettings
    from composer.render.runner import render_project
    ff = ffmpeg_path()
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=640x360", "-frames:v", "1",
                    str(tmp_path / "bg.png")], check=True)
    d = _data(clips=[{"file": "bg.png", "duration": 3}])
    d["subtitle"]["style"] = {"font": "Gothic A1", "size": 40, "color": "#FFFF00", "outline": 0, "position": "bottom",
                              "margin_v": 30}
    d["subtitle"]["items"] = [{"index": 1, "start": 0.2, "end": 1.2, "text": "가나다라마바"},
                              {"index": 2, "start": 1.6, "end": 2.8, "text": "가나다라마바",
                               "style": {"color": "#00FFFF", "position": "top"}}]
    p = Project.from_dict(d, base_dir=tmp_path)
    res = render_project(p, tmp_path / "out.mp4", RenderSettings())

    def frame(t):
        raw = subprocess.run([ff, "-v", "error", "-ss", str(t), "-i", str(res.output), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(360, 640, 3).astype(int)

    def yellow(a):
        return (a[..., 0] > 180) & (a[..., 1] > 180) & (a[..., 2] < 90)

    def cyan(a):
        return (a[..., 0] < 90) & (a[..., 1] > 180) & (a[..., 2] > 180)

    f1, f2 = frame(0.8), frame(2.2)
    y1 = np.nonzero(yellow(f1))[0]
    assert y1.size > 100 and y1.mean() > 360 * 0.7 and cyan(f1).sum() == 0  # 1번: 전체 모양(노랑, 아래)
    y2 = np.nonzero(cyan(f2))[0]
    assert y2.size > 100 and y2.mean() < 360 * 0.3 and yellow(f2).sum() == 0  # 2번만: 하늘색, 위
