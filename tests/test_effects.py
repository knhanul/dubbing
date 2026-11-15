"""✨ 효과: 색 필터 · 밝기/대비/채도 · 분위기 · 영상 움직임 · 시작/끝 페이드 · 효과 탭."""
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from composer.edit_ops import split_clip
from composer.effects import (DEFAULTS, LOOKS, clear_effects, copy_effects, effect_filters, fade_filters,
                              has_effects)
from composer.probe import ffmpeg_path, probe
from composer.render.ffmpeg_builder import RenderSettings, build_assembly, build_clip_job, clip_cache_key
from composer.schema import Clip, Project
from composer.timeline import build_timeline
from composer.validate import validate_project


def validate_project_dict(d):
    iss = validate_project(d)
    return [str(i) for i in iss if i.severity == "error"], [str(i) for i in iss if i.severity != "error"]

try:
    ffmpeg_path()
    HAVE_FFMPEG = True
except FileNotFoundError:
    HAVE_FFMPEG = False
need_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg 필요")


def _graph(args):
    return args[args.index("-filter_complex") + 1]


# --- 필터 ---------------------------------------------------------------
def test_no_effects_no_filters():
    c = Clip(file="a.png", type="image")
    assert effect_filters(c) == [] and not has_effects(c)


@pytest.mark.parametrize("look, needle", [
    ("bw", "hue=s=0"), ("sepia", "colorchannelmixer"), ("vintage", "curves=preset=vintage"),
    ("warm", "colorbalance"), ("cool", "colorbalance"), ("vivid", "unsharp"), ("cinematic", "curves")])
def test_each_look_filter(look, needle):
    c = Clip(file="a.png", type="image", look=look)
    f = effect_filters(c)
    assert f and needle in ",".join(f) and has_effects(c)


def test_adjust_and_mood_filters_in_order():
    c = Clip(file="a.png", type="image", look="bw", brightness=0.1, contrast=1.2, saturation=1.0,
             blur=0.5, vignette=1.0, grain=0.5)
    f = effect_filters(c, 1080)
    assert f[0] == "hue=s=0"
    assert "eq=brightness=0.1:contrast=1.2" in f  # 채도 1(기본)은 빠짐
    assert "gblur=sigma=6" in f and "vignette=angle=1" in f and "noise=alls=22:allf=t+u" in f
    assert [x.split("=")[0] for x in f] == ["hue", "eq", "gblur", "vignette", "noise"]
    assert "gblur=sigma=2" in effect_filters(c, 360)  # 미리보기(360p)도 같은 느낌으로 비례
    only_sat = Clip(file="a.png", type="image", saturation=0.0)
    assert effect_filters(only_sat) == ["eq=saturation=0"] and has_effects(only_sat)


def test_copy_and_clear():
    a = Clip(file="a.png", type="image", look="sepia", grain=0.3, motion="pan_left", motion_strength=0.4)
    b = Clip(file="b.png", type="image")
    copy_effects(a, b)
    assert (b.look, b.grain, b.motion, b.motion_strength) == ("sepia", 0.3, "pan_left", 0.4)
    clear_effects(b)
    assert not has_effects(b) and b.motion == "pan_left"  # 움직임은 그대로


def test_cache_key_changes_with_effects():
    p = Project(clips=[Clip(file="a.png", type="image", duration=2)])
    k = lambda: clip_cache_key(p, p.clips[0], build_timeline(p, lambda c: None).clips[0], RenderSettings(), (1, 2), False)  # noqa: E731
    seen = {k()}
    for field, v in [("look", "warm"), ("brightness", 0.2), ("contrast", 1.3), ("saturation", 0.5),
                     ("vignette", 0.4), ("blur", 0.2), ("grain", 0.1)]:
        setattr(p.clips[0], field, v)
        seen.add(k())
    assert len(seen) == 8


