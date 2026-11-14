"""탐색기 드래그 앤 드롭과 클립 목록 순서 바꾸기 (offscreen, 가짜 QDropEvent)."""
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from composer.importer import classify_paths, describe_unsupported, insert_media, natural_key  # noqa: E402
from composer.schema import Project  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def _touch(folder: Path, *names):
    for n in names:
        p = folder / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return [folder / n for n in names]


def test_natural_sort():
    names = ["img10.png", "img2.png", "Img1.png", "img2b.png", "scene_003.mp4", "scene_1.mp4"]
    assert sorted(names, key=natural_key) == ["Img1.png", "img2.png", "img2b.png", "img10.png",
                                              "scene_1.mp4", "scene_003.mp4"]


def test_classify_files_and_folder(tmp_path):
    loose = _touch(tmp_path, "b10.png", "b2.jpg", "clip.MP4", "sub.srt", "song.flac", "readme.txt", "proj.json")
    d = tmp_path / "폴더"
    _touch(d, "02.png", "10.mp4", "1.webp", "inner/03.png", "takes/line.wav", ".composer_cache/c.mkv",
           "x_takes/s.wav", "music.m4a", "captions.vtt", "notes.docx", "other.json", ".hidden.png")
    plan = classify_paths(loose + [d, tmp_path / "없음.png"])
    assert [p.name for p in plan.media] == ["b2.jpg", "b10.png", "clip.MP4", "1.webp", "02.png", "10.mp4", "03.png"]
    assert [p.name for p in plan.subtitles] == ["sub.srt", "captions.vtt"]
    assert [p.name for p in plan.audio] == ["song.flac", "music.m4a"]  # takes/ 폴더의 녹음은 제외
    assert [p.name for p in plan.projects] == ["proj.json"]  # 폴더 안 json 은 무시
    assert sorted(p.name for p in plan.unsupported) == ["readme.txt", "없음.png"]
    assert plan.folders == [d]
    msg = describe_unsupported(plan.unsupported)
    assert "지원하지 않는 파일 2개" in msg and "readme.txt" in msg


def test_insert_media_positions(tmp_path):
    files = _touch(tmp_path, "a.png", "b.mp4", "c.png")
    p = Project(base_dir=tmp_path)
    assert insert_media(p, files[:2], None, 2.5, "zoom_out") == [0, 1]
    assert insert_media(p, files[2:], 1) == [1]
    assert [c.file for c in p.clips] == ["a.png", "c.png", "b.mp4"]
    assert (p.clips[0].type, p.clips[0].duration, p.clips[0].motion) == ("image", 2.5, "zoom_out")
    assert p.clips[2].type == "video" and p.clips[2].duration is None
    assert insert_media(p, files[:1], 99) == [3]  # 범위를 넘으면 끝


# ---------------------------------------------------------------------------- UI
@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw

    shown = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: shown.append(a[2]) or QMessageBox.Ok)
    from media_fixtures import make_media

    make_media(tmp_path)
    w = mw.MainWindow()
    w.shown_messages = shown
    w.image_defaults.image_duration.setValue(3.0)
    w.image_defaults.image_motion.setCurrentIndex(w.image_defaults.image_motion.findData("none"))
    w.show()
    app.processEvents()
    yield w
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _mime(paths):
    from PySide6.QtCore import QMimeData, QUrl

    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return m


_KEEP = []


