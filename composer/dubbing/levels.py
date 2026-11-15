"""마이크 입력 크기(레벨) 계산 — 녹음 콜백(오디오 스레드)에서 계산하고 화면은 30fps 로 꺼내 씁니다.

* block_levels: 한 덩어리의 RMS/피크(dBFS)와 클리핑 여부
* LevelTap: 오디오 스레드 → 화면 스레드로 안전하게 넘기는 통 (lock + 고정 길이 큐)
  - 10ms 마다 피크 하나씩 쌓아서 '흘러가는 파형'에 씀
  - 녹음 중 거의 무음이 몇 초 이어졌는지 셈(마이크 안 들어옴 경고)
* file_peaks: 녹음 파일의 파형(타임라인 더빙 클립에 그림), 파일이 바뀌면 다시 계산
"""
from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

FLOOR_DB = -60.0       # 미터 맨 왼쪽
YELLOW_DB = -18.0      # 여기부터 노란색(적당히 큼)
RED_DB = -6.0          # 여기부터 빨간색(너무 큼)
CLIP_LEVEL = 0.99      # 이 이상이면 소리가 찌그러짐(클리핑)
SILENCE_DB = -55.0     # 이보다 작으면 '거의 무음'
SILENCE_WARN_S = 3.0   # 녹음 중 무음이 이만큼 이어지면 경고
SILENCE_WARNING = "마이크 소리가 들어오지 않아요. 장치와 Windows 마이크 권한을 확인하세요"


def to_db(x: float) -> float:
    return float(20.0 * np.log10(max(float(x), 1e-5)))


def block_levels(block: np.ndarray) -> tuple[float, float, bool]:
    """(rms_db, peak_db, clipped)."""
    a = np.asarray(block, dtype=np.float32)
    if a.size == 0:
        return -90.0, -90.0, False
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(np.square(a, dtype=np.float64))))
    return to_db(rms), to_db(peak), peak >= CLIP_LEVEL


def level_zone(db: float) -> str:
    return "red" if db >= RED_DB else "yellow" if db >= YELLOW_DB else "green"


class LevelTap:
    """오디오 스레드에서 feed(), 화면 스레드에서 snapshot()/take_wave()."""

    def __init__(self, samplerate: int, wave_len: int = 1500):
        self.samplerate = int(samplerate)
        self.chunk = max(1, self.samplerate // 100)  # 10ms
        self._lock = threading.Lock()
        self._wave: deque = deque(maxlen=wave_len)
        self._new = 0
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.rms_db = -90.0
            self.peak_db = -90.0
            self.hold_db = -90.0       # 천천히 내려오는 최고점 표시
            self.clip_count = 0
            self._quiet_frames = 0
            self._voice_quiet = 10 ** 9  # 마지막 '말소리'(SPEECH_DB 넘는 10ms) 뒤로 지난 샘플 수
            self._wave.clear()
            self._new = 0

    def reset_silence(self) -> None:
        with self._lock:
            self._quiet_frames = 0

    def feed(self, block: np.ndarray, counting: bool = False) -> None:
        a = np.asarray(block, dtype=np.float32)
        if a.ndim > 1:
            a = np.max(np.abs(a), axis=1) if a.shape[1] > 1 else a[:, 0]
        rms_db, peak_db, clipped = block_levels(a)
        n = a.size // self.chunk
        peaks = np.max(np.abs(a[: n * self.chunk]).reshape(n, self.chunk), axis=1) if n else np.zeros(0)
        rest = a[n * self.chunk:]
        if rest.size:
            peaks = np.append(peaks, float(np.max(np.abs(rest))))
        # 10ms 마다 RMS 로 '말소리'인지 (녹음 파일의 speech_end 와 같은 기준) → 마지막 말소리 뒤 무음 길이
        if n:
            rms = np.sqrt(np.mean(np.square(a[: n * self.chunk].reshape(n, self.chunk).astype(np.float64)), axis=1))
            loud = np.nonzero(rms > SPEECH_AMP)[0]
        else:
            loud = np.zeros(0, int)
        with self._lock:
            self.rms_db = rms_db
            self.peak_db = peak_db
            self.hold_db = max(peak_db, self.hold_db - 0.75)
            if clipped:
                self.clip_count += 1
            if counting:
                self._quiet_frames = self._quiet_frames + a.size if rms_db < SILENCE_DB else 0
            if len(loud):
                self._voice_quiet = a.size - (int(loud[-1]) + 1) * self.chunk
            else:
                self._voice_quiet += a.size
            self._wave.extend(float(x) for x in peaks)
            self._new += len(peaks)

    @property
    def silent_seconds(self) -> float:
        with self._lock:
            return self._quiet_frames / float(self.samplerate)

    @property
    def voice_quiet_seconds(self) -> float:
        """마지막 말소리 뒤로 계속 조용했던 시간(초). 막 말하는 중이면 0 에 가까움."""
        with self._lock:
            return self._voice_quiet / float(self.samplerate)

    def snapshot(self) -> dict:
        with self._lock:
            return {"rms_db": self.rms_db, "peak_db": self.peak_db, "hold_db": self.hold_db,
                    "clip_count": self.clip_count, "silent_s": self._quiet_frames / float(self.samplerate)}

    def take_wave(self) -> list[float]:
        """지난번 이후 새로 들어온 10ms 피크들."""
        with self._lock:
            n = min(self._new, len(self._wave))
            self._new = 0
            return list(self._wave)[len(self._wave) - n:] if n else []


_PEAK_CACHE: dict = {}


def file_peaks(path, per_sec: int = 100) -> Optional[np.ndarray]:
    """녹음 파일 파형: 1/per_sec 초마다 피크(0~1). 읽을 수 없으면 None. (경로·수정시각·크기로 캐시)"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size, per_sec)
    if key in _PEAK_CACHE:
        return _PEAK_CACHE[key]
    try:
        import soundfile as sf
        data, sr = sf.read(str(p), dtype="float32", always_2d=True)
    except Exception:
        return None
    a = np.max(np.abs(data), axis=1) if data.size else np.zeros(0, np.float32)
    step = max(1, int(sr // per_sec))
    n = int(np.ceil(a.size / step)) if a.size else 0
    pad = np.zeros(n * step, np.float32)
    pad[: a.size] = a
    peaks = pad.reshape(n, step).max(axis=1) if n else pad
    if len(_PEAK_CACHE) > 200:
        _PEAK_CACHE.clear()
    _PEAK_CACHE[key] = peaks
    return peaks


SPEECH_DB = -42.0  # 이보다 크면 '말소리' (녹음 끝의 무음 찾기, 선택 자막 녹음이 말 끝을 기다릴 때)
SPEECH_AMP = 10 ** (SPEECH_DB / 20.0)


def speech_end(path: Path, threshold_db: float = SPEECH_DB, window: float = 0.02) -> Optional[float]:
    """녹음 파일에서 마지막으로 말소리가 난 시각(파일 기준 초). 끝의 무음은 뺌. 말소리가 없거나 못 읽으면 None."""
    try:
        import soundfile as sf
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001
        return None
    if not len(data):
        return None
    mono = data.mean(axis=1)
    n = max(1, int(sr * window))
    k = len(mono) // n
    if k == 0:
        return None
    blocks = mono[: k * n].reshape(k, n).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(blocks), axis=1))
    loud = np.nonzero(20.0 * np.log10(np.maximum(rms, 1e-5)) > threshold_db)[0]
    if not len(loud):
        return None
    return float((loud[-1] + 1) * n) / float(sr)
