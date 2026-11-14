"""자동 미리보기: 디바운스 / 자동 시작 / 취소 후 재시작 / 끄기 + 실제 렌더 교체."""
import time

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.ui.auto_preview import AutoPreview


class Fake:
    def __init__(self):
        self.key = 1
        self.started = []
        self.cancels = 0

    def make(self, delay=50):
        return AutoPreview(lambda: self.key, self.started.append, self._cancel, delay_ms=delay)

    def _cancel(self):
        self.cancels += 1


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def spin(ms):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def test_debounce_collapses_rapid_edits(app):
    f = Fake(); a = f.make(80)
    for k in range(2, 7):  # 빠르게 여러 번 편집
        f.key = k
        a.notify_changed()
        spin(20)
    assert f.started == [] and a.state() == "waiting"
    spin(150)
    assert f.started == [6]  # 마지막 상태 한 번만 렌더
    assert a.state() == "rendering"


def test_same_key_does_not_rerender(app):
    f = Fake(); a = f.make(30)
    a.request()
    a.on_finished(1, True)
    assert a.up_to_date
    a.notify_changed()  # 자막만 바뀐 경우처럼 key 동일
    spin(80)
    assert f.started == [1]


def test_edit_during_render_cancels_and_restarts(app):
    f = Fake(); a = f.make(30)
    a.request()
    f.key = 2
    a.notify_changed(); spin(80)  # 디바운스 후 request → 진행 중 렌더 취소
    assert f.cancels == 1 and a.pending
    a.on_finished(1, False)  # 취소됨
    assert f.started == [1, 2]
    a.on_finished(2, True)
    assert a.rendered_key == 2 and a.state() == "idle"


def test_stale_finish_ignored_and_change_after_finish_rerenders(app):
    f = Fake(); a = f.make(30)
    a.request()
    a.on_finished(99, True)  # 다른 렌더 결과
    assert a.rendering
    f.key = 3  # 렌더 중에 바뀌었지만 notify 가 늦게 옴
    a.on_finished(1, True)
    spin(80)
    assert f.started == [1, 3]


def test_disabled_does_nothing(app):
    f = Fake(); a = f.make(20)
    a.enabled = False
    f.key = 5
    a.notify_changed(); spin(60)
    assert f.started == []


def test_force_request_rerenders_same_key(app):
    f = Fake(); a = f.make(20)
    a.request(); a.on_finished(1, True)
    a.request(force=True)
    assert f.started == [1, 1]


# --------------------------------------------------------------------- 메인 창 통합
@pytest.fixture
def win(app, tmp_path):
    from PySide6.QtCore import QSettings
    QSettings("knhanul", "VideoComposer").setValue("view/auto_preview", True)
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.confirm = lambda *a, **k: True
    yield w, tmp_path
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _wait(pred, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_open_project_autorenders_and_edit_rerenders(win):
    from composer.schema import Clip, OutputSettings, Project
    from tests.media_fixtures import make_image
    w, tmp = win
    assert w.auto.enabled and w.act_auto.isChecked()
    make_image(tmp / "a.png", 320, 180, "red"); make_image(tmp / "b.png", 320, 180, "blue")
    p = Project(output=OutputSettings(width=320, height=180, fps=24), base_dir=tmp,
                clips=[Clip(file="a.png", type="image", duration=1.0), Clip(file="b.png", type="image", duration=1.0)])
    w.set_project(p, None)
    assert w.auto.rendering  # 열자마자 시작
    assert _wait(lambda: w.player.media_path is not None and not w.auto.busy)
    first = w.player.media_path
    assert w.preview_revision == w._render_revision_key()
    assert w.player.view.notice == "" and w.player.view.progress_text == ""

    # 편집 → 디바운스 후 다시 렌더 → 다른 파일로 교체
    w.auto.timer.setInterval(50)
    w.model.edit(lambda pr: setattr(pr.clips[0], "duration", 1.5))
    assert w.auto.state() == "waiting"
    assert "만드는 중" in w.player.view.notice or "다릅니다" in w.player.view.notice
    assert _wait(lambda: w.player.media_path != first and not w.auto.busy)
    assert w.preview_revision == w._render_revision_key()

    # 자막만 바꾸면 다시 렌더하지 않음
    w.model.edit(lambda pr: setattr(pr.subtitle, "offset", 0.3))
    assert not w.auto.busy

    # 보기 ▸ 자동 미리보기 끄기
    w.act_auto.setChecked(False)
    w.model.edit(lambda pr: setattr(pr.clips[1], "duration", 2.0))
    assert not w.auto.busy and "🔄" in w.player.view.notice


def test_refresh_button_and_progress_overlay(win):
    w, _ = win
    assert w.player.btn_refresh.text() == "🔄 미리보기 새로 만들기"
    w.player.set_progress("미리보기 만드는 중… 40%", 0.4)
    assert w.player.view.progress_text.endswith("40%") and "만드는 중" in w.player.btn_refresh.text()
    w.player.set_progress("", None)
    assert w.player.btn_refresh.text() == "🔄 미리보기 새로 만들기"
