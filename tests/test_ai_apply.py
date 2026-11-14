"""AI 로 구성하기: 붙여넣기 자동 검사 → ✔ 적용 → 현재 타임라인 교체(실행 취소 1회)."""
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.ai_apply import apply_ai_result, is_blank  # noqa: E402
from composer.schema import Project  # noqa: E402

AI = {"output": {"width": 1080, "height": 1920}, "transition": {"type": "fade", "duration": 0.4},
      "clips": [{"file": "img1.png", "duration": 2}, {"file": "vid1.mp4"}]}


def test_apply_logic_replaces_clips_keeps_dubbing_and_paths(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    cur = Project.from_dict({"output": {"width": 1920, "height": 1080, "fps": 24, "file": "mine.mp4"},
                             "clips": [{"file": "old.png", "duration": 1}],
                             "subtitle": {"file": "a.srt", "items": [{"index": 1, "start": 0, "end": 1, "text": "x"}],
                                          "style": {"size": 60}},
                             "bgm": {"file": "m.mp3", "volume": 0.5},
                             "dubbing": {"takes": [{"file": "takes/t.wav", "subtitle": 1}]}}, base_dir=proj_dir)
    new = apply_ai_result(cur, AI, media, keep_base=True)
    assert new.base_dir == proj_dir
    assert [c.file for c in new.clips] == ["../media/img1.png", "../media/vid1.mp4"]  # 프로젝트 폴더 기준으로 보정
    assert (new.output.width, new.output.height, new.output.fps, new.output.file) == (1080, 1920, 24, "mine.mp4")
    assert new.transition.type == "fade"
    assert new.subtitle.file == "a.srt" and new.subtitle.items  # AI 답변에 없으면 유지
    assert new.bgm.volume == 0.5 and new.dubbing.takes[0].file == "takes/t.wav"
    # 자막/음악이 답변에 있으면 교체 (새 자막 파일이면 미세 조정 목록 초기화, 스타일은 병합)
    new2 = apply_ai_result(cur, {**AI, "subtitle": {"file": "b.srt", "style": {"bold": False}}, "bgm": "n.mp3"},
                           proj_dir, keep_base=True)
    assert new2.subtitle.file == "b.srt" and new2.subtitle.items is None
    assert new2.subtitle.style.size == 60 and new2.subtitle.style.bold is False
    assert new2.bgm.file == "n.mp3" and new2.bgm.volume == 0.5
    # 빈 프로젝트는 AI 폴더를 작업 폴더로
    blank = Project(base_dir=Path("."))
    assert is_blank(blank, False) and not is_blank(blank, True) and not is_blank(cur, False)
    new3 = apply_ai_result(blank, AI, media, keep_base=False)
    assert new3.base_dir == media and new3.clips[0].file == "img1.png"


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from media_fixtures import make_media

    make_media(tmp_path)
    dialogs = []
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: dialogs.append("save") or ("", ""))
    monkeypatch.setattr(mw.QMessageBox, "question", lambda *a, **k: dialogs.append("question") or mw.QMessageBox.No)
    w = mw.MainWindow()
    w.ask_save_changes = lambda box: dialogs.append("ask") or mw.QMessageBox.Cancel
    w.dialogs = dialogs
    w.show()
    app.processEvents()
    yield w, mw, tmp_path, app
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_dialog_auto_validates_and_apply_button(win):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest

    from composer.ui.prompt_dialog import PromptDialog

    w, mw, folder, app = win
    dlg = PromptDialog(folder)
    texts = [b.text() for b in dlg.findChildren(type(dlg.b_apply))]
    assert "✔ 적용" in texts and not any("프로젝트 열기" in t or t == "검사하기" for t in texts)
    assert not dlg.b_apply.isEnabled() and dlg.status.text() == PromptDialog.IDLE
    dlg.answer.setPlainText("설명\n```json\n" + json.dumps(AI) + "\n```")
    assert not dlg.b_apply.isEnabled() and dlg.status.text() == "검사 중…"  # 아직 검사 전
    QTest.qWait(600)  # 0.4초 디바운스
    assert dlg.b_apply.isEnabled()
    assert dlg.status.text() == "✔ 문제 없음 — '적용'을 누르면 타임라인에 반영됩니다."
    # 편집하면 바로 비활성 → 오류면 계속 비활성
    dlg.answer.setPlainText('{"clips":[{"file":"없음.png","duration":2}]}')
    assert not dlg.b_apply.isEnabled()
    QTest.qWait(600)
    assert not dlg.b_apply.isEnabled() and dlg.status.text().startswith("✖ 오류 1개") and dlg.b_fix.isEnabled()
    # 클립보드 붙여넣기 버튼은 즉시 검사
    QGuiApplication.clipboard().setText(json.dumps(AI))
    dlg.paste_from_clipboard()
    assert dlg.b_apply.isEnabled()
    got = []
    dlg.apply_requested.connect(lambda d, f: got.append((d, f)))
    dlg.b_apply.click()
    assert got and got[0][1] == folder and dlg.result() == dlg.DialogCode.Accepted
    dlg.deleteLater()


def test_apply_replaces_timeline_keeps_path_single_undo(win):
    w, mw, folder, app = win
    path = folder / "mine.json"
    old = Project.from_dict({"clips": [{"file": "img2.jpg", "duration": 1}], "subtitle": {"file": "subs.srt"}},
                            base_dir=folder)
    w.set_project(old, None)
    w.model.save(path)
    w.model.project.clips[0].duration = 1.5  # 저장 안 한 변경이 있어도 질문 없음
    w.model.commit()
    w.apply_ai_result(AI, folder)
    assert w.dialogs == []
    assert [c.file for c in w.model.project.clips] == ["img1.png", "vid1.mp4"]
    assert w.model.path == path and w.model.dirty  # 같은 파일 유지
    assert w.model.project.subtitle.file == "subs.srt"
    assert w.windowTitle().startswith("mine.json *")
    assert "타임라인에 반영했습니다" in w.statusBar().currentMessage()
    assert "프로젝트 열기" not in w.statusBar().currentMessage()
    w.save()  # Ctrl+S → 저장 창 없이 같은 파일
    assert w.dialogs == [] and json.loads(path.read_text(encoding="utf-8"))["clips"][0]["file"] == "img1.png"
    # Ctrl+Z 한 번이면 이전 타임라인
    w.model.undo()
    assert [c.file for c in w.model.project.clips] == ["img2.jpg"]
    assert w.model.project.clips[0].duration == 1.5
    w.model.redo()
    assert len(w.model.project.clips) == 2


def test_apply_on_blank_project_is_unsaved_and_save_defaults(win, monkeypatch):
    w, mw, folder, app = win
    w.apply_ai_result(AI, folder)
    assert w.dialogs == [] and w.model.path is None and w.model.dirty
    assert w.model.project.base_dir == folder
    assert w.windowTitle().startswith("AI 프로젝트 *")
    assert w.statusBar().currentMessage().endswith("저장하려면 Ctrl+S")
    seen = []
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName",
                        lambda parent, title, default, flt: seen.append(default) or (default, flt))
    assert w.save() and seen == [str(folder / "project.json")]
    assert w.model.path == folder / "project.json"
    w.model.undo()
    assert w.model.project.clips == []


def test_menu_and_toolbar_texts(win):
    w, mw, folder, app = win
    texts = [a.text() for a in w.findChildren(mw.QAction)]
    assert "AI 로 구성하기…" in texts and "🤖 AI 로 구성하기" in texts
    assert not any("AI 프롬프트" in t for t in texts)