def test_clip_job_contains_effects_and_video_motion():
    p = Project(clips=[Clip(file="v.mp4", type="video", trim_end=3, look="cool", vignette=0.5,
                            motion="zoom_in", motion_strength=0.2)])
    p.output.width, p.output.height = 640, 360
    tl = build_timeline(p, lambda c: 3.0)
    job = build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(), Path("v.mp4"), Path("o.mkv"), True)
    g = _graph(job.args)
    assert "perspective=" in g and "eval=frame" in g and "zoompan" not in g
    assert "scale=640:360:flags=lanczos" in g  # 확대해 움직인 뒤 다시 원래 크기로
    assert g.index("perspective=") < g.index("colorbalance") < g.index("vignette") < g.index("setsar=1")
    # 움직임 없는 영상은 확대하지 않음
    p.clips[0].motion = "none"
    g2 = _graph(build_clip_job(p, p.clips[0], tl.clips[0], RenderSettings(), Path("v.mp4"), Path("o.mkv"), True).args)
    assert "perspective" not in g2 and "colorbalance" in g2


def test_split_copies_effects_and_splits_video_motion():
    p = Project(clips=[Clip(file="v.mp4", type="video", trim_end=4, look="vintage", blur=0.3,
                            motion="zoom_in", motion_strength=0.2, motion_easing="linear")])
    tl = build_timeline(p, lambda c: 4.0)
    assert split_clip(p, tl, 0, 2.0)
    a, b = p.clips
    assert (a.look, b.look, a.blur, b.blur) == ("vintage", "vintage", 0.3, 0.3)
    assert a.motion == b.motion == "zoom_in"
    assert a.motion_from == 0 and b.motion_to == 1
    assert a.motion_to == pytest.approx(0.5, abs=0.01) and b.motion_from == pytest.approx(a.motion_to)


def test_fade_filters_and_assembly():
    v, a = fade_filters(10, 1, 2)
    assert v == ["fade=t=in:st=0:d=1:color=black", "fade=t=out:st=8:d=2:color=black"]
    assert a == ["afade=t=in:st=0:d=1", "afade=t=out:st=8:d=2"]
    assert fade_filters(2, 5, 5)[0] == ["fade=t=in:st=0:d=1:color=black", "fade=t=out:st=1:d=1:color=black"]
    assert fade_filters(5, 0, 0) == ([], [])
    p = Project(clips=[Clip(file="a.png", type="image", duration=3), Clip(file="b.png", type="image", duration=3)])
    p.output.fade_in, p.output.fade_out = 0.5, 1.0
    tl = build_timeline(p, lambda c: None)
    g = build_assembly(p, tl, [Path("c0.mkv"), Path("c1.mkv")], RenderSettings(), Path("o.mp4"), [], None).filter_graph
    assert "fade=t=in:st=0:d=0.5:color=black" in g and "fade=t=out:st=5:d=1:color=black" in g
    assert "afade=t=in:st=0:d=0.5" in g and "afade=t=out:st=5:d=1" in g
    p.output.fade_in = p.output.fade_out = 0
    g0 = build_assembly(p, tl, [Path("c0.mkv"), Path("c1.mkv")], RenderSettings(), Path("o.mp4"), [], None).filter_graph
    assert "fade=t=" not in g0


def test_old_project_loads_with_defaults_and_validate():
    p = Project.from_dict({"clips": [{"file": "a.png", "duration": 2}], "output": {"width": 1280, "height": 720}})
    c = p.clips[0]
    assert all(getattr(c, k) == d for k, d in DEFAULTS.items())
    assert p.output.fade_in == 0 and p.output.fade_out == 0
    d = {"clips": [{"file": "v.mp4", "type": "video", "motion": "pan_left", "look": "sepia", "grain": 0.5}],
         "output": {"fade_in": 1, "fade_out": 2}}
    errs, warns = validate_project_dict(d)
    assert not errs and not any("움직임" in w for w in warns)
    errs, _ = validate_project_dict({"clips": [{"file": "a.png", "contrast": 9}], "output": {"fade_in": -1}})
    assert any("contrast" in e for e in errs) and any("fade_in" in e for e in errs)
    # 저장 → 불러오기 그대로
    p2 = Project.from_dict({"clips": [{"file": "a.png", "look": "warm", "vignette": 0.4}], "output": {"fade_out": 1.5}})
    p3 = Project.from_dict(p2.to_dict())
    assert (p3.clips[0].look, p3.clips[0].vignette, p3.output.fade_out) == ("warm", 0.4, 1.5)


