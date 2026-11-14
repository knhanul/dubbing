import re
from pathlib import Path

from composer.render.ffmpeg_builder import (RenderSettings, build_assembly, build_clip_job, clip_cache_key, fmt,
                                            motion_filter, motion_quad, subtitle_force_style, video_encoder_args)
from composer.schema import BGM, Clip, Project, Transition
from composer.subtitles import Cue
from composer.timeline import build_timeline


def _graph(args):
    return args[args.index("-filter_complex") + 1]


def _split_outside_quotes(s, sep):
    parts, cur, q = [], "", False
    for ch in s:
        if ch == "'":
            q = not q
        if ch == sep and not q:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def test_fmt():
    assert fmt(2.0) == "2" and fmt(0.1 + 0.2) == "0.3" and fmt(-0.0) == "0" and fmt(29.97) == "29.97"


def test_motion_quad_has_no_commas_and_starts_full_frame():
    for m in ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "ken_burns", "zoom_in_left"):
        q = motion_quad(m, 0.2, 90)
        assert all("," not in v for v in q.values())
    # 줌 인 첫 프레임(in=0)은 원본 전체: x0=0, x1=W 가 되어야 함
    q = motion_quad("zoom_in", 0.2, 90, "linear")
    env = {"W": 1000.0, "H": 500.0, "in": 0.0, "PI": 3.141592653589793}

    def ev(expr):
        from math import cos
        return eval(expr.replace("in", "IN"), {"cos": cos}, {**env, "IN": env["in"]})

    assert abs(ev(q["x0"])) < 1e-9 and abs(ev(q["x1"]) - 1000) < 1e-9
    env["in"] = 89.0
    assert abs(ev(q["x1"]) - ev(q["x0"]) - 1000 / 1.2) < 1e-6  # 마지막 프레임은 1.2배 확대
    f = motion_filter("pan_right", 0.1, 30)
    assert f.startswith("perspective=") and "interpolation=cubic" in f and "eval=frame" in f


def test_image_motion_job_uses_supersampling(tmp_path):
    p = Project(clips=[Clip(file="a.png", type="image", duration=2, motion="zoom_in", motion_strength=0.2)])
    p.output.width, p.output.height, p.output.fps = 1280, 720, 25
    tl = build_timeline(p, lambda c: None)
    job = build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(upscale=2), Path("a.png"), tmp_path / "o.mkv", False)
    g = _graph(job.args)
    assert "scale=2560:1440:force_original_aspect_ratio=increase" in g and "crop=2560:1440" in g
    assert "format=yuv444p" in g and "perspective=" in g and "scale=1280:720:flags=lanczos" in g
    assert "-loop" in job.args and job.args[job.args.index("-t") + 1] == "2"
    assert "anullsrc=r=48000:cl=stereo" in job.args  # 이미지 = 무음 트랙
    # 쉼표로 나눈 필터들이 따옴표 밖에서 정상 분리되는지
    chain = _split_outside_quotes(g.split("]", 1)[1].rsplit("[", 1)[0], ",")
    assert any(c.startswith("perspective=") for c in chain)
    # 초안은 슬라이스 1배 + 저해상도
    d = build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(draft=True), Path("a.png"), tmp_path / "d.mkv", False)
    assert "scale=640:360" in _graph(d.args) and "ultrafast" in d.args


def test_static_image_and_contain_fit(tmp_path):
    p = Project(clips=[Clip(file="a.png", type="image", duration=1.5, motion="none", fit="contain")])
    p.output.background = "#102030"
    tl = build_timeline(p, lambda c: None)
    g = _graph(build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(), Path("a.png"), tmp_path / "o.mkv", False).args)
    assert "perspective" not in g and "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x102030" in g


def test_video_job_speed_trim_audio(tmp_path):
    p = Project(clips=[Clip(file="v.mp4", type="video", trim_start=1, trim_end=9, speed=3, volume=0.5)])
    tl = build_timeline(p, lambda c: 20.0)
    job = build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(), Path("v.mp4"), tmp_path / "o.mkv", True)
    a = job.args
    assert a[a.index("-ss") + 1] == "1" and a[a.index("-t") + 1] == "8"
    g = _graph(a)
    assert "setpts=(PTS-STARTPTS)/3" in g and "atempo=2,atempo=1.5" in g and "volume=0.5" in g
    dur = fmt(8 / 3)
    assert f"tpad=stop_mode=clone:stop_duration={dur}" in g and f"apad=whole_dur={dur}" in g
    # 음소거면 원본 오디오 대신 무음
    p.clips[0].mute = True
    g2 = _graph(build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(), Path("v.mp4"), tmp_path / "o.mkv", True).args)
    assert "[0:a]" not in g2


def test_encoder_args():
    assert "h264_nvenc" in video_encoder_args(RenderSettings(encoder="h264_nvenc"), "final", 30)
    assert video_encoder_args(RenderSettings(), "final", 30)[:2] == ["-c:v", "libx264"]
    assert "-g" in video_encoder_args(RenderSettings(), "intermediate", 30)


