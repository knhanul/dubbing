"""자동 미리보기: 편집이 멈추고 잠시 뒤(기본 1.5초) 백그라운드에서 미리보기를 다시 만듭니다.

* 영상/소리에 영향을 주는 값(key)이 바뀐 경우에만 다시 만듭니다(자막·더빙 위치는 화면에서 바로 반영되므로 제외).
* 만드는 중에 또 편집하면 진행 중인 렌더를 취소하고 끝나는 대로 새로 시작합니다.
* 실제 렌더 실행(start/cancel)은 주입받으므로 단위 테스트에서 가짜로 바꿀 수 있습니다.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal


class AutoPreview(QObject):
    state_changed = Signal(str)  # idle | waiting | rendering

    def __init__(self, key_fn: Callable[[], object], start_fn: Callable[[object], None],
                 cancel_fn: Callable[[], None], delay_ms: int = 1500, parent=None):
        super().__init__(parent)
        self.key_fn, self.start_fn, self.cancel_fn = key_fn, start_fn, cancel_fn
        self.enabled = True
        self.rendered_key: Optional[object] = None  # 지금 화면에 있는 미리보기의 key
        self.inflight_key: Optional[object] = None  # 만드는 중인 key
        self.pending = False  # 진행 중 렌더가 끝나면 다시 시작해야 함
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay_ms)
        self.timer.timeout.connect(lambda: self.request())

    @property
    def rendering(self) -> bool:
        return self.inflight_key is not None

    @property
    def busy(self) -> bool:
        return self.rendering or self.timer.isActive() or self.pending

    @property
    def up_to_date(self) -> bool:
        return self.rendered_key is not None and self.rendered_key == self.key_fn()

    def state(self) -> str:
        return "rendering" if self.rendering or self.pending else "waiting" if self.timer.isActive() else "idle"

    def notify_changed(self) -> None:
        """프로젝트가 바뀔 때마다 호출. 영상에 영향이 있으면 디바운스 후 렌더."""
        if not self.enabled:
            return
        k = self.key_fn()
        if k == self.rendered_key and not self.rendering:
            self.timer.stop()
            return
        if self.rendering and k == self.inflight_key and not self.pending:
            return
        self.timer.start()
        self.state_changed.emit(self.state())

    def request(self, force: bool = False) -> None:
        """지금 바로 렌더 (진행 중이면 취소 후 다시)."""
        self.timer.stop()
        k = self.key_fn()
        if self.rendering:
            if k == self.inflight_key and not force:
                return
            self.pending = True
            self.cancel_fn()
            self.state_changed.emit(self.state())
            return
        if not force and k == self.rendered_key:
            self.state_changed.emit(self.state())
            return
        self.inflight_key = k
        self.state_changed.emit(self.state())
        self.start_fn(k)

    def on_finished(self, key, ok: bool) -> None:
        """렌더가 끝나거나(ok) 실패/취소되었을 때 호출."""
        if key != self.inflight_key:
            return
        self.inflight_key = None
        if ok:
            self.rendered_key = key
        if self.pending:
            self.pending = False
            self.request(force=True)
        elif ok and self.enabled and self.key_fn() != self.rendered_key:
            self.timer.start()
        self.state_changed.emit(self.state())

    def reset(self) -> None:
        """다른 프로젝트를 열었을 때."""
        self.timer.stop()
        self.rendered_key = None
        if self.rendering:
            self.pending = self.enabled
            self.cancel_fn()
