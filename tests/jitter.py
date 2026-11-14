"""프레임 간 이동량을 위상 상관(phase correlation)으로 측정해 움직임의 떨림을 수치화합니다."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from composer.probe import ffmpeg_path


def read_gray_frames(video: Path, w: int, h: int) -> np.ndarray:
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "gray",
                          "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float64)


def _subpixel(c: np.ndarray, i: int) -> float:
    n = len(c)
    a, b, d = c[(i - 1) % n], c[i], c[(i + 1) % n]
    den = a - 2 * b + d
    return 0.0 if den == 0 else 0.5 * (a - d) / den


def frame_shifts(frames: np.ndarray) -> np.ndarray:
    """연속 프레임 사이 (dx, dy) 이동량(픽셀, 소수점)."""
    win = np.outer(np.hanning(frames.shape[1]), np.hanning(frames.shape[2]))
    out = []
    prev = np.fft.fft2((frames[0] - frames[0].mean()) * win)
    for f in frames[1:]:
        cur = np.fft.fft2((f - f.mean()) * win)
        r = prev * np.conj(cur)
        r /= np.abs(r) + 1e-9
        corr = np.real(np.fft.ifft2(r))
        y, x = np.unravel_index(np.argmax(corr), corr.shape)
        dy = y + _subpixel(corr[:, x], y)
        dx = x + _subpixel(corr[y, :], x)
        h, w = corr.shape
        dx = dx - w if dx > w / 2 else dx
        dy = dy - h if dy > h / 2 else dy
        out.append((dx, dy))
        prev = cur
    return np.array(out)


def jitter_score(frames: np.ndarray) -> float:
    """선형 이동에서 프레임별 이동량의 '2차 차분' 크기(이상적이면 0). 단위: 픽셀."""
    dx = frame_shifts(frames)[:, 0]
    return float(np.abs(np.diff(dx)).mean())