def test_cache_key_changes_only_when_relevant():
    p = Project(clips=[Clip(file="a.png", type="image", duration=2, motion="zoom_in")])
    tl = build_timeline(p, lambda c: None)
    k = lambda: clip_cache_key(p, p.clips[0], build_timeline(p, lambda c: None).clips[0], RenderSettings(), (1, 2), False)  # noqa: E731
    k0 = k()
    p.clips[0].label = "이름만 바꿈"
    p.clips[0].transition = Transition("fade", 1)
    assert k() == k0  # 이름표/전환은 중간 파일과 무관
    p.clips[0].motion_strength = 0.3
    assert k() != k0
    assert clip_cache_key(p, p.clips[0], tl.clips[0], RenderSettings(draft=True), (1, 2), False) != k()


def _assembly_project():
    p = Project(transition=Transition("crossfade", 0.5), clips=[
        Clip(file="a.png", type="image", duration=3),
        Clip(file="b.png", type="image", duration=3, transition=Transition("none", 0)),
        Clip(file="c.png", type="image", duration=2, transition=Transition("wipeleft", 1.0)),
        Clip(file="d.png", type="image", duration=2, transition=Transition("fade", 0.5)),
    ])
    p.bgm = BGM(file="m.mp3", volume=0.4, start_offset=1.0, fade_in=1, fade_out=2, loop=True, duck_under_voice=True)
    return p


def test_assembly_transitions_and_audio():
    p = _assembly_project()
    tl = build_timeline(p, lambda c: None)
    files = [Path(f"c{i}.mkv") for i in range(4)]
    cues = [Cue(1, 1.0, 2.0, "가")]
    plan = build_assembly(p, tl, files, RenderSettings(), Path("out.mp4"), cues, "subs.srt", Path("m.mp3"), 30.0,
                          [(Path("t1.wav"), 0.5, 1.0), (Path("t2.wav"), -0.3, 0.8)])
    g = plan.filter_graph
    assert plan.total == tl.total == 10.0  # 클립 길이 합 (전환은 경계 가운데에 걸치고 길이를 줄이지 않음)
    assert "concat=n=2:v=1:a=1" in g  # a→b 전환 없음
    # 중간 파일에 전환 여분 포함: b=3+0.5, c=0.5+2+0.25, d=0.25+2 → 전환이 경계(6초, 8초) 가운데에 걸침
    assert "xfade=transition=wipeleft:duration=1:offset=5.5" in g  # 6 - 1/2
    assert "xfade=transition=fadeblack:duration=0.5:offset=7.75" in g  # 8 - 0.5/2
    assert g.count("acrossfade") == 2
    assert "subtitles=filename=subs.srt:force_style='FontName=Malgun Gothic" in g
    assert "adelay=delays=500:all=1" in g and "atrim=start=0.3" in g and "volume=0.8" in g
    assert "sidechaincompress" in g and "asplit=2" in g
    assert "afade=t=in:st=0:d=1" in g and "afade=t=out:st=7:d=2" in g  # BGM 길이 9
    assert "adelay=delays=1000:all=1" in g
    a = plan.args
    assert a[a.index("-stream_loop") + 1] == "-1" and a.index("-stream_loop") < a.index("m.mp3")
    assert "alimiter" in g and "-movflags" in a and a[a.index("-t") + 1] == "10"
    # 모든 라벨이 정의된 뒤 사용되는지 (간단한 그래프 검사)
    defined = set()
    for stmt in g.split(";"):
        outs = re.findall(r"\[([^\]]+)\]$", stmt) or []
        ins = re.findall(r"^((?:\[[^\]]+\])+)", stmt)
        for lab in re.findall(r"\[([^\]]+)\]", ins[0] if ins else ""):
            assert re.match(r"\d+:[av]", lab) or lab in defined, lab
        m = re.search(r"((?:\[[^\]]+\])+)$", stmt)
        defined.update(re.findall(r"\[([^\]]+)\]", m.group(1)))
    assert {"vout", "aout"} <= defined


def test_soft_subtitles_and_no_extras():
    p = Project(clips=[Clip(file="a.png", type="image", duration=2)])
    p.subtitle.burn_in = False
    tl = build_timeline(p, lambda c: None)
    plan = build_assembly(p, tl, [Path("c0.mkv")], RenderSettings(), Path("o.mp4"), [Cue(1, 0, 1, "x")], "subs.srt")
    assert "mov_text" in plan.args and "subtitles=" not in plan.filter_graph
    assert "[abase]atrim=duration=2[aout]" in plan.filter_graph
    plan2 = build_assembly(p, tl, [Path("c0.mkv")], RenderSettings(burn_subtitles=False), Path("o.mp4"), [], None)
    assert "mov_text" not in plan2.args


def test_force_style_scaling():
    p = Project()
    p.output.height = 1080
    p.subtitle.style.size = 54
    p.subtitle.style.color = "#FF8000"
    s = subtitle_force_style(p, 1080)
    assert "FontSize=14.4" in s and "PrimaryColour=&H000080FF" in s and "," in s
