"""AI 답변(JSON)을 현재 프로젝트 타임라인에 적용하는 순수 로직."""
from __future__ import annotations

import copy
import os
from pathlib import Path

from .schema import Project

# AI 답변에 있으면 교체하는 항목. dubbing(직접 녹음한 목소리)은 항상 유지합니다.
REPLACE_KEYS = ("output", "transition", "clips")
MERGE_KEYS = ("subtitle", "bgm")  # 답변에 있을 때만 바꿈(없으면 현재 값 유지)


def is_blank(project: Project, has_path: bool) -> bool:
    """저장된 적 없고 아무 내용도 없는 프로젝트인지."""
    return (not has_path and not project.clips and not project.subtitle.file and not project.subtitle.items
            and not project.bgm.file and not project.dubbing.takes)


def _rebase(rel, src: Path, dst: Path):
    if not rel or not isinstance(rel, str):
        return rel
    p = Path(rel)
    if p.is_absolute():
        return rel
    target = (src / p).resolve()
    try:
        return Path(os.path.relpath(target, dst.resolve())).as_posix()
    except ValueError:  # 다른 드라이브
        return str(target)


def apply_ai_result(project: Project, data: dict, folder: Path | str, keep_base: bool) -> Project:
    """현재 프로젝트에 AI 결과를 반영한 새 Project 를 돌려줍니다.

    * output/transition/clips: AI 결과로 교체(output 은 AI 가 준 항목만 덮어씀)
    * subtitle/bgm: AI 결과에 있을 때만 교체(자막 미세 조정 목록은 새 파일이면 초기화)
    * dubbing: 그대로 유지
    * keep_base=True 면 현재 작업 폴더를 유지하고 AI 의 상대 경로를 그 폴더 기준으로 바꿉니다.
      False 면 작업 폴더를 AI 대화상자의 폴더로 바꿉니다(빈 프로젝트일 때).
    """
    folder = Path(folder)
    base = Path(project.base_dir) if keep_base else folder
    data = copy.deepcopy(data)
    if keep_base and folder.resolve() != base.resolve():
        for c in data.get("clips") or []:
            if isinstance(c, dict):
                c["file"] = _rebase(c.get("file"), folder, base)
        for key in MERGE_KEYS:
            v = data.get(key)
            if isinstance(v, str):
                data[key] = _rebase(v, folder, base)
            elif isinstance(v, dict) and v.get("file"):
                v["file"] = _rebase(v["file"], folder, base)
    cur = project.to_dict()
    new = copy.deepcopy(cur)
    if isinstance(data.get("output"), dict):
        new["output"] = {**cur["output"], **data["output"]}
    if "transition" in data:
        new["transition"] = data["transition"]
    new["clips"] = data.get("clips") or []
    for key in MERGE_KEYS:
        if key not in data or data[key] is None:
            continue
        v = data[key]
        if isinstance(v, str):
            v = {"file": v}
        merged = {**cur[key], **v}
        if key == "subtitle":
            if isinstance(v.get("style"), dict):
                merged["style"] = {**cur["subtitle"]["style"], **v["style"]}
            if "items" not in v and v.get("file") != cur["subtitle"].get("file"):
                merged["items"] = None
        new[key] = merged
    if data.get("title"):
        new["title"] = data["title"]
    return Project.from_dict(new, base_dir=base)
