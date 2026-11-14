"""파일/폴더 가져오기(드래그 앤 드롭·파일 선택 공용). UI 와 무관한 순수 로직."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .schema import Clip, Project, guess_type

AUDIO_BGM_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}
SKIP_DIRS = {".composer_cache", "takes", "__pycache__", ".git"}


def natural_key(s: str):
    """'img2' < 'img10' 처럼 숫자를 숫자로 비교하는 정렬 키(대소문자 무시)."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


@dataclass
class ImportPlan:
    media: list[Path] = field(default_factory=list)  # 이미지/영상 (자연 정렬)
    subtitles: list[Path] = field(default_factory=list)
    audio: list[Path] = field(default_factory=list)
    projects: list[Path] = field(default_factory=list)
    unsupported: list[Path] = field(default_factory=list)
    folders: list[Path] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.media or self.subtitles or self.audio or self.projects)


def _walk_folder(folder: Path) -> list[Path]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS and not d.endswith("_takes")]
        for f in files:
            if not f.startswith("."):
                out.append(Path(root) / f)
    out.sort(key=lambda p: natural_key(p.relative_to(folder).as_posix()))
    return out


def classify_paths(paths: Iterable[Path | str]) -> ImportPlan:
    """드롭된 경로들을 종류별로 나눕니다. 폴더는 안의 파일들(하위 폴더 포함)로 펼칩니다.
    폴더 안의 .json 은 프로젝트로 열지 않고 무시합니다."""
    plan = ImportPlan()
    loose: list[Path] = []
    expanded: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            plan.folders.append(p)
            expanded.extend(_walk_folder(p))
        else:
            loose.append(p)
    loose.sort(key=lambda p: natural_key(p.name))
    for p, from_folder in [(x, False) for x in loose] + [(x, True) for x in expanded]:
        ext = p.suffix.lower()
        kind = guess_type(p.name)
        if not from_folder and not p.exists():
            plan.unsupported.append(p)
        elif kind in ("image", "video"):
            plan.media.append(p)
        elif kind == "subtitle":
            plan.subtitles.append(p)
        elif ext in AUDIO_BGM_EXTS:
            plan.audio.append(p)
        elif ext == ".json":
            if not from_folder:
                plan.projects.append(p)
        elif not from_folder:
            plan.unsupported.append(p)
    return plan


def rel_path(project: Project, path: Path | str) -> str:
    try:
        return Path(os.path.relpath(Path(path).resolve(), Path(project.base_dir).resolve())).as_posix()
    except ValueError:  # 다른 드라이브 (Windows)
        return str(Path(path).resolve())


def make_clip(project: Project, path: Path, image_duration: float = 4.0, image_motion: str = "none") -> Clip:
    kind = guess_type(path.name)
    if kind == "image":
        return Clip(file=rel_path(project, path), type="image", duration=float(image_duration), motion=image_motion)
    return Clip(file=rel_path(project, path), type="video")


def insert_media(project: Project, paths: list[Path], index: Optional[int] = None, image_duration: float = 4.0,
                 image_motion: str = "none") -> list[int]:
    """클립을 index 위치에 차례로 삽입(None 이면 끝에 추가)하고 삽입된 클립 번호들을 돌려줍니다."""
    n = len(project.clips)
    at = n if index is None else max(0, min(int(index), n))
    for k, p in enumerate(paths):
        project.clips.insert(at + k, make_clip(project, p, image_duration, image_motion))
    return list(range(at, at + len(paths)))


def describe_unsupported(paths: list[Path]) -> str:
    names = ", ".join(p.name for p in paths[:8]) + (f" 외 {len(paths) - 8}개" if len(paths) > 8 else "")
    return (f"지원하지 않는 파일 {len(paths)}개는 건너뛰었습니다: {names}\n"
            "(가능한 형식: 이미지 png/jpg/webp/bmp/gif, 영상 mp4/mov/mkv/webm/avi, "
            "자막 srt/vtt, 음악 mp3/wav/m4a/flac/ogg, 프로젝트 json)")


def guess_base_dir(plan: ImportPlan) -> Optional[Path]:
    """빈 프로젝트에 처음 드롭할 때 작업 폴더로 쓸 위치."""
    if plan.folders:
        return plan.folders[0]
    for group in (plan.media, plan.subtitles, plan.audio):
        if group:
            return group[0].parent
    return None
