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
