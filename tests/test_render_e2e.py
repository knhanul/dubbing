"""실제 ffmpeg 로 합성 미디어를 만들어 끝까지 렌더해 보는 테스트."""
import shutil
import subprocess
import threading

import numpy as np
import pytest

from composer.probe import ffmpeg_path, probe
from composer.render.ffmpeg_builder import RenderSettings
from composer.render.runner import RenderCancelled, export_voice_wav, render_project
from composer.schema import Project

try:
    ffmpeg_path()
    HAVE_FFMPEG = True
except FileNotFoundError:
    HAVE_FFMPEG = False

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg 필요")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from media_fixtures import make_media

    d = tmp_path_factory.mktemp("media")
    make_media(d)
    return d


def _project(d):
    return Project.from_dict({
        "output": {"width": 640, "height": 360, "fps": 30},
        "transition": {"type": "crossfade", "duration": 0.5},
        "clips": [
            {"file": "img1.png", "duration": 2, "motion": "zoom_in", "motion_strength": 0.2},
            {"file": "vid1.mp4", "trim_start": 0.5, "trim_end": 2.5, "speed": 2.0,
             "transition": {"type": "fade", "duration": 0.4}},
            {"file": "vid_noaudio.mp4", "fit": "contain", "fit_duration": 1.5, "transition": {"type": "none"}},
            {"file": "img2.jpg", "duration": 2, "motion": "pan_right"},
        ],
        "subtitle": {"file": "subs.srt"},
        "bgm": {"file": "music.mp3", "volume": 0.3, "loop": True, "start_offset": 0.5},
        "dubbing": {"takes": [{"file": "take1.wav", "subtitle": 2, "offset": 0}]},
    }, base_dir=d)


def _rms(path, start, dur, af=None):
    extra = ["-af", af] if af else []
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-ss", str(start), "-t", str(dur), "-i", str(path), *extra,
                          "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def test_full_render_and_cache(media):
    p = _project(media)
    seen = []
    res = render_project(p, media / "out.mp4", RenderSettings(), lambda f, m: seen.append(f))
    info = probe(res.output)
    # 2 + 1 + 1.5 + 2 = 6.5 (전환은 경계 가운데에 걸치고 전체 길이를 줄이지 않음)
    assert res.duration == pytest.approx(6.5)
    assert info.duration == pytest.approx(6.5, abs=0.1)
    assert (info.width, info.height, info.has_audio) == (640, 360, True)
    assert res.clips_rendered == 4 and seen[-1] == 1.0
    assert all(0 <= f <= 1 for f in seen) and seen == sorted(seen)
    # 더빙(880Hz)은 2번 자막(3.0초)부터 1초간: 그 구간에서만 880Hz 성분이 큼
    band = "bandpass=f=880:width_type=h:w=60,bandpass=f=880:width_type=h:w=60"
    assert _rms(res.output, 3.1, 0.8, band) > 4 * _rms(res.output, 1.0, 1.5, band)

    # 한 클립만 바꾸면 나머지는 캐시 사용
    p.clips[3].motion_strength = 0.1
    res2 = render_project(p, media / "out2.mp4", RenderSettings())
    assert (res2.clips_rendered, res2.clips_cached) == (1, 3)


def test_draft_render_is_low_res(media):
    p = _project(media)
    p.output.width, p.output.height = 1280, 720
    res = render_project(p, media / "draft.mp4", RenderSettings(draft=True, draft_height=180))
    info = probe(res.output)
    assert (info.width, info.height) == (320, 180)


def test_voice_export(media):
    out = export_voice_wav(_project(media), media / "voice.wav")
    assert probe(out).duration == pytest.approx(6.5, abs=0.05)
    assert _rms(out, 3.1, 0.8) > 0.05 and _rms(out, 0.5, 1.0) < 1e-3


def test_cancel(media):
    p = _project(media)
    ev = threading.Event()
    ev.set()
    with pytest.raises(RenderCancelled):
        render_project(p, media / "cancel.mp4", RenderSettings(upscale=3), cancel=ev)
    assert not (media / "cancel.mp4").exists()


