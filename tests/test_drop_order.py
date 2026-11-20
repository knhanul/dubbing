"""여러 파일을 넣을 때 '순서 정하기' 창: 클릭한 순서대로 번호 → 그 순서로 넣기."""
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from composer.importer import classify_paths
from tests.media_fixtures import _ff, make_image


@pytest.fixture
def files(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))  # 썸네일 캐시: Linux·macOS
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))  # Windows
    d = tmp_path / "m"
    make_image(d / "img1.png", 320, 180, "red")
    make_image(d / "img2.png", 320, 180, "green")
    make_image(d / "img10.png", 320, 180, "blue")
    _ff("-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "clip.mp4"))
    return d


def _click(dlg, path):
    it = dlg.item_for(path)
    r = dlg.grid.visualItemRect(it)
    QTest.mouseClick(dlg.grid.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())


def _nums(dlg):
    from composer.ui.order_dialog import NUM_ROLE
    return {Path(it.data(Qt.UserRole)).name: it.data(NUM_ROLE) for it in dlg.items()}


def test_classify_keeps_given_order(files):
    given = [files / "img10.png", files / "clip.mp4", files / "img1.png"]
    plan = classify_paths(given)
    assert [p.name for p in plan.media] == ["clip.mp4", "img1.png", "img10.png"]  # 이름순
    assert plan.media_given == given  # 놓은 순서 그대로


def make_dialog(files, dropped=None):
    from composer.ui.order_dialog import OrderDialog
    plan = classify_paths(dropped or [files / "img10.png", files / "img2.png", files / "clip.mp4", files / "img1.png"])
    dlg = OrderDialog(plan.media, plan.media_given)
    dlg.resize(900, 600)
    dlg.show()
    QCoreApplication.processEvents()
    return dlg


def test_click_order_badges_and_renumber(files):
    dlg = make_dialog(files)
    names = [p.name for p in dlg.screen_order()]
    assert names == ["clip.mp4", "img1.png", "img2.png", "img10.png"]  # 화면은 처음에 이름순
    assert all(v == 0 for v in _nums(dlg).values())
    _click(dlg, files / "img10.png")
    _click(dlg, files / "clip.mp4")
    _click(dlg, files / "img2.png")
    assert _nums(dlg) == {"img10.png": 1, "clip.mp4": 2, "img2.png": 3, "img1.png": 0}
    assert "1. img10.png → 2. clip.mp4 → 3. img2.png → 4. img1.png" in dlg.summary.text()
    # 다시 클릭하면 빠지고 번호가 당겨짐
    _click(dlg, files / "clip.mp4")
    assert _nums(dlg) == {"img10.png": 1, "img2.png": 2, "clip.mp4": 0, "img1.png": 0}
    # 번호 없는 파일은 뒤에 화면 순서(이름순)대로
    assert [p.name for p in dlg.current_order()] == ["img10.png", "img2.png", "clip.mp4", "img1.png"]
    dlg.grab()  # 번호 배지 그리기 오류 없음
    dlg.b_ok.click()
    assert dlg.result() == QDialog.Accepted
    assert [p.name for p in dlg.result_order] == ["img10.png", "img2.png", "clip.mp4", "img1.png"]


def test_buttons_name_dropped_reset_cancel(files):
    dlg = make_dialog(files)
    _click(dlg, files / "img2.png")
    dlg.b_name.click()
    assert [p.name for p in dlg.result_order] == ["clip.mp4", "img1.png", "img2.png", "img10.png"]
    dlg = make_dialog(files)
    _click(dlg, files / "img2.png")
    dlg.b_dropped.click()
    assert [p.name for p in dlg.result_order] == ["img10.png", "img2.png", "clip.mp4", "img1.png"]
    dlg = make_dialog(files)
    _click(dlg, files / "img2.png"); _click(dlg, files / "img1.png")
    dlg.grid.insertItem(0, dlg.grid.takeItem(3))  # 끌어서 화면 순서 바꾸기
    dlg.b_reset.click()
    assert dlg.picked == [] and [p.name for p in dlg.screen_order()] == ["clip.mp4", "img1.png", "img2.png", "img10.png"]
    dlg.b_cancel.click()
    assert dlg.result() == QDialog.Rejected and dlg.result_order is None
    assert [b.text() for b in (dlg.b_ok, dlg.b_name, dlg.b_dropped, dlg.b_reset, dlg.b_cancel)] == \
        ["✔ 이 순서로 넣기", "이름순", "놓은 순서 그대로", "초기화", "취소"]
    assert dlg.b_ok.isDefault() and dlg.skip_next.text() == "다음부터 묻지 않고 이름순"


def test_drag_reorder_changes_unnumbered_order(files):
    dlg = make_dialog(files)
    dlg.grid.setCurrentRow(3)
    dlg.grid.model().moveRow(dlg.grid.rootIndex(), 3, dlg.grid.rootIndex(), 0) or \
        dlg.grid.insertItem(0, dlg.grid.takeItem(3))
    assert [p.name for p in dlg.screen_order()][0] == "img10.png"
    _click(dlg, files / "img2.png")
    assert [p.name for p in dlg.current_order()] == ["img2.png", "img10.png", "clip.mp4", "img1.png"]
    # 드래그 설정
    from PySide6.QtWidgets import QAbstractItemView
    assert dlg.grid.dragDropMode() == QAbstractItemView.InternalMove


def test_thumbnails_and_video_duration(files, tmp_path):
    from composer.thumbs import thumb_cache_dir
    from composer.ui.order_dialog import DUR_ROLE
    dlg = make_dialog(files)
    assert dlg.wait_thumbs()
    vid = dlg.item_for(files / "clip.mp4")
    assert vid.data(DUR_ROLE) == "3.0초"
    assert not vid.icon().isNull()
    assert thumb_cache_dir().is_relative_to(tmp_path / "cache")
    jpgs = list(thumb_cache_dir().glob("*.jpg"))
    assert len(jpgs) == 1  # 영상만 ffmpeg 로 (캐시)
    # 빨간 이미지 썸네일 색 확인
    img = dlg.item_for(files / "img1.png").icon().pixmap(176, 99).toImage()
    c = img.pixelColor(88, 50)
    assert c.red() > 200 and c.green() < 60
    # 두 번째로 열면 캐시 재사용
    mtime = jpgs[0].stat().st_mtime
    dlg2 = make_dialog(files)
    assert dlg2.wait_thumbs() and jpgs[0].stat().st_mtime == mtime


# ------------------------------------------------------------------ 메인 창
@pytest.fixture
def win(files):
    QSettings("knhanul", "VideoComposer").setValue("import/skip_order_dialog", False)
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.confirm = lambda *a, **k: True
    assert not w.image_defaults.skip_order.isChecked()
    yield w
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def test_drop_uses_chosen_order_at_drop_position_single_undo(win, files):
    w = win
    w.handle_drop([files / "img1.png"])  # 1개는 묻지 않음
    w.handle_drop([files / "img2.png"])
    asked = []

    def fake(media, given):
        asked.append(([p.name for p in media], [p.name for p in given]))
        return [files / "img10.png", files / "clip.mp4"]
    w.ask_media_order = fake
    before = [c.file for c in w.model.project.clips]
    w.handle_drop([files / "clip.mp4", files / "img10.png"], 1)
    assert asked == [(["clip.mp4", "img10.png"], ["clip.mp4", "img10.png"])]
    assert [c.file for c in w.model.project.clips] == ["img1.png", "img10.png", "clip.mp4", "img2.png"]
    w.model.undo()
    assert [c.file for c in w.model.project.clips] == before
    # 취소하면 아무것도 넣지 않음
    w.ask_media_order = lambda m, g: None
    w.handle_drop([files / "clip.mp4", files / "img10.png"])
    assert [c.file for c in w.model.project.clips] == before


def test_real_dialog_from_main_window_and_skip_next(win, files, monkeypatch):
    from composer.ui.order_dialog import OrderDialog
    w = win

    def run(dlg):
        dlg.show(); QCoreApplication.processEvents()
        _click(dlg, files / "img2.png"); _click(dlg, files / "img1.png")
        dlg.skip_next.setChecked(True)
        dlg.b_ok.click()
        return dlg.result()
    monkeypatch.setattr(OrderDialog, "exec", run)
    w.handle_drop([files / "img1.png", files / "img2.png", files / "img10.png"])
    assert [c.file for c in w.model.project.clips] == ["img2.png", "img1.png", "img10.png"]
    # '다음부터 묻지 않고 이름순' → 설정 저장, 다음엔 창 없이 이름순
    assert w.image_defaults.skip_order.isChecked()
    assert str(QSettings("knhanul", "VideoComposer").value("import/skip_order_dialog")).lower() == "true"
    monkeypatch.setattr(OrderDialog, "exec", lambda dlg: pytest.fail("묻지 않아야 함"))
    w.handle_drop([files / "img10.png", files / "img2.png"])
    assert [c.file for c in w.model.project.clips][-2:] == ["img2.png", "img10.png"]


def test_add_clips_multiselect_asks_order(win, files, monkeypatch):
    w = win
    sel = [files / "img10.png", files / "img1.png", files / "clip.mp4"]
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([str(x) for x in sel], "")))
    got = []
    w.ask_media_order = lambda m, g: (got.append([p.name for p in g]), list(reversed(m)))[1]
    w.add_clips()
    assert got == [["img10.png", "img1.png", "clip.mp4"]]  # 고른 순서가 '놓은 순서'
    assert [c.file for c in w.model.project.clips] == ["img10.png", "img1.png", "clip.mp4"]


def test_drop_event_is_processed_after_event_returns(win, files):
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent
    w = win
    calls = []
    w.ask_media_order = lambda m, g: (calls.append(1), m)[1]
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(files / "img1.png")), QUrl.fromLocalFile(str(files / "img2.png"))])
    ev = QDropEvent(QPointF(5, 5), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    w.dropEvent(ev)
    assert calls == [] and not w.model.project.clips  # 드롭 이벤트 안에서는 창을 띄우지 않음
    QApplication.processEvents()
    assert calls == [1] and len(w.model.project.clips) == 2
