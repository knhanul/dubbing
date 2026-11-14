"""트랙 켜고 끄기(👁 감추기 · 🔇 소리 끄기 · S 솔로 · 🔒 잠금): 저장, 검사, 소리 규칙, 내보내기, 미리보기, 화면."""
import json
import os
import subprocess

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from composer.probe import ffmpeg_path  # noqa: E402
from composer.render.ffmpeg_builder import RenderSettings  # noqa: E402
from composer.render.runner import render_bgm_stem, render_project  # noqa: E402
from composer.schema import Project, Tracks  # noqa: E402
from composer.tracks import audible, dimmed, effective_project, off_tracks  # noqa: E402
from composer.validate import validate_project  # noqa: E402


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from media_fixtures import make_media
    d = tmp_path_factory.mktemp("tracks")
    make_media(d)
    return d


def _proj(d, tracks=None):
    return Project.from_dict({
        "output": {"width": 320, "height": 180, "fps": 25},
        "transition": {"type": "none", "duration": 0},
        "clips": [{"file": "vid1.mp4"}, {"file": "vid1.mp4"}],
        "subtitle": {"file": "subs.srt", "burn_in": True,
                     "style": {"box": True, "box_color": "#FFFFFF", "box_opacity": 1.0, "size": 30}},
        "bgm": {"file": "music.mp3", "volume": 0.5, "fade_in": 0, "fade_out": 0},
        "dubbing": {"takes": [{"file": "take1.wav", "subtitle": 2, "offset": 0}]},
        "tracks": tracks or {},
    }, base_dir=d)


def _rms(path, start, dur, f=None):
    af = ["-af", f"bandpass=f={f}:width_type=h:w=40,bandpass=f={f}:width_type=h:w=40"] if f else []
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-ss", str(start), "-t", str(dur), "-i", str(path), *af,
                          "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _frame(path, t, w=320, h=180):
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w).astype(float)


# ------------------------------------------------------------------ 규칙
def test_schema_roundtrip_and_validation(tmp_path):
    p = Project.from_dict({"clips": [], "tracks": {"clips": {"hidden": True}, "bgm": {"solo": True, "x": 1}}})
    assert p.tracks.clips.hidden and p.tracks.bgm.solo and not p.tracks.dubbing.muted
    d = json.loads(p.to_json())
    assert d["tracks"]["clips"] == {"hidden": True, "muted": False, "solo": False, "locked": False}
    assert Project.from_dict(d).tracks == p.tracks
    assert Project.from_dict({"clips": []}).tracks == Tracks()  # 예전 JSON / AI JSON 은 기본값
    iss = validate_project({"clips": [{"file": "a.png"}], "tracks": {"clips": {"hidden": "yes"}, "foo": {}}})
    assert any(i.path == "tracks.clips.hidden" and i.severity == "error" for i in iss)
    assert any(i.path == "tracks.foo" and i.severity == "warning" for i in iss)
    schema = json.loads(open("schema/project.schema.json", encoding="utf-8").read())
    assert "tracks" in schema["properties"]
    from composer.prompt_template import RULES
    assert "tracks" not in RULES  # AI 템플릿은 그대로


def test_mute_solo_rules(media):
    p = _proj(media)
    assert all(audible(p, k) for k in ("clips", "dubbing", "bgm")) and off_tracks(p) == []
    p.tracks.dubbing.solo = True
    assert audible(p, "dubbing") and not audible(p, "clips") and not audible(p, "bgm")
    p.tracks.bgm.solo = True
    assert audible(p, "bgm") and not audible(p, "clips")
    p.tracks.bgm.muted = True  # 소리 끄기가 솔로보다 우선
    assert not audible(p, "bgm") and dimmed(p, "bgm")
    assert off_tracks(p) == ["클립 원본 소리 꺼짐", "배경음악 소리 꺼짐"]
    p.tracks.subtitle.hidden = True
    p.tracks.clips.hidden = True
    assert "자막 감춤" in off_tracks(p) and "클립 화면 감춤(검은 화면)" in off_tracks(p)
    e = effective_project(p)
    assert e.bgm.file is None and all(c.mute for c in e.clips) and e.dubbing.takes
    assert e.subtitle.file is None and e.dubbing.takes[0].subtitle is None  # 자막 빼도 더빙은 제자리
    assert e.tracks.clips.hidden and not e.tracks.dubbing.solo
    assert p.bgm.file == "music.mp3"  # 원본은 그대로


