"""화면 없이(offscreen) UI 를 띄워 보는 스모크 테스트."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_main_window_flow(app, tmp_path):
    from media_fixtures import make_media

    from composer.schema import Project
    from composer.ui.main_window import MainWindow
    from composer.ui.prompt_dialog import PromptDialog

    make_media(tmp_path)
    w = MainWindow()
    w.show()
    p = Project.from_dict({"output": {"width": 640, "height": 360},
                           "clips": [{"file": "img1.png", "duration": 2}, {"file": "vid1.mp4"}],
                           "subtitle": {"file": "subs.srt"}, "bgm": {"file": "music.mp3"},
                           "dubbing": {"takes": [{"file": "take1.wav", "subtitle": 1}]}}, base_dir=tmp_path)
    w.set_project(p)
    for kind, idx in [("clip", 0), ("clip", 1), ("subtitle", 1), ("take", 0), ("bgm", 0)]:
        w.model.select(kind, idx)
        app.processEvents()
    # 미세 조정: 자막 0.1초 늦추기 → 실행 취소
    w.model.select("subtitle", 2)
    w.nudge_selected(0.1)
    assert w.model.cue_by_index(2).start == pytest.approx(3.1)
    w.model.undo()
    assert w.model.cue_by_index(2).start == pytest.approx(3.0)
    # 클립 길이 수정이 타임라인에 반영
    w.model.select("clip", 0)
    w.inspector.clip_page.duration.spin.setValue(2.5)
    assert w.model.timeline.clips[0].duration == pytest.approx(2.5)
    # 카운트다운 표시
    w.dub.on_position(0.2)
    assert "0.3초 후 표시" in w.dub.lbl_next_time.text()
    w.dub.on_position(1.0)
    assert "1.0초 후 사라짐" in w.dub.lbl_cur_time.text()
    assert w.dub.table.rowCount() == 2 and w.dub.table.item(0, 5).text() == "✔"
    # 타임라인 그리기
    w.timeline.canvas.grab()
    # 저장/불러오기
    path = tmp_path / "proj.json"
    w.model.save(path)
    w.load_path(path)
    assert w.model.project.clips[0].duration == pytest.approx(2.5)
    # AI 프롬프트 대화상자
    dlg = PromptDialog(tmp_path)
    assert "img1.png" in dlg.prompt.toPlainText()
    dlg.answer.setPlainText('설명\n```json\n{"clips":[{"file":"img1.png","duration":2,}]}\n```')
    dlg.check()
    assert dlg.b_open.isEnabled()
    dlg.answer.setPlainText('{"clips":[{"file":"nope.png","duration":2}]}')
    dlg.check()
    assert not dlg.b_open.isEnabled() and "nope.png" in dlg.fix_text
    w.model._set_dirty(False)
    w.close()
    dlg.deleteLater()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_render_worker_thread(app, tmp_path):
    from media_fixtures import make_media

    from composer.render.ffmpeg_builder import RenderSettings
    from composer.schema import Project
    from composer.ui.render_dialog import RenderWorker

    make_media(tmp_path)
    p = Project.from_dict({"output": {"width": 320, "height": 180},
                           "clips": [{"file": "img1.png", "duration": 1, "motion": "zoom_out"},
                                     {"file": "vid1.mp4", "transition": {"type": "crossfade", "duration": 0.3}}]},
                          base_dir=tmp_path)
    w = RenderWorker(p, tmp_path / "prev.mp4", RenderSettings(draft=True, encoder="auto"))
    got = []
    w.finished_ok.connect(lambda r: got.append(r))
    w.failed.connect(lambda m, log: got.append(("fail", m, log)))
    w.start()
    assert w.wait(120000)
    app.processEvents()
    assert got and not isinstance(got[0], tuple), got
    assert (tmp_path / "prev.mp4").exists()
