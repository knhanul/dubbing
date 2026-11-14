"""자막 전체 제거 / Delete 키로 자막 한 줄 삭제."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.schema import Project  # noqa: E402
from composer.subtitles import clear_subtitles, detach_takes, has_subtitles, project_cues  # noqa: E402
from composer.timeline import take_position  # noqa: E402


def _proj(folder):
    return Project.from_dict({
        "clips": [{"file": "vid1.mp4"}],
        "subtitle": {"file": "subs.srt", "offset": 0.25},
        "dubbing": {"takes": [{"file": "take1.wav", "subtitle": 2, "offset": -1.0},
                              {"file": "take1.wav", "subtitle": None, "offset": 7.0}]},
    }, base_dir=folder)


def test_clear_subtitles_keeps_file_and_take_positions(tmp_path):
    from media_fixtures import make_media

    make_media(tmp_path)
    p = _proj(tmp_path)
    before = {c.index: c for c in project_cues(p)}
    pos_before = [take_position(t, before) for t in p.dubbing.takes]
    assert has_subtitles(p)
    assert clear_subtitles(p) == 1
    assert p.subtitle.file is None and p.subtitle.items is None and not has_subtitles(p)
    assert project_cues(p) == []
    assert (tmp_path / "subs.srt").exists()  # 디스크 파일은 그대로
    assert [take_position(t, {}) for t in p.dubbing.takes] == pytest.approx(pos_before)  # 3.0+0.25-1 = 2.25
    assert all(t.subtitle is None for t in p.dubbing.takes)
    # 일부만 고정
    p2 = _proj(tmp_path)
    assert detach_takes(p2, {99}) == 0 and detach_takes(p2, {2}) == 1


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from media_fixtures import make_media

    import composer.ui.main_window as mw

    make_media(tmp_path)
    w = mw.MainWindow()
    w.asked = []
    w.answer = True
    w.confirm = lambda title, text: w.asked.append(text) or w.answer
    w.show()
    app.processEvents()
    yield w, tmp_path, app
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_remove_all_button_menu_confirm_and_undo(win):
    w, folder, app = win
    page = w.inspector.sub_file_panel  # 🅰 자막 탭 맨 위
    w.set_project(Project.from_dict({"clips": [{"file": "vid1.mp4"}]}, base_dir=folder))
    assert not page.btn_sub_clear.isEnabled() and not w.act_clear_subs.isEnabled()
    assert w.remove_all_subtitles() is False and w.asked == []  # 자막 없으면 아무 일 없음
    w.set_project(_proj(folder))
    assert page.btn_sub_clear.isEnabled() and w.act_clear_subs.isEnabled()
    assert w.act_clear_subs.text() == "자막 전체 제거"
    # 취소하면 그대로
    w.answer = False
    page.btn_sub_clear.click()
    assert w.asked == ["프로젝트에서 자막을 모두 뺄까요? 원본 SRT 파일은 지워지지 않습니다."]
    assert w.model.project.subtitle.file == "subs.srt" and not w.model.dirty
    # 확인하면 제거 (메뉴에서)
    w.answer = True
    assert w._subtitle_at(1.0) == "안녕하세요"  # 미리보기 위에 그려지는 자막
    w.model.select("subtitle", 1)
    w.act_clear_subs.trigger()
    assert len(w.asked) == 2
    assert w.model.project.subtitle.file is None and w.model.cues == [] and w.model.dirty
    assert w._subtitle_at(1.0) == "" and w.player.view.text == ""  # 미리보기 자막 사라짐
    assert w.model.selection.kind == ""
    assert w.dub.table.rowCount() == 0
    assert not page.btn_sub_clear.isEnabled() and not w.act_clear_subs.isEnabled()
    assert (folder / "subs.srt").exists()
    w.timeline.canvas.grab()
    assert not [i for i in w.timeline.canvas._items() if i[0] == "subtitle"]  # 자막 트랙 비움
    assert "자막을 모두 뺐습니다" in w.statusBar().currentMessage()
    # 한 번의 실행 취소로 복원 (테이크 연결도 복원)
    w.model.undo()
    assert w.model.project.subtitle.file == "subs.srt" and len(w.model.cues) == 2
    assert w.model.project.dubbing.takes[0].subtitle == 2
    w.model.redo()
    assert w.model.project.subtitle.file is None


def test_delete_key_removes_selected_subtitle_line(win):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    w, folder, app = win
    w.set_project(_proj(folder))
    w.model.select("subtitle", 2)
    w.timeline.canvas.setFocus()
    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
    assert w.handle_key(ev)
    assert [c.index for c in w.model.cues] == [1]
    assert w.model.project.dubbing.takes[0].subtitle is None  # 그 줄의 테이크는 위치 고정
    assert w.model.project.dubbing.takes[0].offset == pytest.approx(2.25)
    assert (folder / "subs.srt").exists()
    w.model.undo()
    assert [c.index for c in w.model.cues] == [1, 2]
