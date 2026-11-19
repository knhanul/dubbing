"""미세 조정용 공용 위젯."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QToolButton, QWidget


class FineSpin(QDoubleSpinBox):
    """휠/화살표 = 큰 단계, Shift 를 누르고 있으면 작은 단계 (0.1초 / 0.01초)."""

    def __init__(self, big: float = 0.1, small: float = 0.01, parent=None):
        super().__init__(parent)
        self.big, self.small = big, small
        self.setSingleStep(big)

    def stepBy(self, steps: int) -> None:  # noqa: N802
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        st = self.small if QApplication.keyboardModifiers() & Qt.ShiftModifier else self.big
        self.setValue(round(self.value() + steps * st, self.decimals()))


class NudgeSpin(QWidget):
    """숫자 입력 한 칸. 휠/화살표 = ±0.1, Shift+휠/화살표 = ±0.01, 직접 입력은 0.001 단위까지.
    buttons=True 면 예전처럼 [-0.1][-0.01] … [+0.01][+0.1] 버튼도 붙입니다(좁은 패널에선 기본으로 뺌)."""

    valueChanged = Signal(float)

    def __init__(self, minimum=-36000.0, maximum=36000.0, decimals=3, steps=(0.1, 0.01), suffix=" 초",
                 parent=None, buttons: bool = False):
        super().__init__(parent)
        big, small = max(steps), min(steps)
        self.spin = FineSpin(big, small)
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(suffix)
        self.spin.setKeyboardTracking(False)
        self.spin.setToolTip(f"휠/화살표 = ±{big:g} · Shift 누르고 = ±{small:g} · 직접 입력 가능")
        self.spin.setMinimumWidth(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        if buttons:
            for st in sorted(steps, reverse=True):
                b = QToolButton(text=f"-{st:g}")
                b.setAutoRepeat(True)
                b.clicked.connect(lambda _=False, d=-st: self._bump(d))
                lay.addWidget(b)
        lay.addWidget(self.spin, 1)
        if buttons:
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
        self.head.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)  # 제목이 길어도 패널 폭을 밀지 않게
        self.head.setMinimumWidth(0)
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

    def __init__(self, lo: float, hi: float, step: float = 0.01, fmt: str = "{:.2f}", parent=None,
                 live: bool = True):
        """live=False: 끄는 동안에는 값 글자만 바뀌고, 손을 뗄 때 한 번만 valueChanged (느린 PC 에서 버벅임 방지).
        키보드·휠·막대 클릭은 그대로 바로 반영됩니다."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel, QSlider

        super().__init__(parent)
        self.step = step
        self.fmt = fmt
        self.live = live
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(round(lo / step)), int(round(hi / step)))
        self.slider.setTracking(live)
        self.label = QLabel()
        self.label.setMinimumWidth(40)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._changed)
        if not live:
            self.slider.sliderMoved.connect(lambda iv: self.label.setText(self.fmt.format(round(iv * self.step, 6))))
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


# ------------------------------------------------------------------ 오른쪽 패널(최소 440px)용 촘촘한 배치 부품
# 규칙: 짧은 칸은 한 줄에 2~3개(field + hrow), 2~3개 중 고르기는 버튼 묶음(Segmented), 섹션은 작은 제목 + 가는 줄
# (Section), 탭의 주 버튼은 바닥 고정 줄(Footer), 패널이 720px 이상이면 섹션을 2단으로(Columns).
PANEL_MIN_W = 440
TWO_COL_W = 720
LABEL_W = 72
DIM = "color:#9aa0a6;"
LINK_CSS = ("QToolButton{border:none;background:transparent;color:#8ab4f8;padding:0 2px}"
            "QToolButton:hover{text-decoration:underline}QToolButton:disabled{color:#5f6368}")
ICON_CSS = ("QToolButton{border:1px solid #474b55;border-radius:4px;background:#30333b;color:#e8eaed;padding:0}"
            "QToolButton:hover{border-color:#8ab4f8}QToolButton:disabled{color:#5f6368;border-color:#3a3d45}")


