"""● 선택 자막 녹음: 자막 끝에서 저절로 멈추지 않고 직접 끝낼 때까지(R·스페이스·■ 정지), 목소리가 자막보다 길면 자막 끝을 늘림
(다음 자막 앞까지만, 줄이지는 않음), 끝의 무음은 더빙 조각에서 잘라 냄, 되돌리기 한 번, 자막 트랙 잠금."""
import os

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from composer.dubbing.levels import speech_end
from composer.schema import Project
from composer.subtitles import project_cues, to_srt
from test_dubbing import fake_sd  # noqa: F401

SR = 48000


@pytest.fixture
def win(fake_sd, tmp_path):  # noqa: F811
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    from media_fixtures import make_image
    import composer.ui.main_window as mw
    make_image(tmp_path / "a.png")
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.ask_play_without_preview = lambda: "play"  # 미리보기 없이 재생 (창 안 띄움)
    w.show()
    w.set_project(Project.from_dict({
        "clips": [{"file": "a.png", "duration": 10}],
        "subtitle": {"items": [{"index": 1, "start": 1.0, "end": 2.0, "text": "하나"},
                               {"index": 2, "start": 4.0, "end": 5.0, "text": "둘"}]},
        "dubbing": {"latency_ms": 0}}, base_dir=tmp_path), None)
    w.dub.preroll.setValue(0.5)
    w.dub.auto_next.setChecked(False)
    QCoreApplication.processEvents()
    yield w, tmp_path
    w.dub.stop_recording()
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _feed(w, speech, silence):
    cb = w.dub.recorder._callback
    a = np.full((int(SR * speech), 1), 0.1, np.float32)
    cb(a, len(a), None, None)
    z = np.zeros((int(SR * silence), 1), np.float32)
    cb(z, len(z), None, None)


def _record(w, idx, speech, silence=1.0):
    w.model.select("subtitle", idx)
    w.dub.record_line()
    assert w.dub.mode == "line"
    _feed(w, speech, silence)
    w.dub.stop_recording()
    assert w.dub.mode is None


def _cue(w, idx):
    return next(c for c in w.model.cues if c.index == idx)


def test_speech_end_helper(tmp_path):
    import soundfile as sf
    x = np.concatenate([np.zeros(SR // 2), np.full(SR, 0.1), np.zeros(SR)]).astype(np.float32)
    sf.write(str(tmp_path / "a.wav"), x, SR)
    assert speech_end(tmp_path / "a.wav") == pytest.approx(1.5, abs=0.03)
    sf.write(str(tmp_path / "z.wav"), np.zeros(SR, np.float32), SR)
    assert speech_end(tmp_path / "z.wav") is None


def test_no_auto_stop_and_buttons_toggle(win):
    w, _ = win
    dub = w.dub
    assert not hasattr(dub, "postroll") and "끝난 뒤 여유" not in dub.opts.title
    w.model.select("subtitle", 1)
    assert w.handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_R, Qt.NoModifier))
    assert dub.mode == "line" and dub.btn_rec.text() == "■ 녹음 끝내기 (R)"
    assert "R·스페이스·■ 정지로 끝내기" in w.statusBar().currentMessage()
    _feed(w, 0.5, 0.0)
    dub.on_position(2.6)  # 예전에는 자막 끝 + 여유(2.5초)에서 저절로 멈춤
    dub.on_position(9.0)
    assert dub.mode == "line" and dub.recorder.is_recording
    assert w.handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_R, Qt.NoModifier))  # R 다시 = 끝내기
    assert dub.mode is None and dub.btn_rec.text() == "● 선택 자막 녹음 (R)"
    assert len(w.model.project.dubbing.takes) == 1
    # 버튼 다시 누르기 / 스페이스로도 끝남
    dub.btn_rec.click()
    assert dub.mode == "line"
    _feed(w, 0.3, 0.0)
    dub.btn_rec.click()
    assert dub.mode is None
    dub.btn_rec.click()
    _feed(w, 0.3, 0.0)
    assert w.handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
    assert dub.mode is None
    # 연속 녹음 버튼도 녹음 중엔 '끝내기'
    dub.btn_session.click()
    assert dub.btn_session.text() == "■ 연속 녹음 끝내기 (Shift+R)"
    _feed(w, 0.3, 0.0)
    dub.btn_session.click()
    assert dub.btn_session.text() == "● 여기부터 연속 녹음 (Shift+R)"


def test_extends_cue_when_voice_longer_and_undo(win):
    w, _ = win
    _record(w, 1, speech=2.0, silence=1.0)  # 0.5초부터 녹음 → 목소리 2.5초에 끝
    assert _cue(w, 1).end == pytest.approx(2.7, abs=0.03)  # 마지막 말소리 + 0.2초
    tk = w.model.project.dubbing.takes[0]
    assert tk.trim_end == pytest.approx(2.3, abs=0.03)  # 끝의 무음 잘라 냄 (파일은 3초 그대로)
    assert (w.model.project.base_dir / tk.file).exists()
    assert "1번 자막 끝을" in w.statusBar().currentMessage()
    # 미리보기·타임라인·SRT·렌더가 보는 자막도 같은 끝
    assert w.model.cue_timeline.current(2.6).index == 1
    assert project_cues(w.model.project)[0].end == pytest.approx(2.7, abs=0.03)
    assert "00:00:02,7" in to_srt(w.model.cues)
    # 되돌리기 한 번 = 녹음과 자막 늘리기 둘 다
    w.model.undo()
    assert _cue(w, 1).end == pytest.approx(2.0) and w.model.project.dubbing.takes == []
    w.model.redo()
    assert _cue(w, 1).end == pytest.approx(2.7, abs=0.03) and len(w.model.project.dubbing.takes) == 1


def test_no_change_when_voice_shorter(win):
    w, _ = win
    _record(w, 1, speech=0.8, silence=1.0)  # 목소리 1.3초에 끝 < 자막 끝 2.0초
    assert _cue(w, 1).end == pytest.approx(2.0) and _cue(w, 1).start == pytest.approx(1.0)
    assert w.model.project.dubbing.takes[0].trim_end == pytest.approx(1.1, abs=0.03)
    assert "늘렸어요" not in w.statusBar().currentMessage()


def test_clamp_to_next_cue(win):
    w, _ = win
    _record(w, 1, speech=4.0, silence=0.5)  # 목소리 4.5초에 끝 → 다음 자막(4.0초) 앞까지만
    assert _cue(w, 1).end == pytest.approx(3.95, abs=0.01)
    assert _cue(w, 2).start == pytest.approx(4.0) and _cue(w, 2).end == pytest.approx(5.0)
    assert "다음 자막과 겹치지 않게" in w.statusBar().currentMessage()


def test_subtitle_lock_keeps_cue(win):
    w, _ = win
    w.timeline.header.buttons[("subtitle", "locked")].click()
    _record(w, 1, speech=2.0)
    assert _cue(w, 1).end == pytest.approx(2.0)
    assert "잠겨 있어" in w.statusBar().currentMessage()
    assert len(w.model.project.dubbing.takes) == 1
