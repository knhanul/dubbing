"""더빙 조각 겹침: 빨간 빗금 표시 · 끌어 놓으면 옆 끝에 붙기(Shift = 겹쳐 놓기) · 끝 끌기 길이 조절은 옆 조각에서 멈춤 ·
새 녹음은 그 자리 그대로(표시만) · 내보내기 전에 한 번 확인 · 되돌리기·잠금."""
import os

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from composer.edit_ops import settle_take, split_take, take_overlaps, take_trim_limit
from composer.schema import Project
from test_dubbing import fake_sd  # noqa: F401


def _flen(rel):
    return 1.0  # 모든 더빙 파일 1초


def _proj(tmp_path, offsets, **kw):
    return Project.from_dict({"clips": [{"file": "img1.png", "duration": 10}],
                              "dubbing": {"takes": [{"file": f"takes/s{i}.wav", "offset": o, **kw} for i, o in enumerate(offsets)]}},
                             base_dir=tmp_path)


def test_overlaps_and_settle_rules(tmp_path):
    p = _proj(tmp_path, [0.0, 0.6, 3.0])
    spans, who = take_overlaps(p, _flen)
    assert spans == [(pytest.approx(0.6), pytest.approx(1.0))] and who == {0, 1}
    # 뒤쪽이 더 가까움 → 0번 끝(1.0)에 붙음
    assert settle_take(p, 1, _flen) and p.dubbing.takes[1].offset == pytest.approx(1.0)
    assert take_overlaps(p, _flen) == ([], set())
    # 앞쪽이 더 가까우면 앞 끝에
    p = _proj(tmp_path, [0.0, 3.0, 2.3])
    assert settle_take(p, 2, _flen) and p.dubbing.takes[2].offset == pytest.approx(2.0)
    # 가까운 자리가 또 겹치면 그다음 자리 (1~2초에 빈 틈 없음: 0번 0~1, 1번 1~2)
    p = _proj(tmp_path, [0.0, 1.0, 0.2])
    assert settle_take(p, 2, _flen) and p.dubbing.takes[2].offset == pytest.approx(2.0)
    # 닿기만 한 것(끝에 붙임)은 겹침 아님, 안 겹치면 그대로
    p = _proj(tmp_path, [0.0, 1.0])
    assert not settle_take(p, 1, _flen) and take_overlaps(p, _flen)[1] == set()
    # 0초보다 앞으로는 안 감
    p = _proj(tmp_path, [0.5, 0.0])
    assert settle_take(p, 1, _flen) and p.dubbing.takes[1].offset == pytest.approx(1.5)
    # 길이 조절 한계 = 옆 조각 끝
    p = _proj(tmp_path, [0.0, 2.0, 4.0])
    assert take_trim_limit(p, 1, "left", _flen) == pytest.approx(1.0)
    assert take_trim_limit(p, 1, "right", _flen) == pytest.approx(4.0)
    assert take_trim_limit(p, 0, "left", _flen) is None
    # 자르기는 겹침을 만들지 않음
    assert split_take(p, 1, 2.5, _flen) and take_overlaps(p, _flen)[1] == set()


@pytest.fixture
def win(fake_sd, tmp_path):  # noqa: F811
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    import composer.ui.main_window as mw
    make_media(tmp_path)
    (tmp_path / "takes").mkdir()
    import shutil
    for i in range(3):
        shutil.copy(tmp_path / "take1.wav", tmp_path / "takes" / f"s{i}.wav")  # 1초씩
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.asked = []
    w.answer = True
    w.confirm = lambda title, msg: (w.asked.append(msg) or w.answer)
    w.resize(1400, 900)
    w.show()
    w.set_project(_proj(tmp_path, [0.0, 3.0, 6.0]), None)
    w.timeline.canvas.set_zoom(100)
    QCoreApplication.processEvents()
    yield w, tmp_path
    w.dub.stop_recording()
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _send(c, typ, x, y, mod=Qt.NoModifier):
    pos = QPointF(x, y)
    btn = Qt.LeftButton
    ev = QMouseEvent(typ, pos, c.mapToGlobal(pos.toPoint()), btn if typ != QEvent.MouseButtonRelease else btn,
                     Qt.LeftButton if typ != QEvent.MouseButtonRelease else Qt.NoButton, mod)
    {QEvent.MouseButtonPress: c.mousePressEvent, QEvent.MouseMove: c.mouseMoveEvent,
     QEvent.MouseButtonRelease: c.mouseReleaseEvent}[typ](ev)


def _drag(c, x0, x1, y, mod=Qt.NoModifier):
    _send(c, QEvent.MouseButtonPress, x0, y)
    steps = 8
    for k in range(1, steps + 1):
        _send(c, QEvent.MouseMove, x0 + (x1 - x0) * k / steps, y, mod)
    _send(c, QEvent.MouseButtonRelease, x1, y, mod)


def _take_rects(c):
    c.grab()
    return {i: r for k, i, r in c._items() if k == "take"}


