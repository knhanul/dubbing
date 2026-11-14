"""sounddevice 기반 마이크 녹음기 (입력 레벨 측정 포함).

sounddevice(PortAudio)가 없는 환경에서도 앱이 켜지도록 지연 import 합니다.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .levels import LevelTap


class RecorderUnavailable(RuntimeError):
    pass


def _sd():
    try:
        import sounddevice as sd  # noqa: WPS433

        return sd
    except Exception as exc:  # PortAudio 없음 등
        raise RecorderUnavailable(f"마이크 장치를 사용할 수 없습니다: {exc}") from exc


@dataclass
class InputDevice:
    index: int
    name: str
    channels: int
    samplerate: float
    is_default: bool = False

    def label(self) -> str:
        return f"{'★ ' if self.is_default else ''}{self.name} ({self.channels}ch)"


def list_input_devices() -> list[InputDevice]:
    sd = _sd()
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    res = []
    seen = set()
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d["name"]
        # Windows 는 같은 장치가 MME/DirectSound/WASAPI 로 중복 표시되므로 이름으로 한 번만
        if name in seen and i != default_in:
            continue
        seen.add(name)
        res.append(InputDevice(i, name, int(d["max_input_channels"]), float(d["default_samplerate"]),
                               i == default_in))
    return res


def peak_db(block: np.ndarray) -> float:
    if block.size == 0:
        return -90.0
    peak = float(np.max(np.abs(block)))
    return 20.0 * np.log10(max(peak, 1e-5))


class Recorder:
    """녹음 중이 아닐 때도 모니터링(레벨 미터)만 켤 수 있습니다."""

    def __init__(self, samplerate: int = 48000, channels: int = 1):
        self.samplerate = samplerate
        self.channels = channels
        self.device: Optional[int] = None
        self.level_db = -90.0
        self.tap = LevelTap(samplerate)  # 화면용 레벨·파형 (스레드 안전)
        self._stream = None
        self._file = None
        self._lock = threading.Lock()
        self._frames = 0
        self.started_at: Optional[float] = None
        self.path: Optional[Path] = None

    # --- 스트림 ---
    def _callback(self, indata, frames, time_info, status):
        self.level_db = max(peak_db(indata), self.level_db - 1.5)  # 완만하게 떨어지는 미터
        try:
            self.tap.feed(indata, counting=self._file is not None)
        except Exception:  # 오디오 스레드는 절대 멈추면 안 됨
            pass
        with self._lock:
            if self._file is not None:
                self._file.write(indata.copy())
                self._frames += frames

    def open(self, device: Optional[int] = None) -> None:
        self.close()
        sd = _sd()
        self.device = device
        self._stream = sd.InputStream(samplerate=self.samplerate, channels=self.channels, dtype="float32",
                                      device=device, callback=self._callback, blocksize=0, latency="low")
        self._stream.start()

    def close(self) -> None:
        self.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self.tap.reset()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    @property
    def input_latency(self) -> float:
        try:
            return float(self._stream.latency) if self._stream is not None else 0.0
        except Exception:
            return 0.0

    # --- 녹음 ---
    def start(self, path: Path | str) -> None:
        import soundfile as sf

        if not self.is_open:
            self.open(self.device)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._file = sf.SoundFile(str(path), mode="w", samplerate=self.samplerate,
                                      channels=self.channels, subtype="PCM_16")
            self._frames = 0
        self.tap.reset_silence()
        self.started_at = time.perf_counter()
        self.path = path

    def stop(self) -> float:
        """녹음을 멈추고 길이(초)를 돌려줍니다."""
        with self._lock:
            f, self._file = self._file, None
        if f is None:
            return 0.0
        f.close()
        return self._frames / float(self.samplerate)

    @property
    def recorded_seconds(self) -> float:
        return self._frames / float(self.samplerate)