# --- 실제 ffmpeg -------------------------------------------------------
def _mean_rgb(path, at):
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(float)
    return a.mean(axis=0)


@need_ffmpeg
def test_real_render_with_effects_and_fade(tmp_path):
    from media_fixtures import make_media
    from composer.render.runner import render_project
    make_media(tmp_path)
    p = Project.from_dict({
        "output": {"width": 320, "height": 180, "fps": 25, "fade_in": 0.5, "fade_out": 0.5},
        "transition": {"type": "none", "duration": 0},
        "clips": [
            {"file": "img1.png", "duration": 1.5, "look": "bw", "brightness": 0.05, "contrast": 1.1,
             "vignette": 0.6, "blur": 0.2, "grain": 0.3},
            {"file": "vid1.mp4", "trim_end": 1.5, "look": "sepia", "motion": "zoom_in", "motion_strength": 0.2},
        ],
    }, base_dir=tmp_path)
    res = render_project(p, tmp_path / "out.mp4", RenderSettings())
    info = probe(res.output)
    assert info.duration == pytest.approx(3.0, abs=0.1) and (info.width, info.height) == (320, 180)
    r, g, b = _mean_rgb(res.output, 1.0)  # 흑백 → 세 색이 거의 같음
    assert abs(r - g) < 6 and abs(g - b) < 6
    r2, g2, b2 = _mean_rgb(res.output, 2.2)  # 세피아 → 빨강 > 파랑
    assert r2 > b2 + 10
    assert _mean_rgb(res.output, 0.0).mean() < 20  # 페드 인 첫 프레임은 거의 검정
    # 효과 하나만 바꾸면 그 클립만 다시 만듦
    p.clips[0].grain = 0.0
    res2 = render_project(p, tmp_path / "out2.mp4", RenderSettings())
    assert (res2.clips_rendered, res2.clips_cached) == (1, 1)


@need_ffmpeg
def test_look_thumbnails_cached(tmp_path):
    from media_fixtures import make_image
    from composer.ui.effects_panel import look_thumbnails
    src = make_image(tmp_path / "x.png", color="orange")
    out = look_thumbnails(src, 0.0, False, cache=tmp_path / "c")
    assert set(out) == set(LOOKS) and all(p.stat().st_size > 0 for p in out.values())
    assert look_thumbnails(src, 0.0, False, cache=tmp_path / "c") == out


# --- 화면 (✨ 효과 탭) --------------------------------------------------
@pytest.fixture
def win(tmp_path):
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from tests.media_fixtures import make_image, make_media
    make_media(tmp_path)
    make_image(tmp_path / "a.png")
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.confirm = lambda title, msg: True
    w.resize(1400, 900)
    w.set_project(Project.from_dict({
        "clips": [{"file": "a.png", "duration": 2}, {"file": "a.png", "duration": 2},
                  {"file": "vid1.mp4", "type": "video", "trim_end": 2}],
    }, base_dir=tmp_path))
    w.effects.sync_thumbs = True
    w.show()
    QApplication.processEvents()
    yield w, tmp_path
    w.model.dirty = False
    w.close()


def _slide(vs, v):
    vs.slider.setValue(int(round(v / vs.step)))


