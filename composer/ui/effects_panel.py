"""'✨ 효과' 탭 (프리미어 '효과 컨트롤'처럼): 고른 클립의 색 필터 · 밝기/대비/채도 · 분위기 · 움직임.

* 색 필터는 고른 클립의 한 장면에 필터를 입힌 작은 그림 카드로 보여 줍니다(ffmpeg 한 번, 캐시).
* 모든 수정은 되돌리기(Ctrl+Z) 가능, 클립 트랙이 잠겨 있으면 막힙니다.
* 미리보기: 효과를 바꾸면 자동 미리보기(360p)가 1.5초 뒤 다시 만들어져 모든 효과가 보입니다.
"""
from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QToolButton, QVBoxLayout, QWidget)

from ..effects import ADJUST, DEFAULTS, LOOK_LABELS, LOOKS, MOOD, clear_effects, copy_effects, look_preview_graph
from ..schema import MOTION_LABELS, MOTIONS
from .model import ProjectModel
from .widgets import Collapsible, ValueSlider

THUMB_W, THUMB_H = 96, 54
PREVIEW_NOTE = ("미리보기: 효과를 바꾸면 1.5초 뒤 미리보기 영상이 자동으로 다시 만들어져 모든 효과가 보여요 "
                "(미리보기를 만들기 전 그림에는 효과가 없어요). 내보내기에는 모두 적용돼요.")


def look_thumbnails(source: Path, at: float, is_video: bool, cache: Optional[Path] = None) -> dict[str, Path]:
    """한 장면에 색 필터를 하나씩 입힌 작은 그림들 {룩: png}. ffmpeg 한 번, 같은 파일·시각이면 캐시."""
    from ..probe import ffmpeg_path, popen_kwargs
    from ..thumbs import thumb_cache_dir
    source = Path(source)
    try:
        st = source.stat()
    except OSError:
        return {}
    key = f"{source.resolve()}|{st.st_size}|{int(st.st_mtime)}|{at:.2f}|{THUMB_W}x{THUMB_H}|v1"
    folder = (cache or thumb_cache_dir()) / "looks" / hashlib.sha1(key.encode()).hexdigest()[:16]
    outs = {lk: folder / f"{lk}.png" for lk in LOOKS}
    if all(p.exists() and p.stat().st_size > 0 for p in outs.values()):
        return outs
    folder.mkdir(parents=True, exist_ok=True)
    graph, labels = look_preview_graph(LOOKS, THUMB_W, THUMB_H)
    args = [ffmpeg_path(), "-v", "error", "-y"]
    if is_video and at > 0:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", str(source), "-filter_complex", graph]
    for lk, lab in zip(LOOKS, labels):
        args += ["-map", lab, "-frames:v", "1", str(outs[lk])]
    try:
        subprocess.run(args, timeout=30, capture_output=True, **popen_kwargs())
    except Exception:  # noqa: BLE001
        return {}
    return {lk: p for lk, p in outs.items() if p.exists() and p.stat().st_size > 0}


