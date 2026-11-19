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


# ------------------------------------------------------------------ 녹음 기록 (같은 자막의 예전 녹음들)
import re as _re
from dataclasses import dataclass as _dataclass

_LINE_RE = _re.compile(r"^line_(\d{4})_v(\d+)\.wav$", _re.IGNORECASE)


@_dataclass
class TakeVersion:
    rel: str  # 프로젝트 기준 경로 (takes/line_0003_v2.wav)
    path: Path
    version: int  # v 번호 (자유·세션 녹음 등은 0)
    mtime: float  # 녹음(파일) 시각
    length: float  # 초
    current: bool  # 지금 이 조각이 쓰는 파일


def _audio_len(path: Path) -> float:
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:  # noqa: BLE001
        try:
            from ..probe import probe
            return float(probe(path).duration or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0


def _rel_join(rel_dir: str, name: str) -> str:
    rel_dir = rel_dir.replace("\\", "/").rstrip("/")
    return f"{rel_dir}/{name}" if rel_dir and rel_dir != "." else name


def take_versions(project: Project, take: Take) -> list[TakeVersion]:
    """이 더빙 조각의 녹음 기록. 자막 줄 녹음(line_XXXX_vN.wav)이면 같은 폴더의 같은 번호 파일 모두(v 순),
    자유·세션 녹음이나 불러온 파일이면 그 파일 하나만."""
    cur_rel = take.file.replace("\\", "/")
    cur_path = project.resolve(take.file)
    name = Path(cur_rel).name
    rel_dir = str(Path(cur_rel).parent).replace("\\", "/")
    m = _LINE_RE.match(name)
    files: list[tuple[int, str, Path]] = []
    if m and cur_path is not None and cur_path.parent.is_dir():
        num = m.group(1)
        for f in cur_path.parent.iterdir():
            mm = _LINE_RE.match(f.name)
            if mm and mm.group(1) == num and f.is_file():
                files.append((int(mm.group(2)), _rel_join(rel_dir, f.name), f))
        files.sort(key=lambda x: x[0])
    elif cur_path is not None and cur_path.exists():
        files = [(0, cur_rel, cur_path)]
    out = []
    for v, rel, path in files:
        try:
            mt = path.stat().st_mtime
        except OSError:
            mt = 0.0
        out.append(TakeVersion(rel, path, v, mt, _audio_len(path), path.name.lower() == name.lower()))
    return out


def count_versions(project: Project, subtitle_index: int) -> int:
    """자막 줄 번호로 녹음 파일 개수 (녹음 탭 목록 표시용). 지금 쓰는 조각의 파일 번호를 우선 사용."""
    tk = find_take(project, subtitle_index)
    if tk is not None:
        return len(take_versions(project, tk))
    folder = project.resolve(project.dubbing.folder or "takes")
    if folder is None or not folder.is_dir():
        return 0
    num = f"{subtitle_index:04d}"
    return sum(1 for f in folder.iterdir() if (m := _LINE_RE.match(f.name)) and m.group(1) == num)


def use_version(take: Take, rel: str, length: float) -> None:
    """이 조각이 다른 녹음 파일을 쓰게 — 위치·볼륨은 그대로, 자르기 구간은 새 파일 길이 안에서 유지."""
    take.file = rel
    ts = float(take.trim_start or 0.0)
    if length > 0:
        if ts >= length - 0.05:
            ts = 0.0
        take.trim_start = round(ts, 4) if ts > 0 else 0.0
        if take.trim_end is not None and (take.trim_end > length - 1e-3 or take.trim_end <= ts + 0.05):
            take.trim_end = None


# ------------------------------------------------------------------ 녹음본 이름 · ★ (project.dubbing.notes)
def note_key(rel: str) -> str:
    return rel.replace("\\", "/")


def take_note(project: Project, rel: str) -> dict:
    """{'name': str, 'star': bool} (없으면 빈 값)."""
    n = project.dubbing.notes.get(note_key(rel)) or {}
    return {"name": str(n.get("name") or ""), "star": bool(n.get("star"))}


def set_take_note(project: Project, rel: str, name: Optional[str] = None, star: Optional[bool] = None) -> None:
    k = note_key(rel)
    cur = take_note(project, rel)
    if name is not None:
        cur["name"] = name.strip()
    if star is not None:
        cur["star"] = bool(star)
    clean = {kk: v for kk, v in cur.items() if v}
    if clean:
        project.dubbing.notes[k] = clean
    else:
        project.dubbing.notes.pop(k, None)


def drop_take_note(project: Project, rel: str) -> None:
    project.dubbing.notes.pop(note_key(rel), None)
