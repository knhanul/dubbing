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


MULTI_KINDS = ("clip", "subtitle", "take", "transition")  # 여러 개 고를 수 있는 종류 (같은 종류끼리만)


@dataclass
class Selection:
    kind: str = ""  # "" | clip | subtitle | take | bgm | transition
    index: int = -1  # 기준(마지막에 고른) 항목: clip index / subtitle index(자막 번호) / take list index
    extra: tuple = ()  # 같은 종류로 함께 고른 다른 항목들 (Ctrl/Shift+클릭 · 끌어서 고르기 · Ctrl+A)

    @property
    def indices(self) -> list[int]:
        """고른 항목 전부(기준 항목 포함), 작은 번호부터."""
        if not self.kind:
            return []
        try:
            return sorted({int(self.index), *(int(i) for i in self.extra)})
        except (TypeError, ValueError):
            return []

    @property
    def count(self) -> int:
        return len(self.indices)

    def has(self, kind: str, index: int) -> bool:
        return kind == self.kind and (index == self.index or index in self.extra)


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
        self.migrated = False  # 방금 연 프로젝트를 새 전환 방식으로 바꿨는지
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

    def take_file_len(self, rel: str) -> float:
        """더빙 파일 길이(초). 없거나 못 읽으면 1초."""
        from ..probe import probe
        p = self.project.resolve(rel)
        try:
            return float(probe(p).duration or 1.0) if p and p.exists() else 1.0
        except Exception:
            return 1.0

    def take_positions(self) -> list[float]:
        by = {c.index: c for c in self.cues}
        return [take_position(t, by, self.project.dubbing.latency_ms) for t in self.project.dubbing.takes]

    # --- 변경/실행 취소 ---
    def _migrate(self, project: Project) -> bool:
        """예전 형식(version 1, 겹쳐 놓는 전환) → 지금 형식. 보이는 시간은 그대로."""
        from ..timeline import migrate_v1
        self.migrated = migrate_v1(project, self.duration_of)
        return self.migrated

    def set_project(self, project: Project, path: Optional[Path] = None) -> None:
        migrated = self._migrate(project)
        self.project = project
        self.path = Path(path) if path else None
        self._undo.clear()
        self._redo.clear()
        self._last_key = None
        self._snapshot = project.to_json()
        self.selection = Selection()
        self._set_dirty(migrated)  # 바꿨으면 '저장 안 됨' 표시 → 저장하면 새 형식
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

    def replace_project(self, project: Project) -> None:
        """프로젝트 내용 전체를 바꿉니다(파일 경로 유지). 실행 취소 한 단계로 기록됩니다.
        (작업 폴더는 아무것도 없는 '제목 없음' 프로젝트일 때만 바뀌므로 되돌려도 문제없습니다.)"""
        self._undo.append(self._snapshot)
        if len(self._undo) > 200:
            self._undo.pop(0)
        self._redo.clear()
        self._last_key = None
        self._migrate(project)
        self.project = project
        self._snapshot = project.to_json()
        self.revision += 1
        self._set_dirty(True)
        self.recompute()
        self._prune_selection()
        self.changed.emit()
        self.selection_changed.emit()

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
        self._prune_selection()
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

    def valid_indices(self, kind: str) -> list[int]:
        """kind 종류 항목 번호를 타임라인 왼쪽→오른쪽 순서로 (Shift+클릭 범위·Ctrl+A 용)."""
        if kind == "clip":
            return [c.index for c in self.timeline.clips]
        if kind == "transition":
            return [c.index for c in self.timeline.clips if c.index > 0 and c.transition_in.duration > 0]
        if kind == "subtitle":
            return [c.index for c in sorted(self.cues, key=lambda c: (c.start, c.index))]
        if kind == "take":
            pos = self.take_positions()
            return sorted(range(len(pos)), key=lambda i: (pos[i], i))
        return []

    def select_many(self, kind: str, indices, primary: Optional[int] = None) -> None:
        """같은 종류 여러 개를 고릅니다. primary(기준 항목 = 속성 탭에 보이는 것)가 없으면 마지막 것."""
        idx = list(dict.fromkeys(int(i) for i in indices))
        if not kind or not idx:
            self.select("")
            return
        if kind not in MULTI_KINDS:
            idx = idx[-1:]
        p = primary if primary in idx else idx[-1]
        self.selection = Selection(kind, p, tuple(i for i in idx if i != p))
        self.selection_changed.emit()

    def toggle_select(self, kind: str, index: int) -> None:
        """Ctrl+클릭: 같은 종류면 더하거나 빼고, 다른 종류면 새로 고름."""
        s = self.selection
        if s.kind != kind or kind not in MULTI_KINDS:
            self.select(kind, index)
            return
        cur = s.indices
        if index in cur:
            rest = [i for i in cur if i != index]
            if not rest:
                self.select("")
                return
            self.select_many(kind, rest, s.index if s.index != index else rest[-1])
        else:
            self.select_many(kind, cur + [index], index)

    def select_range(self, kind: str, index: int) -> None:
        """Shift+클릭: 기준 항목부터 index 까지 같은 트랙의 항목을 모두 (타임라인 순서)."""
        s = self.selection
        order = self.valid_indices(kind)
        if s.kind != kind or kind not in MULTI_KINDS or s.index not in order or index not in order:
            self.select(kind, index)
            return
        a, b = order.index(s.index), order.index(index)
        lo, hi = min(a, b), max(a, b)
        self.select_many(kind, order[lo:hi + 1], index)

    def select_all(self, kind: str) -> int:
        """한 트랙(종류)의 항목을 모두 고릅니다. 고른 개수."""
        idx = self.valid_indices(kind)
        if kind == "bgm" and self.project.bgm.file:
            idx = [0]
        if not idx:
            return 0
        self.select_many(kind, idx, self.selection.index if self.selection.kind == kind else None)
        return len(idx)

    def _prune_selection(self) -> None:
        """실행 취소 등으로 없어진 항목은 선택에서 뺌."""
        s = self.selection
        if not s.kind:
            return
        if s.kind == "bgm":
            ok = [0] if self.project.bgm.file else []
        else:
            valid = set(self.valid_indices(s.kind))
            ok = [i for i in s.indices if i in valid]
        if ok != s.indices:
            self.selection = Selection(s.kind, s.index, tuple(i for i in ok if i != s.index)) if s.index in ok                 else (Selection(s.kind, ok[-1], tuple(ok[:-1])) if ok else Selection())


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
    # 아직 녹음이 없으면 기본 'takes' 폴더는 새 JSON 옆에 그대로 둠
    if project.dubbing.takes or (project.dubbing.folder or "takes") != "takes":
        project.dubbing.folder = fix(project.dubbing.folder) or "takes"
