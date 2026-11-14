import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_configure(config):
    # QSettings 가 실제 사용자 설정을 건드리지 않도록 테스트 모드 사용
    try:
        from PySide6.QtCore import QStandardPaths

        QStandardPaths.setTestModeEnabled(True)
    except Exception:  # noqa: BLE001
        pass


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet_settings(tmp_path_factory, monkeypatch):
    """대부분의 UI 테스트는 결정적으로 돌도록 자동 미리보기를 끄고 최근 파일 목록을 비웁니다.
    (자동 미리보기는 tests/test_auto_preview.py 에서 따로 켜서 확인)"""
    from PySide6.QtCore import QSettings
    s = QSettings("knhanul", "VideoComposer")
    s.setValue("view/auto_preview", False)
    s.setValue("file/recent", [])
    s.setValue("import/skip_order_dialog", True)
    s.remove("ui/fold")  # 접기 칸은 기본(접힘)으로 시작
    s.remove("timeline")  # 🧲 자석 기본(켜짐)  # 순서 정하기 창은 tests/test_drop_order.py 에서 따로
    s.sync()
    # 내 자막 스타일(subtitle_presets.json)은 테스트마다 빈 임시 폴더에
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("localappdata")))
    yield
