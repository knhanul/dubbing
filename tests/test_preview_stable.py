"""자막이 나타나고 사라질 때 미리보기 영상이 흔들리지 않는지 (화면 배치가 바뀌지 않는지) + 내보낸 영상 확인."""
import subprocess

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.schema import Project

LONG = "아주 아주 긴 자막 줄입니다 " * 8


@pytest.fixture
def win(tmp_path):
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from tests.media_fixtures import make_image
    make_image(tmp_path / "a.png", 640, 360, "red")
    make_image(tmp_path / "b.png", 360, 640, "blue")
    w = mw.MainWindow()
    w.resize(1400, 850)
    p = Project.from_dict({
        "output": {"width": 1280, "height": 720},
        "clips": [{"file": "a.png", "duration": 7}, {"file": "b.png", "duration": 7}],
        "subtitle": {"items": [{"index": 1, "start": 2, "end": 4, "text": LONG},
                               {"index": 2, "start": 6, "end": 8, "text": "짧음"},
                               {"index": 3, "start": 10, "end": 12, "text": "두 줄\n" + LONG}],
                     "style": {"appear": "pop"}}}, base_dir=tmp_path)
    w.set_project(p, None)
    w.show()
    yield w
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _geo(w):
    for _ in range(8):
        QCoreApplication.processEvents()
    v = w.player.view
    widgets = [v, w.player.slider, w.player.btn_play, w.player.btn_refresh, w.right, w.timeline]
    return ([(x.mapTo(w, x.rect().topLeft()).toTuple(), x.size().toTuple()) for x in widgets],
            v.mapFromScene(v.sceneRect()).boundingRect().getRect(),
            [lab.size().toTuple() for lab in (w.dub.lbl_cur, w.dub.lbl_next, w.dub.lbl_cue)])


def test_video_geometry_unchanged_when_subtitles_change(win):
    w = win
    w.right.setCurrentWidget(w.dub_scroll)
    ref = _geo(w)
    seen_text = set()
    for t in (0, 1, 2.05, 3, 4.5, 6.1, 7.5, 9, 10.05, 11, 12.5, 13):
        w.player.seek(t)
        g = _geo(w)
        seen_text.add(w.player.view.text)
        assert g == ref, f"{t}초에서 화면 배치가 바뀜"
    assert len(seen_text) == 4  # 없음 / 긴 자막 / 짧음 / 두 줄 — 실제로 바뀌었음
    # 진행 표시, 알림, 빈 화면 안내도 배치를 바꾸지 않음
    w.player.set_progress("⏳ 미리보기 만드는 중… 40%", 0.4)
    assert _geo(w) == ref
    w.player.set_progress("", None)
    w.player.set_notice("아주 긴 알림 " * 20)
    w.player.set_empty_hint("안내 " * 30)
    assert _geo(w) == ref
    # 재생 중(타임라인 시계)에도 그대로
    w._play_without_preview_ok = True
    w.player.seek(1.5)
    w.player.play()
    import time
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        assert _geo(w) == ref
    w.player.pause()
    # 다른 탭(자막 스타일)에서도
    w.right.setCurrentWidget(w.sub_scroll)
    ref2 = _geo(w)
    assert ref2[0][0] == ref[0][0] and ref2[1] == ref[1]
    for t in (1, 3, 7, 11):
        w.player.seek(t)
        assert _geo(w) == ref2


def test_burn_in_does_not_shift_picture(tmp_path):
    """새겨 넣은 자막이 나타나고 사라지는 순간에도 자막 밖 영역은 그대로(흔들림 없음)."""
    from composer.probe import ffmpeg_path
    from composer.render.ffmpeg_builder import RenderSettings
    from composer.render.runner import render_project
    ff = ffmpeg_path()
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360", "-frames:v", "1",
                    str(tmp_path / "pat.png")], check=True)
    (tmp_path / "s.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n자막이 나타납니다\n\n"
                                    "2\n00:00:02,000 --> 00:00:03,000\n다른 자막\n", encoding="utf-8")
    p = Project.from_dict({"output": {"width": 640, "height": 360, "fps": 25},
                           "clips": [{"file": "pat.png", "duration": 4, "motion": "none"}],
                           "subtitle": {"file": "s.srt", "style": {"size": 30, "margin_v": 30}}}, base_dir=tmp_path)
    res = render_project(p, tmp_path / "o.mp4", RenderSettings())
    raw = subprocess.run([ff, "-v", "error", "-i", str(res.output), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 360, 640).astype(int)
    region = frames[:, :240, :]  # 자막(아래쪽) 밖
    sub_region = frames[:, 280:, :]
    # 자막 바뀌는 경계(0.96~1.04초, 1.96~2.04초, 2.96~3.04초) 포함 모든 이웃 프레임 차이가 거의 0
    diffs = np.abs(np.diff(region, axis=0)).mean(axis=(1, 2))
    assert diffs.max() < 0.5, diffs.max()
    # 자막 영역은 실제로 바뀜 (테스트가 의미 있음)
    sdiff = np.abs(np.diff(sub_region, axis=0)).mean(axis=(1, 2))
    assert sdiff.max() > 2