def test_image_motion_is_jitter_free(media):
    """위상 상관으로 측정한 프레임 간 이동량의 불규칙성: 일반 zoompan 보다 훨씬 작아야 함."""
    from jitter import frame_shifts, jitter_score, read_gray_frames

    w, h = 320, 180
    p = Project.from_dict({"output": {"width": w, "height": h, "fps": 30}, "transition": {"type": "none"},
                           "clips": [{"file": "img2.jpg", "duration": 3, "motion": "pan_right",
                                      "motion_strength": 0.1, "motion_easing": "linear"}]}, base_dir=media)
    res = render_project(p, media / "pan.mp4", RenderSettings(upscale=2))
    ours_frames = read_gray_frames(res.output, w, h)
    ours = jitter_score(ours_frames)
    # 비교용: 흔히 쓰는 zoompan 방식 (같은 이동량)
    naive = media / "naive.mp4"
    n = 90
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-i", str(media / "img2.jpg"), "-vf",
                    f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                    f"zoompan=z=1.1:x='(iw-iw/zoom)*on/{n - 1}':y='(ih-ih/zoom)/2':d={n}:s={w}x{h}:fps=30",
                    "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(naive)], check=True)
    base = jitter_score(read_gray_frames(naive, w, h))
    dx = frame_shifts(ours_frames)[:, 0]
    assert len(ours_frames) == 90
    assert np.all(dx > 0)  # 한 번도 멈추거나 뒤로 가지 않음
    assert ours < 0.25 and base > 2 * ours, (ours, base)
    # 중간 파일(재인코딩 전) 은 더 매끄러움
    mkv = sorted((media / ".composer_cache").glob("clip_*.mkv"), key=lambda q: q.stat().st_mtime)[-1]
    assert jitter_score(read_gray_frames(mkv, w, h)) < 0.12


def test_zoom_is_smooth(media):
    from jitter import read_gray_frames

    w, h = 320, 180
    p = Project.from_dict({"output": {"width": w, "height": h, "fps": 30},
                           "clips": [{"file": "img1.png", "duration": 2, "motion": "zoom_in",
                                      "motion_strength": 0.15, "motion_easing": "linear"}]}, base_dir=media)
    res = render_project(p, media / "zoom.mp4", RenderSettings())
    fr = read_gray_frames(res.output, w, h)
    diffs = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))[2:-2]
    # 일정한 속도의 줌이면 프레임 차이가 고르게 나옴 (변동계수 작음, 0 인 프레임 없음)
    assert diffs.min() > 0.05
    assert diffs.std() / diffs.mean() < 0.35


def test_probe_without_ffprobe_matches(media):
    """imageio-ffmpeg 처럼 ffprobe 가 없을 때 쓰는 'ffmpeg -i' 파서."""
    from composer.probe import _probe_ffmpeg, _probe_ffprobe, parse_ffmpeg_info, ffprobe_path

    a = _probe_ffmpeg(media / "vid1.mp4")
    assert a.has_video and a.has_audio and (a.width, a.height) == (640, 360) and abs(a.duration - 3) < 0.1
    b = _probe_ffmpeg(media / "vid_noaudio.mp4")
    assert b.has_video and not b.has_audio
    if ffprobe_path():
        c = _probe_ffprobe(media / "vid1.mp4")
        assert abs(c.duration - a.duration) < 0.05 and c.fps == pytest.approx(25)
    t = parse_ffmpeg_info("  Duration: 00:01:02.50, start: 0\n  Stream #0:0: Video: h264, yuv420p, 1920x1080, 29.97 fps\n")
    assert t.duration == 62.5 and t.width == 1920 and t.fps == 29.97 and not t.has_audio


def test_transition_handles_freeze_when_source_runs_out(media):
    """동영상 앞에 원본 여분이 없으면(trim_start 0) 첫 프레임을 멈춰서 채움 → 전체 길이 = 클립 길이 합."""
    p = Project.from_dict({
        "output": {"width": 320, "height": 180, "fps": 25},
        "transition": {"type": "crossfade", "duration": 1.0},
        "clips": [{"file": "img1.png", "duration": 2}, {"file": "vid1.mp4"}, {"file": "img2.jpg", "duration": 1}],
    }, base_dir=media)
    res = render_project(p, media / "handles.mp4", RenderSettings())
    assert res.duration == pytest.approx(6.0)  # 2 + 3 + 1
    assert probe(res.output).duration == pytest.approx(6.0, abs=0.1)