# ------------------------------------------------------------------ 내보내기
def test_export_honors_hidden_and_muted(media):
    st = RenderSettings(draft=False)
    normal = render_project(_proj(media), media / "n.mp4", st).output
    # 3.5초: 영상 + 2번 자막(흰 상자) + 더빙(880) + 배경음악(220) + 원본 소리(440)
    assert _rms(normal, 3.1, 0.8, 880) > 0.02 and _rms(normal, 1.0, 1.0, 220) > 0.02 and _rms(normal, 1.0, 1.0, 440) > 0.02
    hid = render_project(_proj(media, {"clips": {"hidden": True}}), media / "h.mp4", st).output
    f = _frame(hid, 1.2)
    assert np.median(f) < 5 and f.mean() < 60  # 검은 화면 (1.2초엔 자막 1 상자만 흼)
    assert f[:60].max() < 30  # 위쪽은 완전히 검정
    assert _frame(hid, 2.5).max() < 30  # 자막 없는 구간은 전부 검정
    assert _frame(hid, 3.5).max() > 200  # 자막(흰 상자)은 검은 화면 위에 그대로
    nosub = render_project(_proj(media, {"clips": {"hidden": True}, "subtitle": {"hidden": True}}),
                           media / "s.mp4", st).output
    assert _frame(nosub, 3.5).max() < 30
    assert _rms(nosub, 3.1, 0.8, 880) > 0.02  # 자막을 감춰도 자막에 붙은 더빙은 제자리
    muted = render_project(_proj(media, {"clips": {"muted": True}, "bgm": {"muted": True}}), media / "m.mp4", st).output
    assert _rms(muted, 1.0, 1.0, 440) < 0.003 and _rms(muted, 1.0, 1.0, 220) < 0.003
    assert _rms(muted, 3.1, 0.8, 880) > 0.02
    solo = render_project(_proj(media, {"bgm": {"solo": True}}), media / "solo.mp4", st).output
    assert _rms(solo, 1.0, 1.0, 220) > 0.02 and _rms(solo, 1.0, 1.0, 440) < 0.003 and _rms(solo, 3.1, 0.8, 880) < 0.003


def test_bgm_stem(media):
    out = render_bgm_stem(_proj(media), media / "stem.wav")
    assert out.exists()
    assert _rms(out, 1.0, 1.0, 220) > 0.02 and _rms(out, 1.0, 1.0, 440) < 0.003 and _rms(out, 3.1, 0.8, 880) < 0.003
    p = _proj(media)
    p.bgm.file = None
    assert render_bgm_stem(p, media / "none.wav") is None


# ------------------------------------------------------------------ 화면
@pytest.fixture
def win(media):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    w = mw.MainWindow()
    w.asked = []
    w.confirm = lambda title, text: w.asked.append(text) or True
    w.notes = []
    w.notify = lambda text, warning=False: w.notes.append(text)
    w.show()
    w.set_project(_proj(media), None)
    app.processEvents()
    yield w, app
    w.model._set_dirty(False)
    w.shutdown()
    w.close()
    w.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_header_buttons_json_undo_and_dim(win):
    w, app = win
    hd = w.timeline.header
    keys = sorted(hd.buttons)
    assert keys == sorted([("clips", f) for f in ("hidden", "muted", "solo", "locked")]
                          + [("subtitle", "hidden"), ("subtitle", "locked")]
                          + [("dubbing", f) for f in ("muted", "solo", "locked")]
                          + [("bgm", f) for f in ("muted", "solo", "locked")])
    b = hd.buttons[("bgm", "muted")]
    assert b.text() == "🔊" and "배경음악 소리 끄기" in b.toolTip() and not b.isChecked()
    b.click()
    assert w.model.project.tracks.bgm.muted and b.isChecked() and b.text() == "🔇" and "꺼짐" in b.toolTip()
    assert json.loads(w.model.project.to_json())["tracks"]["bgm"]["muted"] is True and w.model.dirty
    assert "line-through" not in hd.labels["bgm"].styleSheet() and "#6b707a" in hd.labels["bgm"].styleSheet()
    hd.buttons[("subtitle", "hidden")].click()
    assert "line-through" in hd.labels["subtitle"].styleSheet()
    w.model.undo()
    assert not w.model.project.tracks.subtitle.hidden and w.model.project.tracks.bgm.muted
    w.model.undo()
    assert not w.model.project.tracks.bgm.muted and not b.isChecked()
    # 솔로 → 다른 트랙 흐리게 + 안내
    hd.buttons[("dubbing", "solo")].click()
    assert "(지금은 다른 트랙이 솔로라 안 들림)" in hd.buttons[("bgm", "solo")].toolTip()
    # 머리는 가로 스크롤해도 제자리
    x0 = hd.mapTo(w, hd.rect().topLeft()).x()
    w.timeline.canvas.set_zoom(400)
    w.timeline.horizontalScrollBar().setValue(500)
    app.processEvents()
    assert hd.mapTo(w, hd.rect().topLeft()).x() == x0 and hd.isVisible()


