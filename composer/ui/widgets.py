"""미세 조정용 공용 위젯."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QToolButton, QWidget


class NudgeSpin(QWidget):
    """숫자 입력 + [-0.1][-0.01][+0.01][+0.1] 버튼. 직접 입력은 0.001 단위까지 가능."""

    valueChanged = Signal(float)

    def __init__(self, minimum=-36000.0, maximum=36000.0, decimals=3, steps=(0.1, 0.01), suffix=" 초",
                 parent=None):
        super().__init__(parent)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(steps[0])
        self.spin.setSuffix(suffix)
        self.spin.setKeyboardTracking(False)
        self.spin.setToolTip("휠/화살표 = ±%g, 직접 입력 가능" % steps[0])
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for st in sorted(steps, reverse=True):
            b = QToolButton(text=f"-{st:g}")
            b.setAutoRepeat(True)
            b.clicked.connect(lambda _=False, d=-st: self._bump(d))
            lay.addWidget(b)
        lay.addWidget(self.spin, 1)
        for st in sorted(steps):
            b = QToolButton(text=f"+{st:g}")
            b.setAutoRepeat(True)
            b.clicked.connect(lambda _=False, d=st: self._bump(d))
            lay.addWidget(b)
        self.spin.valueChanged.connect(self.valueChanged.emit)

    def _bump(self, d: float):
        self.spin.setValue(round(self.spin.value() + d, self.spin.decimals()))

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(float(v))
        self.spin.blockSignals(False)


class Collapsible(QWidget):
    """'▸ 제목' 을 누르면 펼쳐지는 칸. key 를 주면 펼침 상태를 기억합니다(QSettings)."""

    toggled = Signal(bool)

    def __init__(self, title: str, key: str = "", default_open: bool = False, parent=None):
        from PySide6.QtCore import QSettings, Qt
        from PySide6.QtWidgets import QFormLayout, QFrame, QVBoxLayout

        super().__init__(parent)
        self.title = title
        self.summary = ""  # 접혀 있을 때 제목 뒤에 붙는 짧은 요약 ('색 필터 — 빈티지')
        self.key = key
        self._settings = QSettings("knhanul", "VideoComposer") if key else None
        self.head = QToolButton()
        self.head.setCheckable(True)
        self.head.setToolButtonStyle(Qt.ToolButtonTextOnly)
        from PySide6.QtWidgets import QSizePolicy
        self.head.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.head.setStyleSheet("QToolButton{text-align:left;padding:5px 8px;border:1px solid #474b55;"
                                "border-radius:4px;background:#30333b;color:#e8eaed;font-weight:bold}"
                                "QToolButton:hover{border-color:#8ab4f8}")
        self.body = QFrame()
        self.body.setObjectName("foldBody")
        self.form = QFormLayout(self.body)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setContentsMargins(8, 4, 2, 4)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)
        lay.addWidget(self.head)
        lay.addWidget(self.body)
        on = default_open
        if self._settings is not None:
            v = self._settings.value(f"ui/fold/{key}", None)
            if v is not None:
                on = str(v).lower() in ("true", "1")
        self.head.toggled.connect(self._toggled)
        self.set_open(on, remember=False)

    def _toggled(self, on: bool):
        self.set_open(on)

    def set_open(self, on: bool, remember: bool = True):
        self.head.blockSignals(True)
        self.head.setChecked(on)
        self.head.blockSignals(False)
        self._update_head()
        self.body.setVisible(on)
        if remember and self._settings is not None:
            self._settings.setValue(f"ui/fold/{self.key}", bool(on))
        self.toggled.emit(on)

    def _update_head(self):
        on = self.head.isChecked()
        extra = f" — {self.summary}" if self.summary and not on else ""
        self.head.setText(("▾ " if on else "▸ ") + self.title + extra)

    def set_summary(self, text: str):
        self.summary = text or ""
        self._update_head()

    def is_open(self) -> bool:
        return self.head.isChecked()

    def addRow(self, *args):  # noqa: N802
        self.form.addRow(*args)


class ValueSlider(QWidget):
    """슬라이더 + 값 글자. value 는 실수(scale 단위로 나눠 저장), 끌기를 놓을 때까지 한 번의 되돌리기."""

    valueChanged = Signal(float)

    def __init__(self, lo: float, hi: float, step: float = 0.01, fmt: str = "{:.2f}", parent=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel, QSlider

        super().__init__(parent)
        self.step = step
        self.fmt = fmt
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(round(lo / step)), int(round(hi / step)))
        self.label = QLabel()
        self.label.setMinimumWidth(56)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._changed)
        self._changed(self.slider.value(), emit=False)

    def _changed(self, iv: int, emit: bool = True):
        v = round(iv * self.step, 6)
        self.label.setText(self.fmt.format(v))
        if emit:
            self.valueChanged.emit(v)

    def value(self) -> float:
        return round(self.slider.value() * self.step, 6)

    def setValue(self, v: float) -> None:  # noqa: N802
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(float(v) / self.step)))
        self.slider.blockSignals(False)
        self._changed(self.slider.value(), emit=False)
