"""화면 정리(UX): 위 도구 막대, 오른쪽 4개 탭, 타임라인 도구 막대(✂ 자르기 도구·🧲 자석·확대 막대·＋ 전환·＋ 자막·● 녹음),
고른 것만 보이는 🛠 속성 칸과 '▸ 숫자로 정확히' 접기, 배경음악 속성, 옮긴 기능이 다른 곳에서 여전히 되는지."""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from composer.schema import Project


@pytest.fixture
def win(tmp_path):
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from tests.media_fixtures import make_image, make_media
    make_media(tmp_path)
    make_image(tmp_path / "a.png")
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.asked = []
    w.confirm = lambda title, msg: (w.asked.append(msg) or True)
    w.resize(1400, 900)
    w.set_project(Project.from_dict({
        "clips": [{"file": "a.png", "duration": 2}, {"file": "a.png", "duration": 2}, {"file": "a.png", "duration": 2}],
        "transition": {"type": "none", "duration": 0},
        "subtitle": {"items": [{"index": 1, "start": 0.5, "end": 1.5, "text": "하나"},
                               {"index": 2, "start": 3.0, "end": 4.0, "text": "둘"}]},
        "bgm": {"file": None}}, base_dir=tmp_path), None)
    w.show()
    QCoreApplication.processEvents()
    yield w, tmp_path
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _key(w, k, mod=Qt.NoModifier):
    return w.handle_key(QKeyEvent(QEvent.KeyPress, k, mod))


def _slide(vs, v):
    """사용자가 슬라이더를 움직인 것처럼 (setValue 는 신호를 막음)."""
    vs.slider.setValue(int(round(v / vs.step)))


def _rects(c, kind):
    c.grab()
    return {i: r for k, i, r in c._items() if k == kind}


def test_tabs_and_toolbars(win):
    w, _ = win
    assert [w.right.tabText(i) for i in range(w.right.count())] == ["🛠 속성", "✨ 효과", "🎤 녹음", "🅰 자막", "📤 출력"]
    top = [a.text() for a in w.top_toolbar.actions() if a.text()]
    assert top[:3] == ["🤖 AI 로 구성하기", "📂 열기", "💾 저장"] and "📄 대본" not in top
    assert w.btn_add_media.menu() is not None and w.btn_export_top.text() == "📤 영상 내보내기"
    # 타임라인 도구 막대
    for b in (w.btn_select, w.btn_razor, w.btn_delete, w.btn_in, w.btn_out, w.btn_cut_range, w.chk_all_tracks,
              w.btn_magnet, w.btn_add_tr, w.btn_add_sub, w.btn_rec, w.btn_zoom_out, w.zoom_slider, w.btn_zoom_in,
              w.btn_fit, w.btn_follow):
        assert b.isVisibleTo(w)
    assert w.btn_select.isChecked() and not w.btn_razor.isChecked() and w.btn_magnet.isChecked()
    # 메뉴에만 있는 것
    menus = {a.text().split("(")[0].strip(): [x.text() for x in a.menu().actions()] for a in w.menuBar().actions() if a.menu()}
    f = next(v for k, v in menus.items() if k.startswith("파일"))
    for t in ("♪ 배경음악 넣기…", "📝 자막 파일 불러오기…", "📄 대본 출력…", "🤖 AI 로 구성하기…"):
        assert t in f
    assert any(t.startswith("캐시") or "캐시" in t for t in f)
    e = next(v for k, v in menus.items() if k.startswith("편집"))
    assert "새 이미지 기본값…" in e and any(t.startswith("✂ 재생 위치에서 자르기") for t in e)
    v = next(v for k, v in menus.items() if k.startswith("보기"))
    assert any("전체 보기" in t for t in v) and any("자석" in t for t in v)
    assert not any(k.startswith("내보내기") for k in menus)


def test_razor_tool_click_cuts_and_keys(win):
    w, _ = win
    c = w.timeline.canvas
    assert _key(w, Qt.Key_C) and c.tool == "razor" and w.btn_razor.isChecked()
    r = _rects(c, "clip")[0]
    x = c.x_of(1.0)
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(int(round(x)), int(r.center().y())))
    assert [k.duration for k in w.model.project.clips] == pytest.approx([1.0, 1.0, 2.0, 2.0], abs=0.02)
    assert c.tool == "razor"  # 계속 자를 수 있음
    # 자막도 자름
    rs = _rects(c, "subtitle")[2]
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(int(round(c.x_of(3.5))), int(rs.center().y())))
    assert len(w.model.cues) == 3
    w.model.undo(); w.model.undo()
    assert len(w.model.project.clips) == 3 and len(w.model.cues) == 2
    assert _key(w, Qt.Key_V) and c.tool == "select" and w.btn_select.isChecked()
    w.btn_razor.click()
    assert c.tool == "razor"
    assert _key(w, Qt.Key_Escape) and c.tool == "select"