def test_preview_live_audio_and_overlay(win):
    w, app = win
    hd = w.timeline.header
    key = w._render_revision_key()
    assert w._subtitle_at(1.0) == "안녕하세요" and w._cue_at(1.0)
    hd.buttons[("subtitle", "hidden")].click()
    assert w._subtitle_at(1.0) == "" and w._cue_at(1.0) is None
    assert w._render_revision_key() == key  # 자막 감추기는 다시 렌더 없음
    # 🔇/S: 소리 크기만 (다시 렌더 없음)
    hd.buttons[("clips", "muted")].click()
    assert w.player.audio.volume() == 0.0 and w._render_revision_key() == key
    hd.buttons[("clips", "muted")].click()
    assert w.player.audio.volume() > 0.9
    w.bgm_live.wait(); app.processEvents()
    assert w.bgm_live.path is not None and w.bgm_live.audio.volume() > 0.9
    hd.buttons[("dubbing", "solo")].click()
    assert w.player.audio.volume() == 0.0 and w.bgm_live.audio.volume() == 0.0 and w._render_revision_key() == key
    hd.buttons[("dubbing", "solo")].click()
    hd.buttons[("bgm", "muted")].click()
    assert w.bgm_live.audio.volume() == 0.0 and w.player.audio.volume() > 0.9
    # 녹음 중 원본 줄이기는 배경음악에도
    hd.buttons[("bgm", "muted")].click()
    w.player.set_volume_factor(0.2)
    assert w.bgm_live.audio.volume() == pytest.approx(0.2, abs=0.01)
    w.player.set_volume_factor(1.0)
    # 👁 클립 감추기 → 영상 미리보기 다시 만들기 대상 + 자리표시 그림은 검정
    hd.buttons[("clips", "hidden")].click()
    assert w._render_revision_key() != key
    assert w._placeholder_at(1.0)[1] is None


def test_lock_blocks_edits(win, media):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    w, app = win
    hd = w.timeline.header
    hd.buttons[("clips", "locked")].click()
    hd.buttons[("subtitle", "locked")].click()
    hd.buttons[("bgm", "locked")].click()
    assert hd.buttons[("clips", "locked")].text() == "🔒"
    cv = w.timeline.canvas
    rect = next(r for k, i, r in cv._items() if k == "clip")
    before = w.model.project.to_json()
    n_undo = len(w.model._undo) if hasattr(w.model, "_undo") else None
    # 클립 오른쪽 끝을 끌어도 길이 안 바뀜
    p0 = QPointF(rect.right() - 2, rect.center().y())
    for typ, pos, btn in ((QEvent.MouseButtonPress, p0, Qt.LeftButton), (QEvent.MouseMove, p0 + QPointF(80, 0), Qt.NoButton),
                          (QEvent.MouseButtonRelease, p0 + QPointF(80, 0), Qt.LeftButton)):
        ev = QMouseEvent(typ, pos, cv.mapToGlobal(pos.toPoint()), btn, Qt.LeftButton if typ == QEvent.MouseMove else btn,
                         Qt.NoModifier)
        {QEvent.MouseButtonPress: cv.mousePressEvent, QEvent.MouseMove: cv.mouseMoveEvent,
         QEvent.MouseButtonRelease: cv.mouseReleaseEvent}[typ](ev)
    assert w.model.selection.kind == "clip"  # 고르기는 됨
    assert w.model.project.to_json() == before
    # 미세 조정 페이지 잠김 + 안내
    assert w.inspector.lock_banner.isVisibleTo(w.inspector) and not w.inspector.clip_page.isEnabled()
    # Delete / 화살표 미세 이동 막힘
    assert w.handle_key(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    w.nudge_selected(0.1)
    assert w.model.project.to_json() == before and "잠겨 있습니다" in w.statusBar().currentMessage()
    # 출력 설정의 배경음악·자막 묶음 잠김, 제거 기능도 막힘
    pg = w.inspector.project_page
    assert not pg.g_bgm.isEnabled() and "🔒" in pg.g_bgm.title() and not pg.g_sub.isEnabled()
    assert w.remove_bgm() is False and w.remove_all_subtitles() is False and w.asked == []
    # 탐색기에서 끌어 놓기도 넣지 않음
    w.handle_drop([media / "img1.png", media / "subs.srt"], None)
    app.processEvents()
    assert len(w.model.project.clips) == 2 and any("잠겨 있어" in n for n in w.notes)
    # 클립 목록 순서 바꾸기도 무시
    w.clip_panel.apply_order([1, 0])
    assert w.model.project.to_json() == before
    # 풀면 다시 편집 가능
    hd.buttons[("clips", "locked")].click()
    assert w.inspector.clip_page.isEnabled() and not w.inspector.lock_banner.isVisibleTo(w.inspector)


def test_export_dialog_warning(win, monkeypatch):
    w, app = win
    seen = {}

    def fake(opt):
        seen["text"] = opt.track_warning.text()
        seen["vis"] = not opt.track_warning.isHidden()
        return False

    w.exec_export_options = fake
    w.render_final()
    assert seen["vis"] is False
    w.timeline.header.buttons[("bgm", "muted")].click()
    w.timeline.header.buttons[("clips", "hidden")].click()
    w.render_final()
    assert seen["vis"] and "감춰지거나 음소거된 트랙이 있습니다" in seen["text"]
    assert "배경음악 소리 꺼짐" in seen["text"] and "클립 화면 감춤" in seen["text"]
