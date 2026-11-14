"""영상 크기 목록 (출력 설정 탭과 영상 내보내기 창이 함께 씀)."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox

CUSTOM_LABEL = "직접 입력 (아래 가로×세로)"
SIZE_PRESETS: list[tuple[str, int, int]] = [
    ("1920×1080 가로 (유튜브)", 1920, 1080),
    ("1080×1920 세로 (쇼츠·릴스·틱톡)", 1080, 1920),
    ("1280×720 가로 (HD · 가볍게)", 1280, 720),
    ("720×1280 세로 (가볍게)", 720, 1280),
    ("1080×1080 정사각 (인스타 피드)", 1080, 1080),
    ("1080×1350 세로 4:5 (인스타 피드)", 1080, 1350),
    ("2560×1440 가로 (QHD)", 2560, 1440),
    ("3840×2160 가로 (4K 유튜브)", 3840, 2160),
]


def size_key(w: int, h: int) -> str:
    return f"{int(w)}x{int(h)}"


def combo_size(cb: QComboBox, i: int | None = None) -> tuple[int, int]:
    """항목의 (가로, 세로). '직접 입력'은 (0, 0)."""
    d = cb.itemData(cb.currentIndex() if i is None else i) or "0x0"
    w, h = str(d).split("x")
    return int(w), int(h)


def fill_size_combo(cb: QComboBox) -> None:
    cb.clear()
    for label, w, h in SIZE_PRESETS:
        cb.addItem(label, size_key(w, h))
    cb.addItem(CUSTOM_LABEL, "0x0")


def sync_size_combo(cb: QComboBox, w: int, h: int) -> None:
    """현재 가로×세로에 맞는 항목을 고름(없으면 '직접 입력'). 신호는 막음."""
    i = cb.findData(size_key(w, h))
    old = cb.blockSignals(True)
    cb.setCurrentIndex(i if i >= 0 else cb.count() - 1)
    cb.blockSignals(old)


def size_label(w: int, h: int) -> str:
    for label, pw, ph in SIZE_PRESETS:
        if (pw, ph) == (w, h):
            return label
    return f"{w}×{h}"
