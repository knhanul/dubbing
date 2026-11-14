"""ffmpeg 로 테스트용 이미지/영상/음악/자막을 만듭니다."""
from __future__ import annotations

import subprocess
from pathlib import Path

from composer.probe import ffmpeg_path


def _ff(*args: str) -> None:
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_media(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    # 세밀한 무늬가 있는 이미지(떨림 측정용)
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:d=1", "-frames:v", "1", str(folder / "img1.png"))
    _ff("-f", "lavfi", "-i", "mandelbrot=s=1024x1024", "-frames:v", "1", str(folder / "img2.jpg"))
    _ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(folder / "vid1.mp4"))
    _ff("-f", "lavfi", "-i", "smptebars=s=480x640:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(folder / "vid_noaudio.mp4"))
    _ff("-f", "lavfi", "-i", "sine=f=220:d=3", "-c:a", "libmp3lame", str(folder / "music.mp3"))
    _ff("-f", "lavfi", "-i", "sine=f=880:d=1", str(folder / "take1.wav"))
    (folder / "subs.srt").write_text(
        "1\n00:00:00,500 --> 00:00:02,000\n안녕하세요\n\n2\n00:00:03,000 --> 00:00:05,500\n두 번째 자막\n",
        encoding="utf-8-sig")
    return {"folder": folder}
