"""배경음악 제거: 버튼(⚙ 출력 설정) / 편집 메뉴 / 타임라인에서 배경음악 선택 후 Delete."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.schema import Project  # noqa: E402

ASK = "프로젝트에서 배경음악을 뺄까요? 원본 음악 파일은 지워지지 않습니다."


def _proj(folder):
    return Project.from_dict({"clips": [{"file": "vid1.mp4"}],
                              "bgm": {"file": "music.mp3", "volume": 0.4, "fade_in": 2.0}}, base_dir=folder)


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
    w.auto.timer.stop()
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _bgm_bars(w):
    w.timeline.canvas.grab()
    return [i for i in w.timeline.canvas._items() if i[0] == "bgm"]


def test_button_menu_confirm_undo_and_keep_file(win):
    w, folder, app = win
    page = w.inspector.project_page
    w.set_project(Project.from_dict({"clips": [{"file": "vid1.mp4"}]}, base_dir=folder))
    assert not page.btn_bgm_clear.isEnabled() and not w.act_clear_bgm.isEnabled()
    assert w.remove_bgm() is False and w.asked == []
    w.set_project(_proj(folder))
    assert page.btn_bgm_clear.text() == "배경음악 제거" and page.btn_bgm_clear.isEnabled()
    assert w.act_clear_bgm.text() == "배경음악 제거" and w.act_clear_bgm.isEnabled()
    # 편집 메뉴에서 '자막 전체 제거' 바로 옆
    edit = next(a.menu() for a in w.menuBar().actions() if a.text().startswith("편집"))
    acts = [a.text() for a in edit.actions()]
    assert acts.index("배경음악 제거") == acts.index("자막 전체 제거") + 1
    assert _bgm_bars(w)
    # 취소
    w.answer = False
    page.btn_bgm_clear.click()
    assert w.asked == [ASK] and w.model.project.bgm.file == "music.mp3" and not w.model.dirty
    # 확인 (버튼)
    w.answer = True
    page.btn_bgm_clear.click()
    assert w.model.project.bgm.file is None and w.model.dirty
    assert (folder / "music.mp3").exists()  # 파일은 절대 지우지 않음
    assert not _bgm_bars(w) and page.bgm_file.text() == "-"
    assert not page.btn_bgm_clear.isEnabled() and not w.act_clear_bgm.isEnabled()
    assert "배경음악을 뺐습니다" in w.statusBar().currentMessage()
    assert '"file": null' in w.model.project.to_json() or w.model.project.to_dict()["bgm"].get("file") is None
    # 한 번에 되돌리기 (볼륨 등 설정도 그대로)
    w.model.undo()
    b = w.model.project.bgm
    assert (b.file, b.volume, b.fade_in) == ("music.mp3", 0.4, 2.0) and w.act_clear_bgm.isEnabled()
    # 메뉴에서
    w.act_clear_bgm.trigger()
    assert w.model.project.bgm.file is None and len(w.asked) == 3


def test_delete_key_on_selected_bgm_bar(win):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    w, folder, app = win
    w.set_project(_proj(folder))
    w.model.select("bgm")
    assert w.handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert w.asked == [ASK] and w.model.project.bgm.file is None and w.model.selection.kind == ""
    w.model.undo()
    assert w.model.project.bgm.file == "music.mp3"


def test_removal_triggers_auto_preview(win):
    w, folder, app = win
    w.set_project(_proj(folder))
    w.auto.enabled = True
    w.auto.rendered_key = w.auto.key_fn()  # 이미 미리보기가 있는 상태
    assert w.auto.state() == "idle"
    w.remove_bgm()
    assert w.auto.state() == "waiting"  # 디바운스 후 다시 렌더
    w.auto.timer.stop()
    w.auto.enabled = False
