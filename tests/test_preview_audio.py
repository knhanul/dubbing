"""미리보기 소리: 배경음악(따로 만든 WAV) · 클립 원본 소리 · 더빙 실시간 재생.

Windows 에서 '타임라인에 넣은 배경음악이 미리보기에서 안 들림' 문제:
* 재생 중 배경음악 플레이어 위치가 (장치 지연으로) 조금 늦게 보고되면 매 틱마다 다시 맞추느라(seek) 소리가 안 남
* 열려 있는 WAV 를 덮어쓰려다 실패(Windows) → 조용히 배경음악 없음, 같은 설정이면 다시 시도도 안 함
* 불러오기가 끝나기 전에 위치 옮기기/재생
"""
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

import composer.ui.bgm_live as bl
from composer.probe import ffmpeg_path
from composer.schema import Project

try:
    ffmpeg_path()
    HAVE_FFMPEG = True
except FileNotFoundError:
    HAVE_FFMPEG = False
pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg 필요")

KNAME = "배경 음악 (테스트).mp3"


def pump(sec=0.3):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


@pytest.fixture
def win(tmp_path):
    QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    import composer.ui.main_window as mw
    d = tmp_path / "내 프로젝트 폴더"
    make_media(d)
    shutil.copy(d / "music.mp3", d / KNAME)
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-i", str(d / "music.mp3"), "-c:a", "aac", str(d / "노래.m4a")],
                   check=True)
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.confirm = lambda *a: True
    w.ask_play_without_preview = lambda: "play"
    w.show()
    w.set_project(Project.from_dict({"clips": [{"file": "img1.png", "duration": 6}]}, base_dir=d), None)
    QCoreApplication.processEvents()
    yield w, d
    w.player.pause()
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _stem_ok(w):
    import soundfile as sf
    w.bgm_live.wait()
    pump(0.2)
    p = w.bgm_live.path
    assert p is not None and p.exists(), w.bgm_live.error
    a, sr = sf.read(str(p), dtype="float32")
    assert len(a) / sr == pytest.approx(w.model.timeline.total, abs=0.1)
    assert float(np.sqrt(np.mean(a ** 2))) > 0.01  # 실제로 소리가 있음
    return p


def _plays(w):
    w.player.seek(1.0)
    w.player.play()
    bp = w.bgm_live.player
    end = time.monotonic() + 5
    while time.monotonic() < end and not (bp.playbackState() == QMediaPlayer.PlayingState and bp.position() > 1050):
        pump(0.05)
    assert bp.playbackState() == QMediaPlayer.PlayingState
    assert bp.position() > 1050  # 재생 위치(1초)부터 따라 재생 (0초부터가 아님)
    assert w.bgm_live.audio.volume() > 0.9
    w.player.pause()
    pump(0.1)
    assert bp.playbackState() != QMediaPlayer.PlayingState


@pytest.mark.parametrize("how", ["menu", "plus_button", "drop", "ai_json", "m4a"])
def test_every_add_path_builds_stem_and_plays(win, how, monkeypatch):
    w, d = win
    f = d / ("노래.m4a" if how == "m4a" else KNAME)
    if how in ("menu", "m4a"):
        assert w.add_bgm(str(f))
    elif how == "plus_button":
        monkeypatch.setattr(w, "ask_bgm_file", lambda: str(f))
        w.timeline.canvas.add_requested.emit("bgm")
    elif how == "drop":
        w.handle_drop([f])
    else:
        w.apply_ai_result({"clips": [{"file": "img1.png", "duration": 6}], "bgm": {"file": f.name, "volume": 0.5}}, d)
    assert w.model.project.bgm.file == f.name
    assert w.bgm_live.want is not None
    _stem_ok(w)
    _plays(w)


