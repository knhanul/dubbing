"""오른쪽 패널 개편: 최소 폭 440px · 탭 순서/이름 · 넓으면 2단 · 녹음본 이름·★ · 목록 녹음본 고르기 ·
시작하기 단계 · 내보내기 요약 · 효과 탭에 있는 동안 미리보기 다시 만들기 미루기 · 효과 그림은 작은 원본으로."""
import os
import time

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.schema import Project


def _wav(path, sec):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(48000 * sec), np.float32), 48000)


@pytest.fixture
def win(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    from media_fixtures import make_media
    import composer.ui.main_window as mw
    make_media(tmp_path)
    _wav(tmp_path / "takes" / "line_0001_v1.wav", 1.0)
    _wav(tmp_path / "takes" / "line_0001_v2.wav", 1.5)
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.answer = True
    w.confirm = lambda title, msg: w.answer
    w.resize(1000, 900)
    w.show()
    w.set_project(Project.from_dict({"clips": [{"file": "img1.png", "duration": 6}, {"file": "img1.png", "duration": 6}],
                                     "subtitle": {"file": "subs.srt"},
                                     "dubbing": {"takes": [{"file": "takes/line_0001_v2.wav", "subtitle": 1}]}},
                                    base_dir=tmp_path), None)
    QCoreApplication.processEvents()
    yield w, tmp_path
    w.model._set_dirty(False)
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _settle(n=4):
    for _ in range(n):
        QCoreApplication.processEvents()


def test_min_width_and_every_tab_fits_without_side_scroll(win):
    from composer.ui.widgets import PANEL_MIN_W
    w, _ = win
    assert PANEL_MIN_W == 440 and w.right.minimumWidth() == 440
    top = w.right.parentWidget()
    top.setSizes([w.width() - 300, 300])  # 더 좁게 하려 해도 440 아래로는 안 줄어듦
    _settle()
    assert w.right.width() >= 440
    top.setSizes([w.width() - 440, 440])
    _settle()
    for sel in (("clip", 0), ("take", 0), ("subtitle", 1), ("transition", 1), ("", -1)):
        w.model.select(*sel)
        w.right.setCurrentIndex(0)
        _settle()
        page = w.inspector.stack.currentWidget()
        assert page.minimumSizeHint().width() <= w.right.width() - 4, sel
    for tab in (w.dub_scroll, w.sub_scroll, w.effects_scroll, w.output_scroll):
        w.right.setCurrentWidget(tab)
        _settle()
        assert not tab.scroll.horizontalScrollBar().isVisible(), w.right.tabText(w.right.indexOf(tab))


def test_wide_panel_uses_two_columns(win):
    w, _ = win
    out = w.inspector.output_page
    w.right.setCurrentWidget(w.output_scroll)
    top = w.right.parentWidget()
    top.setSizes([w.width() - 440, 440])
    _settle()
    assert not out.cols.is_two()
    w.resize(1700, 900)
    top.setSizes([800, 900])
    _settle()
    assert out.cols.is_two()
    # 오른쪽 단에 '장면 바뀔 때'·'처음·끝'
    assert out.cols.right.indexOf(out.g_tr) >= 0 and out.cols.right.indexOf(out.g_fade) >= 0


def test_take_names_and_star_saved_in_project(win):
    w, d = win
    tp = w.inspector.take_page
    w.model.select("take", 0)
    rows = tp.history_rows
    assert [r["version"].version for r in rows] == [1, 2] and tp.history.title().startswith("녹음본 2개")
    assert rows[1]["use"].isChecked() and not rows[1]["use"].isEnabled()
    assert tp.rename_version("takes/line_0001_v1.wav", "  밝게 ") is True
    rows = tp.history_rows
    assert "밝게" in rows[0]["label"].text()
    rows[0]["star"].click()
    assert w.model.project.dubbing.notes == {"takes/line_0001_v1.wav": {"name": "밝게", "star": True}}
    assert tp.history_rows[0]["star"].text() == "★"
    # 저장했다 다시 불러와도 그대로, JSON 스키마 통과
    from composer.validate import validate_project
    dd = w.model.project.to_dict()
    assert Project.from_dict(dd, base_dir=d).dubbing.notes["takes/line_0001_v1.wav"]["name"] == "밝게"
    assert not [i for i in validate_project(dd, d) if i.severity == "error" and "notes" in str(i)]
    # 되돌리기
    w.model.undo()
    assert w.model.project.dubbing.notes == {"takes/line_0001_v1.wav": {"name": "밝게"}}
    # 이름을 비우고 ★ 도 끄면 항목이 사라짐
    w.model.undo()
    assert w.model.project.dubbing.notes == {}
    # 파일을 지우면 이름·★ 도 정리
    tp.set_star("takes/line_0001_v1.wav", True)
    assert w.delete_take_file("takes/line_0001_v1.wav") is True
    assert "takes/line_0001_v1.wav" not in w.model.project.dubbing.notes


def test_version_play_uses_video(win, monkeypatch):
    w, _ = win
    tp = w.inspector.take_page
    w.model.select("take", 0)
    got = []
    monkeypatch.setattr(w.dub, "play_version", lambda i, rel: got.append((i, rel)))
    tp.history_rows[0]["play"].click()
    assert got == [(0, "takes/line_0001_v1.wav")]


def test_record_list_badge_menu_picks_version(win):
    w, _ = win
    dub = w.dub
    dub.refresh_table()
    assert dub.table.item(0, 5).text() == "✔ 2개 ▾" and dub.list_sec.title() == "자막 2줄 · 녹음 1줄"
    menu = dub.version_menu(0, exec_menu=False)
    acts = [a for a in menu.actions() if a.isCheckable()]
    assert [a.isChecked() for a in acts] == [False, True]
    acts[0].trigger()
    assert w.model.project.dubbing.takes[0].file == "takes/line_0001_v1.wav"
    w.model.undo()
    assert dub.version_menu(1, exec_menu=False) is None  # 녹음 없는 줄
    # 안 한 줄만 보기
    dub.btn_todo.setChecked(True)
    assert dub.table.isRowHidden(0) and not dub.table.isRowHidden(1)
    dub.btn_todo.setChecked(False)
    assert not dub.table.isRowHidden(0)


def test_start_steps_and_next_recording(win):
    w, _ = win
    ins = w.inspector
    w.model.select("")
    st = {k: s.state for k, s in ins.empty.steps.items()}
    assert st == {"media": "done", "sub": "done", "rec": "current", "bgm": "todo", "export": "todo"}
    assert "2줄 중 1줄" in ins.empty.steps["rec"].detail.text() and ins.empty.btn_rec.text() == "● 이어서 녹음"
    ins.empty.btn_rec.click()
    assert w.right.currentWidget() is w.dub_scroll
    assert w.model.selection.kind == "subtitle" and w.model.selection.index == 2  # 녹음 안 한 첫 줄
    w.right.setCurrentWidget(ins)
    ins.empty.btn_export.click()
    assert w.right.currentWidget() is w.output_scroll


def test_export_summary_warns_unrecorded(win):
    w, _ = win
    out = w.inspector.output_page
    assert "목소리 1줄 / 2줄" in out.summary.text() and "녹음 안 한 줄" in out.summary.text()
    assert w.output_scroll.footer is out.footer and out.footer.isAncestorOf(out.btn_export)


def test_effects_tab_holds_auto_preview(win):
    w, _ = win
    w.auto.enabled = True
    w.auto.rendered_key = w._render_revision_key()
    w.right.setCurrentWidget(w.effects_scroll)
    assert w.auto.hold
    w.model.select("clip", 0)
    w.effects._edit("brightness", lambda c: setattr(c, "brightness", 0.2), None)
    assert not w.auto.timer.isActive() and not w.auto.busy
    w.right.setCurrentWidget(w.inspector)  # 탭을 떠나면 그때 예약
    assert not w.auto.hold and w.auto.timer.isActive()
    w.auto.enabled = False
    w.auto.timer.stop()


def test_auto_preview_hold_unit():
    QApplication.instance() or QApplication([])
    from composer.ui.auto_preview import AutoPreview
    key = {"k": 1}
    started = []
    a = AutoPreview(lambda: key["k"], started.append, lambda: None, delay_ms=20)
    a.set_hold(True)
    key["k"] = 2
    a.notify_changed()
    assert not a.timer.isActive()
    a.set_hold(False)
    assert a.timer.isActive()
    t0 = time.monotonic()
    while not started and time.monotonic() - t0 < 2:
        QCoreApplication.processEvents()
    assert started == [2]


def test_effect_images_use_small_source(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    import composer.ui.effects_panel as ep
    from composer.effects import DEFAULTS
    src = tmp_path / "big.png"
    QImage(2400, 1600, QImage.Format_RGB32).save(str(src))
    small = ep.small_source(src, 0.0, False, cache=tmp_path / "c")
    assert small is not None and small.parent.name == "fxsrc"
    im = QImage(str(small))
    assert max(im.width(), im.height()) == ep.SMALL_MAX and im.width() / im.height() == pytest.approx(1.5, abs=0.01)
    calls = []
    real = ep._run_ffmpeg
    monkeypatch.setattr(ep, "_run_ffmpeg", lambda args: (calls.append(args), real(args))[1])
    out = ep.effect_preview(src, 0.0, False, {**DEFAULTS, "brightness": 0.2}, (480, 270), cache=tmp_path / "c")
    assert out and out.exists()
    assert calls and str(small) in calls[-1]  # 큰 원본 대신 작은 그림으로
    assert str(src) not in calls[-1]
