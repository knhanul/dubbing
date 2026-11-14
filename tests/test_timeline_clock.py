"""회귀 테스트: 미리보기 영상이 없어도 재생선·시간 표시·자막·더빙 카운트다운이 같은 타임라인 시계를 따라야 함."""
import os
import subprocess

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def win(tmp_path):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from media_fixtures import make_media

    import composer.ui.main_window as mw
    from composer.probe import ffmpeg_path

    make_media(tmp_path)
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=20",
                    "-pix_fmt", "yuv420p", str(tmp_path / "long.mp4")], check=True)
    (tmp_path / "long.srt").write_text(
        "1\n00:00:01,500 --> 00:00:03,000\n첫줄\n\n2\n00:00:06,000 --> 00:00:08,000\n둘째\n\n"
        "3\n00:00:22,000 --> 00:00:23,000\n셋째\n", encoding="utf-8")
    w = mw.MainWindow()
    w.asked = []
    w.choice = "play"
    w.ask_play_without_preview = lambda: w.asked.append(1) or w.choice
    w.show()
    app.processEvents()
    yield w, tmp_path, app
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _project(folder):
    from composer.schema import Project

    # 클립 두 개 → 미리보기 렌더가 필요한 프로젝트 (렌더 전 상태)
    return Project.from_dict({"output": {"width": 640, "height": 360}, "transition": {"type": "none"},
                              "clips": [{"file": "long.mp4"}, {"file": "img1.png", "duration": 5}],
                              "subtitle": {"file": "long.srt"}}, base_dir=folder)


def _consistent(w, t, tol=0.12):
    """재생선, 시간 표시, 자막 오버레이, 더빙 패널이 모두 같은 시각 t 를 보고 있는지."""
    from composer.ui.preview import fmt_time

    assert w.player.position() == pytest.approx(t, abs=tol)
    assert w.timeline.canvas.playhead == pytest.approx(t, abs=tol)
    assert w.player.lbl_time.text().endswith(fmt_time(25.0))  # 길이 = 타임라인 전체(20 + 5)
    st = w.model.cue_timeline.status(w.player.position())
    cur = st["current"]
    assert w.dub.lbl_cur.text() == (cur.text if cur else "—")
    assert w.player.view.text == (cur.text if cur else "")
    nxt = st["next"]
    if nxt:
        assert w.dub.lbl_next.text().startswith(f"{nxt.index}.")
        shown = float(w.dub.lbl_next_time.text().split("⏳ ")[1].split("초")[0])
        assert shown == pytest.approx(nxt.start - t, abs=tol + 0.05)


def test_seek_without_preview_moves_everything(win):
    w, folder, app = win
    w.set_project(_project(folder))
    assert w.player.media_path is None and not w.player.has_media()
    assert "미리보기 영상 없음" in w.player.view.notice
    w.timeline.canvas.seek_requested.emit(7.0)  # 타임라인 클릭
    app.processEvents()
    _consistent(w, 7.0)
    assert w.dub.lbl_cur.text() == "둘째" and "1.0초 후 사라짐" in w.dub.lbl_cur_time.text()
    assert w.dub.lbl_next.text().startswith("3.")
    # 사용자가 본 버그: 재생선은 54초인데 패널은 1번 자막 카운트다운 → 이제 같이 움직임
    w.player.seek(21.0)
    _consistent(w, 21.0)
    assert w.dub.lbl_cur.text() == "—" and w.dub.lbl_next.text().startswith("3.")
    assert w.dub.lbl_cue.text() == "1"  # 1초 전 큰 카운트다운
    w.player.seek(99)  # 끝을 넘으면 끝으로
    assert w.player.position() == pytest.approx(25.0)


def test_play_without_preview_asks_once_and_runs_timeline_clock(win):
    from PySide6.QtTest import QTest

    w, folder, app = win
    w.set_project(_project(folder))
    w.player.seek(5.5)
    w.choice = "cancel"
    w.player.btn_play.click()
    assert w.asked == [1] and not w.player.is_playing()
    w.choice = "play"
    w.player.toggle()  # Space 와 같은 동작
    assert w.asked == [1, 1] and w.player.is_playing()
    assert "일시정지" in w.player.btn_play.text()
    QTest.qWait(900)
    t = w.player.position()
    assert 6.2 < t < 7.2  # 시계가 실제로 흐름
    app.processEvents()
    QTest.qWait(60)
    _consistent(w, w.player.position(), tol=0.2)
    assert w.dub.lbl_cur.text() == "둘째"
    w.player.pause()
    assert not w.player.is_playing()
    paused = w.player.position()
    QTest.qWait(300)
    assert w.player.position() == pytest.approx(paused)
    w.player.play()  # 두 번째부터는 묻지 않음
    assert w.asked == [1, 1] and w.player.is_playing()
    w.player.pause()
    # 끝에 닿으면 자동 정지
    w.player.seek(24.8)
    w.player.play()
    QTest.qWait(500)
    assert not w.player.is_playing() and w.player.position() == pytest.approx(25.0)


def test_placeholder_shows_current_clip_picture(win):
    w, folder, app = win
    w.set_project(_project(folder))
    w.player.seek(3.0)  # 영상 클립 → ffmpeg 로 뽑은 썸네일
    key1 = w.player._placeholder_key
    assert key1 and "long.mp4" in key1[0]
    assert not w.player.placeholder.pixmap().isNull() and w.player.placeholder.isVisible()
    assert list((folder / ".composer_cache" / "thumbs").glob("*.jpg"))
    w.player.seek(22.0)  # 이미지 클립
    assert "img1.png" in w.player._placeholder_key[0] and not w.player.placeholder.pixmap().isNull()
    assert not w.player.video_item.isVisible()


def test_render_choice_starts_preview_render(win, monkeypatch):
    from PySide6.QtTest import QTest

    w, folder, app = win
    w.set_project(_project(folder))
    called = []
    monkeypatch.setattr(w, "render_preview", lambda: called.append(1))
    w.choice = "render"
    w.player.play()
    QTest.qWait(50)
    assert called == [1] and not w.player.is_playing()


def test_direct_video_still_follows_media(win):
    from PySide6.QtTest import QTest

    from composer.schema import Project

    w, folder, app = win
    w.set_project(Project.from_dict({"clips": [{"file": "long.mp4"}], "subtitle": {"file": "long.srt"}},
                                    base_dir=folder))
    assert w.player.has_media()
    QTest.qWait(400)
    w.player.seek(6.5)
    QTest.qWait(300)
    assert w.timeline.canvas.playhead == pytest.approx(w.player.position(), abs=0.1)
    assert w.dub.lbl_cur.text() == "둘째"
    assert w.asked == []
