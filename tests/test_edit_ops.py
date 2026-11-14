"""자르기(✂)·리플 삭제·구간 잘라내기."""
import pytest

from composer.edit_ops import cut_range, delete_clip, split_clip, split_cue, split_take, take_span
from composer.render.ffmpeg_builder import motion_quad, take_chain
from composer.schema import Clip, Project, SubtitleItem, Take, Transition
from composer.timeline import build_timeline


def dur(v):
    return lambda c: v


def _p():
    return Project(transition=Transition("crossfade", 1.0), clips=[
        Clip(file="a.png", type="image", duration=4, motion="zoom_in"),
        Clip(file="v.mp4", type="video", trim_start=1.0, trim_end=7.0, speed=2.0),  # 3초
        Clip(file="c.png", type="image", duration=2)])


def test_split_image_keeps_motion_continuous():
    p = _p()
    tl = build_timeline(p, dur(10))
    assert split_clip(p, tl, 0, 1.0)
    a, b = p.clips[0], p.clips[1]
    assert (a.duration, b.duration) == (1.0, 3.0)
    assert (a.motion_from, a.motion_to, b.motion_from, b.motion_to) == (0.0, 0.25, 0.25, 1.0)
    assert b.transition.type == "none" and a.transition is None  # 새 경계엔 전환 없음
    tl2 = build_timeline(p, dur(10))
    assert tl2.total == pytest.approx(tl.total)  # 길이 그대로
    assert tl2.clips[2].transition_in.duration == pytest.approx(1.0)  # 원래 있던 전환은 유지
    # 움직임 식: 앞 조각 끝 = 뒤 조각 시작
    q = motion_quad("zoom_in", 0.2, 25, "linear", 0.25, 1.0)
    assert "(0.25+0.75*in/24)" in q["x0"]
    assert not split_clip(p, tl2, 0, 0.05)  # 경계에 너무 가까우면 안 자름


def test_split_video_uses_trim_and_speed():
    p = _p()
    tl = build_timeline(p, dur(10))
    assert tl.clips[1].start == 4.0
    assert split_clip(p, tl, 1, 5.0)  # 클립 안 1초 = 원본 2초(×2)
    a, b = p.clips[1], p.clips[2]
    assert (a.trim_start, a.trim_end, b.trim_start, b.trim_end) == (1.0, 3.0, 3.0, 7.0)
    assert a.file == b.file == "v.mp4" and b.speed == 2.0
    tl2 = build_timeline(p, dur(10))
    assert [round(c.duration, 3) for c in tl2.clips] == [4.0, 1.0, 2.0, 2.0]


def test_ripple_delete_pulls_following_clips():
    p = _p()
    assert delete_clip(p, 0)
    tl = build_timeline(p, dur(10))
    assert tl.clips[0].start == 0.0 and tl.total == pytest.approx(5.0)


def test_split_cue_renumbers_and_keeps_take_links():
    p = _p()
    p.subtitle.items = [SubtitleItem(1, 0.0, 2.0, "하나 둘 셋 넷"), SubtitleItem(2, 3.0, 4.0, "다음")]
    p.dubbing.takes = [Take(file="t2.wav", subtitle=2, offset=-0.5)]
    assert split_cue(p, 1, 1.0)
    items = p.subtitle.items
    assert [(i.index, i.start, i.end, i.text) for i in items] == [
        (1, 0.0, 1.0, "하나 둘"), (2, 1.0, 2.0, "셋 넷"), (3, 3.0, 4.0, "다음")]
    assert p.dubbing.takes[0].subtitle == 3  # 번호가 바뀐 자막을 계속 따라감


def test_split_take_non_destructive():
    p = _p()
    p.dubbing.takes = [Take(file="t.wav", offset=2.0)]
    L = lambda f: 3.0  # noqa: E731
    assert take_span(p, 0, L) == (2.0, 5.0)
    assert split_take(p, 0, 3.5, L)
    t1, t2 = p.dubbing.takes
    assert (t1.trim_start, t1.trim_end, t2.trim_start, t2.trim_end) == (0.0, 1.5, 1.5, None)
    assert t1.file == t2.file and take_span(p, 1, L) == (3.5, 5.0)
    # 렌더: 파일 1.5초부터 → 타임라인 3.5초에
    chain, delay = take_chain(2.0, 1.5, None)
    assert "atrim=start=1.5" in ",".join(chain) and delay == pytest.approx(3.5)
    chain, delay = take_chain(-0.5, 0.0, 1.5)
    assert "atrim=start=0.5:end=1.5" in ",".join(chain) and delay == 0.0


def test_cut_range_all_tracks():
    p = Project(transition=Transition("none", 0.0), clips=[
        Clip(file="a.png", type="image", duration=4), Clip(file="b.png", type="image", duration=4)])
    p.subtitle.items = [SubtitleItem(1, 0.5, 2.5, "앞"), SubtitleItem(2, 3.2, 3.8, "안"),
                        SubtitleItem(3, 4.5, 6.0, "걸침"), SubtitleItem(4, 6.5, 7.5, "뒤")]
    p.dubbing.takes = [Take(file="x.wav", offset=1.0), Take(file="y.wav", subtitle=4, offset=0.0),
                       Take(file="z.wav", offset=6.8), Take(file="w.wav", offset=2.0, trim_start=0.5)]
    L = lambda f: 6.0 if f == "w.wav" else 4.0  # noqa: E731
    reb = lambda: build_timeline(p, dur(None))  # noqa: E731
    removed, note = cut_range(p, reb, 3.0, 5.0, True, L)
    assert removed == pytest.approx(2.0) and "클립" in note
    tl = reb()
    assert tl.total == pytest.approx(6.0) and [c.duration for c in tl.clips] == [3.0, 3.0]
    assert p.clips[1].transition.type == "none"
    items = p.subtitle.items
    assert [(i.index, i.start, i.end, i.text) for i in items] == [
        (1, 0.5, 2.5, "앞"), (2, 3.0, 4.0, "걸침"), (3, 4.5, 5.5, "뒤")]  # 안 → 지움, 걸침 → 짧게, 뒤 → 당김
    tks = p.dubbing.takes
    # x(1~5초): 구간 3~5초가 끝까지 → 앞 조각(1~3)만. y 는 자막 3번을 따라감, z 는 당김.
    # w(2.5~8초): 구간을 통째로 품음 → 앞 조각(2.5~3) + 뒤 조각(파일 3.5초부터, 3초에)
    assert [(t.file, t.offset, t.trim_start, t.trim_end, t.subtitle) for t in tks] == [
        ("x.wav", 1.0, 0.0, 2.0, None), ("y.wav", 0.0, 0.0, None, 3), ("z.wav", 4.8, 0.0, None, None),
        ("w.wav", 2.0, 0.5, 1.0, None), ("w.wav", 0.0, 3.0, None, None)]
    assert take_span(p, 3, L) == (2.5, 3.0) and take_span(p, 4, L) == (3.0, 6.0)
    # 잠긴 트랙은 그대로
    p2 = Project(clips=[Clip(file="a.png", type="image", duration=4)])
    p2.subtitle.items = [SubtitleItem(1, 3.5, 3.9, "x")]
    cut_range(p2, lambda: build_timeline(p2, dur(None)), 1.0, 2.0, True, L, skip={"subtitle"})
    assert p2.clips[0].duration == 1.0 and p2.clips[1].duration == 2.0 and p2.subtitle.items[0].start == 3.5
    assert cut_range(p2, lambda: build_timeline(p2, dur(None)), 0.0, 1.0, True, L, skip={"clips"})[0] == 0.0