def field(label: str, widget: QWidget, label_w: int = LABEL_W, tip: str = "") -> QWidget:
    """'이름 | 칸' 한 덩어리. 이름 너비는 고정(줄끼리 칸이 나란히)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    lab = QLabel(label)
    lab.setStyleSheet("color:#b8bcc4;")
    lab.setFixedWidth(label_w)
    lab.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    if tip:
        lab.setToolTip(tip)
        widget.setToolTip(widget.toolTip() or tip)
    lay.addWidget(lab)
    lay.addWidget(widget, 1)
    w.label = lab
    w.widget = widget
    return w


def hrow(*widgets, stretches=None, spacing: int = 10) -> QWidget:
    """한 줄에 여러 칸 (기본은 같은 비율). None 은 빈 공간."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for i, x in enumerate(widgets):
        s = 1 if stretches is None else stretches[i]
        if x is None:
            lay.addStretch(s)
        else:
            lay.addWidget(x, s)
    return w


def shrink(*widgets, chars: int = 6) -> None:
    """콤보 상자 등이 긴 항목 글자 때문에 패널 폭을 밀어내지 않게 (목록을 펼치면 다 보임)."""
    from PySide6.QtWidgets import QComboBox, QLabel, QSizePolicy

    for w in widgets:
        if isinstance(w, QComboBox):
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(chars)
        elif isinstance(w, QLabel):
            w.setMinimumWidth(0)
            w.setSizePolicy(QSizePolicy.Ignored, w.sizePolicy().verticalPolicy())


def icon_button(text: str, tip: str = "", w: int = 28, h: int = 24) -> QToolButton:
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setFixedSize(w, h)
    b.setStyleSheet(ICON_CSS)
    return b


def link_button(text: str, tip: str = "") -> QToolButton:
    """글자만 있는 작은 단추 (섹션 제목 오른쪽 '모든 경계에 적용', '자세히 ▸' 등)."""
    from PySide6.QtCore import Qt

    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(LINK_CSS)
    return b


class Section(QWidget):
    """작은 제목 + 가는 줄 (+ 바뀐 값 노란 점, + 오른쪽 글자 단추) 아래에 줄들.
    테두리 상자(QGroupBox) 대신 써서 공간을 덜 씁니다."""

    def __init__(self, title: str = "", parent=None):
        from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        self.head = QWidget()
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color:#aab0ba;font-size:11px;font-weight:bold;")
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color:#ffd54f;font-size:9px;")
        self.dot.setToolTip("기본값에서 바꾼 값이 있어요")
        self.dot.hide()
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#3a3f49;background:#3a3f49;max-height:1px;")
        line.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        hl.addWidget(self.title_label)
        hl.addWidget(self.dot)
        hl.addWidget(line, 1)
        self._head_lay = hl
        self.head.setVisible(bool(title))
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(5)
        outer.addWidget(self.head)
        outer.addLayout(self.body)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return False  # Columns 와 같은 이유 (줄이 찌그러지지 않게)

    def sizeHint(self):  # noqa: N802
        return super().sizeHint().expandedTo(self.minimumSizeHint())

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.head.setVisible(bool(title))

    def title(self) -> str:
        return self.title_label.text()

    def set_dirty(self, on: bool) -> None:
        self.dot.setVisible(bool(on))

    def add_link(self, text: str, slot=None, tip: str = "") -> QToolButton:
        b = link_button(text, tip)
        if slot is not None:
            b.clicked.connect(slot)
        self._head_lay.addWidget(b)
        self.head.show()
        return b

    def add(self, w):
        from PySide6.QtWidgets import QLayout

        if isinstance(w, QLayout):
            self.body.addLayout(w)
        else:
            self.body.addWidget(w)
        return w

    def row(self, *widgets, stretches=None) -> QWidget:
        return self.add(hrow(*widgets, stretches=stretches))


