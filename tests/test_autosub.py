"""더빙으로 자막 만들기: 문장 나누기·줄 나누기·대본 정렬·무음 분할(합성 소리)·음성 인식(가짜)·자유 녹음 화면."""
import os
import subprocess
import types
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import composer.autosub as A  # noqa: E402
from composer.autosub import (AutoSubOptions, Word, align_chunks, detect_speech, finalize, make_subtitles,  # noqa: E402
                              proportional_words, script_chunks, split_sentences, wrap_cue_texts)
from composer.autosub import CueInfo, align_detail, distribute_by_silence, hybrid_snap, script_chunks_marked  # noqa: E402,E501
from composer.cue_edit import LOW_CONF, CueList, EditCue, TapSession, join_text, snap, split_text  # noqa: E402
from composer.probe import ffmpeg_path  # noqa: E402
from composer.schema import Project  # noqa: E402


def tones(path: Path, parts):
    """parts: [("tone", 초) | ("sil", 초)] → WAV (말소리 대신 삐 소리, 쉬는 곳은 무음)."""
    args, labels = [ffmpeg_path(), "-v", "error", "-y"], []
    for k, (kind, d) in enumerate(parts):
        src = f"sine=f={300 + 60 * k}:r=16000:d={d}" if kind == "tone" else f"anullsrc=r=16000:cl=mono:d={d}"
        args += ["-f", "lavfi", "-i", src]
        labels.append(f"[{k}:a]")
    args += ["-filter_complex", f"{''.join(labels)}concat=n={len(parts)}:v=0:a=1,aformat=channel_layouts=mono",
             str(path)]
    subprocess.run(args, check=True)
    return path


# ------------------------------------------------------------------ 글
def test_split_sentences():
    assert split_sentences("안녕하세요. 오늘은 날씨가 좋네요! 정말요?\n\n새 줄\"…") == \
        ["안녕하세요.", "오늘은 날씨가 좋네요!", "정말요?", "새 줄\"…"]
    assert split_sentences("말줄임…… 그리고 \"인용.\" 끝") == ["말줄임……", "그리고 \"인용.\"", "끝"]
    assert split_sentences("   ") == []


def test_wrap_lines_max_chars_and_lines():
    long = "나 보기가 역겨워 가실 때에는 말없이 고이 보내 드리우리다 영변에 약산 진달래꽃 아름 따다 가실 길에 뿌리우리다"
    out = wrap_cue_texts(long, 20, 2)
    assert len(out) >= 2
    for cue in out:
        lines = cue.split("\n")
        assert len(lines) <= 2 and all(len(l) <= 20 for l in lines)
    assert " ".join(" ".join(c.split("\n")) for c in out) == long  # 글은 그대로
    assert wrap_cue_texts("짧은 문장", 20, 2) == ["짧은 문장"]
    # 띄어쓰기 없는 긴 글도 자름
    assert all(len(l) <= 10 for c in wrap_cue_texts("가" * 35, 10, 1) for l in c.split("\n"))
    # 한 줄씩
    assert all("\n" not in c for c in wrap_cue_texts(long, 20, 1))
    # 쉼표 뒤에서 먼저 끊기
    assert wrap_cue_texts("오늘은 정말로 날씨가, 아주 맑고 좋은 하루였습니다", 14, 2)[0].split("\n")[0].endswith(",")


# ------------------------------------------------------------------ 정렬
def test_align_script_to_recognized_words():
    words = [Word(0.5, 0.9, "안녕"), Word(0.9, 1.4, "하세요"), Word(2.0, 2.5, "오늘은"), Word(2.6, 3.2, "날시가"),
             Word(3.2, 3.6, "좋네요"), Word(5.0, 5.4, "음"), Word(6.0, 7.0, "마지막입니다")]
    chunks = script_chunks("안녕하세요.\n오늘은 날씨가 좋네요.\n마지막입니다!")
    t = align_chunks(chunks, words)
    assert chunks == ["안녕하세요.", "오늘은 날씨가 좋네요.", "마지막입니다!"]
    assert t[0] == pytest.approx((0.5, 1.4))
    assert t[1] == pytest.approx((2.0, 3.6))  # '날시가'로 잘못 알아들어도 시간은 맞음
    assert t[2] == pytest.approx((6.0, 7.0))  # 대본에 없는 '음'은 무시


def test_align_missing_words_interpolated():
    words = [Word(0.0, 1.0, "하나둘"), Word(4.0, 5.0, "다섯여섯")]
    chunks = ["하나둘", "셋넷", "다섯여섯"]  # '셋넷'은 인식 못 함
    t = align_chunks(chunks, words)
    assert t[0] == pytest.approx((0.0, 1.0)) and t[2] == pytest.approx((4.0, 5.0))
    assert 1.0 <= t[1][0] < t[1][1] <= 4.0  # 사이에 끼워 넣음


