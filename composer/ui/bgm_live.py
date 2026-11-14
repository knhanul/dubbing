"""미리보기에서 배경음악을 따로 재생합니다.

미리보기 영상(360p)에는 클립 원본 소리만 넣고, 배경음악은 '배경음악만 있는 WAV'(반복·페이드·더빙 밑 줄이기 포함)를
백그라운드로 따로 만들어 미리보기 재생 위치에 맞춰 함께 틉니다. 그래서 🔇/S(솔로)를 바꾸면 영상을 다시 만들지 않고
바로 소리만 켜고 끕니다. 배경음악 설정을 바꾸면 이 WAV 만 몇 초 안에 다시 만듭니다(영상은 그대로).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..render.runner import cache_dir, render_bgm_stem
from ..schema import Project

DRIFT = 0.15  # 초 — 이만큼 어긋나면 다시 맞춤


def stem_key(project: Project, total: float) -> Optional[str]:
    b = project.bgm
    if not b.file:
        return None
    p = project.resolve(b.file)
    if not p or not p.exists():
        return None
    st = p.stat()
    d = {"bgm": project.to_dict()["bgm"], "total": round(total, 3), "src": [st.st_size, int(st.st_mtime)]}
    if b.duck_under_voice:  # 더빙 위치가 바뀌면 줄이는 곳도 바뀜
        d["takes"] = [project.to_dict()["dubbing"], project.to_dict()["subtitle"]]
    return json.dumps(d, sort_keys=True, default=str)


class BgmLive(QObject):
    ready = Signal(str, object)  # key, path | None  (작업 스레드에서 → Qt 가 메인 스레드로 넘김)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.key: Optional[str] = None  # 지금 불러온 WAV 의 키
        self.want: Optional[str] = None  # 만들고 있거나 만들어야 할 키
        self.path: Optional[Path] = None
        self.gain = 1.0
        self._cancel: Optional[threading.Event] = None
        self._seq = 0
        self.ready.connect(self._on_ready)
        self.enabled = True  # 테스트에서 끌 수 있음

    # ---------------------------------------------------------------- 만들기
    def update(self, project: Project, total: float):
        key = stem_key(project, total) if self.enabled else None
        if key == self.want:
            return
        self.want = key
        if self._cancel is not None:
            self._cancel.set()
        if key is None:
            self.player.stop()
            self.player.setSource(QUrl())
            self.key, self.path = None, None
            return
        self._seq += 1
        out = cache_dir(project) / f"bgm_stem_{self._seq % 2}.wav"
        if self.path is not None and out == self.path:
            self._seq += 1
            out = cache_dir(project) / f"bgm_stem_{self._seq % 2}.wav"
        cancel = threading.Event()
        self._cancel = cancel
        proj = project.copy()

        def work():
            try:
                res = render_bgm_stem(proj, out, cancel)
            except Exception:  # noqa: BLE001  (취소·실패 → 배경음악 없이 미리보기)
                res = None
            if not cancel.is_set():
                self.ready.emit(key, res)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 20.0):
        t = getattr(self, "_thread", None)
        if t is not None:
            t.join(timeout)

    def _on_ready(self, key: str, path):
        if key != self.want:
            return
        self.key, self.path = key, (Path(path) if path else None)
        was = self.player.playbackState() == QMediaPlayer.PlayingState
        pos = self.player.position()
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        if path and was:
            self.player.setPosition(pos)
            self.player.play()

    # ---------------------------------------------------------------- 재생 맞추기
    def set_volume(self, v: float):
        self.audio.setVolume(max(0.0, min(1.0, v)))

    def sync(self, t: float, playing: bool):
        if self.path is None:
            return
        mine = self.player.playbackState() == QMediaPlayer.PlayingState
        if not playing:
            if mine:
                self.player.pause()
            if abs(self.player.position() / 1000.0 - t) > DRIFT:
                self.player.setPosition(int(t * 1000))
            return
        if abs(self.player.position() / 1000.0 - t) > DRIFT or not mine:
            self.player.setPosition(int(t * 1000))
        if not mine:
            self.player.play()

    def shutdown(self):
        if self._cancel is not None:
            self._cancel.set()
        self.player.stop()