def test_drag_onto_other_take_snaps_to_edge_and_undo(win):
    w, _ = win
    c = w.timeline.canvas
    c.magnet = False
    r = _take_rects(c)
    y = r[1].center().y()
    # 1번(3초)을 0.4초 위치로 → 0번(0~1초)과 겹침 → 뒤 끝 1.0초에 붙음
    _drag(c, r[1].center().x(), r[1].center().x() - c.pps * 2.6, y)
    assert w.model.project.dubbing.takes[1].offset == pytest.approx(1.0, abs=0.01)
    assert not c.take_overlap_rects()
    assert "옆 조각 끝에 붙였습니다" in w.statusBar().currentMessage()
    w.model.undo()  # 끌기 + 붙이기 = 되돌리기 한 번
    assert w.model.project.dubbing.takes[1].offset == pytest.approx(3.0)
    # Shift 를 누른 채 놓으면 겹친 채로 → 빨간 빗금
    r = _take_rects(c)
    _drag(c, r[1].center().x(), r[1].center().x() - c.pps * 2.6, y, Qt.ShiftModifier)
    assert w.model.project.dubbing.takes[1].offset == pytest.approx(0.4, abs=0.02)
    rects = c.take_overlap_rects()
    assert len(rects) == 1 and rects[0].width() == pytest.approx(c.pps * 0.6, abs=3)
    assert rects[0].left() == pytest.approx(c.x_of(0.4), abs=3)
    # 겹친 조각을 고르면 속성 칸에 안내
    w.model.select("take", 1)
    assert "겹칩니다" in w.inspector.take_page.hint.text()


def test_edge_trim_stops_at_neighbor_shift_allows(win):
    w, _ = win
    c = w.timeline.canvas
    c.magnet = False
    def setup(p):
        p.dubbing.takes[1].trim_end = 0.5  # 1번: 3.0~3.5초
        p.dubbing.takes[2].offset = 3.7  # 2번: 3.7~4.7초
    w.model.edit(setup)
    r = _take_rects(c)
    y = r[1].center().y()
    _drag(c, r[1].right() - 1, r[1].right() + c.pps * 0.6, y)  # 오른쪽 끝을 4.1초까지
    assert w.model.project.dubbing.takes[1].trim_end == pytest.approx(0.7, abs=0.02)  # 3.7초(옆 조각)에서 멈춤
    assert not c.take_overlap_rects()
    w.model.undo()
    assert w.model.project.dubbing.takes[1].trim_end == pytest.approx(0.5)
    r = _take_rects(c)
    _drag(c, r[1].right() - 1, r[1].right() + c.pps * 0.6, y, Qt.ShiftModifier)
    assert w.model.project.dubbing.takes[1].trim_end == pytest.approx(1.0, abs=0.02) or w.model.project.dubbing.takes[1].trim_end is None
    assert len(c.take_overlap_rects()) == 1


def test_locked_take_does_not_move(win):
    w, _ = win
    c = w.timeline.canvas
    w.timeline.header.buttons[("dubbing", "locked")].click()
    before = w.model.project.to_json()
    r = _take_rects(c)
    _drag(c, r[1].center().x(), r[1].center().x() - c.pps * 2.6, r[1].center().y())
    assert w.model.project.to_json() == before


def test_new_recording_overlap_is_only_marked(win):
    w, d = win
    dub = w.dub
    w.player.seek(0.3)  # 0번(0~1초) 위에서 자유 녹음
    dub.mode_tabs.setCurrentIndex(1)
    dub.free_page.with_video.setChecked(False)
    dub.free_page.toggle_record()
    assert dub.mode == "free", w.player.position()
    dub.recorder._callback(np.full((48000, 1), 0.1, np.float32), 48000, None, None)
    dub.free_page.toggle_record()
    takes = w.model.project.dubbing.takes
    assert len(takes) == 4
    assert w.model.take_positions()[3] == pytest.approx(0.3, abs=0.06)  # 옮기지 않음
    assert w.timeline.canvas.take_overlap_rects()
    assert "겹칩니다" in w.statusBar().currentMessage()


def test_export_warns_once_about_overlap(win, monkeypatch):
    w, d = win
    opened = []
    w.exec_export_options = lambda opt: opened.append(1) or False
    w.render_final()
    assert w.asked == [] and opened == [1]  # 안 겹치면 묻지 않음
    w.model.edit(lambda p: setattr(p.dubbing.takes[1], "offset", 0.5))
    w.answer = False
    w.render_final()
    assert len(w.asked) == 1 and "겹쳐" in w.asked[0] and "00:00" in w.asked[0] and opened == [1]  # 취소
    w.answer = True
    w.render_final()
    assert len(w.asked) == 2 and opened == [1, 1]  # 계속
    # 목소리만 내보내기도
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    w.answer = False
    w.export_voice()
    assert len(w.asked) == 3
    # 더빙 트랙 소리를 껐으면 묻지 않음
    w.timeline.header.buttons[("dubbing", "muted")].click()
    w.render_final()
    assert len(w.asked) == 3
