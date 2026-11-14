"""AI 대화상자의 '이 프로젝트 열기': 저장 창 없이 저장 안 된 프로젝트로 열기."""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from media_fixtures import make_media

    make_media(tmp_path)
    dialogs = []
    # 어떤 저장/질문 창도 뜨면 기록 (뜨면 안 되는 상황 검사용)
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        lambda *a, **k: dialogs.append(("save", a)) or ("", ""))
    monkeypatch.setattr(mw.QMessageBox, "question", lambda *a, **k: dialogs.append(("question", a)) or mw.QMessageBox.No)
    w = mw.MainWindow()
    w.asked = []
    w.ask_save_changes = lambda box: w.asked.append(box.text()) or w.answer
    w.answer = mw.QMessageBox.Cancel
    w.dialogs = dialogs
    w.show()
    app.processEvents()
    yield w, mw, tmp_path
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _ai_project(folder):
    from composer.schema import Project

    return Project.from_dict({"clips": [{"file": "img1.png", "duration": 2}, {"file": "vid1.mp4"}]}, base_dir=Path("."))


def test_ai_project_opens_unsaved_without_dialogs(win):
    w, mw, folder = win
    (folder / "project.json").write_text("{}", encoding="utf-8")  # 이미 있어도 덮어쓰기 질문 없음
    w.model.mark_modified()  # 비어 있는 제목 없는 프로젝트는 변경돼도 묻지 않음
    w._project_from_ai(_ai_project(folder), folder)
    assert w.dialogs == [] and w.asked == []
    assert w.model.path is None and w.model.dirty
    assert w.model.project.base_dir == folder
    assert [c.file for c in w.model.project.clips] == ["img1.png", "vid1.mp4"]
    assert w.windowTitle().endswith("AI 프로젝트 *")
    assert w.statusBar().currentMessage() == "AI 프로젝트를 열었습니다. F5 로 미리보기를 만드세요. 저장하려면 Ctrl+S"
    assert (folder / "project.json").read_text(encoding="utf-8") == "{}"  # 자동 저장하지 않음


def test_save_unsaved_project_defaults_to_media_folder(win, monkeypatch):
    w, mw, folder = win
    w._project_from_ai(_ai_project(folder), folder)
    seen = []
    target = folder / "project.json"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        lambda parent, title, default, flt: seen.append((title, default)) or (str(target), flt))
    assert w.save()
    assert seen == [("프로젝트 저장", str(target))]
    assert w.model.path == target and not w.model.dirty and target.exists()
    assert w.windowTitle().endswith("project.json")
    # 이후 Ctrl+S 는 창 없이 바로 저장
    seen.clear()
    w.model.project.clips[0].duration = 3
    w.model.commit()
    assert w.save() and seen == []


def test_single_question_when_current_project_has_changes(win):
    w, mw, folder = win
    from composer.schema import Project

    w.set_project(Project.from_dict({"clips": [{"file": "img2.jpg", "duration": 1}]}, base_dir=folder))
    w.model.project.clips[0].duration = 2
    w.model.commit()
    # 취소 → 그대로
    w.answer = mw.QMessageBox.Cancel
    w._project_from_ai(_ai_project(folder), folder)
    assert len(w.asked) == 1 and "저장할까요" in w.asked[0]
    assert [c.file for c in w.model.project.clips] == ["img2.jpg"]
    # 저장 안 함 → AI 프로젝트로 교체, 다른 창 없음
    w.answer = mw.QMessageBox.Discard
    w._project_from_ai(_ai_project(folder), folder)
    assert len(w.asked) == 2 and w.dialogs == []
    assert [c.file for c in w.model.project.clips] == ["img1.png", "vid1.mp4"] and w.model.path is None
