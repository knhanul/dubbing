"""UI 공용 프로젝트 모델: 변경 알림, 실행 취소/다시 실행, 선택 상태."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..probe import probe
from ..schema import Project, load_project, save_project
from ..subtitles import Cue, CueTimeline, project_cues
from ..timeline import Timeline, build_timeline, take_position


@dataclass
class Selection:
    kind: str = ""  # "" | clip | subtitle | take | bgm
    index: int = -1  # clip index / subtitle index(자막 번호) / take list index


class ProjectModel(QObject):
    changed = Signal()  # 프로젝트 내용 변경(타임라인 다시 그리기)
    selection_changed = Signal()
    path_changed = Signal()
    dirty_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.project = Project()
        self.path: Optional[Path] = None
        self.timeline = Timeline([], 0.0)
        self.cues: list[Cue] = []
        self.cue_timeline = CueTimeline([])
        self.selection = Selection()
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._last_key: Optional[str] = None
        self._snapshot = self.project.to_json()
        self.dirty = False
        self.revision = 0  # 변경될 때마다 증가 (미리보기 최신 여부 판단)
        self.recompute()

    # --- 계산 ---
    def duration_of(self, clip) -> Optional[float]:
        p = self.project.resolve(clip.file)
        try:
            return probe(p).duration if p and p.exists() else None
        except Exception:
            return None

    def recompute(self) -> None:
        self.timeline = build_timeline(self.project, self.duration_of)
        try:
            self.cues = project_cues(self.project)
        except Exception:
            self.cues = []
        self.cue_timeline = CueTimeline(self.cues)

    def cue_by_index(self, index: int) -> Optional[Cue]:
        return next((c for c in self.cues if c.index == index), None)

    def take_positions(self) -> list[float]:
        by = {c.index: c for c in self.cues}
        return [take_position(t, by, self.project.dubbing.latency_ms) for t in self.project.dubbing.takes]

    # --- 변경/실행 취소 ---
    def set_project(self, project: Project, path: Optional[Path] = None) -> None:
        self.project = project
        self.path = Path(path) if path else None
        self._undo.clear()
        self._redo.clear()
        self._last_key = None
        self._snapshot = project.to_json()
        self.selection = Selection()
        self._set_dirty(False)
        self.revision += 1
        self.recompute()
        self.changed.emit()
        self.selection_changed.emit()
        self.path_changed.emit()

    def commit(self, merge_key: Optional[str] = None) -> None:
        """프로젝트를 직접 수정한 뒤 호출합니다. 같은 merge_key 의 연속 수정은 한 번의 실행 취소로 묶입니다."""
        new = self.project.to_json()
        if new == self._snapshot:
            self.recompute()
            self.changed.emit()
            return
        if not (merge_key and merge_key == self._last_key and self._undo):
            self._undo.append(self._snapshot)
            if len(self._undo) > 200:
                self._undo.pop(0)
        self._redo.clear()
        self._last_key = merge_key
        self._snapshot = new
        self.revision += 1
        self._set_dirty(True)
        self.recompute()
        self.changed.emit()

    def edit(self, fn: Callable[[Project], None], merge_key: Optional[str] = None) -> None:
        fn(self.project)
        self.commit(merge_key)

    def _restore(self, text: str) -> None:
        base = self.project.base_dir
        self.project = Project.from_dict(json.loads(text), base_dir=base)
        self._snapshot = text
        self._last_key = None
        self.revision += 1
        self._set_dirty(True)
        self.recompute()
        self.changed.emit()
        self.selection_changed.emit()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot)
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot)
        self._restore(self._redo.pop())
        return True

    def mark_modified(self) -> None:
        """저장되지 않은 상태로 표시(예: AI 가 만든 새 프로젝트)."""
        self._set_dirty(True)

    def _set_dirty(self, v: bool) -> None:
        if v != self.dirty:
            self.dirty = v
            self.dirty_changed.emit(v)

    # --- 파일 ---
    def load(self, path: Path | str) -> None:
        self.set_project(load_project(path), Path(path))

    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("저장 경로가 없습니다.")
        old_base = self.project.base_dir
        if target.parent.resolve() != Path(old_base).resolve():
            _rebase_paths(self.project, Path(old_base), target.parent)
        save_project(self.project, target)
        self.path = target
        self._snapshot = self.project.to_json()
        self._set_dirty(False)
        self.path_changed.emit()
        return target

    # --- 선택 ---
    def select(self, kind: str, index: int = -1) -> None:
        self.selection = Selection(kind, index)
        self.selection_changed.emit()


def _rebase_paths(project: Project, old: Path, new: Path) -> None:
    """다른 폴더에 저장할 때 상대 경로가 깨지지 않도록 다시 계산합니다."""
    import os

    def fix(rel):
        if not rel:
            return rel
        p = Path(rel)
        if p.is_absolute():
            return rel
        absolute = (old / p).resolve()
        try:
            return Path(os.path.relpath(absolute, new.resolve())).as_posix()
        except ValueError:  # 다른 드라이브
            return str(absolute)

    for c in project.clips:
        c.file = fix(c.file)
    project.subtitle.file = fix(project.subtitle.file)
    project.bgm.file = fix(project.bgm.file)
    for t in project.dubbing.takes:
        t.file = fix(t.file)
    project.dubbing.folder = fix(project.dubbing.folder) or "takes"