class EffectsPanel(QWidget):
    thumbs_ready = Signal(str, object)  # (그림 키, {룩: 경로}) — 일하는 스레드에서 화면 쪽으로

    def __init__(self, model: ProjectModel, parent=None):
        super().__init__(parent)
        self.model = model
        self._loading = False
        self.sync_thumbs = False  # 테스트: 그림 카드를 바로(같은 스레드에서) 만들기
        self._thumb_key = None
        self.thumb_paths: dict[str, Path] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:13px;font-weight:bold;")
        self.hint = QLabel("타임라인에서 클립을 선택하세요")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#9aa0a6;")
        self.lock_banner = QLabel("🔒 클립 트랙이 잠겨 있습니다 — 타임라인 왼쪽 트랙 머리의 🔒 를 눌러 풀면 고칠 수 있습니다.")
        self.lock_banner.setWordWrap(True)
        self.lock_banner.setStyleSheet("background:#3b4250; color:#e8eaed; padding:6px; border-radius:4px;")
        lay.addWidget(self.head)
        lay.addWidget(self.hint)
        lay.addWidget(self.lock_banner)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.body)

        # --- 색 필터 카드 ---
        lab = QLabel("색 필터")
        lab.setStyleSheet("font-weight:bold;")
        bl.addWidget(lab)
        grid = QGridLayout()
        grid.setSpacing(4)
        self.look_group = QButtonGroup(self)
        self.look_group.setExclusive(True)
        self.look_cards: dict[str, QToolButton] = {}
        for i, lk in enumerate(LOOKS):
            b = QToolButton()
            b.setCheckable(True)
            b.setText(LOOK_LABELS[lk])
            b.setToolTip(f"색 필터: {LOOK_LABELS[lk]}")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(THUMB_W, THUMB_H))
            b.setMinimumSize(THUMB_W + 10, 40)
            b.setStyleSheet("QToolButton{border:1px solid #474b55;border-radius:4px;padding:2px}"
                            "QToolButton:checked{border:2px solid #ffd54f;background:#3a3d45}")
            self.look_group.addButton(b)
            self.look_cards[lk] = b
            grid.addWidget(b, i // 4, i % 4)
            b.clicked.connect(lambda _=False, v=lk: self._edit("look", lambda c, v=v: setattr(c, "look", v), None))
        bl.addLayout(grid)

        # --- 밝기·대비·채도 / 분위기 ---
        self.sliders: dict[str, ValueSlider] = {}
        self.resets: dict[str, QToolButton] = {}
        self.sec_adjust = Collapsible("밝기 · 대비 · 채도", key="fx_adjust", default_open=True)
        self.sec_mood = Collapsible("분위기 — 비네팅 · 흐림 · 필름 노이즈", key="fx_mood", default_open=True)
        for sec, rows in ((self.sec_adjust, ADJUST), (self.sec_mood, MOOD)):
            for name, d, lo, hi, label in rows:
                vs = ValueSlider(lo, hi, 0.01, "{:+.2f}" if name == "brightness" else "{:.2f}")
                rb = QToolButton()
                rb.setText("↺")
                rb.setToolTip(f"{label} 기본값으로 ({d:g})")
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(vs, 1)
                row.addWidget(rb)
                wrap = QWidget()
                wrap.setLayout(row)
                sec.addRow(label, wrap)
                self.sliders[name] = vs
                self.resets[name] = rb
                vs.valueChanged.connect(lambda v, n=name: self._edit(n, lambda c, n=n, v=v: setattr(c, n, round(v, 3)), "fx_" + n))
                rb.clicked.connect(lambda _=False, n=name, d=d: self._edit(n, lambda c, n=n, d=d: setattr(c, n, d), None))
            bl.addWidget(sec)

        # --- 움직임 (🛠 속성에서 옮겨 옴, 영상에도) ---
        self.sec_motion = Collapsible("움직임 — 확대 · 이동 (이미지·영상)", key="fx_motion", default_open=True)
        self.motion = QComboBox()
        for m in MOTIONS:
            self.motion.addItem(MOTION_LABELS[m], m)
        self.strength_slider = ValueSlider(0, 1, 0.01, "{:.2f}")
        self.easing = QComboBox()
        self.easing.addItem("부드럽게(가속/감속)", "ease")
        self.easing.addItem("일정한 속도", "linear")
        self.sec_motion.addRow("움직임", self.motion)
        self.sec_motion.addRow("움직임 강도", self.strength_slider)
        self.sec_motion.addRow("속도감", self.easing)
        bl.addWidget(self.sec_motion)
        self.motion.currentIndexChanged.connect(
            lambda _: self._edit("motion", lambda c: setattr(c, "motion", self.motion.currentData()), None))
        self.strength_slider.valueChanged.connect(
            lambda v: self._edit("motion_strength", lambda c: setattr(c, "motion_strength", round(v, 3)), "fx_str"))
        self.easing.currentIndexChanged.connect(
            lambda _: self._edit("motion_easing", lambda c: setattr(c, "motion_easing", self.easing.currentData()), None))

        self.note = QLabel(PREVIEW_NOTE)
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#9aa0a6;font-size:11px;")
        bl.addWidget(self.note)
        btns = QHBoxLayout()
        self.btn_all = QPushButton("모든 클립에 적용")
        self.btn_all.setToolTip("이 클립의 색 필터·조정·분위기·움직임을 모든 클립에 똑같이 (Ctrl+Z 한 번으로 되돌리기)")
        self.btn_clear = QPushButton("효과 지우기")
        self.btn_clear.setToolTip("이 클립의 색 필터·조정·분위기를 기본값으로 (움직임은 그대로)")
        btns.addWidget(self.btn_all)
        btns.addWidget(self.btn_clear)
        bl.addLayout(btns)
        self.btn_all.clicked.connect(self.apply_to_all)
        self.btn_clear.clicked.connect(self.clear)
        lay.addStretch(1)

        self.thumbs_ready.connect(self._on_thumbs)
        model.selection_changed.connect(self.refresh)
        model.changed.connect(self.refresh)
        self.refresh()

    # --- 상태 ---
    @property
    def index(self) -> int:
        s = self.model.selection
        return s.index if s.kind == "clip" and 0 <= s.index < len(self.model.project.clips) else -1

    @property
    def clip(self):
        i = self.index
        return self.model.project.clips[i] if i >= 0 else None

    def locked(self) -> bool:
        return bool(self.model.project.tracks.clips.locked)

    def _edit(self, field: str, fn, merge_key: Optional[str]) -> bool:
        if self._loading:
            return False
        i = self.index
        if i < 0 or self.locked():
            self.refresh()
            return False
        self.model.edit(lambda p: fn(p.clips[i]), merge_key=merge_key)
        return True

    def apply_to_all(self) -> bool:
        src = self.clip
        if src is None or self.locked() or len(self.model.project.clips) < 2:
            return False
        i = self.index

        def fn(p):
            s = p.clips[i]
            for c in p.clips:
                if c is not s:
                    copy_effects(s, c)
        self.model.edit(fn)
        return True

    def clear(self) -> bool:
        if self.clip is None or self.locked():
            return False
        i = self.index
        self.model.edit(lambda p: clear_effects(p.clips[i]))
        return True

    # --- 화면 ---
    def refresh(self):
        c = self.clip
        self.body.setVisible(c is not None)
        self.hint.setVisible(c is None)
        if c is None:
            self.head.setText("✨ 효과")
            self.lock_banner.hide()
            return
        self._loading = True
        lk = self.locked()
        self.lock_banner.setVisible(lk)
        self.body.setEnabled(not lk)
        kind = "이미지" if c.type == "image" else "동영상"
        self.head.setText(f"✨ {self.index + 1}번 클립 · {c.label or c.file} ({kind})")
        for name, card in self.look_cards.items():
            card.setChecked((c.look or "none") == name)
        for name, vs in self.sliders.items():
            v = getattr(c, name)
            vs.setValue(v)
            self.resets[name].setEnabled(abs(float(v) - DEFAULTS[name]) > 1e-4)
        self.motion.setCurrentIndex(max(0, self.motion.findData(c.motion)))
        self.strength_slider.setValue(c.motion_strength)
        self.strength_slider.setEnabled(c.motion != "none")
        self.easing.setCurrentIndex(max(0, self.easing.findData(c.motion_easing)))
        self.easing.setEnabled(c.motion != "none")
        self.btn_all.setEnabled(len(self.model.project.clips) > 1)
        self._loading = False
        self._request_thumbs(c)

    def _thumb_source(self, c) -> tuple[Optional[Path], float]:
        path = self.model.project.resolve(c.file)
        if not path or not path.exists():
            return None, 0.0
        if c.type == "image":
            return path, 0.0
        i = self.index
        tm = self.model.timeline.clips[i] if 0 <= i < len(self.model.timeline.clips) else None
        at = (tm.source_in + min(1.0, (tm.source_out - tm.source_in) / 2)) if tm else 0.0
        return path, max(0.0, at)

    def _request_thumbs(self, c):
        path, at = self._thumb_source(c)
        if path is None:
            self._set_icons({})
            return
        key = f"{path}|{at:.2f}"
        if key == self._thumb_key:
            return
        self._thumb_key = key
        self._set_icons({})  # 만드는 동안은 글자 카드
        is_video = c.type == "video"
        if self.sync_thumbs:
            self._on_thumbs(key, look_thumbnails(path, at, is_video))
            return
        def work():
            res = look_thumbnails(path, at, is_video)
            try:
                self.thumbs_ready.emit(key, res)
            except RuntimeError:  # 그 사이 창이 닫힘
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_thumbs(self, key: str, paths):
        if key != self._thumb_key:
            return  # 그 사이 다른 클립을 골랐음
        self._set_icons(paths or {})

    def _set_icons(self, paths: dict):
        self.thumb_paths = dict(paths)
        for lk, card in self.look_cards.items():
            p = paths.get(lk)
            pm = QPixmap(str(p)) if p else QPixmap()
            card.setIcon(QIcon(pm) if not pm.isNull() else QIcon())
            card.setMinimumSize(THUMB_W + 10, (THUMB_H + 26) if not pm.isNull() else 40)
