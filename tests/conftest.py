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
def _quiet_settings():
    """대부분의 UI 테스트는 결정적으로 돌도록 자동 미리보기를 끄고 최근 파일 목록을 비웁니다.
    (자동 미리보기는 tests/test_auto_preview.py 에서 따로 켜서 확인)"""
    from PySide6.QtCore import QSettings
    s = QSettings("knhanul", "VideoComposer")
    s.setValue("view/auto_preview", False)
    s.setValue("file/recent", [])
    s.sync()
    yield
