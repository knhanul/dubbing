"""'순서 정하기' 창: 여러 이미지·영상을 넣을 때 클릭한 순서대로 번호를 매겨 그 순서로 넣습니다.

탐색기는 파일을 선택한 순서를 알려 주지 않으므로, 썸네일을 차례로 클릭해 1, 2, 3… 번호를 붙입니다.
* 다시 클릭하면 번호가 빠지고 나머지 번호가 당겨집니다.
* 번호가 없는 파일은 번호 붙은 파일 뒤에, 지금 화면 순서(처음엔 이름순)대로 들어갑니다.
* 썸네일을 끌어서 화면 순서를 바꿀 수도 있습니다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QLabel, QListView, QListWidget,
                               QListWidgetItem, QPushButton, QStyledItemDelegate, QVBoxLayout)

from ..thumbs import fmt_duration, is_video, media_duration, video_thumbnail

PATH_ROLE = Qt.UserRole
NUM_ROLE = Qt.UserRole + 1
DUR_ROLE = Qt.UserRole + 2
THUMB_W, THUMB_H = 176, 99


def _placeholder(video: bool) -> QPixmap:
    pm = QPixmap(THUMB_W, THUMB_H)
    pm.fill(QColor("#2b2f36"))
    p = QPainter(pm)
    p.setPen(QColor("#8a8f99"))
    f = QFont(); f.setPixelSize(28); p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "🎬" if video else "🖼")
    p.end()
    return pm


def _fit(img: QImage) -> QPixmap:
    """썸네일 칸(16:9)에 맞춰 가운데 정렬."""
    pm = QPixmap(THUMB_W, THUMB_H)
    pm.fill(QColor("#111"))
    if not img.isNull():
        sc = img.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        p = QPainter(pm)
        p.drawImage((THUMB_W - sc.width()) // 2, (THUMB_H - sc.height()) // 2, sc)
        p.end()
    return pm


def load_image_thumb(path: Path) -> QImage:
    r = QImageReader(str(path))
    r.setAutoTransform(True)
    sz = r.size()
    if sz.isValid() and sz.width() > THUMB_W * 2:
        r.setScaledSize(sz.scaled(THUMB_W * 2, THUMB_H * 2, Qt.KeepAspectRatio))
    return r.read()


class _BadgeDelegate(QStyledItemDelegate):
    """칸 하나를 직접 그림: 위에 썸네일(왼쪽 위 클릭 순서 번호, 오른쪽 아래 영상 길이), 아래에 파일 이름."""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(THUMB_W + 12, THUMB_H + 38)

    def paint(self, painter, option, index):
        from PySide6.QtWidgets import QStyle
        num = index.data(NUM_ROLE) or 0
        dur = index.data(DUR_ROLE) or ""
        r = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(r.adjusted(1, 1, -1, -1), QColor(255, 255, 255, 22))
        icon_rect = QRect(r.left() + (r.width() - THUMB_W) // 2, r.top() + 4, THUMB_W, THUMB_H)
        icon = index.data(Qt.DecorationRole)
        if icon is not None:
            icon.paint(painter, icon_rect)
        # 이름
        f = QFont(option.font); f.setPixelSize(12)
        painter.setFont(f)
        painter.setPen(QColor("#ffffff") if num else QColor("#c8ccd4"))
        name = painter.fontMetrics().elidedText(str(index.data(Qt.DisplayRole)), Qt.ElideMiddle, THUMB_W)
        painter.drawText(QRect(icon_rect.left(), icon_rect.bottom() + 4, THUMB_W, 20), Qt.AlignHCenter | Qt.AlignTop, name)
        if num:
            pen = painter.pen(); pen.setColor(QColor("#4dd2ff")); pen.setWidth(3); painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(icon_rect.adjusted(1, 1, -1, -1))
            bdg = QRect(icon_rect.left() + 5, icon_rect.top() + 5, 30, 30)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#1f8fff"))
            painter.drawEllipse(bdg)
            f2 = QFont(option.font); f2.setBold(True); f2.setPixelSize(15)
            painter.setFont(f2)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(bdg, Qt.AlignCenter, str(num))
        if dur:
            f3 = QFont(option.font); f3.setPixelSize(11)
            painter.setFont(f3)
            w = painter.fontMetrics().horizontalAdvance(dur) + 10
            d = QRect(icon_rect.right() - w - 3, icon_rect.bottom() - 19, w, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(d, 3, 3)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(d, Qt.AlignCenter, dur)
        painter.restore()


class OrderDialog(QDialog):
    def __init__(self, files: list[Path], dropped: Optional[list[Path]] = None, parent=None,
                 thumb_loader: Optional[Callable[[Path], QImage]] = None):
        super().__init__(parent)
        self.setWindowTitle("순서 정하기")
        self.resize(860, 600)
        self.name_order = [Path(f) for f in files]
        self.dropped_order = [Path(f) for f in (dropped or files)]
        self.picked: list[Path] = []
        self.result_order: Optional[list[Path]] = None
        self._thumb_loader = thumb_loader

        lay = QVBoxLayout(self)
        head = QLabel(f"<b>파일 {len(files)}개</b>를 넣을 순서대로 <b>썸네일을 차례로 클릭</b>하세요. "
                      "1, 2, 3… 번호가 붙습니다. 다시 클릭하면 번호가 빠집니다.<br>"
                      "<small>번호를 안 붙인 파일은 뒤에 화면 순서(처음엔 이름순)대로 들어갑니다. "
                      "썸네일을 끌어서 화면 순서를 바꿀 수도 있습니다.</small>")
        head.setWordWrap(True)
        lay.addWidget(head)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ListMode)  # ListMode + 줄바꿈 = 격자처럼 보이면서 끌어서 순서 바꾸기 가능
        self.grid.setFlow(QListView.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setIconSize(QSize(THUMB_W, THUMB_H))
        self.grid.setGridSize(QSize(THUMB_W + 16, THUMB_H + 42))
        self.grid.setSpacing(4)
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.ElideMiddle)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setDragDropMode(QAbstractItemView.InternalMove)
        self.grid.setDefaultDropAction(Qt.MoveAction)
        self.grid.setItemDelegate(_BadgeDelegate(self.grid))
        self.grid.itemClicked.connect(self.toggle)
        self.grid.model().rowsMoved.connect(lambda *a: self._update_summary())
        lay.addWidget(self.grid, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:#c8ccd4;")
        lay.addWidget(self.summary)

        self.skip_next = QCheckBox("다음부터 묻지 않고 이름순")
        self.skip_next.setToolTip("나중에 🎞 클립 목록 탭의 '새로 추가하는 이미지 기본값'에서 다시 켤 수 있습니다")
        lay.addWidget(self.skip_next)

        row = QHBoxLayout()
        self.b_name = QPushButton("이름순")
        self.b_name.setToolTip("번호와 상관없이 파일 이름 순서(1, 2, 10…)로 바로 넣습니다")
        self.b_dropped = QPushButton("놓은 순서 그대로")
        self.b_dropped.setToolTip("탐색기가 넘겨 준 순서 그대로 바로 넣습니다")
        self.b_reset = QPushButton("초기화")
        self.b_reset.setToolTip("번호를 모두 지우고 화면을 이름순으로 되돌립니다")
        self.b_cancel = QPushButton("취소")
        self.b_ok = QPushButton("✔ 이 순서로 넣기")
        self.b_ok.setDefault(True)
        self.b_ok.setStyleSheet("QPushButton{background:#1f8fff;color:white;font-weight:bold;padding:6px 14px;}")
        for b in (self.b_name, self.b_dropped, self.b_reset):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.b_cancel)
        row.addWidget(self.b_ok)
        lay.addLayout(row)
        self.b_ok.clicked.connect(lambda: self._finish(self.current_order()))
        self.b_name.clicked.connect(lambda: self._finish(list(self.name_order)))
        self.b_dropped.clicked.connect(lambda: self._finish(list(self.dropped_order)))
        self.b_reset.clicked.connect(self.reset)
        self.b_cancel.clicked.connect(self.reject)

        self._fill(self.name_order)
        self._start_thumbs()

    # ------------------------------------------------------------------ 목록
    def _fill(self, order: list[Path]):
        self.grid.clear()
        for f in order:
            it = QListWidgetItem(QIcon(_placeholder(is_video(f))), f.name)
            it.setData(PATH_ROLE, str(f))
            it.setToolTip(str(f))
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
            it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.grid.addItem(it)
        self._renumber()

    def items(self) -> list[QListWidgetItem]:
        return [self.grid.item(i) for i in range(self.grid.count())]

    def item_for(self, path: Path) -> Optional[QListWidgetItem]:
        for it in self.items():
            if Path(it.data(PATH_ROLE)) == Path(path):
                return it
        return None

    def screen_order(self) -> list[Path]:
        return [Path(it.data(PATH_ROLE)) for it in self.items()]

    def current_order(self) -> list[Path]:
        """번호 붙은 파일(클릭 순서) + 나머지(화면 순서)."""
        rest = [p for p in self.screen_order() if p not in self.picked]
        return list(self.picked) + rest

    def toggle(self, item: QListWidgetItem):
        p = Path(item.data(PATH_ROLE))
        if p in self.picked:
            self.picked.remove(p)
        else:
            self.picked.append(p)
        self._renumber()

    def reset(self):
        self.picked.clear()
        self._fill(self.name_order)
        self._apply_cached_thumbs()

    def _renumber(self):
        for it in self.items():
            p = Path(it.data(PATH_ROLE))
            it.setData(NUM_ROLE, self.picked.index(p) + 1 if p in self.picked else 0)
        self.grid.viewport().update()
        self._update_summary()

    def _update_summary(self):
        order = self.current_order()
        n = len(self.picked)
        shown = " → ".join(f"{i + 1}. {p.name}" for i, p in enumerate(order[:12]))
        if len(order) > 12:
            shown += f" … (모두 {len(order)}개)"
        state = f"번호 붙임 {n}개" + (f", 나머지 {len(order) - n}개는 뒤에" if n and n < len(order) else "")
        self.summary.setText(f"<b>넣을 순서</b> ({state if n else '아직 번호 없음 — 지금은 화면 순서'}): {shown}")

    def _finish(self, order: list[Path]):
        self.result_order = order
        self.accept()

    # ------------------------------------------------------------------ 썸네일 (백그라운드)
    def _start_thumbs(self):
        self._thumbs: dict[str, QImage] = {}
        self._durs: dict[str, str] = {}
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._futures = {str(p): self._pool.submit(self._load, p) for p in self.name_order}
        self._poll = QTimer(self)
        self._poll.setInterval(60)
        self._poll.timeout.connect(self._collect)
        self._poll.start()

    def _load(self, p: Path):
        if self._thumb_loader is not None:
            return self._thumb_loader(p), ""
        if is_video(p):
            jpg = video_thumbnail(p, THUMB_W * 2)
            img = QImage(str(jpg)) if jpg else QImage()
            return img, fmt_duration(media_duration(p))
        return load_image_thumb(p), ""

    def _collect(self):
        for key, fut in list(self._futures.items()):
            if fut.done():
                del self._futures[key]
                try:
                    img, dur = fut.result()
                except Exception:  # noqa: BLE001
                    img, dur = QImage(), ""
                self._thumbs[key] = img
                self._durs[key] = dur
                self._apply_thumb(key)
        if not self._futures:
            self._poll.stop()

    def _apply_thumb(self, key: str):
        it = self.item_for(Path(key))
        if it is None:
            return
        img = self._thumbs.get(key)
        if img is not None and not img.isNull():
            it.setIcon(QIcon(_fit(img)))
        it.setData(DUR_ROLE, self._durs.get(key, ""))

    def _apply_cached_thumbs(self):
        for key in list(self._thumbs):
            self._apply_thumb(key)

    def wait_thumbs(self, timeout: float = 20.0) -> bool:
        """(테스트용) 썸네일이 다 만들어질 때까지 기다림."""
        import time

        from PySide6.QtCore import QCoreApplication
        end = time.monotonic() + timeout
        while self._futures and time.monotonic() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        self._collect()
        return not self._futures

    def done(self, r):  # noqa: D401
        self._poll.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        super().done(r)