def test_tab_and_hint(win):
    w, _ = win
    assert w.right.tabText(1) == "✨ 효과"
    w.model.select("none", -1)
    w.right.setCurrentWidget(w.effects_scroll)
    fx = w.effects
    assert fx.hint.isVisibleTo(fx) and fx.hint.text() == "타임라인에서 클립을 선택하세요"
    assert not fx.body.isVisibleTo(fx)
    w.model.select("clip", 0)
    assert fx.body.isVisibleTo(fx) and not fx.hint.isVisibleTo(fx)
    assert w.right.currentWidget() is w.effects_scroll  # 효과 탭에서 클립을 고르면 탭 그대로
    if HAVE_FFMPEG:
        assert set(fx.thumb_paths) == set(LOOKS)
        assert not fx.look_cards["sepia"].icon().isNull()


def test_look_card_and_undo(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 1)
    fx.look_cards["sepia"].click()
    assert w.model.project.clips[1].look == "sepia" and fx.look_cards["sepia"].isChecked()
    assert w.timeline.canvas.clip_has_fx(1) and not w.timeline.canvas.clip_has_fx(0)
    w.timeline.canvas.grab()  # ✨ 표시 그리기
    assert "흑백" not in w.inspector.clip_page.lbl_fx.text()
    w.model.undo()
    assert w.model.project.clips[1].look == "none" and fx.look_cards["none"].isChecked()


