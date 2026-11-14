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