class Segmented(QWidget):
    """2~4개 중 하나 고르기를 한 줄짜리 버튼 묶음으로 (드롭다운보다 한눈에 보임)."""

    changed = Signal(object)

    CSS = ("QPushButton{border:1px solid #474b55;background:#2b2e36;color:#b8bcc4;padding:3px 6px;border-radius:0}"
           "QPushButton:checked{background:#2f4a6e;color:#fff;border-color:#5b8fd8;font-weight:bold}"
           "QPushButton:hover:!checked{background:#343842}"
           "QPushButton:disabled{color:#5f6368}")

    def __init__(self, items, height: int = 26, parent=None):
        from PySide6.QtWidgets import QButtonGroup, QPushButton, QSizePolicy

        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        self.values = []
        for k, it in enumerate(items):
            value, text = it[0], it[1]
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFixedHeight(height)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setStyleSheet(self.CSS)
            if len(it) > 2 and it[2]:
                b.setToolTip(it[2])
            self.group.addButton(b, k)
            self.buttons.append(b)
            self.values.append(value)
            lay.addWidget(b, 1)
        self.group.idClicked.connect(lambda k: self.changed.emit(self.values[k]))

    def set_value(self, value) -> None:
        if value not in self.values:  # 해당 없음 → 모두 끔
            self.group.setExclusive(False)
            for b in self.buttons:
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
            self.group.setExclusive(True)
            return
        for b, v in zip(self.buttons, self.values):
            b.blockSignals(True)
            b.setChecked(v == value)
            b.blockSignals(False)

    def value(self):
        k = self.group.checkedId()
        return self.values[k] if k >= 0 else None

    def button(self, value):
        return self.buttons[self.values.index(value)]


class Columns(QWidget):
    """섹션 목록을 패널 폭에 따라 1단(좁을 때) 또는 2단(720px 이상)으로 배치.
    add(w, col): col 0 = 왼쪽(1단일 때 위), 1 = 오른쪽(1단일 때 그 아래)."""

    def __init__(self, two_col_w: int = TWO_COL_W, spacing: int = 12, parent=None):
        from PySide6.QtWidgets import QVBoxLayout

        super().__init__(parent)
        self.two_col_w = two_col_w
        self.items: list = []
        self.cols = 0
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(16)
        self.left = QVBoxLayout()
        self.right = QVBoxLayout()
        for c in (self.left, self.right):
            c.setContentsMargins(0, 0, 0, 0)
            c.setSpacing(spacing)
            self._lay.addLayout(c, 1)
        self._arrange(1)

    def add(self, w: QWidget, col: int = 0) -> QWidget:
        self.items.append((w, col))
        self._arrange(self.cols, force=True)
        return w

    def is_two(self) -> bool:
        return self.cols == 2

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        # 줄바꿈 글(QLabel)이 섞이면 높이 계산이 최소 높이보다 작게 나와 줄들이 찌그러짐 → 최소/권장 높이로만
        return False

    def sizeHint(self):  # noqa: N802
        return super().sizeHint().expandedTo(self.minimumSizeHint())

    def _arrange(self, n: int, force: bool = False) -> None:
        if n == self.cols and not force:
            return
        self.cols = n
        for c in (self.left, self.right):
            while c.count():
                c.takeAt(0)
        for w, col in self.items:
            (self.right if n == 2 and col == 1 else self.left).addWidget(w)
        self.left.addStretch(1)
        if n == 2:
            self.right.addStretch(1)
        self._lay.setStretch(1, 1 if n == 2 else 0)
        self.updateGeometry()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._arrange(2 if e.size().width() >= self.two_col_w else 1)


class Footer(QWidget):
    """탭 바닥에 붙는 줄 (스크롤해도 늘 보이는 주 버튼)."""

    def __init__(self, parent=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QVBoxLayout

        super().__init__(parent)
        self.setObjectName("panelFooter")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#panelFooter{background:#20232a;border-top:1px solid #3a3f49}")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(8, 6, 8, 6)
        self.v.setSpacing(4)

    def add(self, w):
        from PySide6.QtWidgets import QLayout

        if isinstance(w, QLayout):
            self.v.addLayout(w)
        else:
            self.v.addWidget(w)
        return w


def scroll_page(content: QWidget, footer: QWidget = None, header: QWidget = None) -> QWidget:
    """탭 하나: [머리(고정)] + 스크롤되는 내용 + [바닥 줄(고정)]. 돌려준 위젯의 .scroll / .content 로 접근."""
    from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout

    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setWidget(content)
    if header is not None:
        lay.addWidget(header)
    lay.addWidget(sc, 1)
    if footer is not None:
        lay.addWidget(footer)
    page.scroll = sc
    page.content = content
    page.footer = footer
    page.widget = lambda: content  # QScrollArea.widget() 처럼
    return page