def test_rapid_edits_build_once_and_new_file_each_time(win, monkeypatch):
    w, d = win
    calls = []
    real = bl.render_bgm_stem
    monkeypatch.setattr(bl, "render_bgm_stem", lambda p, out, c=None: (calls.append(Path(out)), real(p, out, c))[1])
    w.add_bgm(str(d / KNAME))
    for v in (0.5, 0.6, 0.7):  # 볼륨 슬라이더를 끄는 것처럼
        w.model.edit(lambda p, v=v: setattr(p.bgm, "volume", v), merge_key="bv")
    assert w.bgm_live.building
    first = _stem_ok(w)
    assert len(calls) == 1  # 0.25초 모아서 한 번만
    m = first.stat().st_mtime_ns
    w.model.edit(lambda p: setattr(p.bgm, "fade_in", 2.5))
    second = _stem_ok(w)
    assert second != first and len(calls) == 2  # 늘 새 이름 (열린 파일을 덮어쓰지 않음 — Windows)
    assert not first.exists() or first.stat().st_mtime_ns == m  # 예전 것은 지우거나 그대로, 덮어쓰지 않음
    assert not w.bgm_live.building


def test_build_failure_is_reported_and_retried_on_play(win, monkeypatch):
    from composer.render.runner import RenderError
    w, d = win
    real = bl.render_bgm_stem
    state = {"fail": True}

    def fake(p, out, c=None):
        if state["fail"]:
            raise RenderError("ffmpeg 가 오류로 끝났습니다 (코드 1). 로그: x")
        return real(p, out, c)
    monkeypatch.setattr(bl, "render_bgm_stem", fake)
    w.add_bgm(str(d / KNAME))
    w.bgm_live.wait()
    pump(0.1)
    assert w.bgm_live.path is None and "ffmpeg" in w.bgm_live.error
    msg = w.statusBar().currentMessage()
    assert "배경음악을 미리보기에서 틀 준비를 하지 못했어요" in msg and "ffmpeg" in msg
    state["fail"] = False
    w.player.play()  # 재생을 누르면 한 번 더
    assert w.bgm_live.building and w.statusBar().currentMessage() == w.BGM_PREP
    _stem_ok(w)
    assert w.statusBar().currentMessage() != w.BGM_PREP  # 준비되면 안내 지움
    pump(0.8)
    assert w.bgm_live.player.playbackState() == QMediaPlayer.PlayingState


def test_volume_initialised_and_mute_solo(win):
    w, d = win
    w.add_bgm(str(d / KNAME))
    _stem_ok(w)
    assert w.bgm_live.audio.volume() == pytest.approx(1.0)
    w.model.edit(lambda p: setattr(p.tracks.bgm, "muted", True))
    assert w.bgm_live.audio.volume() == 0.0
    w.model.undo()
    assert w.bgm_live.audio.volume() == pytest.approx(1.0)


# --- 재생 맞추기 (가짜 플레이어로 Windows 처럼) ------------------------------
class FakePlayer:
    def __init__(self, lag=0.0, follows=True):
        self.pos = 0.0  # 실제 소리 위치(초)
        self.lag, self.follows = lag, follows
        self.state = QMediaPlayer.PlaybackState.StoppedState
        self.status = QMediaPlayer.MediaStatus.LoadedMedia
        self.set_calls = []

    def playbackState(self):  # noqa: N802
        return self.state

    def mediaStatus(self):  # noqa: N802
        return self.status

    def position(self):
        return int(max(0.0, self.pos - self.lag) * 1000)  # 장치 지연만큼 늦게 알려 줌

    def setPosition(self, ms):  # noqa: N802
        self.set_calls.append(ms)
        if self.follows:
            self.pos = ms / 1000.0

    def play(self):
        self.state = QMediaPlayer.PlaybackState.PlayingState

    def pause(self):
        self.state = QMediaPlayer.PlaybackState.PausedState

    def stop(self):
        self.state = QMediaPlayer.PlaybackState.StoppedState


def _live_with(fake, monkeypatch, clock):
    QApplication.instance() or QApplication([])
    live = bl.BgmLive()
    live.player = fake
    live.path = Path("x.wav")
    monkeypatch.setattr(bl.time, "perf_counter", lambda: clock[0])
    return live


def _run(live, fake, clock, secs, start=0.0, rate=1.0, dt=0.03):
    t = start
    n = int(secs / dt)
    for _ in range(n):
        live.sync(t, True)
        clock[0] += dt
        t += dt
        if fake.state == QMediaPlayer.PlaybackState.PlayingState:
            fake.pos += dt * rate
    return t