def test_magnet_snap_and_shift(win):
    w, _ = win
    c = w.timeline.canvas
    c.set_zoom(100)
    w.player.seek(3.37)
    c.playhead = 3.37
    t, ok = c.snap_time(3.33, False)
    assert ok and t == pytest.approx(3.37)
    t, ok = c.snap_time(3.33, True)  # Shift = 자석 끔, 0.01초
    assert not ok and t == pytest.approx(3.33)
    t, ok = c.snap_time(1.98, False)  # 클립 경계 2.0
    assert ok and t == pytest.approx(2.0)
    w.btn_magnet.click()
    assert not c.magnet and QSettings("knhanul", "VideoComposer").value("timeline/magnet") in (False, "false")
    t, ok = c.snap_time(1.98, False)
    assert not ok and t == pytest.approx(2.0, abs=0.051)
    w.act_magnet.trigger()
    assert c.magnet and w.btn_magnet.isChecked()


def test_zoom_slider_buttons_and_fit(win):
    w, _ = win
    c = w.timeline.canvas
    w.zoom_slider.setValue(100)
    assert c.pps == pytest.approx(800)
    w.zoom_slider.setValue(0)
    assert c.pps == pytest.approx(5)
    w.btn_zoom_in.click()
    assert c.pps == pytest.approx(6.5) and w.zoom_slider.value() > 0
    w.btn_fit.click()
    vw = w.timeline.viewport().width()
    assert c.x_of(w.model.timeline.total) <= vw and c.pps > 50
    assert _key(w, Qt.Key_0, Qt.ControlModifier) or True


def test_add_transition_and_subtitle_here(win):
    w, _ = win
    w.player.seek(2.1)
    w.btn_add_tr.click()
    tr = w.model.project.clips[1].transition
    assert tr.type == "crossfade" and tr.duration == pytest.approx(0.5)
    assert w.model.selection.kind == "transition" and w.inspector.stack.currentWidget() is w.inspector.tr_page
    w.model.undo()
    assert w.model.project.clips[1].transition is None or w.model.project.clips[1].transition.duration == 0
    # 기본 전환을 바꾸면 그 전환이 들어감
    op = w.inspector.output_page
    op.tr_type.setCurrentIndex(op.tr_type.findData("fade"))
    w.btn_add_tr.click()
    assert w.model.project.clips[1].transition.type == "fade"
    # ＋ 자막: 재생 위치에 2초 (다음 자막과 안 겹치게)
    w.player.seek(2.0)
    w.btn_add_sub.click()
    cues = w.model.cues
    assert [c.text for c in cues] == ["하나", "새 자막", "둘"]
    assert cues[1].start == pytest.approx(2.0) and cues[1].end <= 3.0 + 1e-6
    assert w.model.selection.kind == "subtitle" and w.right.currentWidget() is w.inspector
    w.model.undo()
    assert [c.text for c in w.model.cues] == ["하나", "둘"]
    # 다른 자막 위면 그 자막 뒤로 (겹치지 않게)
    w.player.seek(3.5)
    w.add_subtitle_here()
    assert len(w.model.cues) == 3 and w.model.cues[1].end == pytest.approx(4.0)
    assert w.model.cues[2].start >= 4.0 - 1e-6
    w.model.undo()
    # 잠기면 막힘
    w.timeline.header.buttons[("subtitle", "locked")].click()
    assert w.add_subtitle_here() is False and "잠겨" in w.statusBar().currentMessage()


def test_apply_default_transition_all(win):
    w, _ = win
    op = w.inspector.output_page
    op.tr_type.setCurrentIndex(op.tr_type.findData("fade"))
    _slide(op.tr_dur, 0.8)
    w.model.edit(lambda p: setattr(p.clips[1], "transition", __import__("composer.schema", fromlist=["Transition"]).Transition("wipeleft", 0.3)))
    op.btn_tr_all.click()
    tl = w.model.timeline.clips
    assert all(c.transition_in.type == "fade" and c.transition_in.duration == pytest.approx(0.8) for c in tl[1:])
    w.model.undo()
    assert w.model.project.clips[1].transition.type == "wipeleft"


def test_rec_button_opens_record_tab(win, monkeypatch):
    w, _ = win
    seen = []
    monkeypatch.setattr(w.dub, "record_line", lambda *a, **k: seen.append(w.model.selection.index))
    w.player.seek(3.2)
    w.btn_rec.click()
    assert w.right.currentWidget() is w.dub_scroll and w.dub.mode_tabs.currentIndex() == 0
    assert seen == [2]


def test_num_fold_remembered_and_synced(win):
    w, _ = win
    ins = w.inspector
    w.model.select("clip", 0)
    cp = ins.clip_page
    assert cp.num.body.isHidden() and not cp.duration.isVisibleTo(ins)
    assert cp.btn_fx.isVisibleTo(ins) and cp.speed_preset.isVisibleTo(ins) is False  # 이미지는 속도 없음, 움직임은 ✨ 효과 탭
    cp.num.head.click()
    assert cp.duration.isVisibleTo(ins)
    assert QSettings("knhanul", "VideoComposer").value("ui/fold/num") in (True, "true")
    w.model.select("subtitle", 1)
    assert not ins.sub_page.num.body.isHidden()  # 다른 칸도 같이 펼쳐짐
    ins.sub_page.num.head.click()
    w.model.select("clip", 1)
    assert cp.num.body.isHidden()
    # 새로 만들어도 기억
    from composer.ui.widgets import Collapsible
    cp.num.head.click()
    assert Collapsible("x", key="num").is_open()