def test_finalize_min_duration_and_no_overlap():
    opts = AutoSubOptions(min_duration=1.0, gap=0.05, linger=0.3)
    cues = finalize([("둘", 2.0, 2.2), ("하나", 0.0, 0.3), ("셋", 2.1, 4.0), ("", 9, 10)], opts)
    assert [c.text for c in cues] == ["하나", "둘", "셋"] and [c.index for c in cues] == [1, 2, 3]
    assert cues[0].end == pytest.approx(1.0)  # 최소 1초
    for a, b in zip(cues, cues[1:]):
        assert a.end <= b.start - 0.049
    assert cues[1].duration >= 0.2 and cues[2].end == pytest.approx(4.3)


def test_proportional_and_one_to_one():
    regions = [(0.0, 1.0), (2.0, 3.0)]
    assert proportional_words(regions, ["가나", "다라"]) == regions  # 개수가 같으면 1:1
    t = proportional_words(regions, ["가", "나다", "라"])  # 개수가 다르면 글자 수 비율(1:2:1)로 말한 2초를 나눔
    assert t[0] == pytest.approx((0.0, 0.5)) and t[1] == pytest.approx((0.5, 2.5)) and t[2] == pytest.approx((2.5, 3.0))


# ------------------------------------------------------------------ 무음 분할 (합성 소리)
# ------------------------------------------------------------------ 혼용 정렬 · 신뢰도
def test_script_chunks_marked_sentence_edges():
    m = script_chunks_marked("짧은 문장.\n이 문장은 꽤 길어서 자막 한 개에 다 들어가지 않고 두 개로 나뉘는 문장입니다.", 10, 2)
    assert m[0] == ("짧은 문장.", True, True)
    assert m[1][1] and not m[1][2] and not m[-1][1] and m[-1][2]


def test_align_detail_ratio_and_heard():
    words = [Word(0.0, 0.5, "안녕하세요"), Word(1.0, 1.5, "엉뚱한말")]
    d = align_detail(["안녕하세요.", "반갑습니다."], words)
    assert d[0][2] == pytest.approx(1.0) and d[0][3] == "안녕하세요"
    assert d[1][2] < LOW_CONF and d[1][3] == "엉뚱한말"


def test_hybrid_snap_to_silence_and_confidence():
    regions = [(0.0, 1.0), (1.5, 3.0)]
    marks = [("가.", True, True), ("나.", True, True)]
    out = hybrid_snap([(0.1, 0.9, 1.0, "가"), (1.6, 2.4, 1.0, "나")], marks, regions)
    assert out[0][:2] == (0.0, 1.0) and out[0][2].conf == 1.0  # 둘 다 경계에 붙음
    assert out[1][0] == 1.5 and out[1][1] == 2.4 and out[1][2].low  # 끝(2.4)이 말 끝(3.0)과 0.6초 어긋남
    # 줄 길이로 나눈 문장 가운데(경계 아님)는 붙이지도 깎지도 않음
    out2 = hybrid_snap([(1.6, 2.2, 1.0, ""), (2.2, 2.95, 1.0, "")], [("가", True, False), ("나.", False, True)], regions)
    assert out2[0][1] == 2.2 and out2[1][0] == 2.2 and out2[1][1] == 3.0 and not out2[0][2].low
    # 인식 일치율이 낮으면 노란색
    assert hybrid_snap([(0.0, 1.0, 0.3, "")], [("가.", True, True)], regions)[0][2].low


def test_distribute_by_silence_merge_split_and_confidence():
    regs = [(0.0, 1.0), (1.5, 2.5), (3.0, 4.0)]
    M = lambda *t: [(x, True, True) for x in t]  # noqa: E731
    # 개수가 같으면 1:1
    o = distribute_by_silence(regs, M("가나", "다라", "마바"))
    assert [x[:2] for x in o] == regs and not any(x[2].low for x in o)
    # 자막 2개 / 구간 3개 → 글자 수 비율(1:2)에 가까운 쉬는 곳에서 나누고 나머지는 합침
    o = distribute_by_silence(regs, M("가나", "다라마바"))
    assert [x[:2] for x in o] == [(0.0, 1.0), (1.5, 4.0)]
    # 자막 3개 / 구간 1개 → 쉬는 곳이 없어 구간 안을 글자 수로 나눔 → 문장 사이라 노란색
    o = distribute_by_silence([(0.0, 3.0)], M("가", "나", "다"))
    assert [x[0] for x in o] == pytest.approx([0.0, 1.025, 2.025]) and all(x[2].low for x in o)
    # 같은 문장을 줄 길이로 나눈 경우는 쉬지 않는 게 보통 → 노란색 아님
    o = distribute_by_silence([(0.0, 3.0)], [("가나", True, False), ("다라.", False, True)])
    assert not any(x[2].low for x in o)
    # 1:1 인데 말 길이와 글 길이가 크게 다르면 확인 필요
    o = distribute_by_silence([(0.0, 0.3), (1.0, 4.0)], M("아주 긴 문장입니다 정말로", "네"))
    assert o[0][2].low or o[1][2].low


