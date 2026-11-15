"""미리보기에서 배경음악을 따로 재생합니다.

미리보기 영상(360p)에는 클립 원본 소리만 넣고, 배경음악은 '배경음악만 있는 WAV'(반복·페이드·더빙 밑 줄이기 포함)를
백그라운드로 따로 만들어 미리보기 재생 위치에 맞춰 함께 틉니다. 그래서 🔇/S(솔로)를 바꾸면 영상을 다시 만들지 않고
바로 소리만 켜고 끕니다. 배경음악 설정을 바꾸면 이 WAV 만 몇 초 안에 다시 만듭니다(영상은 그대로).

재생 맞추기 (Windows 에서 소리가 안 들리던 문제):
* 재생 중에는 위치를 자주 다시 맞추지 않습니다. 오디오 장치 지연 때문에 플레이어가 알려 주는 위치는 실제보다
  0.1~0.3초 늦을 수 있는데, 그때마다 다시 맞추면(seek) 소리가 계속 끊겨 아무것도 안 들립니다.
  → 0.8초 넘게 어긋난 상태가 이어질 때만, 한 번 맞춘 뒤 3초는 기다리고, 맞춰도 소용없으면 그 재생에서는 그만둡니다.
  재생 중 사용자가 위치를 옮기면(0.5초 넘게 튐) 바로 따라갑니다.
* 파일을 막 바꿨을 때(불러오는 중)는 위치 맞추기·재생을 불러오기가 끝난 뒤에 합니다.
* WAV 는 만들 때마다 새 이름(열려 있는 파일을 덮어쓰지 않음 — Windows 는 열린 파일을 못 바꿈), 실패하면 알려 주고
  다음 재생/편집 때 다시 시도합니다.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..render.runner import cache_dir, render_bgm_stem
from ..schema import Project

DRIFT_PLAY = 0.8    # 초 — 재생 중 이만큼 넘게 어긋나야 다시 맞춤 (배경음악은 조금 어긋나도 티가 안 남)
DRIFT_PAUSE = 0.05  # 멈춰 있을 때는 정확히
DRIFT_COUNT = 3     # 어긋남이 이만큼 이어져야 (한 번 튄 값은 무시)
SEEK_COOLDOWN = 3.0  # 다시 맞춘 뒤 이만큼은 기다림 (플레이어 위치가 따라올 시간)
MAX_FAILS = 2       # 맞춰도 계속 어긋나면 이번 재생에서는 더 맞추지 않음 (계속 끊기는 것보다 나음)
JUMP = 0.5          # 재생 중 위치가 이만큼 튀면 = 사용자가 옮김 → 바로 따라감
DEBOUNCE_MS = 250   # 편집이 이어질 때 WAV 다시 만들기를 모아서


def stem_key(project: Project, total: float) -> Optional[str]:
    b = project.bgm
    if not b.file:
        return None
    p = project.resolve(b.file)
    if not p or not p.exists():
        return None
    st = p.stat()
    d = {"bgm": project.to_dict()["bgm"], "total": round(total, 3), "src": [str(p), st.st_size, int(st.st_mtime)]}
    if b.duck_under_voice:  # 더빙 위치가 바뀌면 줄이는 곳도 바뀜
        d["takes"] = [project.to_dict()["dubbing"], project.to_dict()["subtitle"]]
    return json.dumps(d, sort_keys=True, default=str)


def _short(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class BgmLive(QObject):
    ready = Signal(str, object, str)  # key, path | None, 오류 글 (작업 스레드에서 → Qt 가 메인 스레드로 넘김)
    building_changed = Signal(bool)   # 배경음악 WAV 만드는 중?
    failed = Signal(str)              # 사람이 읽을 오류 (상태 표시줄)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.key: Optional[str] = None  # 지금 불러온 WAV 의 키
        self.want: Optional[str] = None  # 만들고 있거나 만들어야 할 키
        self.path: Optional[Path] = None
        self.error = ""
        self.building = False
        self.seeks = 0  # 재생 중 다시 맞춘 횟수 (테스트·진단)
        self._cancel: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._seq = 0
        self._job = None  # (project, total, key) — 모았다가 만들기
        self._drift_n = 0
        self._last_seek = -1e9
        self._fails = 0
        self._give_up = False
        self.seeks_this_play = 0
        self._last_tick: Optional[tuple[float, float, bool]] = None
        self._pending: Optional[tuple[float, bool, float]] = None  # 불러오기 끝나면 (t, 재생?, 요청 시각)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_job)
        self.ready.connect(self._on_ready)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.enabled = True  # 테스트에서 끌 수 있음

    # ---------------------------------------------------------------- 만들기
    def update(self, project: Project, total: float, force: bool = False):
        """편집할 때마다 불림. 배경음악 값이 바뀌었을 때만 (0.25초 모아서) WAV 를 새로 만듭니다."""
        key = stem_key(project, total) if self.enabled else None
        if key == self.want and not force:
            return
        self.want = key
        self.error = ""
        if self._cancel is not None:
            self._cancel.set()
        if key is None:
            self._debounce.stop()
            self._job = None
            self._set_building(False)
            self.player.stop()
            self.player.setSource(QUrl())
            self.key, self.path, self._pending = None, None, None
            return
        self._job = (project.copy(), total, key)
        self._set_building(True)
        self._debounce.start()

    def retry_if_failed(self, project: Project, total: float):
        """만들기가 실패했으면 (재생을 누를 때) 한 번 더."""
        if self.error and not self.building:
            self.update(project, total, force=True)

    def _set_building(self, on: bool):
        if on != self.building:
            self.building = on
            self.building_changed.emit(on)

    def _start_job(self):
        job, self._job = self._job, None
        if job is None:
            return
        proj, _total, key = job
        cdir = cache_dir(proj)
        self._seq += 1
        out = cdir / f"bgm_stem_{_short(key)}_{self._seq}.wav"  # 늘 새 이름 (열린 파일을 덮어쓰지 않음)
        cancel = threading.Event()
        self._cancel = cancel

        def work():
            err = ""
            try:
                res = render_bgm_stem(proj, out, cancel)
            except Exception as e:  # noqa: BLE001  (취소·실패 → 알리고 배경음악 없이 미리보기)
                res = None
                err = str(e) or type(e).__name__
            if not cancel.is_set():
                self.ready.emit(key, str(res) if res else None, err)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 20.0):
        """(테스트) 모아 둔 만들기를 바로 시작하고 끝날 때까지 기다림. 결과 반영은 이벤트 처리 때."""
        if self._debounce.isActive():
            self._debounce.stop()
            self._start_job()
        t = self._thread
        if t is not None:
            t.join(timeout)
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

    def _on_ready(self, key: str, path, err: str):
        if key != self.want:
            return
        self._set_building(False)
        if err:
            self.error = err
            self.failed.emit(err)
        new = Path(path) if path else None
        old = self.path
        self.key, self.path = key, new
        was = self.player.playbackState() == QMediaPlayer.PlayingState
        t = self.player.position() / 1000.0
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(new)) if new else QUrl())
        if new is not None:
            self._pending = (t, was, time.perf_counter())
            self._on_status(self.player.mediaStatus())  # 이미 불러왔으면 바로
        self._cleanup(old, new)

    def _cleanup(self, old: Optional[Path], keep: Optional[Path]):
        """예전 WAV 지우기 (Windows 에서 아직 열려 있으면 다음 기회에)."""
        folder = (keep or old).parent if (keep or old) else None
        if folder is None or not folder.exists():
            return
        for f in folder.glob("bgm_stem_*.wav"):
            if keep is not None and f == keep:
                continue
            try:
                f.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- 재생 맞추기
    def set_volume(self, v: float):
        self.audio.setVolume(max(0.0, min(1.0, v)))

    def _loaded(self) -> bool:
        return self.player.mediaStatus() in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                                             QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.EndOfMedia)

    def _on_status(self, status):
        if self._pending is None or not self._loaded():
            return
        t, playing, at = self._pending
        self._pending = None
        if playing:
            t += time.perf_counter() - at  # 불러오는 동안 지난 시간
        self._seek(t)
        if playing:
            self.player.play()

    def _seek(self, t: float):
        self.player.setPosition(int(max(0.0, t) * 1000))
        self._last_seek = time.perf_counter()
        self._drift_n = 0

    def sync(self, t: float, playing: bool):
        if self.path is None:
            return
        now = time.perf_counter()
        last = self._last_tick
        self._last_tick = (t, now, playing)
        if not self._loaded():  # 불러오는 중: 끝나면 이 위치·상태로
            self._pending = (t, playing, now)
            return
        mine = self.player.playbackState() == QMediaPlayer.PlayingState
        drift = abs(self.player.position() / 1000.0 - t)
        if not playing:
            if mine:
                self.player.pause()
            if drift > DRIFT_PAUSE:
                self._seek(t)
            return
        if not mine:  # 재생 시작: 한 번 맞추고 틀기
            self._reset_drift()
            if drift > DRIFT_PAUSE:
                self._seek(t)
            self.player.play()
            return
        if last is not None and last[2] and abs(t - (last[0] + (now - last[1]))) > JUMP:
            self._reset_drift()  # 재생 중 다른 곳으로 옮김 → 바로 따라감
            self._seek(t)
            return
        if drift <= DRIFT_PLAY:
            self._drift_n = 0
            self._fails = 0
            return
        if self._give_up:
            return
        self._drift_n += 1
        if self._drift_n < DRIFT_COUNT or now - self._last_seek < SEEK_COOLDOWN:
            return
        if self.seeks_this_play > 0:
            self._fails += 1  # 지난번에 맞췄는데도 또 어긋남
            if self._fails >= MAX_FAILS:
                self._give_up = True  # 맞춰도 소용없음(장치 지연 등) → 이번 재생에서는 그만 (끊김 방지)
                return
        self.seeks += 1
        self.seeks_this_play += 1
        self._seek(t)

    def _reset_drift(self):
        self._drift_n = 0
        self._fails = 0
        self._give_up = False
        self.seeks_this_play = 0

    def shutdown(self):
        self._debounce.stop()
        if self._cancel is not None:
            self._cancel.set()
        self.player.stop()