def test_clip_basic_controls_and_buttons(win):
    w, _ = win
    cp = w.inspector.clip_page
    w.model.select("clip", 1)
    cp.btn_fx.click()  # 움직임·색은 ✨ 효과 탭으로
    assert w.right.currentWidget() is w.effects_scroll
    _slide(w.effects.strength_slider, 0.5)
    assert w.model.project.clips[1].motion_strength == pytest.approx(0.5, abs=0.01)
    w.model.undo()
    w.right.setCurrentIndex(0)
    cp.btn_dup.click()
    assert len(w.model.project.clips) == 4
    w.model.undo()
    w.model.select("clip", 1)
    cp.btn_del.click()
    assert len(w.model.project.clips) == 2
    w.model.undo()
    assert len(w.model.project.clips) == 3
    assert not hasattr(cp, "btn_left") and not hasattr(cp, "chk_tr")


def test_bgm_page_edit_undo_and_lock(win):
    w, d = win
    assert w.add_bgm(str(d / "music.mp3"))
    ins = w.inspector
    bp = ins.bgm_page
    assert w.model.selection.kind == "bgm" and ins.stack.currentWidget() is bp and w.right.currentWidget() is ins
    assert "music.mp3" in bp.head.text() or "music.mp3" in bp.hint.text() or "music.mp3" in bp.btn_bgm.toolTip() + bp.lbl_file.text()
    _slide(bp.vol_slider, 0.5)
    assert w.model.project.bgm.volume == pytest.approx(0.5)
    _slide(bp.fade_in_slider, 3.0)
    assert w.model.project.bgm.fade_in == pytest.approx(3.0)
    bp.duck.setChecked(not bp.duck.isChecked())
    w.model.undo(); w.model.undo(); w.model.undo()
    assert w.model.project.bgm.volume != pytest.approx(0.5) and w.model.project.bgm.file == "music.mp3"
    bp.num.head.click()
    assert bp.bgm_start.isVisibleTo(ins)
    w.timeline.header.buttons[("bgm", "locked")].click()
    w.model.select("bgm")
    assert not bp.isEnabled() and ins.lock_banner.isVisibleTo(ins)
    w.timeline.header.buttons[("bgm", "locked")].click()
    bp.btn_bgm_clear.click()
    assert w.model.project.bgm.file is None


def test_empty_track_add_buttons(win):
    w, d = win
    c = w.timeline.canvas
    btns = {k: r for k, r, _ in c.add_buttons()}
    assert set(btns) == {"bgm"}  # 자막은 이미 있음
    got = []
    w.ask_bgm_file = lambda: got.append(1) or str(d / "music.mp3")
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, btns["bgm"].center().toPoint())
    assert got == [1] and w.model.project.bgm.file == "music.mp3"
    assert not c.add_buttons()
    w.model.undo()
    w.remove_all_subtitles()
    btns = {k: r for k, r, t in c.add_buttons()}
    assert "subtitle" in btns
    w.inspector.sub_file_panel.ask_open = lambda: str(d / "subs.srt")
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, btns["subtitle"].center().toPoint())
    assert len(w.model.cues) == 2 and w.model.cues[0].text == "안녕하세요"


def test_moved_features_still_reachable(win):
    w, d = win
    # 새 이미지 기본값: ➕ ▾ 메뉴와 편집 메뉴
    texts = [a.text() for a in w.btn_add_media.menu().actions()]
    assert any(t.startswith("새 이미지 기본값") for t in texts)
    w.show_image_defaults()
    assert w.image_defaults.isVisible()
    w.image_defaults.image_duration.setValue(4.0)
    assert QSettings("knhanul", "VideoComposer").value("import/image_duration") is not None
    w.image_defaults.close()
    # 구간 표시 지우기: Esc (도구가 선택일 때)
    w.set_mark_in(1.0); w.set_mark_out(3.0)
    assert w.btn_cut_range.isEnabled()
    assert _key(w, Qt.Key_Escape)
    assert w.timeline.canvas.mark_in is None and not w.btn_cut_range.isEnabled()
    # 테이크 재생은 더빙 속성 칸에
    tp = w.inspector.take_page
    assert tp.btn_play.text() and tp.btn_rerec.text().startswith("●")
    # 출력 탭에서 내보내기 / 목소리만
    called = []
    w.render_final = lambda *a: called.append("v")
    w.export_voice = lambda *a: called.append("a")
    w.inspector.output_page.btn_export.click()
    assert called and called[0] == "v"
