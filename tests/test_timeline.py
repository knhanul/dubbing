import pytest

from composer.schema import Clip, Project, Take, Transition
from composer.subtitles import Cue
from composer.timeline import DEFAULT_IMAGE_DURATION, atempo_chain, build_timeline, clip_timing, take_position


def dur(n):
    return lambda clip: n


def test_image_and_video_timing():
    assert clip_timing(Clip(file="a.png", type="image", duration=3.2), None) == (3.2, 1.0, 0.0, 3.2)
    assert clip_timing(Clip(file="a.png", type="image"), None)[0] == DEFAULT_IMAGE_DURATION
    # 10초 영상, 2~8초 사용, 2배속 → 3초
    out, sp, a, b = clip_timing(Clip(file="v.mp4", type="video", trim_start=2, trim_end=8, speed=2), 10)
    assert (out, sp, a, b) == (3.0, 2.0, 2.0, 8.0)
    # trim_end 가 원본보다 길면 원본 길이까지
    assert clip_timing(Clip(file="v.mp4", type="video", trim_end=99), 10)[3] == 10
    # 0.5배속 슬로모션
    assert clip_timing(Clip(file="v.mp4", type="video", speed=0.5), 4)[0] == 8.0


def test_fit_duration_overrides_speed():
    out, sp, _, _ = clip_timing(Clip(file="v.mp4", type="video", fit_duration=2.5, speed=9, trim_start=1), 11)
    assert out == 2.5 and sp == pytest.approx(4.0)
    out, sp, _, _ = clip_timing(Clip(file="a.png", type="image", duration=5, fit_duration=2), None)
    assert out == 2 and sp == 1.0


def test_transitions_overlap_and_total():
    p = Project(transition=Transition("crossfade", 1.0), clips=[
        Clip(file="a.png", type="image", duration=4),
        Clip(file="b.png", type="image", duration=3),  # 전역 1초
        Clip(file="c.png", type="image", duration=2, transition=Transition("none", 0)),
        Clip(file="d.mp4", type="video", speed=2, transition=Transition("fade", 0.5)),  # 6초 영상 → 3초
    ])
    tl = build_timeline(p, dur(6.0))
    starts = [round(c.start, 3) for c in tl.clips]
    assert starts == [0.0, 3.0, 6.0, 7.5]
    assert tl.clips[0].transition_in.type == "none"
    assert tl.clips[2].transition_in.duration == 0
    assert tl.total == pytest.approx(10.5)  # 4+3+2+3 - 1 - 0.5
    assert tl.clip_at(3.5).index == 1 and tl.clip_at(100) is None


def test_transition_clamped_to_half_of_shorter_clip():
    p = Project(transition=Transition("crossfade", 5.0), clips=[
        Clip(file="a.png", type="image", duration=4), Clip(file="b.png", type="image", duration=1)])
    tl = build_timeline(p, dur(0))
    assert tl.clips[1].transition_in.duration == 0.5
    assert tl.total == pytest.approx(4.5)


def test_atempo_chain():
    assert atempo_chain(1.0) == []
    assert atempo_chain(1.5) == ["atempo=1.5"]
    assert atempo_chain(4.0) == ["atempo=2", "atempo=2"]
    assert atempo_chain(5.0) == ["atempo=2", "atempo=2", "atempo=1.25"]
    assert atempo_chain(0.25) == ["atempo=0.5", "atempo=0.5"]
    for s in (0.13, 0.7, 3.3, 15.9):
        prod = 1.0
        for part in atempo_chain(s):
            v = float(part.split("=")[1])
            assert 0.5 <= v <= 2.0
            prod *= v
        assert prod == pytest.approx(s, rel=1e-5)


def test_take_position_and_latency():
    cues = {1: Cue(1, 5.0, 7.0, "a")}
    assert take_position(Take("t.wav", subtitle=1, offset=-2.0), cues) == 3.0
    assert take_position(Take("t.wav", subtitle=1, offset=-2.0), cues, latency_ms=100) == pytest.approx(2.9)
    assert take_position(Take("t.wav", subtitle=None, offset=12.5), cues) == 12.5
    assert take_position(Take("t.wav", subtitle=99, offset=1.0), cues) == 1.0  # 없는 자막 → 절대 시간
