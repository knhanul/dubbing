"""자막 스타일 모음: 기본 스타일, 크기 맞춤, 글꼴 대체, 내 스타일 저장/이름 바꾸기/삭제/가져오기/내보내기, 탭 UI."""
import json
from dataclasses import asdict

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.schema import Project, SubtitleStyle
from composer.subtitle_presets import BUILTIN, DEFAULT_FONT, PresetError, PresetStore, parse_presets, \
    preset_from_style, presets_path, resolve_font, style_from_preset
from composer.validate import validate_project

NAMES = ["유튜브 기본", "쇼츠 강조", "뉴스 자막", "영화 자막", "감성 시", "깔끔 박스"]


def B(name):
    return next(p for p in BUILTIN if p.name == name)


def test_builtin_presets_valid_and_as_described():
    assert [p.name for p in BUILTIN] == NAMES
    for p in BUILTIN:
        st = style_from_preset(p, 1920, 1080)
        errs = [i for i in validate_project({"clips": [], "subtitle": {"style": asdict(st)}}, ".") if i.severity == "error" and i.path.startswith("subtitle")]
        assert not errs, (p.name, errs)
    yt = style_from_preset(B("유튜브 기본"), 1920, 1080)
    assert (yt.color, yt.outline_color, yt.position, yt.box) == ("#FFFFFF", "#000000", "bottom", False) and yt.outline >= 2
    sh = style_from_preset(B("쇼츠 강조"), 1080, 1920)
    assert sh.color == "#FFE14D" and sh.bold and sh.outline >= 5 and sh.appear == "pop" and sh.size >= 70
    assert sh.position == "bottom" and sh.margin_v == round(1920 * 0.24)  # 화면 아래쪽 가운데
    nw = style_from_preset(B("뉴스 자막"), 1920, 1080)
    assert nw.box and 0.5 < nw.box_opacity < 1 and nw.color == "#FFFFFF" and nw.position == "bottom"
    mv = style_from_preset(B("영화 자막"), 1920, 1080)
    assert mv.size < yt.size and mv.outline < yt.outline and mv.appear == "fade"
    po = style_from_preset(B("감성 시"), 1920, 1080, ["Batang", "Arial"])
    assert po.position == "center" and po.appear == "typing" and po.font == "Batang"
    bx = style_from_preset(B("깔끔 박스"), 1920, 1080)
    assert bx.box and bx.box_color == "#FFFFFF" and bx.box_opacity == 1.0 and bx.color == "#111111"


def test_scaling_to_output_size():
    p = B("유튜브 기본")
    full = style_from_preset(p, 1920, 1080)
    small = style_from_preset(p, 1280, 720)
    vert = style_from_preset(p, 1080, 1920)
    assert small.size == round(full.size * 720 / 1080) and small.margin_v == round(60 * 720 / 1080)
    assert vert.size == full.size  # 짧은 변 기준


def test_font_fallback():
    assert resolve_font(["NanumMyeongjo", "Batang"], ["batang", "Arial"]) == "batang"
    assert resolve_font(["없는글꼴"], ["Arial"]) == DEFAULT_FONT
    st = style_from_preset(B("감성 시"), 1920, 1080, ["Arial"])
    assert st.font == DEFAULT_FONT


def test_store_save_rename_delete_and_file(tmp_path):
    path = presets_path()
    assert "localappdata" in str(path)  # 테스트에서는 임시 폴더
    assert path.name == "subtitle_presets.json" and path.parent.name == "VideoComposer"
    s = PresetStore()
    st = SubtitleStyle(size=30, color="#FF0000")
    s.add(preset_from_style("내 빨강", st, 1280, 720))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["presets"][0]["name"] == "내 빨강" and data["presets"][0]["ref"] == 720
    # 같은 이름 → (2)
    assert s.add(preset_from_style("내 빨강", st, 1280, 720)).name == "내 빨강 (2)"
    s.rename("내 빨강 (2)", "두번째")
    with pytest.raises(PresetError):
        s.rename("두번째", "내 빨강")
    with pytest.raises(PresetError):
        s.rename("유튜브 기본", "x")
    with pytest.raises(PresetError):
        s.delete("뉴스 자막")
    s.delete("두번째")
    s2 = PresetStore()
    assert [p.name for p in s2.user] == ["내 빨강"]
    # 다시 불러와 적용하면 1080p 에서는 비율대로 커짐
    assert style_from_preset(s2.user[0], 1920, 1080).size == 45 and style_from_preset(s2.user[0], 1280, 720) == st


def test_import_export_and_bad_files(tmp_path):
    s = PresetStore()
    s.add(preset_from_style("A", SubtitleStyle(color="#00FF00"), 1920, 1080))
    out = s.export(tmp_path / "x.json")
    s.delete("A")
    added = s.import_file(out)
    assert [p.name for p in added] == ["A"] and s.find("A").style["color"] == "#00FF00"
    # 스타일 dict 하나만 있는 파일도
    (tmp_path / "one.json").write_text(json.dumps({"color": "#123456", "size": 40}), encoding="utf-8")
    assert s.import_file(tmp_path / "one.json")[0].name == "가져온 스타일"
    (tmp_path / "bad.json").write_text(json.dumps({"name": "x", "style": {"color": "빨강"}}), encoding="utf-8")
    with pytest.raises(PresetError, match="올바르지"):
        s.import_file(tmp_path / "bad.json")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(PresetError):
        s.import_file(tmp_path / "broken.json")
    with pytest.raises(PresetError):
        parse_presets(3)
    # 깨진 저장 파일이어도 프로그램은 뜸
    presets_path().write_text("{oops", encoding="utf-8")
    s3 = PresetStore()
    assert s3.user == [] and s3.load_error


