"""타임라인 여러 개 고르기(같은 트랙끼리): Ctrl+클릭 · Shift+클릭 범위 · 빈 곳 끌어 상자 · Ctrl+A ·
함께 끌기(자막·더빙 이동, 클립 묶어 순서 이동) · [ ] · Delete 한꺼번에 · 되돌리기."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from composer.schema import Project  # noqa: E402


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from media_fixtures import make_image, make_media
    d = tmp_path_factory.mktemp("multi")
    make_media(d)
    for n, c in (("a", "red"), ("b", "blue"), ("c", "green"), ("d", "yellow")):
        make_image(d / f"{n}.png", color=c)
    return d


def _proj(d):
    return Project.from_dict({
        "output": {"width": 320, "height": 180, "fps": 25},
        "clips": [{"file": f"{n}.png", "duration": 2, "label": n} for n in "abcd"],
        "subtitle": {"items": [{"index": 1, "start": 0.5, "end": 1.5, "text": "하나"},
                               {"index": 2, "start": 2.0, "end": 3.0, "text": "둘"},
                               {"index": 3, "start": 4.0, "end": 5.0, "text": "셋"},
                               {"index": 4, "start": 6.0, "end": 7.0, "text": "넷"}]},
        "dubbing": {"takes": [{"file": "take1.wav", "offset": 0.5}, {"file": "take1.wav", "offset": 3.0},
                              {"file": "take1.wav", "offset": 6.0}]},
    }, base_dir=d)


@pytest.fixture
def win(media):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.confirm = lambda title, text: True
    w.notify = lambda text, warning=False: None
    w.show()
    w.set_project(_proj(media), None)
    w.timeline.canvas.set_zoom(100)  # 1초 = 100점
    w.timeline.canvas.magnet = False
    app.processEvents()
    yield w, app
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _ev(c, typ, x, y, mods=Qt.NoModifier):
    pos = QPointF(x, y)
    btn = Qt.NoButton if typ == QEvent.MouseMove else Qt.LeftButton
    return QMouseEvent(typ, pos, c.mapToGlobal(pos.toPoint()), btn, Qt.LeftButton, mods)


def _click(c, x, y, mods=Qt.NoModifier):
    c.mousePressEvent(_ev(c, QEvent.MouseButtonPress, x, y, mods))
    c.mouseReleaseEvent(_ev(c, QEvent.MouseButtonRelease, x, y, mods))


def _drag(c, x0, x1, y, mods=Qt.NoModifier):
    c.mousePressEvent(_ev(c, QEvent.MouseButtonPress, x0, y, mods))
    c.mouseMoveEvent(_ev(c, QEvent.MouseMove, (x0 + x1) / 2, y, mods))
    c.mouseMoveEvent(_ev(c, QEvent.MouseMove, x1, y, mods))
    c.mouseReleaseEvent(_ev(c, QEvent.MouseButtonRelease, x1, y, mods))


def _center(c, kind, idx):
    r = next(r for k, i, r in c._items() if k == kind and i == idx)
    return r.center().x(), r.center().y()


def _key(w, k, mod=Qt.NoModifier):
    return w.handle_key(QKeyEvent(QEvent.KeyPress, k, mod))


def test_ctrl_click_adds_and_removes_same_kind_only(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    _click(c, *_center(c, "subtitle", 1))
    _click(c, *_center(c, "subtitle", 3), Qt.ControlModifier)
    assert m.selection.kind == "subtitle" and m.selection.indices == [1, 3] and m.selection.index == 3
    _click(c, *_center(c, "subtitle", 1), Qt.ControlModifier)  # 다시 누르면 빠짐
    assert m.selection.indices == [3]
    _click(c, *_center(c, "take", 0), Qt.ControlModifier)  # 다른 종류 → 새로 고름
    assert m.selection.kind == "take" and m.selection.indices == [0]


def test_shift_click_selects_range_in_timeline_order(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    _click(c, *_center(c, "clip", 0))
    _click(c, *_center(c, "clip", 2), Qt.ShiftModifier)
    assert m.selection.indices == [0, 1, 2] and m.selection.index == 2
    # 고른 것 중 하나를 그냥 누르면 그것만
    _click(c, *_center(c, "clip", 1))
    assert m.selection.indices == [1]


def test_shift_drag_still_moves_only_that_item_finely(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    _click(c, *_center(c, "subtitle", 1))
    x, y = _center(c, "subtitle", 3)
    _drag(c, x, x + 23, y, Qt.ShiftModifier)  # +0.23초 (0.01초 단위)
    items = {it.index: it for it in m.project.subtitle.items}
    assert items[3].start == pytest.approx(4.23) and items[1].start == pytest.approx(0.5)
    assert m.selection.indices == [3]


def test_marquee_on_empty_track_area(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    r = c.track_rect("subtitle")
    y = r.center().y()
    _drag(c, c.x_of(1.7), c.x_of(5.5), y)  # 1.7~5.5초: 2, 3번 자막에 걸침 (1.7초는 빈 곳)
    assert m.selection.kind == "subtitle" and m.selection.indices == [2, 3]
    assert c.marquee is None
    # Ctrl 로 시작하면 원래 고른 것에 더함
    _drag(c, c.x_of(5.6), c.x_of(6.5), y, Qt.ControlModifier)
    assert m.selection.indices == [2, 3, 4]


def test_drag_moves_selected_subtitles_and_takes_together_one_undo(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    m.select_many("subtitle", [1, 3])
    x, y = _center(c, "subtitle", 1)
    n_undo = len(m._undo)
    _drag(c, x, x + 50, y)  # +0.5초
    items = {it.index: it for it in m.project.subtitle.items}
    assert items[1].start == pytest.approx(1.0) and items[3].start == pytest.approx(4.5)
    assert items[2].start == pytest.approx(2.0)  # 안 고른 것은 그대로
    assert m.selection.indices == [1, 3]  # 끌고 나서도 선택 유지
    assert len(m._undo) == n_undo + 1
    # 0초 밑으로는 고른 것 전부가 같이 멈춤
    x, y = _center(c, "subtitle", 3)
    _drag(c, x, x - 900, y)
    items = {it.index: it for it in m.project.subtitle.items}
    assert items[1].start == pytest.approx(0.0) and items[3].start == pytest.approx(3.5)

    takes = m.project.dubbing.takes
    m.select_many("take", [0, 2])
    x, y = _center(c, "take", 2)
    _drag(c, x, x + 30, y, Qt.ShiftModifier)  # Shift = 겹쳐도 그대로 (settle 없음)
    assert takes[0].offset == pytest.approx(0.8) and takes[2].offset == pytest.approx(6.3)
    assert takes[1].offset == pytest.approx(3.0)


def test_drag_moves_selected_clips_as_block(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    m.select_many("clip", [0, 1])
    x, y = _center(c, "clip", 0)
    _drag(c, x, c.x_of(7.5), y)  # 마지막(d) 뒤로
    assert [cl.label for cl in m.project.clips] == ["c", "d", "a", "b"]
    assert m.selection.indices == [2, 3] and m.selection.index == 2
    m.undo()
    assert [cl.label for cl in m.project.clips] == list("abcd")


def test_delete_and_nudge_apply_to_all_selected(win):
    w, app = win
    m = w.model
    m.select_many("subtitle", [2, 4])
    _key(w, Qt.Key_BracketRight)
    items = {it.index: it for it in m.project.subtitle.items}
    assert items[2].start == pytest.approx(2.1) and items[4].start == pytest.approx(6.1)
    assert items[1].start == pytest.approx(0.5)

    m.select_many("clip", [1, 3])
    assert _key(w, Qt.Key_Delete)
    assert [cl.label for cl in m.project.clips] == ["a", "c"] and m.selection.kind == ""
    m.undo()
    assert len(m.project.clips) == 4

    m.select_many("take", [0, 1])
    w.delete_selected()
    assert [t.offset for t in m.project.dubbing.takes] == [pytest.approx(6.0)]

    m.select_many("subtitle", [1, 2, 3])
    w.delete_selected()
    assert [it.text for it in m.project.subtitle.items] == ["넷"]


def test_ctrl_a_selects_whole_track_and_undo_prunes(win):
    w, app = win
    m = w.model
    m.select("take", 1)
    assert _key(w, Qt.Key_A, Qt.ControlModifier)
    assert m.selection.kind == "take" and m.selection.indices == [0, 1, 2] and m.selection.index == 1
    m.select("")
    _key(w, Qt.Key_A, Qt.ControlModifier)  # 아무것도 안 골랐으면 클립
    assert m.selection.kind == "clip" and m.selection.count == 4
    assert "4개 고름" in w.statusBar().currentMessage() or "4개를 모두" in w.statusBar().currentMessage()
    # 클립을 하나 지운 뒤 되돌리기·다시 실행 → 없어진 번호는 선택에서 빠짐
    m.select("clip", 3)
    w.delete_selected()
    m.undo()
    m.select_all("clip")
    m.redo()
    assert m.selection.indices == [0, 1, 2]


def test_context_menu_keeps_multi_selection(win):
    w, app = win
    c, m = w.timeline.canvas, w.model
    m.select_many("subtitle", [1, 2])
    from PySide6.QtGui import QContextMenuEvent
    x, y = _center(c, "subtitle", 2)
    c.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPointF(x, y).toPoint()))
    assert m.selection.indices == [1, 2]
    texts = [a.text() for a in w._last_menu.actions()]
    assert "🗑 삭제 (2개)" in texts and any(t.startswith("☑ 자막 모두 고르기") for t in texts)
    w._last_menu.close()
