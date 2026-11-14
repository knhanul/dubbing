from composer.schema import Project, SubtitleSettings
from composer.subtitles import (CueTimeline, apply_offset, decode_bytes, ensure_items, format_srt_time, load_subtitles,
                                move_item, nudge_item, parse_subtitles, project_cues, to_srt)

SRT = """1
00:00:01,000 --> 00:00:03,500
첫 번째 <i>자막</i>

2
00:00:05,250 --> 00:00:07,000
두 번째
두 줄

3
00:01:02,5 --> 00:01:04,000
세 번째
"""

VTT = """WEBVTT

NOTE 주석

intro
00:00.500 --> 00:02.000 align:center
안녕

00:00:03.000 --> 00:00:04.250
<v 화자>반가워
"""


def test_parse_srt():
    cues = parse_subtitles(SRT)
    assert [c.index for c in cues] == [1, 2, 3]
    assert cues[0].start == 1.0 and cues[0].end == 3.5 and cues[0].text == "첫 번째 자막"
    assert cues[1].text == "두 번째\n두 줄"
    assert cues[2].start == 62.5  # 밀리초 자리수가 부족해도 처리
    assert abs(cues[1].duration - 1.75) < 1e-9


def test_parse_vtt_and_crlf():
    cues = parse_subtitles(VTT.replace("\n", "\r\n"))
    assert len(cues) == 2
    assert cues[0].start == 0.5 and cues[0].text == "안녕"
    assert cues[1].end == 4.25 and cues[1].text == "반가워"


def test_unsorted_and_missing_numbers_are_renumbered():
    text = "00:00:05,000 --> 00:00:06,000\nB\n\n7\n00:00:01,000 --> 00:00:02,000\nA\n"
    cues = parse_subtitles(text)
    assert [(c.index, c.text) for c in cues] == [(1, "A"), (2, "B")]


def test_encodings(tmp_path):
    for enc, raw in [("utf-8-sig", SRT.encode("utf-8-sig")), ("cp949", SRT.encode("cp949")),
                     ("utf-16", SRT.encode("utf-16"))]:
        p = tmp_path / f"s_{enc}.srt"
        p.write_bytes(raw)
        cues = load_subtitles(p)
        assert cues[0].text == "첫 번째 자막", enc
    assert decode_bytes("한글".encode("euc-kr")) == "한글"


def test_offset_and_roundtrip():
    cues = parse_subtitles(SRT)
    shifted = apply_offset(cues, -1.5)
    assert shifted[0].start == 0.0 and shifted[0].end == 2.0  # 음수로 가면 0 으로
    gone = apply_offset(cues, -10)
    assert len(gone) == 1  # 끝이 0 이하인 자막은 제거
    again = parse_subtitles(to_srt(cues))
    assert [(c.start, c.end, c.text) for c in again] == [(c.start, c.end, c.text) for c in cues]
    assert format_srt_time(3661.0015) == "01:01:01,002"


def test_cue_timeline_countdowns():
    tl = CueTimeline(parse_subtitles(SRT))
    s = tl.status(0.25)
    assert s["current"] is None and s["next"].index == 1 and abs(s["until_next"] - 0.75) < 1e-9
    s = tl.status(2.0)
    assert s["current"].index == 1 and abs(s["until_end"] - 1.5) < 1e-9 and abs(s["progress"] - 0.4) < 1e-9
    assert s["next"].index == 2 and abs(s["until_next"] - 3.25) < 1e-9
    s = tl.status(4.0)
    assert s["current"] is None and s["next"].index == 2
    s = tl.status(100)
    assert s["current"] is None and s["next"] is None


def test_overlapping_cues():
    tl = CueTimeline(parse_subtitles("1\n00:00:00,000 --> 00:00:10,000\nA\n\n2\n00:00:02,000 --> 00:00:03,000\nB\n"))
    assert tl.current(2.5).text == "B"
    assert tl.current(5.0).text == "A"


def test_project_items_nudge_and_offset(tmp_path):
    (tmp_path / "s.srt").write_text(SRT, encoding="utf-8")
    p = Project(base_dir=tmp_path, subtitle=SubtitleSettings(file="s.srt", offset=0.5))
    assert project_cues(p)[0].start == 1.5
    items = ensure_items(p)
    assert len(items) == 3 and items[0].start == 1.0  # 오프셋 적용 전 값
    nudge_item(p, 1, d_start=0.1)
    nudge_item(p, 1, d_end=-0.2)
    assert (items[0].start, items[0].end) == (1.1, 3.3)
    nudge_item(p, 1, d_start=5)  # 최소 길이 유지
    assert round(items[0].end - items[0].start, 3) == 0.1
    move_item(p, 2, -10)  # 0 아래로 못 감
    assert items[1].start == 0.0 and items[1].end == 1.75
    cues = project_cues(p)
    assert cues[0].start == 0.5  # 정렬 + 전역 오프셋
    # 직렬화 후에도 유지
    p2 = Project.from_dict(p.to_dict(), base_dir=tmp_path)
    assert [c.start for c in project_cues(p2)] == [c.start for c in cues]
