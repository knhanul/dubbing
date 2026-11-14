"""녹음 입력 소리 보이기: 레벨 계산 · 스레드 안전 전달 · 무음 경고 · 화면(미터/파형/● REC) · 타임라인 더빙 파형."""
import os
import threading

import numpy as np
import pytest
import soundfile as sf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.dubbing import levels as lv  # noqa: E402
from composer.dubbing import recorder as rec_mod  # noqa: E402
from composer.dubbing.recorder import Recorder  # noqa: E402
from composer.schema import Project  # noqa: E402
from test_dubbing import fake_sd  # noqa: E402,F401


def _sine(amp, sec=0.1, sr=48000):
    t = np.arange(int(sr * sec)) / sr
    return (amp * np.sin(2 * np.pi * 440 * t)).astype("float32").reshape(-1, 1)


def test_block_levels_zones_and_clip():
    rms, peak, clip = lv.block_levels(_sine(0.5))
    assert peak == pytest.approx(-6.02, abs=0.05) and rms == pytest.approx(-9.03, abs=0.1) and not clip
    assert lv.block_levels(_sine(1.0))[2]  # 꽉 찬 소리 = 클리핑
    assert lv.block_levels(np.zeros((0, 1)))[:2] == (-90.0, -90.0)
    assert [lv.level_zone(d) for d in (-40, -12, -3)] == ["green", "yellow", "red"]


def test_tap_wave_silence_and_threads():
    tap = lv.LevelTap(48000)
    tap.feed(_sine(0.3, 0.1))  # 0.1초 = 10ms 피크 10개
    w = tap.take_wave()
    assert len(w) == 10 and max(w) == pytest.approx(0.3, abs=0.01)
    assert tap.take_wave() == []  # 새 것만
    for _ in range(31):  # 녹음 중 3.1초 무음
        tap.feed(np.zeros((4800, 1), np.float32), counting=True)
    assert tap.silent_seconds == pytest.approx(3.1)
    tap.feed(_sine(0.3), counting=True)
    assert tap.silent_seconds == 0.0  # 소리 들어오면 바로 0
    # 오디오 스레드에서 쏟아부어도 화면 쪽 꺼내기가 깨지지 않음
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            tap.feed(_sine(0.2, 0.01))
    th = threading.Thread(target=pump)
    th.start()
    got = 0
    for _ in range(200):
        got += len(tap.take_wave())
        tap.snapshot()
    stop.set(); th.join()
    assert got > 0


def test_recorder_feeds_tap(fake_sd, tmp_path):  # noqa: F811
    r = Recorder()
    r.open(1)
    r._callback(_sine(1.0), 4800, None, None)
    s = r.tap.snapshot()
    assert s["clip_count"] == 1 and s["peak_db"] > -0.1 and s["silent_s"] == 0
    r.start(tmp_path / "a.wav")
    for _ in range(35):
        r._callback(np.zeros((4800, 1), np.float32), 4800, None, None)
    assert r.tap.silent_seconds == pytest.approx(3.5)
    r.stop()
    r.close()
    assert r.tap.snapshot()["peak_db"] == -90.0


@pytest.fixture
def win(fake_sd, tmp_path):  # noqa: F811
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    from composer.ui.main_window import MainWindow
    make_media(tmp_path)
    w = MainWindow()
    w.show()
    w.set_project(Project.from_dict({"clips": [{"file": "vid1.mp4"}], "subtitle": {"file": "subs.srt"}},
                                    base_dir=tmp_path))
    yield w, app, tmp_path
    w.dub.stop_recording()
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_mic_test_meter_and_rec_indicator(win):
    w, app, d = win
    dub = w.dub
    st, fst = dub.strip, dub.free_page.strip
    dub.recorder.close()
    dub._meter()
    assert not dub.btn_test.isChecked() and not st.meter.active and "대기" in st.rec.text()
    # 🎧 마이크 테스트 → 녹음 없이 미터·파형
    dub.btn_test.setChecked(True)
    assert dub.recorder.is_open and not dub.recorder.is_recording
    assert dub.dev.count() >= 1 and dub.dev.currentText().startswith("★ USB 마이크")
    dub.recorder._callback(_sine(0.3), 4800, None, None)
    dub._meter()
    for s in (st, fst):  # 두 탭 모두
        assert s.meter.active and s.meter.zone == "yellow" and len(s.wave.values) == 10
        assert "소리 확인 중" in s.rec.text() and "USB 마이크" in s.dev_label.text()
        s.grab()
    dub.recorder._callback(_sine(1.0), 4800, None, None)
    dub._meter()
    assert st.meter.clipping and st.meter.zone == "red"
    dub.btn_test.setChecked(False)
    assert not dub.recorder.is_open


