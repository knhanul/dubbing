"""'✨ 효과' 탭 (프리미어 '효과 컨트롤'처럼): 고른 클립의 움직임 · 색 필터 · 밝기·색 · 분위기.

느린 PC 를 위해: 슬라이더는 손을 뗄 때 한 번만 반영, 그림은 작은 원본(960px)으로 한 번에 하나씩(낮은 우선순위) 만들고,
이 탭에 있는 동안 미리보기 영상 다시 만들기는 미룹니다(main_window → AutoPreview.set_hold).

* 맨 위 **합쳐 보기**: 고른 클립의 한 장면에 지금 효과를 모두 입힌 큰 그림 — 내보내기와 같은 ffmpeg 필터
  (화면 맞춤 + 색 필터 + 밝기·대비·채도 + 흐림·비네팅·노이즈). 값을 바꾸면 0.3초 뒤 백그라운드에서 다시 만듦(캐시).
* 색 필터 카드도 '이 필터 + 지금 내 다른 조정'으로 보여 줍니다(ffmpeg 한 번, 캐시).
* 모든 수정은 바로 적용·되돌리기(Ctrl+Z) 가능, 클립 트랙이 잠겨 있으면 막힙니다.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ..effects import (ADJUST, DEFAULTS, EFFECT_FIELDS, LOOK_LABELS, LOOKS, MOOD, clear_effects, copy_effects,
                       effect_filters)
from ..schema import MOTION_LABELS, MOTIONS
from .model import ProjectModel
from .widgets import ValueSlider

THUMB_W, THUMB_H = 96, 54
PREVIEW_MAX_W, PREVIEW_MAX_H = 480, 270
DEBOUNCE_MS = 300
PREVIEW_SHOW_MAX_H = 150  # 탭 안에서 그림이 차지하는 최대 높이 (옵션이 첫 화면에 보이게)
PREVIEW_NOTE = ("위 그림은 지금 효과를 모두 합친 모습(내보내기와 같은 필터, 움직임은 빼고 한 장면)이에요. "
                "슬라이더는 손을 떼면 반영돼요. 미리보기 영상은 이 탭을 떠나거나 ▶ 재생하면 새로 만들어요.")


def effect_params(clip) -> dict:
    """그림 캐시 키에 쓰는 효과 값들(색 필터 + 조정 + 분위기)."""
    return {k: getattr(clip, k, DEFAULTS[k]) for k in EFFECT_FIELDS}


def _phash(d) -> str:
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _source_key(source: Path, at: float) -> Optional[str]:
    try:
        st = source.stat()
    except OSError:
        return None
    return f"{source.resolve()}|{st.st_size}|{int(st.st_mtime)}|{at:.2f}"


def _run_ffmpeg(args: list[str]) -> None:
    from ..probe import popen_kwargs
    try:
        subprocess.run(args, timeout=30, capture_output=True, **popen_kwargs(low_priority=True))
    except Exception:  # noqa: BLE001
        pass


SMALL_MAX = 960  # 효과 그림을 만들 때 쓰는 작은 원본 크기 (그림은 480×270 이하라 충분)


def small_source(source: Path, at: float, is_video: bool, cache: Optional[Path] = None) -> Optional[Path]:
    """원본(큰 사진·영상)의 한 장면을 가로·세로 960px 이하 PNG 로 한 번만 만들어 둠 (캐시).
    효과 그림은 이걸로 만들어서, 값을 바꿀 때마다 4000px 사진을 다시 읽거나 영상을 다시 찾아가지 않음."""
    from ..probe import ffmpeg_path
    from ..thumbs import thumb_cache_dir
    sk = _source_key(Path(source), at)
    if sk is None:
        return None
    out = (cache or thumb_cache_dir()) / "fxsrc" / f"{_phash({'src': sk, 'max': SMALL_MAX, 'v': 1})}.png"
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    args = [ffmpeg_path(), "-v", "error", "-y"]
    if is_video and at > 0:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", str(source), "-frames:v", "1", "-vf",
             f"scale=w='min({SMALL_MAX},iw)':h='min({SMALL_MAX},ih)':force_original_aspect_ratio=decrease,format=rgb24",
             str(tmp)]
    _run_ffmpeg(args)
    if tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(out)
        return out
    return None


def _input_args(source: Path, at: float, is_video: bool, cache: Optional[Path]) -> list[str]:
    """ffmpeg 입력: 작은 원본이 있으면 그것(한 장짜리 그림), 없으면 원래 파일."""
    small = small_source(source, at, is_video, cache)
    if small is not None:
        return ["-i", str(small)]
    return (["-ss", f"{at:.3f}"] if is_video and at > 0 else []) + ["-i", str(source)]


def preview_size(out_w: int, out_h: int) -> tuple[int, int]:
    """합쳐 보기 그림 크기 — 출력 화면 비율 그대로, 탭 폭·높이 안에 (짝수)."""
    out_w, out_h = max(2, int(out_w)), max(2, int(out_h))
    k = min(PREVIEW_MAX_W / out_w, PREVIEW_MAX_H / out_h)
    return max(2, int(out_w * k) // 2 * 2), max(2, int(out_h * k) // 2 * 2)


def effect_preview_path(source: Path, at: float, params: dict, size: tuple[int, int], fit: str, bg: str,
                        cache: Optional[Path] = None, anchor: str = "center") -> Optional[Path]:
    from ..thumbs import thumb_cache_dir
    sk = _source_key(Path(source), at)
    if sk is None:
        return None
    key = _phash({"src": sk, "p": params, "size": list(size), "fit": fit, "bg": bg, "anchor": anchor, "v": 1})
    return (cache or thumb_cache_dir()) / "fx" / f"{key}.png"


def effect_preview(source: Path, at: float, is_video: bool, params: dict, size: tuple[int, int],
                   fit: str = "cover", bg: str = "#000000", cache: Optional[Path] = None,
                   anchor: str = "center") -> Optional[Path]:
    """한 장면에 효과를 모두 입힌 그림 한 장 (내보내기와 같은 화면 맞춤 + effect_filters). 같은 값이면 캐시."""
    from ..probe import ffmpeg_path
    from ..render.ffmpeg_builder import _fit_filters
    out = effect_preview_path(source, at, params, size, fit, bg, cache, anchor)
    if out is None:
        return None
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    vf = _fit_filters(fit, w, h, bg, anchor=anchor) + effect_filters(SimpleNamespace(**params), h) + ["format=rgb24"]
    args = [ffmpeg_path(), "-v", "error", "-y"] + _input_args(Path(source), at, is_video, cache)
    tmp = out.with_suffix(".tmp.png")
    args += ["-frames:v", "1", "-vf", ",".join(vf), str(tmp)]
    _run_ffmpeg(args)
    if tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(out)  # 다 만든 뒤에 이름 바꾸기 (반쯤 쓴 파일을 캐시로 읽지 않게)
        return out
    return None


def look_cards_graph(params: dict, tw: int, th: int) -> tuple[str, list[str]]:
    """룩마다 '이 색 필터 + 지금 다른 조정(밝기·대비·채도·분위기)'을 입힌 작은 그림 (filter_complex, 출력 라벨)."""
    n = len(LOOKS)
    parts = [f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1,split={n}"
             + "".join(f"[s{i}]" for i in range(n))]
    labels = []
    for i, lk in enumerate(LOOKS):
        chain = ",".join(effect_filters(SimpleNamespace(**{**params, "look": lk}), th) or ["null"])
        parts.append(f"[s{i}]{chain},format=rgb24[o{i}]")
        labels.append(f"[o{i}]")
    return ";".join(parts), labels


def _looks_folder(source: Path, at: float, params: Optional[dict], cache: Optional[Path]):
    """카드 그림 폴더 + 카드에 함께 입힐 '다른 조정' 값 (색 필터만 빼고 모두)."""
    from ..thumbs import thumb_cache_dir
    sk = _source_key(Path(source), at)
    if sk is None:
        return None, None
    base = {k: DEFAULTS[k] for k in EFFECT_FIELDS}
    base.update({k: v for k, v in (params or {}).items() if k in base})
    base["look"] = "none"
    key = _phash({"src": sk, "size": [THUMB_W, THUMB_H], "others": base, "v": 2})
    return (cache or thumb_cache_dir()) / "looks" / key, base


def look_thumbs_cached(source: Path, at: float, params: Optional[dict] = None,
                       cache: Optional[Path] = None) -> Optional[dict]:
    """이미 만들어 둔 카드 그림이 있으면 바로 {룩: png} (없으면 None) — 화면 스레드에서 깜빡임 없이."""
    folder, _ = _looks_folder(source, at, params, cache)
    if folder is None:
        return None
    outs = {lk: folder / f"{lk}.png" for lk in LOOKS}
    return outs if all(p.exists() and p.stat().st_size > 0 for p in outs.values()) else None


def look_thumbnails(source: Path, at: float, is_video: bool, cache: Optional[Path] = None,
                    params: Optional[dict] = None) -> dict[str, Path]:
    """한 장면에 색 필터를 하나씩(+ 지금 다른 조정) 입힌 작은 그림들 {룩: png}. ffmpeg 한 번, 같은 값이면 캐시."""
    from ..probe import ffmpeg_path
    hit = look_thumbs_cached(source, at, params, cache)
    if hit:
        return hit
    folder, base = _looks_folder(source, at, params, cache)
    if folder is None:
        return {}
    folder.mkdir(parents=True, exist_ok=True)
    outs = {lk: folder / f"{lk}.png" for lk in LOOKS}
    graph, labels = look_cards_graph(base, THUMB_W, THUMB_H)
    args = [ffmpeg_path(), "-v", "error", "-y"] + _input_args(Path(source), at, is_video, cache)
    args += ["-filter_complex", graph]
    for lk, lab in zip(LOOKS, labels):
        args += ["-map", lab, "-frames:v", "1", str(outs[lk])]
    _run_ffmpeg(args)
    return {lk: p for lk, p in outs.items() if p.exists() and p.stat().st_size > 0}


class PreviewLabel(QLabel):
    """합쳐 보기 그림: 탭 폭에 맞춰 비율 그대로 줄여 보여 줌 (키우지는 않음)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pm = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_image(self, pm: QPixmap):
        self._pm = pm
        self._fit()

    def has_image(self) -> bool:
        return not self._pm.isNull()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        if self._pm.isNull():
            self.setPixmap(QPixmap())
            self.setFixedHeight(60)
            return
        w = max(40, min(self.width() - 4, self._pm.width()))
        h = int(self._pm.height() * w / max(1, self._pm.width()))
        if h > PREVIEW_SHOW_MAX_H:  # 세로 영상·넓은 패널에서도 그림이 옵션을 밀어내지 않게
            w = max(20, int(self._pm.width() * PREVIEW_SHOW_MAX_H / max(1, self._pm.height())))
        pm = self._pm if w >= self._pm.width() else self._pm.scaledToWidth(w, Qt.SmoothTransformation)
        self.setFixedHeight(pm.height() + 4)
        self.setPixmap(pm)


