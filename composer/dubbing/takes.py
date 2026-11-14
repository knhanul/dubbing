"""자막 줄별 더빙 테이크 파일 관리."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schema import Project, Take


def take_filename(subtitle_index: Optional[int], version: int = 1) -> str:
    if subtitle_index is None:
        return f"session_{version:03d}.wav"
    return f"line_{subtitle_index:04d}_v{version}.wav"


def next_take_path(project: Project, subtitle_index: Optional[int]) -> tuple[str, Path]:
    """다시 녹음해도 이전 파일을 덮어쓰지 않도록 버전 번호를 올립니다. (상대경로, 절대경로)"""
    folder = project.resolve(project.dubbing.folder or "takes")
    folder.mkdir(parents=True, exist_ok=True)
    v = 1
    while (folder / take_filename(subtitle_index, v)).exists():
        v += 1
    name = take_filename(subtitle_index, v)
    rel = f"{(project.dubbing.folder or 'takes').rstrip('/')}/{name}"
    return rel, folder / name


def find_take(project: Project, subtitle_index: Optional[int]) -> Optional[Take]:
    for t in project.dubbing.takes:
        if t.subtitle == subtitle_index:
            return t
    return None


def set_take(project: Project, subtitle_index: Optional[int], rel_file: str, offset: float,
             volume: float = 1.0) -> Take:
    """자막 줄의 테이크를 교체(줄마다 하나). 세션 녹음(subtitle=None)은 추가됩니다."""
    if subtitle_index is not None:
        project.dubbing.takes = [t for t in project.dubbing.takes if t.subtitle != subtitle_index]
    take = Take(file=rel_file, subtitle=subtitle_index, offset=round(offset, 3), volume=volume)
    project.dubbing.takes.append(take)
    return take


def remove_take(project: Project, take: Take) -> None:
    project.dubbing.takes = [t for t in project.dubbing.takes if t is not take]