def test_recording_rec_blink_and_silence_warning(win):
    w, app, d = win
    dub = w.dub
    msgs = []
    dub.status.connect(msgs.append)
    w.model.select("subtitle", 2)
    dub.record_line()
    assert dub.mode == "line"
    dub.recorder._callback(_sine(0.3, 0.2), 9600, None, None)
    dub._meter()
    st = dub.strip
    assert "REC" in st.rec.text() and "00:00.2" in st.rec.text() and st.wave.recording
    assert st.rec.blink_on and not dub.btn_test.isEnabled()
    dub.recorder._callback(np.zeros((158400, 1), np.float32), 158400, None, None)  # 3.3초 무음
    dub._meter()
    assert not st.rec.blink_on and "00:03.5" in st.rec.text()  # 3.5초 → 깜빡 꺼진 차례
    assert not st.warn.isHidden() and st.warn.text().endswith(lv.SILENCE_WARNING)
    assert msgs.count("⚠ " + lv.SILENCE_WARNING) == 1
    dub._meter()
    assert msgs.count("⚠ " + lv.SILENCE_WARNING) == 1  # 한 번만 알림
    dub.recorder._callback(_sine(0.3), 4800, None, None)
    dub._meter()
    assert st.warn.isHidden()  # 소리 들어오면 사라짐
    dub.stop_recording()
    dub._meter()
    assert "REC" not in st.rec.text()


def test_free_record_strip_and_timeline_take_wave(win):
    w, app, d = win
    dub = w.dub
    dub.mode_tabs.setCurrentIndex(1)
    dub.free_page.toggle_record()
    assert dub.mode == "free"
    dub.recorder._callback(np.zeros((48000 * 4, 1), np.float32), 48000 * 4, None, None)
    dub._meter()
    assert not dub.free_page.strip.warn.isHidden() and "REC" in dub.free_page.strip.rec.text()
    dub.recorder._callback(_sine(0.5, 1.0), 48000, None, None)
    dub.free_page.toggle_record()
    tk = w.model.project.dubbing.takes[-1]
    peaks = lv.file_peaks(w.model.project.resolve(tk.file))
    assert len(peaks) == 500 and peaks[:400].max() < 0.01 and peaks[420:].min() > 0.4
    # 타임라인 더빙 클립에 파형이 그려짐(뒤쪽 소리 부분이 밝음)
    c = w.timeline.canvas
    c.pps = 100
    c.update_size() if hasattr(c, "update_size") else None
    rect = next(r for k, i, r in c._items() if k == "take" and i == len(w.model.project.dubbing.takes) - 1)
    img = c.grab().toImage()
    y = int(rect.center().y() - rect.height() / 4)
    quiet = img.pixelColor(int(rect.left() + rect.width() * 0.3), y).lightness()
    loud = img.pixelColor(int(rect.left() + rect.width() * 0.95), y).lightness()
    assert loud > quiet + 30


def test_file_peaks_cache_and_missing(tmp_path):
    p = tmp_path / "x.wav"
    sf.write(p, np.full(4800, 0.5, np.float32), 48000)
    a = lv.file_peaks(p)
    assert len(a) == 10 and a.max() == pytest.approx(0.5, abs=1e-3) and lv.file_peaks(p) is a
    sf.write(p, np.full(9600, 0.2, np.float32), 48000)
    os.utime(p, ns=(1, 2))
    assert len(lv.file_peaks(p)) == 20  # 파일이 바뀌면 다시
    assert lv.file_peaks(tmp_path / "none.wav") is None