class _Fmt:
    """ValueSlider 의 값 글자를 함수로 (밝기 0.1 → '+10')."""

    def __init__(self, fn):
        self.fn = fn

    def format(self, v):  # noqa: A003
        return self.fn(v)


def show_value(name: str, v: float) -> str:
    """사람이 읽기 쉬운 값: 밝기·대비·채도는 기본 0 기준 ±(×100), 분위기·움직임 강도는 0~100."""
    v = float(v)
    if name == "brightness":
        return f"{v * 100:+.0f}"
    if name in ("contrast", "saturation"):
        return f"{(v - 1) * 100:+.0f}"
    return f"{v * 100:.0f}"


class FxCard(QFrame):
    """둥근 테두리 카드: 머리(▾ 제목 · 바뀐 값 점 · 접혔을 때 요약)를 누르면 접기/펼치기(상태 기억).
    안은 '이름 | 슬라이더 | 값 | ↺' 한 줄짜리 촘촘한 줄."""

    ROW_H = 22
    LABEL_W = 84

    def __init__(self, title: str, key: str, default_open: bool = True, parent=None):
        super().__init__(parent)
        self.title, self.key, self.summary, self.dirty = title, key, "", False
        self._settings = QSettings("knhanul", "VideoComposer")
        self.setObjectName("fxCard")
        self.setStyleSheet("QFrame#fxCard{border:1px solid #474b55;border-radius:7px;background:#25282f}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = QToolButton()
        self.head.setCheckable(True)
        self.head.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.head.setFixedHeight(28)
        self.head.setStyleSheet("QToolButton{border:none;border-radius:7px;background:transparent}"
                                "QToolButton:hover{background:#2f333b}")
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(10, 0, 10, 0)
        hl.setSpacing(6)
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet("font-weight:bold;color:#e8eaed;background:transparent")
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color:#ffd54f;font-size:9px;background:transparent")
        self.dot.setToolTip("기본값에서 바꾼 값이 있어요")
        self.lbl_sum = QLabel()
        self.lbl_sum.setStyleSheet("color:#9aa0a6;font-size:11px;background:transparent")
        self.lbl_sum.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for wdg in (self.lbl_title, self.dot, self.lbl_sum):
            wdg.setAttribute(Qt.WA_TransparentForMouseEvents)
        hl.addWidget(self.lbl_title)
        hl.addWidget(self.dot)
        hl.addStretch(1)
        hl.addWidget(self.lbl_sum)
        self.dot.hide()
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent")
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(10, 0, 8, 6)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnStretch(1, 1)
        lay.addWidget(self.head)
        lay.addWidget(self.body)
        v = self._settings.value(f"ui/fold/{key}", None)
        on = default_open if v is None else str(v).lower() in ("true", "1")
        self.head.toggled.connect(self.set_open)
        self.set_open(on, remember=False)

    def set_open(self, on: bool, remember: bool = True):
        self.head.blockSignals(True)
        self.head.setChecked(on)
        self.head.blockSignals(False)
        self.body.setVisible(on)
        self._update_head()
        if remember:
            self._settings.setValue(f"ui/fold/{self.key}", bool(on))

    def is_open(self) -> bool:
        return self.head.isChecked()

    def _update_head(self):
        on = self.is_open()
        self.lbl_title.setText(("▾ " if on else "▸ ") + self.title)
        self.lbl_sum.setText("" if on else self.summary)
        self.dot.setVisible(self.dirty)
        self.head.setToolTip(f"{self.title} — {self.summary}" if self.summary else self.title)

    def set_summary(self, text: str, dirty: Optional[bool] = None):
        self.summary = text or ""
        if dirty is not None:
            self.dirty = bool(dirty)
        self._update_head()

    def header_text(self) -> str:
        """머리에 보이는 글 (테스트·접근성)."""
        t = self.lbl_title.text()
        return t + (f" — {self.lbl_sum.text()}" if self.lbl_sum.text() else "")

    def add_widget_row(self, widget):
        self.grid.addWidget(widget, self.grid.rowCount(), 0, 1, 4)

    def add_row(self, label: str, widget, value: Optional[QLabel] = None, reset: Optional[QToolButton] = None):
        r = self.grid.rowCount()
        lab = QLabel(label)
        lab.setStyleSheet("color:#c8ccd2;background:transparent")
        lab.setFixedHeight(self.ROW_H)
        lab.setFixedWidth(self.LABEL_W)  # 카드끼리 슬라이더 줄 맞춤
        self.grid.addWidget(lab, r, 0)
        if value is None and reset is None:
            self.grid.addWidget(widget, r, 1, 1, 3)
            return
        self.grid.addWidget(widget, r, 1)
        if value is not None:
            self.grid.addWidget(value, r, 2)
        if reset is not None:
            self.grid.addWidget(reset, r, 3)

    def add_slider_row(self, label: str, vs: ValueSlider, reset: Optional[QToolButton] = None):
        """ValueSlider 를 '슬라이더 | 값' 두 칸으로 나눠 줄 맞춤."""
        vs.layout().removeWidget(vs.label)
        vs.label.setParent(None)
        vs.label.setMinimumWidth(32)
        vs.label.setStyleSheet("color:#e8eaed;background:transparent")
        vs.slider.setFixedHeight(self.ROW_H)
        self.add_row(label, vs, vs.label, reset)


def _reset_button(tip: str) -> QToolButton:
    rb = QToolButton()
    rb.setText("↺")
    rb.setToolTip(tip)
    rb.setFixedSize(22, 20)
    rb.setStyleSheet("QToolButton{border:1px solid #474b55;border-radius:4px;background:#30333b;color:#e8eaed}"
                     "QToolButton:disabled{color:#5f6368;border-color:#3a3d45}")
    return rb


class EffectsPanel(QWidget):
    rendered = Signal(str, str, object)  # (종류 'preview'|'thumbs'|'done', 키, 결과) — 일하는 스레드에서 화면 쪽으로

    def __init__(self, model: ProjectModel, parent=None):
        from .widgets import DIM, Columns, Footer, Segmented

        super().__init__(parent)
        self.model = model
        self._loading = False
        self.sync_thumbs = False  # 테스트: 그림을 바로(같은 스레드에서, 기다림 없이) 만들기
        self._want = {"preview": None, "thumbs": None}  # 지금 보여 줘야 할 그림 키
        self._job = None  # 0.3초 뒤 만들 일 (preview_args, thumbs_args)
        self._running = False  # 그림 만드는 일은 한 번에 하나만 (느린 PC 에서 ffmpeg 가 겹쳐 돌지 않게)
        self.thumb_paths: dict[str, Path] = {}
        self.preview_path: Optional[Path] = None
        self.compare = "fx"  # 'fx' = 효과 적용 / 'orig' = 원래 모습
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._launch)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.head = QLabel()
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:13px;font-weight:bold;")
        self.cmp = Segmented([("orig", "원래", "효과를 넣기 전 모습"), ("fx", "효과 적용", "지금 효과를 모두 넣은 모습")], height=24)
        self.cmp.setFixedWidth(150)
        self.cmp.set_value("fx")
        self.cmp.changed.connect(self.set_compare)
        top.addWidget(self.head, 1)
        top.addWidget(self.cmp)
        self.hint = QLabel("타임라인에서 클립을 선택하세요")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#9aa0a6;")
        self.lock_banner = QLabel("🔒 클립 트랙이 잠겨 있습니다 — 타임라인 왼쪽 트랙 머리의 🔒 를 눌러 풀면 고칠 수 있습니다.")
        self.lock_banner.setWordWrap(True)
        self.lock_banner.setStyleSheet("background:#3b4250; color:#e8eaed; padding:6px; border-radius:4px;")
        lay.addLayout(top)
        lay.addWidget(self.hint)
        lay.addWidget(self.lock_banner)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        lay.addWidget(self.body)

        # --- 합쳐 보기 (지금 효과를 모두 입힌 그림, 높이 최대 150px) ---
        self.preview = PreviewLabel()
        self.preview.setStyleSheet("background:#111318;border:1px solid #474b55;border-radius:4px;color:#9aa0a6;")
        self.preview.setToolTip("고른 클립의 한 장면에 지금 효과(색 필터 · 밝기·색 · 분위기)를 모두 입힌 모습 — "
                                "내보내기와 같은 필터 (움직임은 빼고). 위 '원래'를 누르면 효과 넣기 전")
        self.busy = QLabel("만드는 중…", self.preview)
        self.busy.setStyleSheet("background:rgba(0,0,0,170);color:#ffd54f;padding:2px 6px;border-radius:3px;font-size:11px;")
        self.busy.hide()
        bl.addWidget(self.preview)

        # --- 카드 4개: 움직임(맨 위, 사진 영상에서 가장 많이 씀) / 색 필터 / 밝기·색 / 분위기 ---
        self.sec_motion = FxCard("영상 움직임", key="fx_motion")
        self.sec_motion.setToolTip("확대 · 이동 (이미지·영상 모두)")
        self.motion = QComboBox()
        for m in MOTIONS:
            self.motion.addItem(MOTION_LABELS[m], m)
        self.strength_slider = ValueSlider(0, 1, 0.01, _Fmt(lambda v: show_value("motion_strength", v)), live=False)
        self.easing = QComboBox()
        self.easing.addItem("부드럽게", "ease")
        self.easing.addItem("일정하게", "linear")
        self.easing.setToolTip("움직임 속도감: 부드럽게 = 천천히 시작해서 천천히 멈춤")
        for cb in (self.motion, self.easing):
            cb.setFixedHeight(24)
        mrow = QWidget()
        mrow.setStyleSheet("background:transparent")
        ml = QHBoxLayout(mrow)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        ml.addWidget(self.motion, 3)
        ml.addWidget(self.easing, 2)
        self.sec_motion.add_row("종류", mrow)
        self.sec_motion.add_slider_row("세기", self.strength_slider)
        self.motion.currentIndexChanged.connect(
            lambda _: self._edit("motion", lambda c: setattr(c, "motion", self.motion.currentData()), None))
        self.strength_slider.valueChanged.connect(
            lambda v: self._edit("motion_strength", lambda c: setattr(c, "motion_strength", round(v, 3)), "fx_str"))
        self.easing.currentIndexChanged.connect(
            lambda _: self._edit("motion_easing", lambda c: setattr(c, "motion_easing", self.easing.currentData()), None))

        self.sec_look = FxCard("색 필터", key="fx_look")
        cards = QWidget()
        cards.setStyleSheet("background:transparent")
        grid = QGridLayout(cards)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self.look_group = QButtonGroup(self)
        self.look_group.setExclusive(True)
        self.look_cards: dict[str, QToolButton] = {}
        for i, lk in enumerate(LOOKS):
            b = QToolButton()
            b.setCheckable(True)
            b.setText(LOOK_LABELS[lk])
            b.setToolTip(f"색 필터: {LOOK_LABELS[lk]} (그림 = 이 필터 + 지금 다른 조정)")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(THUMB_W, THUMB_H))
            b.setMinimumSize(40, 34)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setStyleSheet("QToolButton{border:1px solid #474b55;border-radius:5px;padding:1px;font-size:11px;"
                            "background:#2b2e36}QToolButton:checked{border:2px solid #ffd54f;background:#3a3d45}")
            self.look_group.addButton(b)
            self.look_cards[lk] = b
            grid.addWidget(b, i // 4, i % 4)
            b.clicked.connect(lambda _=False, v=lk: self._edit("look", lambda c, v=v: setattr(c, "look", v), None))
        self.sec_look.add_widget_row(cards)

        self.sliders: dict[str, ValueSlider] = {}
        self.resets: dict[str, QToolButton] = {}
        self.sec_adjust = FxCard("밝기 · 색", key="fx_adjust")
        self.sec_mood = FxCard("분위기", key="fx_mood")
        for sec, rows in ((self.sec_adjust, ADJUST), (self.sec_mood, MOOD)):
            for name, d, lo, hi, label in rows:
                # live=False: 끄는 동안은 숫자만, 손을 떼면 한 번 반영 (그때 그림을 다시 만듦)
                vs = ValueSlider(lo, hi, 0.01, _Fmt(lambda v, n=name: show_value(n, v)), live=False)
                rb = _reset_button(f"{label} 기본값으로")
                sec.add_slider_row(label, vs, rb)
                self.sliders[name] = vs
                self.resets[name] = rb
                vs.valueChanged.connect(lambda v, n=name: self._edit(n, lambda c, n=n, v=v: setattr(c, n, round(v, 3)), "fx_" + n))
                rb.clicked.connect(lambda _=False, n=name, d=d: self._edit(n, lambda c, n=n, d=d: setattr(c, n, d), None))

        # 넓으면(720px 이상) 2단: 왼쪽 움직임·색 필터, 오른쪽 밝기·색·분위기
        self.cols = Columns(spacing=6)
        self.cols.add(self.sec_motion, 0)
        self.cols.add(self.sec_look, 0)
        self.cols.add(self.sec_adjust, 1)
        self.cols.add(self.sec_mood, 1)
        bl.addWidget(self.cols)

        self.note = QLabel(PREVIEW_NOTE)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(DIM + "font-size:11px;")
        bl.addWidget(self.note)
        lay.addStretch(1)

        # --- 바닥 줄 (스크롤해도 보임): 효과 지우기 · 모든 클립에 같은 효과 ---
        self.footer = Footer()
        self.btn_all = QPushButton("모든 클립에 같은 효과")
        self.btn_all.setToolTip("이 클립의 색 필터·밝기·분위기·움직임을 모든 클립에 똑같이 (Ctrl+Z 한 번으로 되돌리기)")
        self.btn_all.setStyleSheet("QPushButton{background:#2f6fbf;color:white;font-weight:bold;border-radius:4px;padding:4px 10px}"
                                   "QPushButton:disabled{background:#3a3f49;color:#8a8f98}")
        self.btn_clear = QPushButton("효과 지우기")
        self.btn_clear.setToolTip("이 클립의 색 필터·밝기·분위기를 기본값으로 (움직임은 그대로)")
        fb = QHBoxLayout()
        fb.addWidget(self.btn_clear)
        fb.addStretch(1)
        fb.addWidget(self.btn_all)
        self.footer.add(fb)
        self.btn_all.clicked.connect(self.apply_to_all)
        self.btn_clear.clicked.connect(self.clear)

        self.rendered.connect(self._on_rendered)
        model.selection_changed.connect(self.refresh)
        model.changed.connect(self.refresh)
        self.refresh()

    def set_compare(self, mode: str) -> None:
        """위 그림: '원래'(효과 넣기 전) / '효과 적용'."""
        self.compare = "orig" if mode == "orig" else "fx"
        self.cmp.set_value(self.compare)
        c = self.clip
        if c is not None:
            self._request_images(c)

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
            self.cmp.setVisible(False)
            self.lock_banner.hide()
            self._debounce.stop()
            self._job = None
            self._want = {"preview": None, "thumbs": None}
            return
        self._loading = True
        lk = self.locked()
        self.lock_banner.setVisible(lk)
        self.body.setEnabled(not lk)
        kind = "이미지" if c.type == "image" else "동영상"
        self.head.setText(f"✨ {self.index + 1}번 클립 · {c.label or c.file} ({kind})")
        self.cmp.setVisible(True)
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
        self._update_summaries(c)
        self._loading = False
        self._request_images(c)

    def _update_summaries(self, c):
        """카드 머리: 접혔을 때 요약, 기본값에서 바뀐 값이 있으면 노란 점."""
        look = c.look or "none"
        self.sec_look.set_summary(LOOK_LABELS.get(look, look), look != "none")
        for card, rows, empty in ((self.sec_adjust, ADJUST, "기본"), (self.sec_mood, MOOD, "없음")):
            parts = [f"{label} {show_value(name, getattr(c, name))}" for name, d, _lo, _hi, label in rows
                     if abs(float(getattr(c, name)) - d) > 1e-4]
            card.set_summary(" · ".join(parts) or empty, bool(parts))
        moving = c.motion != "none" and c.motion_strength > 0
        self.sec_motion.set_summary(f"{MOTION_LABELS.get(c.motion, c.motion)} · 강도 {show_value('s', c.motion_strength)}"
                                    if moving else "없음", moving)

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

    def preview_spec(self, c):
        """합쳐 보기 그림을 만들 값 (원본, 시각, 영상?, 효과 값, 크기, 화면 맞춤, 여백 색)."""
        path, at = self._thumb_source(c)
        if path is None:
            return None
        out = self.model.project.output
        return (path, at, c.type == "video", effect_params(c), preview_size(out.width, out.height),
                c.fit or out.fit, out.background)

    def _request_images(self, c):
        spec = self.preview_spec(c)
        if spec is None:
            self._set_preview(None)
            self._set_icons({})
            self._want = {"preview": None, "thumbs": None}
            return
        path, at, is_video, params, size, fit, bg = spec
        pparams = params if self.compare == "fx" else {k: DEFAULTS[k] for k in EFFECT_FIELDS}
        pkey = str(effect_preview_path(path, at, pparams, size, fit, bg))
        tfolder, _ = _looks_folder(path, at, params, None)
        tkey = str(tfolder)
        need_p = need_t = False
        if pkey != self._want["preview"]:
            self._want["preview"] = pkey
            if Path(pkey).exists() and Path(pkey).stat().st_size > 0:
                self._set_preview(Path(pkey))  # 캐시에 있으면 바로
            else:
                need_p = True
        if tkey != self._want["thumbs"]:
            self._want["thumbs"] = tkey
            hit = look_thumbs_cached(path, at, params)
            if hit:
                self._set_icons(hit)
            else:
                need_t = True
        if not (need_p or need_t):
            if self._job is None:
                self.busy.hide()
            return
        job_p = (pkey, (path, at, is_video, pparams, size, fit, bg)) if need_p else (self._job or {}).get("p")
        job_t = (tkey, (path, at, is_video, params)) if need_t else (self._job or {}).get("t")
        self._job = {"p": job_p, "t": job_t}
        if need_p:
            self._show_busy()
        if self.sync_thumbs:
            self._launch()
        else:
            self._debounce.start()  # 끄는 동안은 0.3초마다 다시 미룸

    def _show_busy(self):
        self.busy.adjustSize()
        self.busy.move(max(0, self.preview.width() - self.busy.width() - 6), 6)
        self.busy.show()
        self.busy.raise_()

    def flush(self):
        """기다리지 않고 지금 만들기 (테스트)."""
        if self._debounce.isActive():
            self._debounce.stop()
            self._launch()

    def _launch(self):
        if self._running and not self.sync_thumbs:
            return  # 지금 만드는 일이 끝나면 이어서 (_on_rendered 'done')
        job, self._job = self._job, None
        if not job:
            return
        self._running = True

        def work():
            if job.get("p"):
                key, (path, at, is_video, params, size, fit, bg) = job["p"]
                self._emit("preview", key, effect_preview(path, at, is_video, params, size, fit, bg))
            if job.get("t"):
                key, (path, at, is_video, params) = job["t"]
                self._emit("thumbs", key, look_thumbnails(path, at, is_video, params=params))
            self._emit("done", "", None)
        if self.sync_thumbs:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def _emit(self, kind, key, res):
        try:
            self.rendered.emit(kind, key, res)
        except RuntimeError:  # 그 사이 창이 닫힘
            pass

    def _on_rendered(self, kind: str, key: str, res):
        if kind == "done":
            self._running = False
            if self._job and not self._debounce.isActive():
                self._launch()  # 만드는 동안 값이 또 바뀌었으면 마지막 값으로 한 번 더
            return
        if key != self._want.get(kind):
            return  # 그 사이 값이 또 바뀜 (새 그림을 기다리는 중)
        if kind == "preview":
            self._set_preview(res)
            if self._job is None or not self._job.get("p"):
                self.busy.hide()
        else:
            self._set_icons(res or {})

    def _set_preview(self, path: Optional[Path]):
        self.preview_path = Path(path) if path else None
        pm = QPixmap(str(path)) if path else QPixmap()
        self.preview.setText("" if not pm.isNull() or self.clip is None else "그림을 만들 수 없어요 (파일을 찾을 수 없음)")
        self.preview.set_image(pm)

    def _set_icons(self, paths: dict):
        self.thumb_paths = dict(paths)
        for lk, card in self.look_cards.items():
            p = paths.get(lk)
            pm = QPixmap(str(p)) if p else QPixmap()
            card.setIcon(QIcon(pm) if not pm.isNull() else QIcon())
            card.setMinimumSize(THUMB_W + 10, (THUMB_H + 26) if not pm.isNull() else 40)
