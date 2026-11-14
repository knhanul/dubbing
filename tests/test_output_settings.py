"""'⚙ 출력 설정' 탭(항상 보임), 영상 크기 목록, 미세 조정 탭 안내, 내보내기 창의 크기·fps 연동."""
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from composer.schema import Project


@pytest.fixture
def win(tmp_path):
    QApplication.instance() or QApplication([])
    import composer.ui.main_window as mw
    from tests.media_fixtures import make_image
    make_image(tmp_path / "a.png")
    w = mw.MainWindow()
    w.notify = lambda *a, **k: None
    w.set_project(Project.from_dict({"output": {"width": 1920, "height": 1080, "fps": 30},
                                     "clips": [{"file": "a.png", "duration": 2}, {"file": "a.png", "duration": 2}],
                                     "bgm": {"file": None}}, base_dir=tmp_path), None)
    yield w
    w.shutdown(); w.deleteLater(); QCoreApplication.processEvents()


def _tabs(w):
    return [w.right.tabText(i) for i in range(w.right.count())]


def test_settings_tab_always_visible_with_size_list(win):
    w = win
    assert "⚙ 출력 설정" in _tabs(w)
    page = w.inspector.project_page
    assert w.settings_scroll.widget() is page
    items = [page.preset.itemText(i) for i in range(page.preset.count())]
    assert "1920×1080 가로 (유튜브)" in items and "1080×1920 세로 (쇼츠·릴스·틱톡)" in items
    assert items[-1].startswith("직접 입력")
    assert page.preset.currentText() == "1920×1080 가로 (유튜브)"
    # 프로젝트 전체 설정이 모두 이 탭에
    for attr in ("w", "h", "fps", "fit", "tr_type", "tr_dur", "bgm_vol", "duck", "dub_vol", "latency", "sub_offset", "burn"):
        assert page.isAncestorOf(getattr(page, attr))
    # '크기 프리셋' 이라는 말은 없음
    from PySide6.QtWidgets import QLabel
    assert not [lab for lab in page.findChildren(QLabel) if "프리셋" in lab.text()]
    # 미세 조정 탭은 선택 항목만 — 프로젝트 설정 페이지가 그 안에 없음
    assert not w.inspector.isAncestorOf(page)


def test_size_preset_changes_project_and_triggers_auto_preview(win):
    w = win
    page = w.inspector.project_page
    w.auto.enabled = True
    w.auto.rendered_key = w._render_revision_key()  # 미리보기가 최신인 상태
    assert not w.auto.busy
    page.preset.setCurrentIndex(page.preset.findData("1080x1920"))
    o = w.model.project.output
    assert (o.width, o.height) == (1080, 1920)
    assert w.auto.state() == "waiting"  # 크기를 바꾸면 자동 미리보기 예약
    w.auto.enabled = False; w.auto.timer.stop()
    # 직접 입력하면 목록은 '직접 입력'
    page.w.setValue(1000)
    assert page.preset.currentText().startswith("직접 입력")
    page.w.setValue(1080)
    assert page.preset.currentText().startswith("1080×1920")
    w.model.undo(); w.model.undo(); w.model.undo()
    assert (w.model.project.output.width, w.model.project.output.height) == (1920, 1080)


def test_fine_tune_tab_hint_and_buttons(win):
    w = win
    ins = w.inspector
    w.model.select("none")
    assert ins.stack.currentWidget() is ins.empty
    from PySide6.QtWidgets import QLabel
    text = " ".join(lab.text() for lab in ins.empty.findChildren(QLabel))
    assert "⚙ 출력 설정" in text and "🅰 자막 스타일" in text
    ins.btn_settings.click()
    assert w.right.currentWidget() is w.settings_scroll
    ins.btn_style.click()
    assert w.right.currentWidget() is w.style_panel
    # 클립 고르면 미세 조정, 배경음악 고르면 출력 설정
    w.model.select("clip", 0)
    assert w.right.currentWidget() is w.inspector and ins.stack.currentWidget() is ins.clip_page
    w.model.select("bgm")
    assert w.right.currentWidget() is w.settings_scroll
    # 보기 메뉴
    view_menu = next(a.menu() for a in w.menuBar().actions() if a.text().startswith("보기"))
    act = next(a for a in view_menu.actions() if a.text().startswith("⚙ 출력 설정"))
    w.right.setCurrentIndex(0)
    act.trigger()
    assert w.right.currentWidget() is w.settings_scroll


def test_export_dialog_shows_and_syncs_size_fps(win, tmp_path):
    from composer.ui.render_dialog import FinalRenderOptions
    w = win
    seen = {}

    def fake_exec(opt):
        seen["combo"] = opt.size_combo.currentText()
        seen["vals"] = (opt.w.value(), opt.h.value(), opt.fps.value())
        opt.size_combo.setCurrentIndex(opt.size_combo.findData("1080x1920"))
        assert (opt.w.value(), opt.h.value()) == (1080, 1920)
        opt.fps.setValue(60)
        opt.path.setText(str(tmp_path / "x.mp4"))
        return False  # 크기만 확인하고 취소 → 프로젝트 그대로
    w.exec_export_options = fake_exec
    w.render_final()
    assert seen["combo"] == "1920×1080 가로 (유튜브)" and seen["vals"] == (1920, 1080, 30)
    assert w.model.project.output.width == 1920  # 취소하면 바뀌지 않음

    # 확인하면 프로젝트에 반영 (실행 취소 1회), 렌더는 가짜로
    started = {}

    class FakeDlg:
        def __init__(self, worker, title, parent):
            started["title"] = title
            started["size"] = (worker.project.output.width, worker.project.output.height, worker.project.output.fps)

        def exec_worker(self):
            return False
    import composer.ui.main_window as mw
    orig = mw.ProgressDialog
    mw.ProgressDialog = FakeDlg
    try:
        def ok_exec(opt):
            opt.w.setValue(1280); opt.h.setValue(720); opt.fps.setValue(25)
            assert opt.size_combo.currentText().startswith("1280×720")
            opt.path.setText(str(tmp_path / "x.mp4"))
            return True
        w.exec_export_options = ok_exec
        w.render_final()
    finally:
        mw.ProgressDialog = orig
    o = w.model.project.output
    assert (o.width, o.height, o.fps) == (1280, 720, 25)
    assert started == {"title": "영상 내보내기", "size": (1280, 720, 25)}
    assert w.inspector.project_page.preset.currentText().startswith("1280×720")  # 출력 설정 탭과 연동
    w.model.undo()
    assert (w.model.project.output.width, w.model.project.output.height) == (1920, 1080)
    # 창 단독
    d = FinalRenderOptions(tmp_path / "o.mp4", None, 1080, 1080, 24)
    assert d.size_combo.currentText() == "1080×1080 정사각 (인스타 피드)" and d.output_size() == (1080, 1080, 24.0)
    d.w.setValue(1081)
    assert d.output_size()[0] == 1080  # 짝수로
