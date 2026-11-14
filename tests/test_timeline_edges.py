"""타임라인: 전환으로 겹친 클립의 가장자리 잡기, 왼쪽 끝 조절, 겹침 표시, 잠금·되돌리기."""
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


def test_overlap_edges_are_grabbable(cv):
    c, m, app = cv
    tl = m.timeline.clips
    assert tl[1].transition_in.duration == pytest.approx(1.0) and tl[1].start == pytest.approx(2.0)  # 2~3초가 겹침
    r = _rects(c)
    y = r[0].center().y()
    # 앞 클립(0) 오른쪽 끝(3초)은 뒤 클립(1) 몸통 위에 있지만 가장자리가 먼저 잡힘
    assert r[1].left() < r[0].right() < r[1].right()
    h = c.hit_test(QPointF(r[0].right() - 2, y))
    assert (h.kind, h.index, h.part) == ("clip", 0, "right")
    h = c.hit_test(QPointF(r[0].right() + 2, y))
    assert (h.index, h.part) == (0, "right")
    # 뒤 클립 왼쪽 끝(2초)도 잡힘
    h = c.hit_test(QPointF(r[1].left() + 2, y))
    assert (h.index, h.part) == (1, "left")
    # 겹침 가운데는 몸통 (위에 그려진 뒤 클립)
    h = c.hit_test(QPointF((r[1].left() + r[0].right()) / 2, y))
    assert (h.index, h.part) == (1, "body")
    # 가장자리에 올리면 ↔ 커서 + 강조
    ev = QMouseEvent(QEvent.MouseMove, QPointF(r[0].right() - 1, y), QPointF(), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    c.mouseMoveEvent(ev)
    assert c.cursor().shape() == Qt.SizeHorCursor and c._hover.index == 0 and c._hover.part == "right"
    img = c.grab().toImage()  # 겹침 위 절반 = 앞 클립, 아래 절반 = 뒤 클립, 노란 강조선
    assert img.pixelColor(int(r[0].right()), int(r[0].top() + 6)).red() > 200


def test_drag_right_edge_of_overlapped_clip_changes_length_and_undo(cv):
    c, m, app = cv
    r = _rects(c)
    y = r[0].center().y()
    _drag(c, r[0].right() - 2, r[0].right() - 2 + 100, y)  # +1초
    assert m.project.clips[0].duration == pytest.approx(4.0)
    assert m.selection.kind == "clip" and m.selection.index == 0
    assert m.undo() and m.project.clips[0].duration == pytest.approx(3.0)
    # 줄이면 전환도 자동으로 짧아짐(짧은 쪽 절반까지만)
    r = _rects(c)
    _drag(c, r[0].right() - 2, r[0].right() - 2 - 200, y)  # 3초 → 1초
    assert m.project.clips[0].duration == pytest.approx(1.0)
    assert m.timeline.clips[1].transition_in.duration == pytest.approx(0.5)


def test_left_edge_image_length_and_video_trim(cv):
    c, m, app = cv
    r = _rects(c)
    y = r[1].center().y()
    # 이미지 왼쪽 끝을 오른쪽으로 0.5초 → 길이 2.5초
    _drag(c, r[1].left() + 2, r[1].left() + 2 + 50, y)
    assert m.project.clips[1].duration == pytest.approx(2.5)
    # 동영상 왼쪽 끝: 오른쪽으로 0.5초 → 시작을 더 자름, 왼쪽으로 끌면 되살림(원본 처음까지만)
    r = _rects(c)
    _drag(c, r[2].left() + 2, r[2].left() + 2 + 50, y)
    assert m.project.clips[2].trim_start == pytest.approx(1.5) and m.timeline.clips[2].duration == pytest.approx(2.5)
    r = _rects(c)
    _drag(c, r[2].left() + 2, r[2].left() + 2 - 400, y)
    assert m.project.clips[2].trim_start == pytest.approx(0.0) and m.timeline.clips[2].duration == pytest.approx(4.0)
    assert m.project.clips[2].trim_end == pytest.approx(4.0)  # 끝은 그대로
    for _ in range(3):
        assert m.undo()
    assert m.project.clips[1].duration == pytest.approx(3.0) and m.project.clips[2].trim_start == pytest.approx(1.0)


def test_locked_edges_not_highlighted_or_dragged(cv):
    c, m, app = cv
    m.edit(lambda p: setattr(p.tracks.clips, "locked", True))
    r = _rects(c)
    y = r[0].center().y()
    ev = QMouseEvent(QEvent.MouseMove, QPointF(r[0].right() - 1, y), QPointF(), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    c.mouseMoveEvent(ev)
    assert c._hover is None
    before = m.project.to_json()
    _drag(c, r[0].right() - 2, r[0].right() + 98, y)
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
