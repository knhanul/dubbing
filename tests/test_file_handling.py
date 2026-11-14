"""보통 프로그램 같은 파일 다루기: 새로 만들기 / 열기 / 최근 파일 / 저장 / 다른 이름으로 저장."""
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from tests.media_fixtures import make_image


@pytest.fixture
def win(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    # 폴더 선택 창이 뜨면 실패
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: pytest.fail("폴더 선택 창이 뜨면 안 됩니다")))
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.confirm = lambda *a, **k: True
    w.asked = []
    w.ask_save_changes = lambda box: (w.asked.append(box.text()), QMessageBox.Discard)[1]
    yield w, tmp_path, monkeypatch
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _actions(w):
    f = w.menuBar().actions()[0].menu()
    return f, [(a.text(), a.shortcut().toString()) for a in f.actions() if not a.isSeparator()]


def test_file_menu_layout(win):
    w, _, _ = win
    f, acts = _actions(w)
    names = [t for t, _ in acts]
    assert names[:5] == ["새로 만들기", "열기…", "최근 파일", "저장", "다른 이름으로 저장…"]
    assert names[-1] == "끝내기"
    sc = dict(acts)
    assert sc["새로 만들기"] == "Ctrl+N" and sc["열기…"] == "Ctrl+O"
    assert sc["저장"] == "Ctrl+S" and sc["다른 이름으로 저장…"] == "Ctrl+Shift+S"
    # "빈 프로젝트" 라는 말은 어디에도 없음
    every = []
    for top in w.menuBar().actions():
        every += [a.text() for a in top.menu().actions()]
    every += [a.text() for a in w.findChildren(type(f.actions()[0]))]
    assert not any("빈 프로젝트" in t for t in every)


def test_starts_untitled_with_empty_hint(win):
    w, _, _ = win
    QCoreApplication.processEvents()
    assert w.model.path is None and not w.model.project.clips
    assert w.windowTitle().startswith("제목 없음")
    hint = "이미지·동영상을 끌어다 놓거나 'AI 로 구성하기'로 시작하세요"
    assert w.player.view.empty_hint == hint
    assert w.timeline.canvas.empty_hint == hint
    w.timeline.canvas.repaint(); w.player.view.viewport().repaint()  # 그리기 오류 없음


def test_new_project_asks_only_when_modified_and_never_asks_folder(win):
    w, tmp, _ = win
    make_image(tmp / "m" / "a.png")
    w.handle_drop([tmp / "m" / "a.png"], None)
    assert w.player.view.empty_hint == ""  # 안내 사라짐
    assert w.model.dirty
    w.new_project()
    assert len(w.asked) == 1  # 저장할지 한 번만
    assert not w.model.project.clips and w.model.path is None
    assert w.windowTitle().startswith("제목 없음")
    assert w.player.view.empty_hint
    w.new_project()  # 빈 프로젝트는 묻지 않음
    assert len(w.asked) == 1


def test_add_clips_sets_base_dir_from_first_media(win):
    w, tmp, mp = win
    a = make_image(tmp / "media" / "img2.png"); b = make_image(tmp / "media" / "img10.png")
    mp.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *x, **k: ([str(b), str(a)], "")))
    w.add_clips()
    p = w.model.project
    assert Path(p.base_dir) == tmp / "media"
    assert [c.file for c in p.clips] == ["img2.png", "img10.png"]
    # 두 번째 추가는 작업 폴더를 바꾸지 않음
    c = make_image(tmp / "other" / "c.png")
    mp.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *x, **k: ([str(c)], "")))
    w.add_clips()
    assert Path(p.base_dir) == tmp / "media"
    assert p.clips[-1].file == "../other/c.png"


def test_first_save_rewrites_relative_paths(win):
    w, tmp, mp = win
    a = make_image(tmp / "media" / "a.png")
    (tmp / "media" / "s.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n안녕\n", encoding="utf-8")
    w.handle_drop([a, tmp / "media" / "s.srt"], None)
    target = tmp / "projects" / "my.json"
    target.parent.mkdir()
    seen = {}

    def fake_save(parent, title, default, flt):
        seen["default"] = default
        return str(target), ""
    mp.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_save))
    assert w.save()
    assert Path(seen["default"]) == tmp / "media" / "project.json"  # 미디어 폴더 / project.json 제안
    d = json.loads(target.read_text(encoding="utf-8"))
    assert d["clips"][0]["file"] == "../media/a.png"
    assert d["subtitle"]["file"] == "../media/s.srt"
    assert d["dubbing"]["folder"] == "takes"  # 녹음 없으면 JSON 옆 takes 그대로
    assert Path(w.model.project.base_dir) == target.parent
    assert w.model.project.resolve(d["clips"][0]["file"]).exists()
    assert w.windowTitle().startswith("my.json") and not w.model.dirty
    # 최근 파일에 등록
    assert w.recent_files()[0] == str(target)


def test_recent_files_menu_and_missing_file(win):
    w, tmp, mp = win
    a = make_image(tmp / "a.png")
    proj = tmp / "p1.json"
    proj.write_text(json.dumps({"output": {"width": 320, "height": 180, "fps": 24},
                                "clips": [{"file": "a.png", "type": "image", "duration": 1}]}), encoding="utf-8")
    w.load_path(proj)
    assert w.recent_files() == [str(proj)]
    w._fill_recent_menu()
    texts = [x.text() for x in w.recent_menu.actions() if not x.isSeparator()]
    assert "p1.json" in texts[0] and texts[-1] == "목록 지우기"
    w.new_project()
    assert not w.model.project.clips
    w.recent_menu.actions()[0].trigger()
    assert w.model.path == proj and len(w.model.project.clips) == 1

    # 없어진 파일은 알려 주고 목록에서 뺌
    gone = tmp / "gone.json"
    w.add_recent(gone)
    shown = []
    mp.setattr(QMessageBox, "information", staticmethod(lambda *x, **k: shown.append(x[2])))
    w.open_recent(str(gone))
    assert shown and str(gone) not in w.recent_files()
    assert w.model.path == proj
