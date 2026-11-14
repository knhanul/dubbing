"""ffmpeg 명령줄 생성기.

렌더는 두 단계입니다.

1. **클립 정규화**: 각 클립을 같은 해상도·fps·픽셀형식·오디오(48kHz 스테레오)로 만든
   중간 파일(.mkv)을 생성합니다. 이 파일은 캐시되어, 다른 클립만 바꾸면 다시 만들지 않습니다.
2. **조립**: 중간 파일들을 전환(xfade/acrossfade)으로 잇고, 자막·더빙·BGM 을 섞어
   최종 mp4 를 만듭니다.

이미지 움직임 떨림 방지
-----------------------
ffmpeg ``zoompan`` 은 잘라낼 위치/크기를 정수 픽셀로 반올림하기 때문에 프레임마다 0~1픽셀씩
불규칙하게 튀어 "떨림"이 생깁니다(4배 확대 후 zoompan 을 써도 일부 남습니다). 여기서는

* 이미지를 출력 크기의 ``upscale``배(기본 2배)로 고품질(lanczos) 확대해 두고,
* ``perspective`` 필터로 잘라낼 사각형의 네 꼭짓점을 **소수점 좌표**로 매 프레임 계산해
  bicubic 보간으로 샘플링하고(반올림 없음),
* 마지막에 출력 크기로 lanczos 축소해 선명도를 유지합니다.
* 줌/위치는 프레임 번호 기반 연속 함수(선형 또는 코사인 이징)이고 fps 는 고정입니다.

tests/test_render_e2e.py 에서 위상 상관으로 프레임 간 이동량을 측정해 떨림을 검사합니다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..schema import Clip, Project
from ..subtitles import Cue
from ..timeline import ClipTiming, Timeline, atempo_chain, take_position

BUILDER_VERSION = 4
AUDIO_RATE = 48000


@dataclass
class RenderSettings:
    draft: bool = False
    encoder: str = "libx264"  # libx264 | h264_nvenc
    upscale: int = 2  # 이미지 움직임 계산용 확대 배율(슈퍼샘플링)
    draft_height: int = 360
    burn_subtitles: Optional[bool] = None  # None 이면 project.subtitle.burn_in

    def size(self, project: Project) -> tuple[int, int]:
        w, h = int(project.output.width), int(project.output.height)
        if self.draft and h > self.draft_height:
            nh = self.draft_height
            nw = int(round(w * nh / h / 2.0)) * 2
            return max(2, nw), nh
        return w, h

    def motion_upscale(self) -> int:
        return 1 if self.draft else max(1, int(self.upscale))


def fmt(v: float) -> str:
    """ffmpeg 에 넘길 숫자 문자열 (불필요한 0 제거)."""
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def video_encoder_args(settings: RenderSettings, stage: str, fps: float) -> list[str]:
    """stage: 'intermediate' | 'final'"""
    gop = str(max(1, int(round(fps * 2))))
    nv = settings.encoder == "h264_nvenc"
    if settings.draft:
        if nv:
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30", "-g", gop, "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-g", gop, "-pix_fmt", "yuv420p"]
    if stage == "intermediate":
        if nv:
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "14", "-b:v", "0",
                    "-g", gop, "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", gop, "-pix_fmt", "yuv420p"]
    if nv:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                "-g", gop, "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-g", gop, "-pix_fmt", "yuv420p"]


def _fit_filters(fit: str, w: int, h: int, bg: str, flags: str = "lanczos") -> list[str]:
    color = bg.replace("#", "0x")
    if fit == "contain":
        return [f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags={flags}",
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={color}"]
    return [f"scale={w}:{h}:force_original_aspect_ratio=increase:flags={flags}",
            f"crop={w}:{h}"]


def motion_quad(motion: str, strength: float, frames: int, easing: str = "ease") -> dict[str, str]:
    """perspective 필터용 원본 좌표 꼭짓점 식(x0..y3). 쉼표 없는 식만 사용합니다.

    W, H 는 (확대된) 입력 크기, in 은 프레임 번호입니다.
    """
    n1 = max(1, frames - 1)
    p = f"(in/{n1})" if easing == "linear" else f"((1-cos(PI*in/{n1}))/2)"
    s = fmt(max(0.0, min(1.0, strength)))
    zin, zout, zfix = f"(1+{s}*{p})", f"(1+{s}*(1-{p}))", f"(1+{s})"
    # (줌 식, 가로 여유 공간 중 위치 비율 0~1, 세로 위치 비율 0~1)
    table = {
        "zoom_in": (zin, "0.5", "0.5"),
        "zoom_out": (zout, "0.5", "0.5"),
        "pan_right": (zfix, p, "0.5"),
        "pan_left": (zfix, f"(1-{p})", "0.5"),
        "pan_down": (zfix, "0.5", p),
        "pan_up": (zfix, "0.5", f"(1-{p})"),
        "zoom_in_left": (zin, f"(0.5-0.5*{p})", "0.5"),
        "zoom_in_right": (zin, f"(0.5+0.5*{p})", "0.5"),
        "ken_burns": (zin, p, p),
    }
    z, fx, fy = table.get(motion, ("1", "0", "0"))
    cw, ch = f"(W/{z})", f"(H/{z})"
    x, y = f"((W-W/{z})*{fx})", f"((H-H/{z})*{fy})"
    return {"x0": x, "y0": y, "x1": f"{x}+{cw}", "y1": y,
            "x2": x, "y2": f"{y}+{ch}", "x3": f"{x}+{cw}", "y3": f"{y}+{ch}"}


def motion_filter(motion: str, strength: float, frames: int, easing: str = "ease") -> str:
    q = motion_quad(motion, strength, frames, easing)
    pts = ":".join(f"{k}='{v}'" for k, v in q.items())
    return f"perspective={pts}:interpolation=cubic:sense=source:eval=frame"


def frame_count(duration: float, fps: float) -> int:
    return max(1, int(round(duration * fps)))


@dataclass
class ClipJob:
    index: int
    args: list[str]  # ffmpeg 실행 파일 이름을 제외한 인자
    output: Path
    duration: float
    cache_key: str


def clip_cache_key(project: Project, clip: Clip, timing: ClipTiming, settings: RenderSettings,
                   source_stat: tuple, has_audio: bool) -> str:
    w, h = settings.size(project)
    d = asdict(clip)
    d.pop("transition", None)
    d.pop("label", None)
    payload = {
        "v": BUILDER_VERSION, "clip": d, "dur": round(timing.duration, 4), "speed": round(timing.speed, 6),
        "in": round(timing.source_in, 4), "out": round(timing.source_out, 4),
        "w": w, "h": h, "fps": project.output.fps, "fit": clip.fit or project.output.fit,
        "bg": project.output.background, "draft": settings.draft, "up": settings.motion_upscale(),
        "enc": video_encoder_args(settings, "intermediate", project.output.fps),
        "src": list(source_stat), "audio": has_audio,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def build_clip_job(project: Project, clip: Clip, timing: ClipTiming, settings: RenderSettings,
                   source: Path, output: Path, has_audio: bool, cache_key: str = "") -> ClipJob:
    w, h = settings.size(project)
    fps = float(project.output.fps)
    fps_s = fmt(fps)
    fit = clip.fit or project.output.fit
    bg = project.output.background
    dur = timing.duration
    dur_s = fmt(dur)
    args: list[str] = ["-hide_banner", "-y", "-nostdin"]
    vf: list[str]
    use_audio = clip.type == "video" and has_audio and not clip.mute and clip.volume > 0

    if clip.type == "image":
        motion = clip.motion or "none"
        if motion == "none" or clip.motion_strength <= 0:
            args += ["-loop", "1", "-framerate", fps_s, "-t", dur_s, "-i", str(source)]
            vf = _fit_filters(fit, w, h, bg) + [f"fps={fps_s}"]
        else:
            k = settings.motion_upscale()
            n = frame_count(dur, fps)
            args += ["-loop", "1", "-framerate", fps_s, "-t", dur_s, "-i", str(source)]
            vf = _fit_filters(fit, w * k, h * k, bg) + [
                "format=yuv444p",
                motion_filter(motion, clip.motion_strength, n, clip.motion_easing),
            ]
            if k > 1:
                vf.append(f"scale={w}:{h}:flags=lanczos")
            vf.append(f"fps={fps_s}")
    else:
        used = timing.source_out - timing.source_in
        if timing.source_in > 0:
            args += ["-ss", fmt(timing.source_in)]
        args += ["-t", fmt(used), "-i", str(source)]
        sp = timing.speed
        vf = [f"setpts=(PTS-STARTPTS)/{fmt(sp)}" if abs(sp - 1) > 1e-6 else "setpts=PTS-STARTPTS",
              f"fps={fps_s}"] + _fit_filters(fit, w, h, bg)

    vf += ["setsar=1", "format=yuv420p",
           f"tpad=stop_mode=clone:stop_duration={dur_s}", f"trim=duration={dur_s}", "setpts=PTS-STARTPTS"]

    if use_audio:
        af = ["asetpts=PTS-STARTPTS"] + atempo_chain(timing.speed) + [
            f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if abs(clip.volume - 1) > 1e-6:
            af.append(f"volume={fmt(clip.volume)}")
        af += [f"apad=whole_dur={dur_s}", f"atrim=duration={dur_s}"]
        graph = f"[0:v]{','.join(vf)}[v];[0:a]{','.join(af)}[a]"
        amap = "[a]"
    else:
        args += ["-f", "lavfi", "-t", dur_s, "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        graph = f"[0:v]{','.join(vf)}[v]"
        amap = "1:a"
    args += ["-filter_complex", graph, "-map", "[v]", "-map", amap]
    args += video_encoder_args(settings, "intermediate", fps)
    args += ["-r", fps_s, "-c:a", "pcm_s16le", "-ar", str(AUDIO_RATE), "-ac", "2", "-t", dur_s,
             "-progress", "pipe:1", "-nostats", str(output)]
    return ClipJob(timing.index, args, output, dur, cache_key)


# ---------------------------------------------------------------------------
# 자막 스타일
# ---------------------------------------------------------------------------
def _ass_color(hex_color: str) -> str:
    h = (hex_color or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def subtitle_force_style(project: Project, height: int) -> str:
    st = project.subtitle.style
    k = 288.0 / max(1, project.output.height)  # libass SRT 기본 PlayResY=288 기준으로 환산
    parts = [
        f"FontName={st.font}",
        f"FontSize={fmt(round(st.size * k, 2))}",
        f"PrimaryColour={_ass_color(st.color)}",
        f"OutlineColour={_ass_color(st.outline_color)}",
        "BorderStyle=1",
        f"Outline={fmt(round(st.outline * k, 2))}",
        "Shadow=0",
        f"Bold={-1 if st.bold else 0}",
        f"MarginV={int(round(st.margin_v * k))}",
        "Alignment=2",
    ]
    return ",".join(parts)


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------
XFADE_NAMES = {"fade": "fadeblack", "crossfade": "fade"}


@dataclass
class AssemblyPlan:
    args: list[str]
    filter_graph: str
    total: float
    inputs: list[str] = field(default_factory=list)


def build_assembly(project: Project, timeline: Timeline, clip_files: list[Path], settings: RenderSettings,
                   output: Path, cues: list[Cue], subs_name: Optional[str] = "subs.srt",
                   bgm_path: Optional[Path] = None, bgm_duration: Optional[float] = None,
                   take_files: Optional[list[tuple[Path, float, float]]] = None) -> AssemblyPlan:
    """take_files: (파일, 타임라인 위치(초), 볼륨) 목록. 자막 파일은 작업 폴더(cwd)에 subs_name 으로 있어야 합니다."""
    w, h = settings.size(project)
    fps = float(project.output.fps)
    total = timeline.total
    total_s = fmt(total)
    args = ["-hide_banner", "-y", "-nostdin"]
    inputs: list[str] = []
    for f in clip_files:
        args += ["-i", str(f)]
        inputs.append(str(f))
    n = len(clip_files)
    g: list[str] = []
    # 모든 입력의 시간 기준/프레임레이트를 맞춥니다(xfade 는 고정 fps 와 같은 timebase 를 요구).
    for i in range(n):
        g.append(f"[{i}:v]setpts=PTS-STARTPTS,fps={fmt(fps)}[v{i}]")
        g.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
    cur_v, cur_a = "v0", "a0"
    length = timeline.clips[0].duration if timeline.clips else 0.0
    for i in range(1, n):
        t = timeline.clips[i]
        tr = t.transition_in
        nv, na = f"vx{i}", f"ax{i}"
        if tr.type == "none" or tr.duration <= 0:
            g.append(f"[{cur_v}][{cur_a}][v{i}][a{i}]concat=n=2:v=1:a=1[{nv}c][{na}]")
            g.append(f"[{nv}c]fps={fmt(fps)}[{nv}]")
            length += t.duration
        else:
            name = XFADE_NAMES.get(tr.type, tr.type)
            off = max(0.0, length - tr.duration)
            g.append(f"[{cur_v}][v{i}]xfade=transition={name}:duration={fmt(tr.duration)}:offset={fmt(off)}[{nv}]")
            g.append(f"[{cur_a}][a{i}]acrossfade=d={fmt(tr.duration)}:c1=tri:c2=tri[{na}]")
            length = length + t.duration - tr.duration
        cur_v, cur_a = nv, na

    burn = settings.burn_subtitles if settings.burn_subtitles is not None else project.subtitle.burn_in
    vlast = cur_v
    if cues and burn and subs_name:
        style = subtitle_force_style(project, h)
        g.append(f"[{vlast}]subtitles=filename={subs_name}:force_style='{style}'[vsub]")
        vlast = "vsub"
    g.append(f"[{vlast}]format=yuv420p[vout]")

    # --- 오디오 ---
    idx = n
    g.append(f"[{cur_a}]volume={fmt(project.output.original_volume)}[abase]")
    mix = ["[abase]"]
    voice_labels = []
    vol_dub = project.dubbing.volume
    for j, (path, pos, vol) in enumerate(take_files or []):
        args += ["-i", str(path)]
        inputs.append(str(path))
        chain = [f"aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        if pos < 0:
            chain += [f"atrim=start={fmt(-pos)}", "asetpts=PTS-STARTPTS"]
            pos = 0.0
        chain.append(f"volume={fmt(vol * vol_dub)}")
        ms = int(round(pos * 1000))
        if ms > 0:
            chain.append(f"adelay=delays={ms}:all=1")
        g.append(f"[{idx}:a]{','.join(chain)}[t{j}]")
        voice_labels.append(f"[t{j}]")
        idx += 1
    voice = None
    if voice_labels:
        if len(voice_labels) == 1:
            g.append(f"{voice_labels[0]}apad=whole_dur={total_s},atrim=duration={total_s}[voice]")
        else:
            g.append(f"{''.join(voice_labels)}amix=inputs={len(voice_labels)}:normalize=0:duration=longest,"
                     f"apad=whole_dur={total_s},atrim=duration={total_s}[voice]")
        voice = "voice"

    bgm = project.bgm
    if bgm_path is not None and bgm.file and bgm.volume > 0 and bgm.start_offset < total:
        if bgm.loop:
            args += ["-stream_loop", "-1"]
        args += ["-i", str(bgm_path)]
        inputs.append(str(bgm_path))
        avail = total - bgm.start_offset
        if not bgm.loop and bgm_duration:
            avail = min(avail, max(0.1, bgm_duration - bgm.file_start))
        blen = fmt(avail)
        chain = []
        if bgm.file_start > 0:
            chain.append(f"atrim=start={fmt(bgm.file_start)}")
        chain += ["asetpts=PTS-STARTPTS", f"aresample={AUDIO_RATE}",
                  "aformat=sample_fmts=fltp:channel_layouts=stereo", f"atrim=duration={blen}"]
        if bgm.fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={fmt(min(bgm.fade_in, avail))}")
        if bgm.fade_out > 0:
            fo = min(bgm.fade_out, avail)
            chain.append(f"afade=t=out:st={fmt(avail - fo)}:d={fmt(fo)}")
        chain.append(f"volume={fmt(bgm.volume)}")
        ms = int(round(bgm.start_offset * 1000))
        if ms > 0:
            chain.append(f"adelay=delays={ms}:all=1")
        chain += [f"apad=whole_dur={total_s}", f"atrim=duration={total_s}"]
        g.append(f"[{idx}:a]{','.join(chain)}[bgm]")
        idx += 1
        if voice and bgm.duck_under_voice:
            g.append(f"[{voice}]asplit=2[voicemix][voicesc]")
            g.append(f"[bgm][voicesc]sidechaincompress=threshold=0.02:ratio={fmt(bgm.duck_ratio)}"
                     f":attack=20:release=400[bgmd]")
            voice = "voicemix"
            mix.append("[bgmd]")
        else:
            mix.append("[bgm]")
    if voice:
        mix.append(f"[{voice}]")
    if len(mix) > 1:
        g.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first:dropout_transition=0,"
                 f"alimiter=limit=0.97:level=0,atrim=duration={total_s}[aout]")
    else:
        g.append(f"[abase]atrim=duration={total_s}[aout]")

    graph = ";".join(g)
    soft = cues and not burn and subs_name
    if soft:
        args += ["-i", subs_name]
    args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
    if soft:
        args += ["-map", f"{idx}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=kor"]
    args += video_encoder_args(settings, "final", fps)
    args += ["-r", fmt(fps), "-c:a", "aac", "-b:a", "128k" if settings.draft else "192k", "-ar", str(AUDIO_RATE),
             "-t", total_s, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return AssemblyPlan(args, graph, total, inputs)


def take_entries(project: Project, cues: list[Cue]) -> list[tuple[str, float, float]]:
    """(상대 파일, 위치, 볼륨) — 파일 존재 여부는 호출자가 확인."""
    by_index = {c.index: c for c in cues}
    res = []
    for t in project.dubbing.takes:
        res.append((t.file, take_position(t, by_index, project.dubbing.latency_ms), t.volume))
    return res
