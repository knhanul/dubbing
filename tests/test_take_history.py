"""더빙 '녹음 기록': 같은 자막을 다시 녹음한 예전 파일(line_XXXX_vN.wav) 찾기 · 듣기 · 이걸로 쓰기(되돌리기) · 파일 삭제(확인, 지금 쓰는 건 불가) ·
녹음 탭 목록의 녹음 개수."""
import os

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.dubbing.takes import count_versions, take_versions, use_version
from composer.schema import Project, Take


def _wav(path, sec):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(48000 * sec), np.float32), 48000)


def _files(d):
    _wav(d / "takes" / "line_0002_v1.wav", 1.0)
    _wav(d / "takes" / "line_0002_v2.wav", 2.0)
    _wav(d / "takes" / "line_0002_v3.wav", 0.5)
    _wav(d / "takes" / "line_0001_v1.wav", 1.0)
    _wav(d / "takes" / "line_0012_v1.wav", 1.0)  # 다른 번호(0012)는 섞이지 않음
    _wav(d / "takes" / "session_001.wav", 3.0)


def test_versions_backend(tmp_path):
    _files(tmp_path)
    p = Project.from_dict({"dubbing": {"takes": [{"file": "takes/line_0002_v2.wav", "subtitle": 2},
                                                 {"file": "takes/session_001.wav"}]}}, base_dir=tmp_path)
    vs = take_versions(p, p.dubbing.takes[0])
    assert [v.version for v in vs] == [1, 2, 3] and [v.current for v in vs] == [False, True, False]
    assert [round(v.length, 2) for v in vs] == [1.0, 2.0, 0.5] and vs[0].rel == "takes/line_0002_v1.wav"
    assert all(v.mtime > 0 for v in vs)
    # 자유·세션 녹음은 그 파일 하나만
    ss = take_versions(p, p.dubbing.takes[1])
    assert len(ss) == 1 and ss[0].current and ss[0].version == 0
    assert count_versions(p, 2) == 3 and count_versions(p, 1) == 1 and count_versions(p, 5) == 0
    # 이걸로 쓰기: 위치·볼륨 그대로, 자르기 구간은 새 파일 안에서
    t = Take(file="takes/line_0002_v2.wav", subtitle=2, offset=-0.4, volume=0.7, trim_start=0.2, trim_end=1.5)
    use_version(t, "takes/line_0002_v1.wav", 1.0)
    assert (t.file, t.offset, t.volume, t.trim_start, t.trim_end) == ("takes/line_0002_v1.wav", -0.4, 0.7, 0.2, None)
    t = Take(file="x", trim_start=0.8, trim_end=0.9)
    use_version(t, "takes/line_0002_v3.wav", 0.5)
    assert t.trim_start == 0.0 and t.trim_end is None
    t = Take(file="x", trim_start=0.1, trim_end=0.3)
    use_version(t, "takes/line_0002_v3.wav", 0.5)
    assert (t.trim_start, t.trim_end) == (0.1, 0.3)


@pytest.fixture
def win(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    import composer.ui.main_window as mw
    make_media(tmp_path)
    _files(tmp_path)
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.asked = []
    w.answer = True
    w.confirm = lambda title, msg: (w.asked.append(msg) or w.answer)
    w.show()
    w.set_project(Project.from_dict({"clips": [{"file": "img1.png", "duration": 8}], "subtitle": {"file": "subs.srt"},
                                     "dubbing": {"takes": [{"file": "takes/line_0002_v2.wav", "subtitle": 2, "offset": -0.3,
                                                            "volume": 0.8, "trim_start": 0.1}]}}, base_dir=tmp_path), None)
    QCoreApplication.processEvents()
    yield w, tmp_path
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def test_history_list_use_play_delete(win, monkeypatch):
    w, d = win
    tp = w.inspector.take_page
    w.model.select("take", 0)
    assert w.inspector.stack.currentWidget() is tp and tp.history.isVisibleTo(w.inspector)
    rows = tp.history_rows
    assert [r["version"].version for r in rows] == [1, 2, 3] and tp.history.title() == "🎞 녹음 기록 (3개)"
    assert rows[1]["label"].text().startswith("✔ v2") and "2.0초" in rows[1]["label"].text() and "지금 사용" in rows[1]["label"].text()
    assert not rows[1]["use"].isEnabled() and not rows[1]["delete"].isEnabled()
    assert rows[0]["use"].isEnabled() and rows[0]["delete"].isEnabled()
    # ▶ 듣기
    played = []
    monkeypatch.setattr(w.dub, "play_file", lambda rel: played.append(rel))
    tp.play_file_requested.disconnect()
    tp.play_file_requested.connect(w.dub.play_file)
    rows[2]["play"].click()
    assert played == ["takes/line_0002_v3.wav"]
    # 이걸로 쓰기 → 위치·볼륨·자르기 그대로, 되돌리기 한 번
    rows[0]["use"].click()
    t = w.model.project.dubbing.takes[0]
    assert (t.file, t.offset, t.volume, t.trim_start) == ("takes/line_0002_v1.wav", -0.3, 0.8, 0.1)
    assert [r["version"].current for r in tp.history_rows] == [True, False, False]
    w.model.undo()
    assert w.model.project.dubbing.takes[0].file == "takes/line_0002_v2.wav"
    w.model.redo()
    w.model.undo()
    # 🗑 파일 삭제: 취소하면 그대로, 확인하면 지움
    w.model.select("take", 0)
    rows = tp.history_rows
    w.answer = False
    rows[2]["delete"].click()
    assert w.asked and "되돌릴 수 없습니다" in w.asked[-1] and (d / "takes" / "line_0002_v3.wav").exists()
    w.answer = True
    tp.history_rows[2]["delete"].click()
    assert not (d / "takes" / "line_0002_v3.wav").exists()
    assert [r["version"].version for r in tp.history_rows] == [1, 2]
    # 지금 쓰는 파일은 지울 수 없음 (확인 창도 안 뜸)
    n = len(w.asked)
    assert w.delete_take_file("takes/line_0002_v2.wav") is False and len(w.asked) == n
    assert (d / "takes" / "line_0002_v2.wav").exists() and "지울 수 없습니다" in w.statusBar().currentMessage()


def test_record_tab_shows_count_and_lock(win):
    w, d = win
    tbl = w.dub.table
    w.dub.refresh_table()
    texts = {tbl.item(r, 0).text(): tbl.item(r, 5).text() for r in range(tbl.rowCount())}
    assert texts == {"1": "", "2": "✔ 3개"}
    # 잠그면 녹음 기록도 못 바꿈
    w.timeline.header.buttons[("dubbing", "locked")].click()
    w.model.select("take", 0)
    tp = w.inspector.take_page
    assert not tp.isEnabled()
    assert tp.use_version(tp.history_rows[0]["version"]) is False
    assert w.delete_take_file("takes/line_0002_v1.wav") is False and (d / "takes" / "line_0002_v1.wav").exists()


def test_play_file_real(win):
    w, d = win
    assert w.dub.play_file("takes/line_0002_v1.wav") is True
    assert w.dub.play_file("takes/none.wav") is False
    w.dub.take_player.stop()