@pytest.mark.parametrize("lag", [0.0, 0.15, 0.3, 0.6])
def test_reported_lag_does_not_cause_seek_storm(monkeypatch, lag):
    clock = [100.0]
    fake = FakePlayer(lag=lag)
    live = _live_with(fake, monkeypatch, clock)
    _run(live, fake, clock, 10.0)
    assert fake.state == QMediaPlayer.PlaybackState.PlayingState
    assert live.seeks <= (0 if lag < bl.DRIFT_PLAY else 2)  # 예전: 0.25초 지연이면 틱마다(초당 30번) 다시 맞춤
    assert len(fake.set_calls) <= 3


def test_seeking_that_does_not_help_gives_up(monkeypatch):
    clock = [100.0]
    fake = FakePlayer(follows=False)  # 위치를 옮겨도 안 따라옴 (아주 느린 장치)
    live = _live_with(fake, monkeypatch, clock)
    _run(live, fake, clock, 30.0, rate=0.5)  # 점점 크게 어긋남
    assert live.seeks <= bl.MAX_FAILS and live._give_up


def test_real_drift_is_corrected_and_jump_followed(monkeypatch):
    clock = [100.0]
    fake = FakePlayer()
    live = _live_with(fake, monkeypatch, clock)
    t = _run(live, fake, clock, 2.0)
    fake.pos -= 2.0  # 소리가 2초 밀림 (끊김 등)
    t = _run(live, fake, clock, 1.0, start=t)
    assert live.seeks == 1 and abs(fake.pos - t) < 0.1
    # 재생 중 사용자가 8초로 옮김 → 바로 따라감 (기다림 없이)
    n = len(fake.set_calls)
    live.sync(8.0, True)
    assert len(fake.set_calls) == n + 1 and fake.pos == pytest.approx(8.0)


def test_waits_for_loaded_media_before_seek_and_play(monkeypatch):
    clock = [100.0]
    fake = FakePlayer()
    fake.status = QMediaPlayer.MediaStatus.LoadingMedia
    live = _live_with(fake, monkeypatch, clock)
    live.sync(3.0, True)
    assert fake.set_calls == [] and fake.state == QMediaPlayer.PlaybackState.StoppedState
    clock[0] += 0.4  # 불러오는 데 0.4초
    fake.status = QMediaPlayer.MediaStatus.LoadedMedia
    live._on_status(fake.status)
    assert fake.set_calls == [3400] and fake.state == QMediaPlayer.PlaybackState.PlayingState


# --- 클립 원본 소리 · 더빙 실시간 ---------------------------------------------
def test_clip_original_audio_in_preview(win):
    from composer.probe import probe
    from composer.render.ffmpeg_builder import RenderSettings
    from composer.render.runner import render_project
    w, d = win
    w.set_project(Project.from_dict({"clips": [{"file": "vid1.mp4"}], "bgm": {"file": KNAME}}, base_dir=d), None)
    # 미리보기와 같은 프로젝트 (배경음악·더빙 빼고)
    proj = w.model.project.copy()
    proj.dubbing.takes = []
    proj.bgm.file = None
    res = render_project(proj, d / "pv.mp4", RenderSettings(draft=True))
    assert probe(res.output).has_audio
    assert w.player.audio.volume() > 0.9  # 원본 소리 트랙 켜짐


def test_live_take_starts_mid_take_after_load(win):
    import soundfile as sf
    w, d = win
    (d / "takes").mkdir(exist_ok=True)
    sf.write(str(d / "takes" / "t.wav"), (0.1 * np.sin(np.arange(48000 * 3) / 48000 * 2 * np.pi * 440)).astype(np.float32), 48000)
    w.model.edit(lambda p: p.dubbing.takes.append(__import__("composer.schema", fromlist=["Take"]).Take(
        file="takes/t.wav", subtitle=None, offset=1.0)))
    dub = w.dub
    dub.stop_live()
    dub._live_takes(2.0, 2.0)  # 재생을 2초(조각 가운데)에서 시작
    mp = next(m for m in dub._live_players if m.source().toLocalFile().endswith("t.wav"))
    pump(1.0)
    assert mp.playbackState() == QMediaPlayer.PlayingState
    assert mp.position() >= 900  # 파일 1초 지점부터 (불러온 뒤 옮김)
    # 이어지는 틱에서는 다시 시작하지 않음
    src_before = mp.position()
    dub._live_takes(2.0, 2.03)
    assert mp.position() >= src_before