# ------------------------------------------------------------------ 자막 맞추기 편집 규칙
def _cl():
    cues = [EditCue(0.0, 1.0, "첫 번째 문장", conf=0.9), EditCue(1.5, 3.0, "둘째 문장 여기까지", "둘째 문장", conf=0.4),
            EditCue(3.5, 4.0, "셋")]
    return CueList(cues, regions=[(0.0, 0.95), (1.6, 2.9), (3.5, 4.2)], gap=0.05)


def test_snap_and_drag_edges_with_bounds_and_undo():
    assert snap(1.02, [1.0, 2.0], 0.05) == 1.0 and snap(1.2, [1.0], 0.05) == 1.2
    cl = _cl()
    assert cl.set_start(1, 1.55, tol=0.1) == 1.6  # '말 시작'(1.6)에 붙음
    assert cl.set_start(1, 1.55, snap_on=False) == 1.55  # Shift = 자석 끄기
    assert cl.set_end(0, 0.9, tol=0.1) == 0.95  # '말 끝'에
    assert cl.set_end(0, 2.0, snap_on=False) == pytest.approx(1.5)  # 다음 자막과 안 겹침 (1.55 - 간격)
    assert cl.set_start(2, 0.0, snap_on=False) == pytest.approx(3.05)  # 앞 자막(끝 3.0) 뒤로만
    # 한 번 끌기 = 되돌리기 한 번 (같은 key)
    cl2 = _cl()
    for x in (1.4, 1.3, 1.2):
        cl2.set_end(0, x, snap_on=False, key="drag1")
    assert cl2.cues[0].end == 1.2 and cl2.undo() and cl2.cues[0].end == 1.0 and not cl2.can_undo()
    assert cl2.redo() and cl2.cues[0].end == 1.2


def test_move_keeps_length_and_snaps():
    cl = _cl()
    cl.move(1, 1.62, tol=0.1)  # 시작이 1.6 에 붙음
    assert (cl.cues[1].start, cl.cues[1].end) == (1.6, 3.1) or (cl.cues[1].start, cl.cues[1].end) == (1.6, 3.1)
    cl.move(1, 9.0, snap_on=False)
    assert cl.cues[1].end <= cl.cues[2].start - 0.05 + 1e-9 and cl.cues[1].end - cl.cues[1].start == pytest.approx(1.5)


def test_split_and_merge():
    assert split_text("하나 둘 셋 넷", 0.5) == ("하나 둘", "셋 넷")
    assert split_text("가나다라마바", 0.5) == ("가나다", "라마바")
    assert split_text("하나 둘 셋 넷", cursor=2) == ("하나", "둘 셋 넷")
    cl = _cl()
    assert not cl.split(1, 1.55)  # 너무 가장자리
    assert cl.split(1, 2.25)
    assert [c.text for c in cl.cues] == ["첫 번째 문장", "둘째 문장", "여기까지", "셋"]
    assert cl.cues[1].end == pytest.approx(2.225) and cl.cues[2].start == pytest.approx(2.275)
    assert cl.cues[1].conf == 0.4 and cl.cues[1].heard == "둘째" and len(cl) == 4
    assert cl.merge_next(1) and [c.text for c in cl.cues][1] == "둘째 문장 여기까지"
    assert cl.cues[1].start == 1.5 and cl.cues[1].end == 3.0
    assert cl.merge_next(0) and cl.cues[0].text == "첫 번째 문장 둘째 문장 여기까지"
    assert cl.cues[0].conf == 0.4 and not cl.merge_next(len(cl) - 1)
    for _ in range(3):
        assert cl.undo()
    assert [c.text for c in cl.cues] == ["첫 번째 문장", "둘째 문장 여기까지", "셋"]
    assert join_text("가", "나") == "가 나" and join_text("가나다", "라마바", 4) == "가나다\n라마바"  # 줄 길이 넘으면 줄바꿈


def test_next_low_and_text_edit_undo_group():
    cl = _cl()
    assert cl.next_low(None) == 1 and cl.next_low(1) == 1 and cl.low_count() == 1
    for t in ("둘", "둘째", "둘째!"):
        cl.set_text(1, t, key="text1")
    assert cl.cues[1].text == "둘째!" and cl.undo() and cl.cues[1].text == "둘째 문장 여기까지"
    cl.mark_checked(1)
    assert cl.next_low(0) is None
    assert [c.index for c in cl.to_cues()] == [1, 2, 3]


