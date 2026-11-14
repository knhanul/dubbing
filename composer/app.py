"""프로그램 시작점."""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME
    from .ui.main_window import MainWindow

    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    _install_crash_handler()
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    _dark(app)
    win = MainWindow()
    win.show()
    if len(argv) > 1 and Path(argv[1]).suffix.lower() == ".json" and Path(argv[1]).exists():
        win.load_path(Path(argv[1]))
    code = app.exec()
    # 창을 QApplication 보다 먼저 확실히 정리 (PySide6 멀티미디어 종료 순서 문제 방지)
    win.shutdown()
    win.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    del win
    return code


def _install_crash_handler():
    """예상치 못한 오류를 창으로 보여주고 로그 파일에 남깁니다(pythonw/exe 는 콘솔이 없으므로)."""
    import traceback

    log = Path.home() / "VideoComposer_error.log"

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            log.write_text(text, encoding="utf-8")
        except OSError:
            pass
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, "오류", f"예상치 못한 오류가 났습니다.\n{value}\n\n자세한 내용: {log}")
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(etype, value, tb)

    sys.excepthook = hook


def _dark(app):
    from PySide6.QtGui import QColor, QPalette

    p = QPalette()
    base, alt, text = QColor("#202227"), QColor("#2b2d33"), QColor("#e6e6e6")
    p.setColor(QPalette.Window, alt)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, alt)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.ToolTipBase, base)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Highlight, QColor("#3d6fb6"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor("#8a8f99"))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b6f78"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b6f78"))
    app.setPalette(p)


if __name__ == "__main__":
    raise SystemExit(main())
