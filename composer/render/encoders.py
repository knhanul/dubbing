"""사용 가능한 H.264 인코더 감지 (NVIDIA NVENC 우선)."""
from __future__ import annotations

import subprocess
from functools import lru_cache

from ..probe import ffmpeg_path, popen_kwargs


@lru_cache(maxsize=None)
def detect_encoder() -> str:
    """h264_nvenc 가 실제로 동작하면 'h264_nvenc', 아니면 'libx264'."""
    ff = ffmpeg_path()
    try:
        enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=20, errors="replace", **popen_kwargs()).stdout
        if "h264_nvenc" not in enc:
            return "libx264"
        test = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30, **popen_kwargs())
        return "h264_nvenc" if test.returncode == 0 else "libx264"
    except Exception:
        return "libx264"


ENCODER_LABELS = {"h264_nvenc": "NVIDIA GPU (NVENC)", "libx264": "CPU (x264)"}
