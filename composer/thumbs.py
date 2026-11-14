"""파일 썸네일·길이 (순서 정하기 창 등). 영상은 ffmpeg 로 첫 장면을 뽑아 사용자 캐시 폴더에 저장합니다."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .probe import ffmpeg_path, popen_kwargs, probe
from .schema import guess_type

_dur_cache: dict[tuple, Optional[float]] = {}


def thumb_cache_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = root / "VideoComposer" / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sig(path: Path) -> tuple:
    st = path.stat()
    return str(path.resolve()), st.st_size, int(st.st_mtime)


def video_thumbnail(path: Path, width: int = 320, at: float = 0.0, cache: Optional[Path] = None) -> Optional[Path]:
    """영상 첫 장면(at 초) JPG. 같은 파일(크기·수정 시각)이면 캐시 재사용."""
    path = Path(path)
    try:
        key = "|".join(map(str, _sig(path))) + f"|{width}|{at:.2f}"
    except OSError:
        return None
    out = (cache or thumb_cache_dir()) / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".jpg")
    if out.exists() and out.stat().st_size > 0:
        return out
    kw = popen_kwargs()
    try:
        subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", str(out)], timeout=20, capture_output=True, **kw)
    except Exception:  # noqa: BLE001
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def media_duration(path: Path) -> Optional[float]:
    path = Path(path)
    try:
        k = _sig(path)
    except OSError:
        return None
    if k not in _dur_cache:
        try:
            _dur_cache[k] = probe(path).duration or None
        except Exception:  # noqa: BLE001
            _dur_cache[k] = None
    return _dur_cache[k]


def fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return ""
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}" if m else f"{s:.1f}초"


def is_video(path: Path) -> bool:
    return guess_type(Path(path).name) == "video"