def test_tap_mode_logic():
    cues = [EditCue(0.0, 1.0, "가"), EditCue(1.2, 2.0, "나"), EditCue(2.2, 3.0, "다")]
    cl = CueList(cues, regions=[(0.3, 1.1), (2.4, 3.0), (4.0, 5.0)], gap=0.05)
    tap = TapSession(cl, 0, linger=0.3)
    assert tap.current_text() == "가" and tap.next_text() == "나"
    assert tap.tap(0.3) == 0
    assert cl.cues[0].start == 0.3 and cl.cues[1].start == pytest.approx(1.5)  # 뒤 자막도 함께 밀림
    assert tap.tap(2.4) == 1
    assert cl.cues[0].end == pytest.approx(1.4)  # 말 끝(1.1) + 0.3 (다음 시작 직전보다 이르면 거기서)
    assert cl.cues[1].start == 2.4 and cl.cues[2].start == pytest.approx(3.4)
    assert tap.tap(3.2) == 2
    assert cl.cues[1].end == pytest.approx(3.15)  # 사이에 말 끝이 없으면 다음 시작 직전
    assert tap.done and tap.tap(9) is None
    assert cl.undo() and [c.start for c in cl.cues] == [0.0, 1.2, 2.2]  # 한 번 탭 모음 = 되돌리기 한 번
    # 중간부터 다시
    cl.redo()
    tap.restart(1)
    assert tap.current_text() == "나" and tap.tap(2.6) == 1 and cl.cues[1].start == 2.6
    assert tap.tap(0.0) == 2 and cl.cues[2].start > cl.cues[1].start  # 거꾸로는 못 감


def test_detect_speech_on_synthetic_audio(tmp_path):
    p = tones(tmp_path / "a.wav", [("sil", 0.5), ("tone", 1.0), ("sil", 0.8), ("tone", 1.5), ("sil", 0.6),
                                   ("tone", 0.7)])
    sp = detect_speech(p)
    assert len(sp) == 3
    exp = [(0.5, 1.5), (2.3, 3.8), (4.4, 5.1)]
    for (s, e), (es, ee) in zip(sp, exp):
        assert s == pytest.approx(es, abs=0.06) and e == pytest.approx(ee, abs=0.06)
    # 짧은 쉼(0.2초)은 끊지 않음
    p2 = tones(tmp_path / "b.wav", [("tone", 1.0), ("sil", 0.2), ("tone", 1.0)])
    assert len(detect_speech(p2)) == 1


def test_make_subtitles_silence_with_script_and_offsets(tmp_path):
    a = tones(tmp_path / "a.wav", [("tone", 1.0), ("sil", 0.8), ("tone", 1.2)])
    b = tones(tmp_path / "b.wav", [("sil", 0.6), ("tone", 1.0)])
    script = "첫 번째 문장입니다.\n두 번째 문장이에요.\n세 번째는 다른 녹음."
    r = make_subtitles([(b, 10.0), (a, 2.0)], script, AutoSubOptions(engine="silence"))
    assert r.engine == "silence" and [c.text for c in r.cues] == split_sentences(script)
    assert r.cues[0].start == pytest.approx(2.0, abs=0.06)
    assert r.cues[1].start == pytest.approx(3.8, abs=0.06)
    assert r.cues[2].start == pytest.approx(10.6, abs=0.06)  # 두 번째 녹음 위치 + 0.6초
    assert r.speech_end == pytest.approx(11.6, abs=0.06)
    # 대본이 없으면 '(N번 대사)'
    r2 = make_subtitles([(a, 0.0)], "", AutoSubOptions(engine="silence"))
    assert [c.text for c in r2.cues] == ["(1번 대사)", "(2번 대사)"] and "대본이 없어" in r2.note
    with pytest.raises(ValueError):
        make_subtitles([(tmp_path / "없음.wav", 0)], "", AutoSubOptions())


def test_cancel(tmp_path):
    import threading
    a = tones(tmp_path / "a.wav", [("tone", 1.0)])
    ev = threading.Event()
    ev.set()
    with pytest.raises(A.Cancelled):
        make_subtitles([(a, 0.0)], "", AutoSubOptions(engine="silence"), cancel=ev)