# ------------------------------------------------------------------ 탭 UI
@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    p = Project.from_dict({"output": {"width": 1080, "height": 1920}, "clips": [],
                           "subtitle": {"items": [{"index": 1, "start": 0, "end": 2, "text": "안녕"}]}}, base_dir=".")
    w.set_project(p, None)
    yield w
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()



def test_panel_grid_apply_one_undo_and_json(win):
    from composer.ui.subtitle_style_panel import NAME_ROLE, SubtitleStylePanel
    panel = win.findChild(SubtitleStylePanel)
    assert panel is not None
    lst = panel.presets
    assert [lst.item(i).data(NAME_ROLE) for i in range(lst.count())] == NAMES
    assert not lst.item(0).icon().isNull()
    # 스타일 모음이 탭 맨 위
    assert panel.layout().itemAt(0).widget().title().startswith("스타일 모음")
    before = asdict(win.model.project.subtitle.style)
    item = panel._preset_item("쇼츠 강조")
    lst.itemClicked.emit(item)
    st = win.model.project.subtitle.style
    assert st.color == "#FFE14D" and st.appear == "pop" and st.margin_v == round(1920 * 0.24)
    assert json.loads(win.model.project.to_json())["subtitle"]["style"]["color"] == "#FFE14D"
    assert panel.size.value() == st.size and panel.appear.currentData() == "pop"  # 아래 칸들도 갱신
    assert win.player.view.style is st or win.player.view.style == st
    assert lst.currentItem().data(NAME_ROLE) == "쇼츠 강조" and lst.currentItem().isSelected()
    win.model.undo()
    assert asdict(win.model.project.subtitle.style) == before  # 한 번에 되돌림
    # 직접 바꾸면 어느 스타일과도 같지 않아 선택 해제
    lst.itemClicked.emit(panel._preset_item("뉴스 자막"))
    panel.size.setValue(panel.size.value() + 3)
    assert not lst.selectedItems()


def test_panel_user_presets_save_rename_delete_import_export(win, tmp_path):
    from composer.ui.subtitle_style_panel import NAME_ROLE, SubtitleStylePanel
    panel = win.findChild(SubtitleStylePanel)
    msgs = []
    panel.notify = msgs.append
    panel.confirm = lambda t: True
    assert not panel.btn_export_preset.isEnabled()
    panel.presets.setCurrentItem(panel._preset_item("유튜브 기본"))
    assert not panel.btn_rename_preset.isEnabled() and not panel.btn_delete_preset.isEnabled()
    win.model.edit(lambda p: setattr(p.subtitle.style, "color", "#ABCDEF"))
    panel.ask_text = lambda *a: "내 하늘색"
    panel.btn_save_preset.click()
    names = [panel.presets.item(i).data(NAME_ROLE) for i in range(panel.presets.count())]
    assert names[-1] == "내 하늘색" and panel.presets.item(len(names) - 1).text() == "★ 내 하늘색"
    assert panel.presets.currentItem().data(NAME_ROLE) == "내 하늘색"
    assert panel.btn_rename_preset.isEnabled() and panel.btn_delete_preset.isEnabled()
    assert "내 하늘색" in presets_path().read_text(encoding="utf-8")
    # 같은 이름으로 다시 저장 → 덮어쓰기 확인
    win.model.edit(lambda p: setattr(p.subtitle.style, "size", 33))
    panel.btn_save_preset.click()
    assert len(panel.store.user) == 1 and panel.store.user[0].style["size"] == 33
    # 기본 스타일 이름으로 저장하면 (2)
    panel.ask_text = lambda *a: "뉴스 자막"
    panel.btn_save_preset.click()
    assert panel.store.user[-1].name == "뉴스 자막 (2)"
    panel.presets.setCurrentItem(panel._preset_item("뉴스 자막 (2)"))
    panel.btn_delete_preset.click()
    assert [p.name for p in panel.store.user] == ["내 하늘색"]
    # 이름 바꾸기
    panel.presets.setCurrentItem(panel._preset_item("내 하늘색"))
    panel.ask_text = lambda *a: "하늘"
    panel.btn_rename_preset.click()
    assert panel._preset_item("하늘") is not None and panel._preset_item("내 하늘색") is None
    # 다른 스타일 적용 후 내 스타일 적용
    panel.presets.itemClicked.emit(panel._preset_item("영화 자막"))
    panel.presets.itemClicked.emit(panel._preset_item("하늘"))
    assert win.model.project.subtitle.style.color == "#ABCDEF" and win.model.project.subtitle.style.size == 33
    # 내보내기 / 가져오기
    panel.ask_save_file = lambda d: tmp_path / "out.json"
    panel.btn_export_preset.click()
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["presets"][0]["name"] == "하늘"
    panel.ask_open_file = lambda: tmp_path / "out.json"
    panel.btn_import_preset.click()
    assert panel._preset_item("하늘 (2)") is not None and "1개를 가져왔습니다" in msgs[-1]
    (tmp_path / "bad.json").write_text("[1]", encoding="utf-8")
    panel.ask_open_file = lambda: tmp_path / "bad.json"
    panel.btn_import_preset.click()
    assert msgs[-1].startswith("가져오지 못했습니다")


def test_missing_font_tooltip(win, monkeypatch):
    from composer.ui.subtitle_style_panel import SubtitleStylePanel
    panel = win.findChild(SubtitleStylePanel)
    monkeypatch.setattr(panel, "_fonts", lambda: ["Arial"])
    panel._preset_sig = None
    panel.fill_presets()
    it = panel._preset_item("감성 시")
    assert "대신합니다" in it.toolTip()
    panel.presets.itemClicked.emit(it)
    assert win.model.project.subtitle.style.font == DEFAULT_FONT
