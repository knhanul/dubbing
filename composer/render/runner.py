"""렌더 실행: 클립 캐시 → 조립, 진행률 콜백과 취소 지원 (UI 와 무관한 순수 파이썬)."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..probe import ffmpeg_path, popen_kwargs, probe
from ..schema import Project
from ..subtitles import project_cues, to_srt
from .ass import build_ass
from ..timeline import build_timeline
from .ffmpeg_builder import (RenderSettings, build_assembly, build_clip_job, clip_cache_key,
                             take_chain, take_entries)

ProgressFn = Callable[[float, str], None]


class RenderError(RuntimeError):
    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


class RenderCancelled(RuntimeError):
    pass


@dataclass
class RenderResult:
    output: Path
    duration: float
    clips_rendered: int
    clips_cached: int


def cache_dir(project: Project) -> Path:
    return project.base_dir / ".composer_cache"


def clear_cache(project: Project) -> None:
    shutil.rmtree(cache_dir(project), ignore_errors=True)


def source_duration(project: Project):
    def fn(clip):
        p = project.resolve(clip.file)
        return probe(p).duration if p and p.exists() else None
    return fn


def _run_ffmpeg(args: list[str], duration: float, on_frac: Callable[[float], None],
                cancel: threading.Event, log_path: Path, cwd: Optional[Path] = None) -> None:
    cmd = [ffmpeg_path()] + args
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(" ".join(f'"{a}"' if " " in a else a for a in cmd) + "\n\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", cwd=str(cwd) if cwd else None, **popen_kwargs())
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel.is_set():
                    proc.kill()
                    break
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and val.strip().lstrip("-").isdigit():
                    t = int(val) / 1_000_000.0
                    if duration > 0:
                        on_frac(max(0.0, min(1.0, t / duration)))
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if cancel.is_set():
        raise RenderCancelled()
    if proc.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise RenderError(f"ffmpeg 가 오류로 끝났습니다 (코드 {proc.returncode}). 로그: {log_path}", tail)


def render_project(project: Project, output: Path | str, settings: RenderSettings,
                   progress: Optional[ProgressFn] = None, cancel: Optional[threading.Event] = None) -> RenderResult:
    progress = progress or (lambda f, m: None)
    cancel = cancel or threading.Event()
    output = Path(output)
    if not output.is_absolute():
        output = project.base_dir / output
    output.parent.mkdir(parents=True, exist_ok=True)
    from ..tracks import effective_project
    project = effective_project(project)  # 👁/🔇/S 트랙 설정 반영
    if not project.clips:
        raise RenderError("클립이 없습니다.")
    for c in project.clips:
        p = project.resolve(c.file)
        if not p or not p.exists():
            raise RenderError(f"파일을 찾을 수 없습니다: {c.file}")

    cdir = cache_dir(project)
    cdir.mkdir(parents=True, exist_ok=True)
    work = cdir / ("work_draft" if settings.draft else "work_final")
    work.mkdir(exist_ok=True)

    timeline = build_timeline(project, source_duration(project))
    # 이미지 움직임은 계산이 무거우므로 가중치를 크게 줍니다.
    weights = []
    jobs = []
    for clip, timing in zip(project.clips, timeline.clips):
        src = project.resolve(clip.file)
        st = src.stat()
        has_audio = probe(src).has_audio if clip.type == "video" else False
        key = clip_cache_key(project, clip, timing, settings, (st.st_size, int(st.st_mtime)), has_audio)
        out = cdir / f"clip_{key}.mkv"
        job = build_clip_job(project, clip, timing, settings, src, out, has_audio, key)
        jobs.append(job)
        heavy = 3.0 if clip.type == "image" and clip.motion != "none" else 1.0
        weights.append(0.0 if out.exists() else timing.duration * heavy)
    assemble_w = max(0.5, timeline.total * 0.8)
    total_w = sum(weights) + assemble_w
    done_w = 0.0
    rendered = cached = 0

    for i, (job, w) in enumerate(zip(jobs, weights)):
        if cancel.is_set():
            raise RenderCancelled()
        name = project.clips[i].file
        if job.output.exists():
            cached += 1
            progress(done_w / total_w, f"클립 {i + 1}/{len(jobs)} 캐시 사용: {name}")
            continue
        tmp = job.output.with_name(job.output.stem + ".part.mkv")
        args = job.args[:-1] + [str(tmp)]
        msg = f"클립 {i + 1}/{len(jobs)} 준비 중: {name}"
        progress(done_w / total_w, msg)
        _run_ffmpeg(args, job.duration, lambda f, b=done_w, w=w: progress((b + f * w) / total_w, msg),
                    cancel, work / f"clip_{i + 1}.log")
        os.replace(tmp, job.output)
        rendered += 1
        done_w += w

    cues = project_cues(project)
    subs_name = ass_name = None
    if cues:
        subs_name = "subs.srt"  # 소프트 자막용
        (work / subs_name).write_text(to_srt(cues), encoding="utf-8")
        ass_name = "subs.ass"  # 새겨 넣기용 (스타일·위치·나타나기 효과)
        (work / ass_name).write_text(build_ass(project, cues), encoding="utf-8")
    takes = []
    for rel, pos, vol, ts, te in take_entries(project, cues):
        p = project.resolve(rel)
        if p and p.exists():
            takes.append((p, pos, vol, ts, te))
    bgm_path = project.resolve(project.bgm.file) if project.bgm.file else None
    bgm_dur = None
    if bgm_path is not None:
        if not bgm_path.exists():
            raise RenderError(f"배경음악 파일을 찾을 수 없습니다: {project.bgm.file}")
        bgm_dur = probe(bgm_path).duration
    tmp_out = output.with_name(output.stem + ".part" + output.suffix)
    plan = build_assembly(project, timeline, [j.output for j in jobs], settings, tmp_out, cues,
                          subs_name, bgm_path, bgm_dur, takes, ass_name=ass_name)
    msg = "최종 조립 중(전환·자막·더빙·배경음악)…"
    progress(done_w / total_w, msg)
    _run_ffmpeg(plan.args, plan.total, lambda f: progress((done_w + f * assemble_w) / total_w, msg),
                cancel, work / "assemble.log", cwd=work)
    os.replace(tmp_out, output)
    progress(1.0, f"완료: {output.name}")
    return RenderResult(output, timeline.total, rendered, cached)


def export_voice_wav(project: Project, output: Path | str, cancel: Optional[threading.Event] = None) -> Path:
    """더빙 테이크만 타임라인 위치에 맞춰 섞은 WAV."""
    from .ffmpeg_builder import AUDIO_RATE, fmt

    cancel = cancel or threading.Event()
    output = Path(output)
    timeline = build_timeline(project, source_duration(project))
    cues = project_cues(project)
    total = fmt(timeline.total)
    args = ["-hide_banner", "-y", "-nostdin", "-f", "lavfi", "-t", total, "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
    g, labels = [], ["[0:a]"]
    k = 1
    for rel, pos, vol, ts, te in take_entries(project, cues):
        p = project.resolve(rel)
        if not p or not p.exists():
            continue
        args += ["-i", str(p)]
        chain, pos = take_chain(pos, ts, te)
        chain.append(f"volume={fmt(vol * project.dubbing.volume)}")
        if pos > 0:
            chain.append(f"adelay=delays={int(round(pos * 1000))}:all=1")
        g.append(f"[{k}:a]{','.join(chain)}[t{k}]")
        labels.append(f"[t{k}]")
        k += 1
    g.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=first,atrim=duration={total}[out]")
    args += ["-filter_complex", ";".join(g), "-map", "[out]", "-c:a", "pcm_s16le", "-progress", "pipe:1",
             "-nostats", str(output)]
    cdir = cache_dir(project)
    cdir.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(args, timeline.total, lambda f: None, cancel, cdir / "voice.log")
    return output


def render_bgm_stem(project: Project, output: Path | str, cancel: Optional[threading.Event] = None) -> Optional[Path]:
    """미리보기용: 배경음악만(더빙 밑 줄이기 포함) WAV. 배경음악이 없으면 None."""
    from .ffmpeg_builder import build_bgm_stem

    cancel = cancel or threading.Event()
    bgm_path = project.resolve(project.bgm.file) if project.bgm.file else None
    if bgm_path is None or not bgm_path.exists() or not project.clips:
        return None
    timeline = build_timeline(project, source_duration(project))
    takes = []
    for rel, pos, vol, ts, te in take_entries(project, project_cues(project)):
        p = project.resolve(rel)
        if p and p.exists():
            takes.append((p, pos, vol, ts, te))
    output = Path(output)
    tmp = output.with_name(output.stem + ".part" + output.suffix)
    args = build_bgm_stem(project, timeline.total, bgm_path, probe(bgm_path).duration, takes, tmp)
    if args is None:
        return None
    cdir = cache_dir(project)
    cdir.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(args, timeline.total, lambda f: None, cancel, cdir / "bgm_stem.log")
    os.replace(tmp, output)
    return output