# ------------------------------------------------------------------ 음성 인식 (가짜 모델)
def test_whisper_path_mocked(tmp_path, monkeypatch):
    a = tones(tmp_path / "a.wav", [("tone", 0.8), ("sil", 0.4), ("tone", 0.9)])  # 말 0~0.8, 1.2~2.1
    calls = []

    def fake_transcribe(path, opts, progress, cancel, p0=0.0, p1=1.0):
        calls.append(opts.model)
        progress(p1, "음성 인식 중…")
        return [Word(0.2, 0.6, "안녕하세요."), Word(1.0, 1.3, "반갑습니다"), Word(1.3, 1.8, "여러분.")]

    monkeypatch.setattr(A, "transcribe", fake_transcribe)
    r = make_subtitles([(a, 5.0)], "", AutoSubOptions(engine="auto"))
    assert r.engine == "whisper" and calls == ["small"]
    assert [c.text for c in r.cues] == ["안녕하세요.", "반갑습니다 여러분."]  # 인식한 글 그대로
    # 혼용: 인식 시각(5.2 / 6.0초)을 가까운 '말 시작'(무음 경계 5.0 / 6.2초)에 붙임
    assert r.cues[0].start == pytest.approx(5.0, abs=0.03) and r.cues[1].start == pytest.approx(6.2, abs=0.03)
    assert r.low_count == 0 and len(r.regions) == 2
    # 소리가 쉬지 않고 이어지는데 인식은 문장이 끝났다고 하면 → 두 방식이 어긋남 → 노란색
    b = tones(tmp_path / "b.wav", [("tone", 3.0)])
    rb = make_subtitles([(b, 0.0)], "", AutoSubOptions())
    assert rb.infos[1].low and "어긋남" in rb.infos[1].reason
    r2 = make_subtitles([(a, 0.0)], "안녕하세요!\n반갑습니다, 여러분!", AutoSubOptions())
    assert [c.text for c in r2.cues] == ["안녕하세요!", "반갑습니다, 여러분!"]  # 대본 글 그대로


def test_auto_falls_back_to_silence(tmp_path, monkeypatch):
    a = tones(tmp_path / "a.wav", [("tone", 1.0), ("sil", 0.8), ("tone", 1.0)])

    def broken(*a, **k):
        raise A.EngineUnavailable("없음")

    monkeypatch.setattr(A, "transcribe", broken)
    r = make_subtitles([(a, 0.0)], "하나.\n둘.", AutoSubOptions(engine="auto"))
    assert r.engine == "silence" and "설치되어 있지 않아" in r.note and len(r.cues) == 2
    with pytest.raises(A.EngineUnavailable):
        make_subtitles([(a, 0.0)], "", AutoSubOptions(engine="whisper"))


def test_whisper_heard_nothing_uses_silence(tmp_path, monkeypatch):
    a = tones(tmp_path / "a.wav", [("tone", 1.0), ("sil", 0.8), ("tone", 1.0)])
    monkeypatch.setattr(A, "transcribe", lambda *a, **k: [])
    r = make_subtitles([(a, 0.0)], "하나.\n둘.", AutoSubOptions(engine="auto"))
    assert r.engine == "silence" and "알아듣지 못해" in r.note
    assert [round(c.start, 1) for c in r.cues] == [0.0, 1.8]
    # 대본과 전혀 다른 말을 알아들었을 때도
    monkeypatch.setattr(A, "transcribe", lambda *a, **k: [Word(0, 1, "hello world")])
    assert make_subtitles([(a, 0.0)], "하나.\n둘.", AutoSubOptions(engine="auto")).engine == "silence"