def test_sliders_reset_and_merge(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    _slide(fx.sliders["brightness"], 0.2)
    _slide(fx.sliders["brightness"], 0.3)
    assert w.model.project.clips[0].brightness == pytest.approx(0.3)
    assert fx.resets["brightness"].isEnabled() and not fx.resets["contrast"].isEnabled()
    fx.resets["brightness"].click()
    assert w.model.project.clips[0].brightness == 0
    w.model.undo()  # 리셋 되돌리기
    assert w.model.project.clips[0].brightness == pytest.approx(0.3)
    w.model.undo()  # 끌기 한 번은 한 단계
    assert w.model.project.clips[0].brightness == 0
    _slide(fx.sliders["vignette"], 0.5)
    assert w.model.project.clips[0].vignette == pytest.approx(0.5)


def test_lock_blocks(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    w.model.edit(lambda p: setattr(p.tracks.clips, "locked", True))
    assert fx.lock_banner.isVisibleTo(fx) and not fx.body.isEnabled()
    assert not fx._edit("look", lambda c: setattr(c, "look", "bw"), None)
    assert not fx.apply_to_all() and not fx.clear()
    assert w.model.project.clips[0].look == "none"


def test_apply_all_is_one_undo_and_clear(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    fx.look_cards["warm"].click()
    _slide(fx.sliders["grain"], 0.4)
    fx.motion.setCurrentIndex(fx.motion.findData("pan_left"))
    fx.btn_all.click()
    pr = w.model.project
    assert all(c.look == "warm" and c.grain == pytest.approx(0.4) and c.motion == "pan_left" for c in pr.clips)
    w.model.undo()
    pr = w.model.project
    assert pr.clips[1].look == "none" and pr.clips[2].look == "none" and pr.clips[0].look == "warm"
    w.model.select("clip", 0)
    fx.btn_clear.click()
    c = w.model.project.clips[0]
    assert not has_effects(c) and c.motion == "pan_left"


def test_video_motion_in_effects_tab_not_in_clip_page(win):
    w, _ = win
    cp = w.inspector.clip_page
    assert not hasattr(cp, "strength_slider") and not hasattr(cp, "motion")
    w.model.select("clip", 2)
    fx = w.effects
    assert fx.motion.isEnabled() and "동영상" in fx.head.text()
    fx.motion.setCurrentIndex(fx.motion.findData("zoom_in"))
    assert w.model.project.clips[2].motion == "zoom_in" and fx.strength_slider.isEnabled()
    _slide(fx.strength_slider, 0.3)
    assert w.model.project.clips[2].motion_strength == pytest.approx(0.3)
    w.right.setCurrentIndex(0)
    w.model.select("clip", 2)
    assert "확대" in cp.lbl_fx.text() or "줌" in cp.lbl_fx.text()
    cp.btn_fx.click()
    assert w.right.currentWidget() is w.effects_scroll


def test_sections_fold_remembered(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    assert not fx.sec_mood.body.isHidden()
    fx.sec_mood.head.click()
    assert fx.sec_mood.body.isHidden()
    assert QSettings("knhanul", "VideoComposer").value("ui/fold/fx_mood") in (False, "false")


def test_output_fade_sliders(win):
    w, _ = win
    op = w.inspector.output_page
    _slide(op.fade_in, 1.0)
    _slide(op.fade_out, 2.0)
    assert (w.model.project.output.fade_in, w.model.project.output.fade_out) == (pytest.approx(1.0), pytest.approx(2.0))
    w.model.undo()
    assert w.model.project.output.fade_out == 0 and w.model.project.output.fade_in == pytest.approx(1.0)


# --- 합쳐 보기 · 카드 그림 · 카드 접기 (v2) -------------------------------
def _img_mean(path):
    from PySide6.QtGui import QImage
    im = QImage(str(path))
    assert not im.isNull()
    rs = gs = bs = 0
    n = 0
    for y in range(0, im.height(), 6):
        for x in range(0, im.width(), 6):
            c = im.pixelColor(x, y)
            rs += c.red(); gs += c.green(); bs += c.blue(); n += 1
    return rs / n, gs / n, bs / n


@need_ffmpeg
def test_effect_preview_combines_all_and_caches(tmp_path):
    from media_fixtures import make_image
    from composer.ui.effects_panel import effect_params, effect_preview, preview_size
    src = make_image(tmp_path / "x.png", color="orange")
    size = preview_size(1920, 1080)
    assert size == (480, 270) and preview_size(1080, 1920)[1] == 270
    base = effect_params(Clip(file="x.png", type="image"))
    p0 = effect_preview(src, 0, False, base, size, cache=tmp_path / "c")
    r, g, b = _img_mean(p0)
    assert r > b + 100  # 주황 그대로
    bw = {**base, "look": "bw", "brightness": 0.2}
    p1 = effect_preview(src, 0, False, bw, size, cache=tmp_path / "c")
    assert p1 != p0
    r1, g1, b1 = _img_mean(p1)
    assert abs(r1 - b1) < 8 and abs(r1 - g1) < 8  # 흑백
    m = p1.stat().st_mtime_ns
    assert effect_preview(src, 0, False, dict(bw), size, cache=tmp_path / "c") == p1 and p1.stat().st_mtime_ns == m  # 캐시
    # 비네팅: 가장자리가 가운데보다 어두움
    from PySide6.QtGui import QImage
    pv = effect_preview(src, 0, False, {**base, "vignette": 1.0}, size, cache=tmp_path / "c")
    im = QImage(str(pv))
    assert im.pixelColor(2, 2).red() + 40 < im.pixelColor(240, 135).red()


@need_ffmpeg
def test_look_cards_include_other_adjustments(tmp_path):
    from media_fixtures import make_image
    from composer.ui.effects_panel import look_cards_graph, look_thumbnails
    g, _ = look_cards_graph({**DEFAULTS, "brightness": 0.3, "vignette": 0.5}, 96, 54)
    assert g.count("eq=brightness=0.3") == len(LOOKS) and g.count("vignette=") == len(LOOKS)
    src = make_image(tmp_path / "x.png", color="gray")
    plain = look_thumbnails(src, 0, False, cache=tmp_path / "c", params=dict(DEFAULTS))
    bright = look_thumbnails(src, 0, False, cache=tmp_path / "c", params={**DEFAULTS, "look": "sepia", "brightness": 0.3})
    assert plain["none"].parent != bright["none"].parent  # 다른 조정이 바뀌면 다른 캐시
    assert _img_mean(bright["none"])[1] > _img_mean(plain["none"])[1] + 30
    # 색 필터만 바꾸면 카드 그림은 그대로 (캐시 재사용)
    again = look_thumbnails(src, 0, False, cache=tmp_path / "c", params={**DEFAULTS, "look": "bw", "brightness": 0.3})
    assert again == bright


@need_ffmpeg
def test_panel_preview_updates_sync(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    assert fx.preview_path is not None and fx.preview.has_image()
    first = fx.preview_path
    _slide(fx.sliders["contrast"], 1.5)
    assert fx.preview_path != first and fx.preview_path.exists()
    w.model.undo()
    assert fx.preview_path == first  # 같은 값이면 캐시 그림 그대로


@need_ffmpeg
def test_panel_preview_debounced_in_background(win, monkeypatch):
    import time
    import composer.ui.effects_panel as ep
    w, _ = win
    fx = w.effects
    w.model.select("clip", 1)
    fx.sync_thumbs = False
    calls = []
    real = ep.effect_preview
    monkeypatch.setattr(ep, "effect_preview", lambda *a, **k: (calls.append(a[3]), real(*a, **k))[1])
    for v in (0.31, 0.32, 0.33):  # 빠르게 여러 번 → 0.3초 뒤 한 번만
        _slide(fx.sliders["grain"], v)
    assert fx._debounce.isActive() and fx._debounce.interval() == 300
    assert fx.busy.isVisibleTo(fx) and fx.busy.text() == "만드는 중…"
    assert w.model.project.clips[1].grain == pytest.approx(0.33)  # 값은 바로 적용 (그림만 기다림)
    t0 = time.time()
    while fx.busy.isVisibleTo(fx) and time.time() - t0 < 15:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not fx.busy.isVisibleTo(fx)
    assert len(calls) == 1 and calls[0]["grain"] == pytest.approx(0.33)
    assert fx.preview_path is not None and fx.preview_path.exists()
    t0 = time.time()
    while set(fx.thumb_paths) != set(LOOKS) and time.time() - t0 < 15:
        QApplication.processEvents()
        time.sleep(0.01)
    assert set(fx.thumb_paths) == set(LOOKS)


def test_cards_fold_with_summary_and_dot(win):
    w, _ = win
    fx = w.effects
    w.model.select("clip", 0)
    cards = (fx.sec_look, fx.sec_adjust, fx.sec_mood, fx.sec_motion)
    assert all(c.is_open() for c in cards) and not any(c.dirty for c in cards[:3])
    fx.look_cards["vintage"].click()
    _slide(fx.sliders["brightness"], 0.1)
    _slide(fx.sliders["saturation"], 0.95)
    _slide(fx.sliders["vignette"], 0.3)
    for c in cards:
        c.head.click()
    assert all(not c.is_open() and c.body.isHidden() for c in cards)
    assert fx.sec_look.header_text() == "▸ 색 필터 — 빈티지"
    assert fx.sec_adjust.header_text() == "▸ 밝기 · 대비 · 채도 — 밝기 +10 · 채도 -5"
    assert fx.sec_mood.header_text() == "▸ 분위기 — 비네팅 30"
    assert fx.sec_look.dot.isVisibleTo(fx) and fx.sec_adjust.dot.isVisibleTo(fx) and fx.sec_mood.dot.isVisibleTo(fx)
    assert QSettings("knhanul", "VideoComposer").value("ui/fold/fx_look") in (False, "false")
    from composer.ui.effects_panel import FxCard
    assert not FxCard("x", key="fx_look").is_open()  # 새로 만들어도 접힘 기억
    fx.sec_look.head.click()
    assert fx.sec_look.header_text() == "▾ 색 필터"  # 펼치면 요약 없음
    fx.btn_clear.click()
    assert not fx.sec_adjust.dot.isVisibleTo(fx) and fx.sec_adjust.summary == "기본"
    # 값 글자도 같은 모양
    assert fx.sliders["brightness"].label.text() == "+0" and fx.sliders["vignette"].label.text() == "0"
