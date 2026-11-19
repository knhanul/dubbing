"""ffmpeg/ffprobe 위치 찾기와 미디어 정보 읽기."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if sys.platform == "win32" else 0


def popen_kwargs(low_priority: bool = False) -> dict:
    """low_priority: 미리보기·효과 그림처럼 뒤에서 도는 ffmpeg 는 우선순위를 낮춰 화면이 버벅이지 않게 (Windows)."""
    flags = CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
    return {"creationflags": flags} if flags else {}


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=None)
def ffmpeg_path() -> str:
    """우선순위: 환경변수 COMPOSER_FFMPEG → 실행 폴더의 ffmpeg(.exe) → PATH → imageio-ffmpeg."""
    env = os.environ.get("COMPOSER_FFMPEG")
    if env and Path(env).exists():
        return env
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for cand in (_app_dir() / name, _app_dir() / "ffmpeg" / name, _app_dir() / "ffmpeg" / "bin" / name):
        if cand.exists():
            return str(cand)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise FileNotFoundError("ffmpeg 를 찾을 수 없습니다. README 의 설치 안내를 확인하세요.") from exc


@lru_cache(maxsize=None)
def ffprobe_path() -> Optional[str]:
    ff = Path(ffmpeg_path())
    cand = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
    if cand.exists() and cand != ff:
        return str(cand)
    return shutil.which("ffprobe")


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False


_cache: dict[tuple, MediaInfo] = {}


def probe(path: Path | str) -> MediaInfo:
    path = Path(path)
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key in _cache:
        return _cache[key]
    info = _probe_ffprobe(path) if ffprobe_path() else None
    if info is None:
        info = _probe_ffmpeg(path)
    _cache[key] = info
    return info


def _probe_ffprobe(path: Path) -> Optional[MediaInfo]:
    try:
        out = subprocess.run(
            [ffprobe_path(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, **popen_kwargs(),
        )
        data = json.loads(out.stdout or "{}")
    except Exception:
        return None
    info = MediaInfo()
    try:
        info.duration = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        pass
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.width, info.height = int(s.get("width") or 0), int(s.get("height") or 0)
            num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
            try:
                info.fps = float(num) / float(den or 1) if float(den or 1) else 0.0
            except ValueError:
                pass
        elif s.get("codec_type") == "audio":
            info.has_audio = True
    return info


def _probe_ffmpeg(path: Path) -> MediaInfo:
    """ffprobe 가 없을 때(imageio-ffmpeg 사용 시) ffmpeg -i 출력으로 정보를 읽습니다."""
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace", **popen_kwargs())
    return parse_ffmpeg_info(proc.stderr)


def parse_ffmpeg_info(text: str) -> MediaInfo:
    info = MediaInfo()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vm = re.search(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})", text)
    if vm:
        info.has_video = True
        info.width, info.height = int(vm.group(1)), int(vm.group(2))
        fm = re.search(r"(\d+(?:\.\d+)?) fps", text)
        if fm:
            info.fps = float(fm.group(1))
    info.has_audio = bool(re.search(r"Stream #.*?Audio:", text))
    return info
