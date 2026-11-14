"""녹음기/테이크 관리 테스트 (실제 마이크 대신 가짜 장치 사용)."""
import os
import types

import numpy as np
import pytest
import soundfile as sf

from composer.dubbing import recorder as rec_mod
from composer.dubbing.recorder import Recorder, peak_db
from composer.dubbing.takes import find_take, next_take_path, remove_take, set_take
from composer.schema import Project


class FakeStream:
    latency = 0.01

    def __init__(self, **kw):
        self.kw = kw

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_sd(monkeypatch):
    sd = types.SimpleNamespace(
        InputStream=FakeStream,
        query_devices=lambda: [{"name": "스피커", "max_input_channels": 0, "default_samplerate": 48000},
                               {"name": "USB 마이크", "max_input_channels": 1, "default_samplerate": 48000},
                               {"name": "USB 마이크", "max_input_channels": 1, "default_samplerate": 44100}],
        default=types.SimpleNamespace(device=(1, 0)),
    )
    monkeypatch.setattr(rec_mod, "_sd", lambda: sd)
    return sd


def test_list_devices_dedup(fake_sd):
    devs = rec_mod.list_input_devices()
    assert [d.name for d in devs] == ["USB 마이크"] and devs[0].is_default
    assert devs[0].label().startswith("★")


def test_recorder_writes_wav_and_meters(fake_sd, tmp_path):
    r = Recorder(samplerate=48000)
    r.open(1)
    block = (0.5 * np.sin(np.linspace(0, 100, 4800))).astype("float32").reshape(-1, 1)
    r._callback(block, len(block), None, None)  # 녹음 전 모니터링
    assert r.level_db > -7 and not r.is_recording
    r.start(tmp_path / "t" / "a.wav")
    for _ in range(10):
        r._callback(block, len(block), None, None)
    assert r.is_recording and r.recorded_seconds == pytest.approx(1.0)
    assert r.stop() == pytest.approx(1.0)
    data, sr = sf.read(tmp_path / "t" / "a.wav")
    assert sr == 48000 and len(data) == 48000
    r.close()
    assert not r.is_open
    assert peak_db(np.zeros((0, 1))) == -90.0


def test_take_versions_and_replace(tmp_path):
    p = Project(base_dir=tmp_path)
    rel, path = next_take_path(p, 3)
    assert rel == "takes/line_0003_v1.wav" and path.parent.is_dir()
    path.write_bytes(b"x")
    rel2, _ = next_take_path(p, 3)
    assert rel2.endswith("_v2.wav")  # 다시 녹음해도 이전 파일 보존
    set_take(p, 3, rel, -2.0)
    set_take(p, 3, rel2, -1.9)
    assert len(p.dubbing.takes) == 1 and find_take(p, 3).file == rel2
    s1 = set_take(p, None, "takes/session_001.wav", 10.0)
    set_take(p, None, "takes/session_002.wav", 20.0)
    assert len(p.dubbing.takes) == 3  # 세션 녹음은 계속 추가
    remove_take(p, s1)
    assert len(p.dubbing.takes) == 2


def test_record_line_flow_in_ui(fake_sd, tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from media_fixtures import make_media

    from composer.ui.main_window import MainWindow

    make_media(tmp_path)
    w = MainWindow()
    p = Project.from_dict({"clips": [{"file": "vid1.mp4"}], "subtitle": {"file": "subs.srt"}}, base_dir=tmp_path)
    w.set_project(p)
    assert w.preview_is_direct  # 영상 하나면 렌더 없이 원본 재생
    w.dub.preroll.setValue(2.0)
    w.model.select("subtitle", 2)  # 3.0초 자막
    w.dub.record_line()
    assert w.dub.mode == "line" and w.dub.recorder.is_recording
    block = np.full((48000, 1), 0.1, dtype="float32")
    w.dub.recorder._callback(block, len(block), None, None)
    w.dub.stop_recording()
    take = find_take(w.model.project, 2)
    assert take is not None and take.file == "takes/line_0002_v1.wav"
    assert take.offset == pytest.approx(-2.0, abs=1.0)
    assert (tmp_path / take.file).exists()
    assert w.model.dirty
    w.model._set_dirty(False)
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