def _events(x, y, mime):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent

    enter = QDragEnterEvent(QPoint(int(x), int(y)), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    move = QDragMoveEvent(QPoint(int(x), int(y)), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    drop = QDropEvent(QPointF(x, y), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    _KEEP.append(mime)  # 이벤트는 QMimeData 를 소유하지 않으므로 참조를 유지
    return enter, move, drop


def test_drop_on_empty_window_sets_folder_and_appends(win, tmp_path):
    imgs = [tmp_path / "img2.jpg", tmp_path / "img1.png", tmp_path / "vid1.mp4"]
    _, _, drop = _events(10, 10, _mime(imgs))
    win.dropEvent(drop)
    QApplication.processEvents()  # 드롭 처리는 이벤트가 끝난 뒤
    assert drop.isAccepted()
    p = win.model.project
    assert p.base_dir == tmp_path
    assert [c.file for c in p.clips] == ["img1.png", "img2.jpg", "vid1.mp4"]  # 자연 정렬
    assert p.clips[0].duration == 3.0 and p.clips[0].motion == "none"
    assert "클립 3개" in win.last_drop_report and win.model.dirty


def test_timeline_drop_inserts_at_position_with_indicator_and_undo(win, tmp_path):
    p = Project.from_dict({"clips": [{"file": "img1.png", "duration": 2}, {"file": "img2.jpg", "duration": 2}]},
                          base_dir=tmp_path)
    win.set_project(p)
    extra = _touch(tmp_path, "new10.png", "new9.png")
    canvas = win.timeline.canvas
    cr = canvas.track_rect("clip")
    x = canvas.x_of(2.5)  # 두 번째 클립(2~4초)의 앞쪽 절반 → 1번 자리
    enter, move, drop = _events(x, cr.center().y(), _mime(extra))
    canvas.dragEnterEvent(enter)
    assert enter.isAccepted() and canvas.drop_hint["index"] == 1
    assert canvas.drop_hint["x"] == pytest.approx(canvas.x_of(2.0))
    assert "클립 2개를 2번 자리에 삽입" in canvas.drop_hint["text"]
    canvas.dragMoveEvent(move)
    canvas.grab()  # 표시선 그리기
    canvas.dropEvent(drop)
    QApplication.processEvents()  # 드롭 처리는 이벤트가 끝난 뒤
    assert canvas.drop_hint is None
    files = [c.file for c in win.model.project.clips]
    assert files == ["img1.png", "new9.png", "new10.png", "img2.jpg"]
    assert win.model.selection.kind == "clip" and win.model.selection.index == 1
    # 한 번의 실행 취소로 모두 되돌림
    win.model.undo()
    assert [c.file for c in win.model.project.clips] == ["img1.png", "img2.jpg"]
    win.model.redo()
    assert len(win.model.project.clips) == 4
    # 드래그 취소 시 표시 제거
    enter2, _, _ = _events(x, cr.center().y(), _mime(extra))
    canvas.dragEnterEvent(enter2)
    canvas.dragLeaveEvent(None)
    assert canvas.drop_hint is None


def test_drop_subtitle_bgm_unsupported_and_confirm(win, tmp_path):
    p = Project.from_dict({"clips": [{"file": "img1.png", "duration": 2}]}, base_dir=tmp_path)
    win.set_project(p)
    other = _touch(tmp_path, "song2.wav", "memo.txt")
    win.handle_drop([tmp_path / "subs.srt", tmp_path / "music.mp3"])
    assert win.model.project.subtitle.file == "subs.srt" and win.model.project.bgm.file == "music.mp3"
    assert win.model.cues and "자막을 subs.srt" in win.last_drop_report
    asked = []
    win.confirm = lambda title, text: asked.append(text) or False
    win.handle_drop([other[0]])
    assert asked and "music.mp3" in asked[0] and win.model.project.bgm.file == "music.mp3"
    assert "그대로" in win.last_drop_report
    win.confirm = lambda title, text: True
    win.handle_drop([other[0], other[1]])
    assert win.model.project.bgm.file == "song2.wav"
    assert "지원하지 않는 파일 1개" in win.last_drop_report and "memo.txt" in win.last_drop_report
    assert win.shown_messages and "memo.txt" in win.shown_messages[-1]  # 한국어 안내 창
    # 저장한 JSON 에 반영
    path = tmp_path / "p.json"
    win.model.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["bgm"]["file"] == "song2.wav" and data["subtitle"]["file"] == "subs.srt"


def test_drop_json_opens_project(win, tmp_path):
    proj = {"clips": [{"file": "vid1.mp4"}, {"file": "img1.png", "duration": 5}]}
    (tmp_path / "open_me.json").write_text(json.dumps(proj), encoding="utf-8")
    win.handle_drop([tmp_path / "open_me.json"])
    assert win.model.path == tmp_path / "open_me.json"
    assert [c.file for c in win.model.project.clips] == ["vid1.mp4", "img1.png"]
    assert "프로젝트를 열었습니다" in win.last_drop_report


def test_drop_folder(win, tmp_path):
    d = tmp_path / "scenes"
    d.mkdir()
    import shutil

    shutil.copy(tmp_path / "img1.png", d / "s10.png")
    shutil.copy(tmp_path / "img1.png", d / "s2.png")
    shutil.copy(tmp_path / "vid1.mp4", d / "s1.mp4")
    win.handle_drop([d])
    assert [c.file for c in win.model.project.clips] == ["s1.mp4", "s2.png", "s10.png"]
    assert win.model.project.base_dir == d


def test_clip_reorder_by_timeline_drag(win, tmp_path):
    """🎞 클립 목록 탭은 없어졌고, 순서 바꾸기는 타임라인에서 끌어서 (되돌리기 한 번)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    p = Project.from_dict({"clips": [{"file": "img1.png", "duration": 1}, {"file": "img2.jpg", "duration": 1},
                                     {"file": "vid1.mp4"}], "transition": {"type": "none", "duration": 0}}, base_dir=tmp_path)
    win.set_project(p)
    c = win.timeline.canvas
    c.magnet = False
    rects = {i: r for k, i, r in c._items() if k == "clip"}
    src = rects[2].center().toPoint()
    dst = QPoint(int(rects[0].left()) + 3, src.y())
    QTest.mousePress(c, Qt.LeftButton, Qt.NoModifier, src)
    for x in range(src.x(), dst.x(), -12):
        QTest.mouseMove(c, QPoint(x, src.y()))
    QTest.mouseMove(c, dst)
    QTest.mouseRelease(c, Qt.LeftButton, Qt.NoModifier, dst)
    assert [x.file for x in win.model.project.clips] == ["vid1.mp4", "img1.png", "img2.jpg"]
    assert win.model.selection.kind == "clip" and win.model.selection.index == 0
    win.model.undo()
    assert [x.file for x in win.model.project.clips] == ["img1.png", "img2.jpg", "vid1.mp4"]
    assert not hasattr(win, "clip_panel")
