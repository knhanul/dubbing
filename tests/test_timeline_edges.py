"""타임라인(프리미어 방식): 클립은 맞닿게, 전환은 경계 가운데 블록. 가장자리 잡기·왼쪽 끝 조절·전환 블록·잠금·되돌리기."""
import os
import subprocess

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from composer.probe import ffmpeg_path  # noqa: E402
from composer.schema import Project  # noqa: E402
from composer.ui.model import ProjectModel  # noqa: E402
from composer.ui.timeline_widget import EDGE, TimelineCanvas  # noqa: E402


@pytest.fixture
def cv(tmp_path):
    app = QApplication.instance() or QApplication([])
    ff = [ffmpeg_path(), "-v", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=red:s=64x36", "-frames:v", "1", str(tmp_path / "a.png")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=s=64x36:r=25:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                         str(tmp_path / "v.mp4")], check=True)
    pr = Project.from_dict({
        "transition": {"type": "fade", "duration": 1.0},
        "clips": [{"file": "a.png", "duration": 3}, {"file": "a.png", "duration": 3},
                  {"file": "v.mp4", "trim_start": 1.0, "trim_end": 4.0}]}, base_dir=tmp_path)
    m = ProjectModel()
    m.set_project(pr, None)
    area = QScrollArea()
    c = TimelineCanvas(m)
    area.setWidget(c)
    c.set_zoom(100)  # 1초 = 100점
    area.resize(1400, 300)
    area.show()
    app.processEvents()
    yield c, m, app
    area.close()


def _drag(c, x0, x1, y, mods=Qt.NoModifier):
    for typ, x, btn in ((QEvent.MouseButtonPress, x0, Qt.LeftButton), (QEvent.MouseMove, (x0 + x1) / 2, Qt.NoButton),
                        (QEvent.MouseMove, x1, Qt.NoButton), (QEvent.MouseButtonRelease, x1, Qt.LeftButton)):
        pos = QPointF(x, y)
        ev = QMouseEvent(typ, pos, c.mapToGlobal(pos.toPoint()), btn, Qt.LeftButton if typ == QEvent.MouseMove else btn, mods)
        {QEvent.MouseButtonPress: c.mousePressEvent, QEvent.MouseMove: c.mouseMoveEvent,
         QEvent.MouseButtonRelease: c.mouseReleaseEvent}[typ](ev)


def _rects(c):
    return {i: r for k, i, r in c._items() if k == "clip"}


def test_clips_butt_and_edges_are_grabbable(cv):
    c, m, app = cv
    tl = m.timeline.clips
    assert tl[1].start == pytest.approx(3.0) and m.timeline.total == pytest.approx(9.0)  # 맞닿게, 길이 = 합
    r = _rects(c)
    assert r[0].right() == pytest.approx(r[1].left())  # 겹치지 않음
    y = r[0].top() + 8  # 위쪽(전환 블록이 없는 높이)
    h = c.hit_test(QPointF(r[0].right() - 4, y))
    assert (h.kind, h.index, h.part) == ("clip", 0, "right")
    h = c.hit_test(QPointF(r[1].left() + 4, y))
    assert (h.index, h.part) == (1, "left")
    assert c.hit_test(QPointF(r[0].center().x(), y)).part == "body"
    # 가장자리에 올리면 ↔ 커서 + 노란 강조
    ev = QMouseEvent(QEvent.MouseMove, QPointF(r[0].right() - 4, y), QPointF(), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    c.mouseMoveEvent(ev)
    assert c.cursor().shape() == Qt.SizeHorCursor and c._hover.index == 0 and c._hover.part == "right"
    img = c.grab().toImage()
    px = img.pixelColor(int(r[0].right()), int(r[0].top() + 4))
    assert px.red() > 200 and px.green() > 180  # 노란 강조선


def test_transition_block_select_resize_and_delete(cv):
    from composer.ui.inspector import TransitionPage
    c, m, app = cv
    blocks = {i: r for k, i, r in c._items() if k == "transition"}
    assert set(blocks) == {1, 2}
    b = blocks[1]
    r = _rects(c)
    assert b.center().x() == pytest.approx(r[1].left())  # 경계 가운데에 걸침
    assert b.width() == pytest.approx(100) and b.top() > r[1].center().y() - 1  # 1초 = 100점, 아래쪽 일부 높이만
    yb = b.center().y()
    h = c.hit_test(QPointF(b.center().x(), yb))
    assert (h.kind, h.index, h.part) == ("transition", 1, "body")
    assert c.hit_test(QPointF(b.right() - 2, yb)).part == "right"
    # 클릭 = 고르기, 오른쪽 끝을 25점(0.25초) 끌면 양쪽으로 같이 → 1.5초
    _drag(c, b.right() - 2, b.right() - 2 + 25, yb)
    assert m.selection.kind == "transition" and m.selection.index == 1
    assert m.project.clips[1].transition.duration == pytest.approx(1.5)
    assert m.timeline.total == pytest.approx(9.0)  # 전체 길이는 그대로
    # 아무리 늘려도 짧은 쪽 클립 길이(3초)까지
    b = {i: r for k, i, r in c._items() if k == "transition"}[1]
    _drag(c, b.right() - 2, b.right() - 2 + 900, yb)
    assert m.timeline.clips[1].transition_in.duration == pytest.approx(3.0)
    assert m.undo() and m.project.clips[1].transition.duration == pytest.approx(1.5)
    # 인스펙터: 종류/길이, 없애기(Delete 와 같음) + 되돌리기
    pg = TransitionPage(m)
    m.select("transition", 1)
    pg.refresh()
    assert "1번 ↔ 2번" in pg.lbl.text() and pg.tr_dur.value() == pytest.approx(1.5)
    pg.tr_type.setCurrentIndex(pg.tr_type.findData("fade"))
    assert m.project.clips[1].transition.type == "fade"
    pg.remove()
    assert m.timeline.clips[1].transition_in.duration == 0 and m.selection.kind == ""
    assert not any(k == "transition" and i == 1 for k, i, _ in c._items())
    assert m.undo() and m.timeline.clips[1].transition_in.type == "fade"


def test_drag_right_edge_changes_length_and_limits_transition(cv):
    c, m, app = cv
    r = _rects(c)
    y = r[0].top() + 8
    _drag(c, r[0].right() - 4, r[0].right() - 4 + 100, y)  # +1초
    assert m.project.clips[0].duration == pytest.approx(4.0)
    assert m.selection.kind == "clip" and m.selection.index == 0
    assert m.undo() and m.project.clips[0].duration == pytest.approx(3.0)
    # 0.6초로 줄이면 전환(1초)도 자동으로 0.6초까지
    r = _rects(c)
    _drag(c, r[0].right() - 4, r[0].right() - 4 - 240, y)
    assert m.project.clips[0].duration == pytest.approx(0.6)
    assert m.timeline.clips[1].transition_in.duration == pytest.approx(0.6)


def test_left_edge_image_length_and_video_trim(cv):
    c, m, app = cv
    r = _rects(c)
    y = r[1].top() + 8
    # 이미지 왼쪽 끝을 오른쪽으로 0.5초 → 길이 2.5초
    _drag(c, r[1].left() + 4, r[1].left() + 4 + 50, y)
    assert m.project.clips[1].duration == pytest.approx(2.5)
    # 동영상 왼쪽 끝: 오른쪽으로 0.5초 → 시작을 더 자름, 왼쪽으로 끌면 되살림(원본 처음까지만)
    r = _rects(c)
    _drag(c, r[2].left() + 4, r[2].left() + 4 + 50, y)
    assert m.project.clips[2].trim_start == pytest.approx(1.5) and m.timeline.clips[2].duration == pytest.approx(2.5)
    r = _rects(c)
    _drag(c, r[2].left() + 4, r[2].left() + 4 - 400, y)
    assert m.project.clips[2].trim_start == pytest.approx(0.0) and m.timeline.clips[2].duration == pytest.approx(4.0)
    assert m.project.clips[2].trim_end == pytest.approx(4.0)  # 끝은 그대로
    for _ in range(3):
        assert m.undo()
    assert m.project.clips[1].duration == pytest.approx(3.0) and m.project.clips[2].trim_start == pytest.approx(1.0)


def test_locked_edges_not_highlighted_or_dragged(cv):
    c, m, app = cv
    m.edit(lambda p: setattr(p.tracks.clips, "locked", True))
    r = _rects(c)
    y = r[0].top() + 8
    ev = QMouseEvent(QEvent.MouseMove, QPointF(r[0].right() - 4, y), QPointF(), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    c.mouseMoveEvent(ev)
    assert c._hover is None
    before = m.project.to_json()
    _drag(c, r[0].right() - 4, r[0].right() + 96, y)
    assert m.project.to_json() == before


def test_tiny_clip_keeps_body(cv):
    c, m, app = cv
    m.edit(lambda p: (setattr(p.clips[0], "duration", 0.1), setattr(p, "transition", __import__("composer.schema", fromlist=["Transition"]).Transition("none", 0.0))))
    c.set_zoom(100)
    r = _rects(c)
    assert r[0].width() == pytest.approx(10, abs=1) and EDGE >= 3
    # 아주 짧은 클립 가운데는 여전히 몸통(옮기기), 양끝은 가장자리
    h = c.hit_test(QPointF(r[0].center().x(), r[0].center().y()))
    assert h.part == "body" and h.index == 0