def test_model_download_progress_mocked(tmp_path, monkeypatch):
    """첫 사용 때 내려받기 진행률이 표시되는지 (실제 내려받기는 안 함)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import huggingface_hub
    seen = {}

    def fake_snapshot(repo, local_dir, tqdm_class, allow_patterns):
        seen["repo"] = repo
        bar = tqdm_class(total=480_000_000, unit="B", disable=True)
        for _ in range(4):
            bar.update(120_000_000)
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        (Path(local_dir) / "model.bin").write_bytes(b"x")
        return local_dir

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    msgs = []
    import threading
    path = A._download("small", lambda f, m: msgs.append((f, m)), threading.Event())
    assert seen["repo"] == "Systran/faster-whisper-small" and A.model_downloaded("small")
    assert any("내려받는 중" in m and "480MB" in m for _, m in msgs)
    assert Path(path).name == "small"


def test_script_from_file(tmp_path):
    from composer.autosub import script_from_file
    (tmp_path / "a.txt").write_bytes("첫 줄\n둘째 줄".encode("cp949"))
    assert script_from_file(tmp_path / "a.txt") == "첫 줄\n둘째 줄"
    (tmp_path / "a.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n가나\n다라\n", encoding="utf-8")
    assert script_from_file(tmp_path / "a.srt") == "가나 다라"
    from docx import Document
    d = Document(); d.add_paragraph("워드 대본"); d.save(str(tmp_path / "a.docx"))
    assert script_from_file(tmp_path / "a.docx") == "워드 대본"


# ------------------------------------------------------------------ 화면
@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from composer.dubbing import recorder as rec_mod

    class FakeStream:
        latency = 0.01

        def __init__(self, **kw): pass
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    sd = types.SimpleNamespace(InputStream=FakeStream,
                               query_devices=lambda: [{"name": "마이크", "max_input_channels": 1, "default_samplerate": 48000}],
                               default=types.SimpleNamespace(device=(0, 0)))
    monkeypatch.setattr(rec_mod, "_sd", lambda: sd)
    app = QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    import composer.ui.main_window as mw
    make_media(tmp_path)
    w = mw.MainWindow()
    # 이미지 클립 = 미리보기 영상 없이 타임라인 시계로 재생 (화면 없는 테스트에서도 위치가 정확)
    w.set_project(Project.from_dict({"clips": [{"file": "img1.png", "duration": 3}]}, base_dir=tmp_path), None)
    app.processEvents()
    yield w, tmp_path, app
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _sync_worker(worker):
    box = {}
    worker.done.connect(lambda r: box.setdefault("res", r))
    worker.failed.connect(lambda m: box.setdefault("err", m))
    worker.run()  # 같은 스레드에서 바로 실행
    assert "err" not in box, box.get("err")
    return box.get("res")


def _aligner(store):
    """'자막 맞추기' 화면을 실제로 만들고 바로 '✔ 적용'한 것처럼 결과를 돌려줌."""
    from composer.ui.sub_align import SubAlignDialog

    def open_(cues, regions, sources, note=""):
        dlg = SubAlignDialog(cues, regions, sources, note=note)
        store.append(dlg)
        out = dlg.result_cues()
        dlg.done(SubAlignDialog.Accepted)
        return out
    return open_


def test_free_tab_buttons_and_flow(win, monkeypatch):
    w, d, app = win
    fp = w.dub.free_page
    assert w.dub.btn_mode_follow.text() == "🎤 자막 보며 녹음" and w.dub.btn_mode_free.text() == "🎙 자유롭게 녹음 (대본 먼저)"
    w.dub.btn_mode_free.click()
    assert w.dub.mode_tabs.currentWidget() is fp and w.dub.btn_mode_free.isChecked()
    assert fp.opts.body.isHidden()  # 자막 만들기 옵션은 접혀 있음
    sfp = w.inspector.sub_file_panel
    assert fp.btn_rec.text() == "● 자유 녹음 시작" and fp.btn_import.text() == "📂 음성 파일 불러오기…"
    assert fp.btn_make.text() == "🎙→📝 더빙으로 자막 만들기" and not fp.btn_make.isEnabled()
    assert not sfp.btn_sub_export.isEnabled() and fp.whisper_model.currentData() == "small"
    assert not hasattr(fp, "btn_save_srt")  # SRT 저장은 🅰 자막 탭 한 곳에
    # 대본 → 텔레프롬프터
    fp.script.setPlainText("첫 번째 문장입니다. 두 번째 문장이에요.")
    assert "두 번째 문장이에요." in fp.prompter.toPlainText()
    # 영상 없이 자유 녹음 (재생 위치 1.0초부터)
    fp.with_video.setChecked(False)
    w.player.seek(1.0)
    fp.btn_rec.click()
    assert w.dub.mode == "free" and fp.btn_rec.text() == "■ 녹음 끝내기" and not w.player.is_playing()
    sr = 48000
    t = np.arange(int(sr * 2.6)) / sr
    sig = (0.3 * np.sin(2 * np.pi * 400 * t)).astype("float32")
    sig[int(sr * 1.0):int(sr * 1.6)] = 0  # 가운데 0.6초 쉼
    w.dub.recorder._callback(sig.reshape(-1, 1), len(sig), None, None)
    fp.btn_rec.click()
    assert w.dub.mode is None and fp.btn_rec.text() == "● 자유 녹음 시작"
    take = w.model.project.dubbing.takes[-1]
    assert take.subtitle is None and take.offset == pytest.approx(1.0, abs=0.01)
    assert w.player.position() == pytest.approx(min(3.6, w.model.timeline.total), abs=0.05)  # 다음 녹음은 이어서 (영상 끝까지만)
    assert fp.btn_make.isEnabled()
    # 자막 만들기 (무음 구간 + 대본)
    fp.engine.setCurrentIndex(fp.engine.findData("silence"))
    fp.run_worker = _sync_worker
    opened = []
    fp.open_aligner = _aligner(opened)
    fp.btn_make.click()
    assert len(opened) == 1 and opened[0].table.rowCount() == 2  # 자막 맞추기 화면이 열림
    assert len(opened[0].cl.regions) == 2 and len(opened[0].view.peaks) > 200  # 무음 경계 + 파형
    cues = w.model.cues
    assert [c.text for c in cues] == ["첫 번째 문장입니다.", "두 번째 문장이에요."]
    assert cues[0].start == pytest.approx(1.0, abs=0.06) and cues[1].start == pytest.approx(2.6, abs=0.06)
    assert w.model.project.subtitle.items and w.model.project.subtitle.file is None
    assert "자막 2줄" in fp.lbl_result.text() and sfp.btn_sub_export.isEnabled() and fp.btn_align.isEnabled()
    assert "🅰 자막 탭" in fp.lbl_result.text()
    # 취소하면 자막을 넣지 않음
    fp.open_aligner = lambda *a: None
    before = list(w.model.cues)
    fp.confirm = lambda t, m: True
    fp.btn_make.click()
    assert w.model.cues == before
    # '✏ 자막 맞추기…' = 지금 자막 다시 맞추기
    def shift(cues, regions, sources, note=""):
        assert len(sources) == 1 and len(regions) == 2 and cues[0].conf is None
        return [type(w.model.cues[0])(k + 1, c.start + 0.1, c.end, c.text) for k, c in enumerate(cues)]
    fp.open_aligner = shift
    fp.btn_align.click()
    assert w.model.cues[0].start == pytest.approx(1.1, abs=0.06)
    w.model.undo()
    # 녹음은 더빙 트랙에 그대로 (자막과 같은 시간)
    assert w.model.take_positions()[-1] == pytest.approx(1.0, abs=0.01)
    # 되돌리기 한 번 = 자막 없음
    w.model.undo()
    assert w.model.cues == []
    w.model.redo()
    # SRT 저장
    fp.ask_srt_path = lambda: d / "out.srt"
    out = fp.save_srt()
    assert "첫 번째 문장입니다." in out.read_text(encoding="utf-8-sig")
    assert sfp.export_sub(str(d / "out2.srt")) and "두 번째 문장이에요." in (d / "out2.srt").read_text(encoding="utf-8")


def test_replace_confirm_import_audio_and_long_warning(win):
    w, d, app = win
    fp = w.dub.free_page
    # 기존 자막 + 줄별 녹음
    w.model.edit(lambda p: (setattr(p.subtitle, "file", "subs.srt"),
                            p.dubbing.takes.append(__import__("composer.schema", fromlist=["Take"]).Take(
                                file="take1.wav", subtitle=2, offset=0.0))))
    line_pos = w.model.take_positions()[0]
    long = tones(d / "long.wav", [("tone", 1.0), ("sil", 0.8), ("tone", 1.2)])
    fp.ask_audio_file = lambda: long
    w.player.seek(1.5)
    fp.btn_import.click()
    tk = w.model.project.dubbing.takes[-1]
    assert tk.file == "long.wav" and tk.offset == pytest.approx(1.5) and tk.subtitle is None
    assert "영상" in fp.lbl_result.text() and "깁니다" in fp.lbl_result.text()  # 1.5+3.0 > 3초 영상
    assert len(w.model.project.clips) == 1 and w.model.timeline.total == pytest.approx(3.0, abs=0.1)  # 자동 변경 없음
    fp.engine.setCurrentIndex(fp.engine.findData("silence"))
    fp.run_worker = _sync_worker
    fp.open_aligner = _aligner([])
    asked = []
    fp.confirm = lambda t, m: asked.append(m) or False
    fp.btn_make.click()
    assert asked and "바꿀까요" in asked[0] and w.model.project.subtitle.file == "subs.srt"  # 취소 → 그대로
    fp.confirm = lambda t, m: True
    fp.btn_make.click()
    assert w.model.project.subtitle.file is None and [c.text for c in w.model.cues] == ["(1번 대사)", "(2번 대사)"]
    assert w.model.project.dubbing.takes[0].subtitle is None  # 줄별 녹음은 제자리 고정
    assert w.model.take_positions()[0] == pytest.approx(line_pos)


def test_engine_note_texts(win, monkeypatch):
    w, d, app = win
    fp = w.dub.free_page
    monkeypatch.setattr("composer.ui.free_record.whisper_available", lambda: False)
    fp._update_engine_note()
    assert "설치되어 있지 않습니다" in fp.lbl_engine.text()
    monkeypatch.setattr("composer.ui.free_record.whisper_available", lambda: True)
    monkeypatch.setattr("composer.ui.free_record.model_downloaded", lambda m: False)
    fp._update_engine_note()
    assert "처음 한 번" in fp.lbl_engine.text() and "485MB" in fp.lbl_engine.text()


# ------------------------------------------------------------------ 자막 맞추기 화면
def test_align_dialog_ui(tmp_path):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from composer.ui.sub_align import SubAlignDialog
    app = QApplication.instance() or QApplication([])
    a = tones(tmp_path / "a.wav", [("tone", 1.0), ("sil", 0.8), ("tone", 1.2)])  # 말 0~1.0, 1.8~3.0
    cues = [EditCue(0.1, 0.9, "첫 문장", "첫 문장", 0.95), EditCue(1.7, 3.0, "둘째 문장 여기까지", "엉뚱", 0.3, "인식 일치 30%")]
    dlg = SubAlignDialog(cues, detect_speech(a), [(a, 0.0)], note="안내")
    asked = []
    dlg.confirm = lambda t, m: asked.append(m) or False
    dlg.resize(1100, 600)
    dlg.show()
    app.processEvents()
    t = dlg.table
    assert [t.horizontalHeaderItem(k).text() for k in range(6)] == ["번호", "시작", "끝", "대본 문구", "인식 문구", "신뢰도"]
    assert t.item(1, 4).text() == "엉뚱" and t.item(1, 5).text() == "30%" and t.item(1, 0).background().color().alpha() > 0
    assert dlg.view.sel == 1  # 처음에 첫 노란색 자막을 골라 둠
    names = [b.text() for b in (dlg.btn_split, dlg.btn_merge, dlg.btn_play_cue, dlg.btn_tap, dlg.btn_apply)]
    assert names == ["✂ 여기서 나누기", "🔗 다음과 합치기", "▶ 이 자막 듣기", "⌨ 탭으로 맞추기", "✔ 적용"]
    assert "(1)" in dlg.btn_next_low.text()
    v = dlg.view
    # 가장자리 끌기: 첫 자막 끝(0.9)을 오른쪽으로 → 자석으로 말 끝(1.0)에 붙음
    y = 80
    x_end = int(v.x_of(0.9))
    QTest.mousePress(v, Qt.LeftButton, Qt.NoModifier, QPoint(x_end, y))
    QTest.mouseMove(v, QPoint(x_end + int(0.08 * v.pps), y))
    QTest.mouseMove(v, QPoint(x_end + int(0.12 * v.pps), y))
    QTest.mouseRelease(v, Qt.LeftButton, Qt.NoModifier, QPoint(x_end + int(0.12 * v.pps), y))
    assert dlg.cl.cues[0].end == pytest.approx(1.0, abs=0.02)
    assert dlg.cl.undo() and dlg.cl.cues[0].end == 0.9 and not dlg.cl.can_undo()  # 끌기 한 번 = 되돌리기 한 번
    # Shift 끌기 = 자석 끔
    x0 = int(v.x_of(0.1))
    QTest.mousePress(v, Qt.LeftButton, Qt.ShiftModifier, QPoint(x0, y))
    ev_x = x0 + int(0.03 * v.pps) + 4
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent, QPointF
    me = QMouseEvent(QEvent.MouseMove, QPointF(ev_x, y), QPointF(ev_x, y), Qt.NoButton, Qt.LeftButton, Qt.ShiftModifier)
    v.mouseMoveEvent(me)
    QTest.mouseRelease(v, Qt.LeftButton, Qt.ShiftModifier, QPoint(ev_x, y))
    assert dlg.cl.cues[0].start > 0.11  # 말 시작(0.0)에 붙지 않음
    dlg.undo()
    # 블록 클릭 = 그 구간만 듣기
    played = []
    dlg.play_range = lambda s, e: played.append((s, e))
    v.play_requested.disconnect()
    v.play_requested.connect(dlg.play_range)
    xm = int(v.x_of(2.3))
    QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(xm, y))
    assert played == [(1.7, 3.0)] and dlg.view.sel == 1
    # 더블클릭 = 문구 고치기
    QTest.mouseDClick(v, Qt.LeftButton, Qt.NoModifier, QPoint(xm, y))
    dlg.text.setPlainText("둘째 문장 고침 여기까지")
    assert dlg.cl.cues[1].text == "둘째 문장 고침 여기까지" and "고침" in t.item(1, 3).text()
    # 여기서 나누기 (재생 위치) → 다음과 합치기 → 되돌리기
    dlg.seek(2.35)
    assert dlg.split_here() and len(dlg.cl) == 3 and dlg.table.rowCount() == 3
    assert dlg.cl.cues[1].end == pytest.approx(2.325) and dlg.cl.cues[2].start == pytest.approx(2.375)
    assert dlg.merge_next() and len(dlg.cl) == 2
    dlg.btn_undo.click()
    assert len(dlg.cl) == 3
    dlg.btn_redo.click()
    assert len(dlg.cl) == 2
    # 다음 노란색으로 / 확인함
    dlg.select(0)
    assert dlg.goto_next_low() == 1 and played[-1][0] == 1.7
    dlg.btn_ok_low.click()
    assert dlg.cl.low_count() == 0 and "없음" in dlg.btn_next_low.text()
    # 탭으로 맞추기: 처음부터, 누를 때마다 다음 자막 시작
    dlg.select(0)
    dlg.btn_tap.setChecked(True)
    assert dlg.tap_box.isVisibleTo(dlg) and dlg.sc_space.isEnabled() and "첫 문장" in dlg.lbl_tap_cur.text()
    assert "다음:" in dlg.lbl_tap_next.text()
    assert dlg.tap_now(0.05) == 0 and dlg.tap_now(1.85) == 1
    assert dlg.cl.cues[0].start == 0.05 and dlg.cl.cues[1].start == 1.85
    assert dlg.cl.cues[0].end == pytest.approx(1.3)  # 말 끝(1.0) + 0.3
    assert "끝까지" in dlg.lbl_tap_cur.text()
    dlg.btn_tap.setChecked(False)
    # 닫기: 고친 게 있으면 묻고, '아니오'면 안 닫힘
    dlg.reject()
    assert asked and dlg.isVisible()
    out = dlg.result_cues()
    assert [c.index for c in out] == [1, 2] and out[1].start == 1.85
    dlg.accept()
    assert not dlg.isVisible()
