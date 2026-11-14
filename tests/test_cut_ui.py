"""화면: ✂ 자르기(Ctrl+K) · 🗑 삭제(리플) · In/Out 구간 잘라내기 · 오른쪽 클릭 메뉴 · 예전 프로젝트 자동 변환."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

from composer.schema import Project  # noqa: E402


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from media_fixtures import make_image, make_media
    d = tmp_path_factory.mktemp("cut")
    make_media(d)
    make_image(d / "a.png")
    make_image(d / "b.png", color="blue")
    return d


def _proj(d, version=2):
    return Project.from_dict({
        "version": version,
        "output": {"width": 320, "height": 180, "fps": 25},
        "transition": {"type": "crossfade", "duration": 1.0},
        "clips": [{"file": "a.png", "duration": 4, "motion": "zoom_in"}, {"file": "b.png", "duration": 4}],
        "subtitle": {"items": [{"index": 1, "start": 0.5, "end": 2.5, "text": "하나 둘 셋 넷"},
                               {"index": 2, "start": 5.0, "end": 6.0, "text": "뒤"}]},
        "dubbing": {"takes": [{"file": "take1.wav", "offset": 6.5}]},
    }, base_dir=d)


@pytest.fixture
def win(media):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.asked = []
    w.confirm = lambda title, text: w.asked.append(text) or True
    w.notes = []
    w.notify = lambda text, warning=False: w.notes.append(text)
    w.show()
    w.set_project(_proj(media), None)
    app.processEvents()
    yield w, app
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _key(w, k, mod=Qt.NoModifier):
    return w.handle_key(QKeyEvent(QEvent.KeyPress, k, mod))


def test_buttons_menu_and_cut_clip(win):
    w, app = win
    assert w.btn_razor.text() == "✂ 자르기 (C)" and w.btn_delete.text() == "🗑 삭제"
    assert w.btn_cut_range.text() == "✂ 구간 잘라내기" and w.chk_all_tracks.text() == "모든 트랙"
    assert not hasattr(w, "btn_cut")  # 재생 위치에서 자르기는 Ctrl+K·편집 메뉴·오른쪽 클릭
    assert not w.btn_cut_range.isEnabled()
    edit = next(a.menu() for a in w.menuBar().actions() if a.text().startswith("편집"))
    cut_act = next(a for a in edit.actions() if a.text().startswith("✂ 재생 위치에서 자르기"))
    assert cut_act.shortcut().toString() == "Ctrl+K"
    # 아무것도 안 고르고 1초에서 자르기 → 그 아래 클립(이미지)이 둘로, 전체 길이 그대로
    w.model.select("")
    w.player.seek(1.0)
    assert w.model.timeline.total == pytest.approx(8.0)
    cut_act.trigger()
    pr = w.model.project
    assert [c.duration for c in pr.clips] == [1.0, 3.0, 4.0] and w.model.timeline.total == pytest.approx(8.0)
    assert pr.clips[1].transition.type == "none" and pr.clips[1].motion_from == pytest.approx(0.25)
    assert w.model.selection.kind == "clip" and w.model.selection.index == 1
    # 🗑 삭제 = 리플 삭제 (뒤가 당겨짐)
    w.btn_delete.click()
    assert [c.duration for c in pr.clips] == [1.0, 4.0] and w.model.timeline.total == pytest.approx(5.0)
    assert w.model.undo() and w.model.undo()
    assert [c.duration for c in w.model.project.clips] == [4.0, 4.0]


def test_cut_subtitle_and_take(win):
    w, app = win
    w.model.select("subtitle", 1)
    w.player.seek(1.5)
    assert w.cut_at()
    items = w.model.project.subtitle.items
    assert [(i.index, i.text) for i in items] == [(1, "하나 둘"), (2, "셋 넷"), (3, "뒤")]
    # 더빙 조각 (take1.wav 1초, 6.5초부터) → 7.0초에서 자르기
    w.model.select("take", 0)
    assert w.cut_at(7.0)
    tk = w.model.project.dubbing.takes
    assert len(tk) == 2 and tk[0].trim_end == pytest.approx(0.5) and tk[1].trim_start == pytest.approx(0.5)
    rects = [r for k, i, r in w.timeline.canvas._items() if k == "take"]
    assert rects[0].right() == pytest.approx(rects[1].left(), abs=0.5)  # 타임라인에 두 조각이 맞닿아 보임
    # 위치가 항목 밖이면 안내만
    w.model.select("take", 0)
    assert not w.cut_at(6.52)


def test_in_out_range_cut_all_tracks_and_undo(win):
    w, app = win
    w.player.seek(3.0)
    assert _key(w, Qt.Key_I)
    w.player.seek(5.0)
    assert _key(w, Qt.Key_O)
    c = w.timeline.canvas
    assert (c.mark_in, c.mark_out) == (3.0, 5.0) and w.btn_cut_range.isEnabled()
    c.grab()  # In/Out 표시 그리기
    before = w.model.project.to_json()
    assert w.cut_marked_range()
    pr = w.model.project
    assert w.model.timeline.total == pytest.approx(6.0) and [c.duration for c in pr.clips] == [3.0, 3.0]
    assert [(i.start, i.end) for i in pr.subtitle.items] == [(0.5, 2.5), (3.0, 4.0)]  # 뒤 자막도 당겨짐
    assert pr.dubbing.takes[0].offset == pytest.approx(4.5)
    assert c.mark_in is None and not w.btn_cut_range.isEnabled()
    assert w.model.undo() and w.model.project.to_json() == before  # 되돌리기 한 번
    # '모든 트랙' 끄면 클립만
    w.set_mark_in(3.0)
    w.set_mark_out(5.0)
    w.chk_all_tracks.setChecked(False)
    assert w.cut_marked_range()
    assert w.model.project.subtitle.items[1].start == 5.0


def test_locked_and_context_menu(win):
    w, app = win
    w.model.edit(lambda p: setattr(p.tracks.clips, "locked", True))
    before = w.model.project.to_json()
    w.model.select("")
    assert not w.cut_at(1.0)
    w.set_mark_in(1.0)
    w.set_mark_out(2.0)
    assert not w.cut_marked_range() and w.model.project.to_json() == before
    w.model.edit(lambda p: setattr(p.tracks.clips, "locked", False))
    from PySide6.QtCore import QPoint
    w._timeline_menu(QPoint(10, 10), 2.0)
    texts = [a.text() for a in w._last_menu.actions() if a.text()]
    assert texts[0].startswith("✂ 여기서 자르기") and "🗑 삭제" in texts and "✂ 구간 잘라내기" in texts
    w._last_menu.close()


def test_old_project_migrates_with_same_timing(win, media):
    w, app = win
    w.set_project(_proj(media, version=1), None)
    pr = w.model.project
    assert pr.version == 2 and w.model.migrated and w.model.dirty  # 저장하면 새 형식
    # 예전: 4 + 4 - 1(겹침) = 7초 → 지금도 7초, 전환은 3~4초(예전 겹침과 같은 곳)
    assert w.model.timeline.total == pytest.approx(7.0)
    assert [c.duration for c in pr.clips] == [3.5, 3.5]
    assert w.model.timeline.transition_span(1) == (3.0, 4.0)
