"""자동 미리보기 및 미리보기 렌더 조율.

main_window.py 에서 분리한 클래스입니다.
미리보기 관련 상태와 워커 관리를 담당합니다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from ..render.ffmpeg_builder import RenderSettings
from ..render.runner import cache_dir
from ..schema import SubtitleSettings
from ..validate import validate_project
from .auto_preview import AutoPreview
from .render_dialog import RenderWorker


class PreviewController:
    """360p 초안 미리보기 자동 생성 및 교체를 관리합니다."""

    def __init__(self, window):
        self.win = window  # MainWindow 참조 (모델, 플레이어, 상태바 등 접근)
        self.preview_revision = -1
        self.preview_is_direct = False
        self._preview_seq = 0
        self._preview_worker: Optional[RenderWorker] = None

        self.auto = AutoPreview(
            self._render_revision_key,
            self._start_preview_worker,
            self._cancel_preview_worker,
            delay_ms=1500,
            parent=window,
        )
        self.auto.enabled = str(
            window.settings.value("view/auto_preview", True)
        ).lower() not in ("false", "0")
        self.auto.state_changed.connect(lambda _: self._update_notice())
        window.model.changed.connect(self.auto.notify_changed)

    # ------------------------------------------------------------------ 공개 API
    def request(self, force: bool = False):
        self.auto.request(force=force)

    def load_for_project(self):
        """프로젝트 로드/적용 시 미리보기 설정."""
        src = self._direct_source()
        self.preview_is_direct = bool(src and src.exists())
        if self.preview_is_direct:
            self.win.player.load(src, keep_position=False)
        else:
            self.win.player.load(None)
            self.preview_revision = -1
            if not self.auto.enabled:
                old = sorted(
                    cache_dir(self.win.model.project).glob("preview_*.mp4"),
                    key=lambda p: p.stat().st_mtime,
                )
                if old:
                    self.win.player.load(old[-1], keep_position=False)
            else:
                self.auto.request()
        self._update_notice()

    def render_now(self):
        """F5 / 수동 새로 만들기."""
        if not self._check_renderable():
            return
        if self._direct_source():
            self.load_for_project()
            return
        self.auto.request(force=True)

    def shutdown(self):
        self.auto.enabled = False
        self.auto.timer.stop()
        self._cancel_preview_worker()

    def on_auto_toggled(self, on: bool):
        self.auto.enabled = on
        self.win.settings.setValue("view/auto_preview", on)
        if on:
            self.auto.notify_changed()
        self._update_notice()

    # ------------------------------------------------------------------ 내부
    def _check_renderable(self) -> bool:
        if not self.win.model.project.clips:
            QMessageBox.information(self.win, "내보내기", "클립이 없습니다. 이미지·동영상을 먼저 넣으세요.")
            return False
        issues = [
            i
            for i in validate_project(
                self.win.model.project.to_dict(), self.win.model.project.base_dir
            )
            if i.severity == "error"
        ]
        if issues:
            QMessageBox.warning(self.win, "아직 만들 수 없습니다", "\n".join(map(str, issues[:20])))
            return False
        return True

    def _render_revision_key(self):
        """미리보기 영상에 영향을 주는 값(자막·더빙 제외)만으로 비교합니다."""
        d = self.win.model.project.to_dict()
        d.pop("subtitle", None)
        d.pop("dubbing", None)
        d.pop("title", None)
        d.pop("bgm", None)
        d["tracks"] = {"clips_hidden": d["tracks"]["clips"]["hidden"]}
        d["output"].pop("file", None)
        for c in d.get("clips", []):
            c.pop("label", None)
        return hash(str(d))

    def _direct_source(self) -> Optional[Path]:
        """영상 하나뿐이고 변형이 없으면 렌더 없이 원본을 바로 미리보기."""
        p = self.win.model.project
        if len(p.clips) != 1 or p.tracks.clips.hidden:
            return None
        c = p.clips[0]
        if (
            c.type != "video"
            or c.trim_start
            or c.trim_end
            or c.fit_duration
            or abs(c.speed - 1) > 1e-6
        ):
            return None
        from ..effects import has_effects

        if (
            has_effects(c)
            or (c.motion != "none" and c.motion_strength > 0)
            or p.output.fade_in > 0
            or p.output.fade_out > 0
        ):
            return None
        return p.resolve(c.file)

    def _preview_ready_to_render(self) -> bool:
        p = self.win.model.project
        if not p.clips or self._direct_source():
            return False
        return not any(
            i.severity == "error"
            for i in validate_project(p.to_dict(), p.base_dir)
        )

    def _start_preview_worker(self, key):
        """AutoPreview 가 호출: 360p 초안을 백그라운드로 렌더."""
        if not self._preview_ready_to_render():
            n = len(
                [
                    i
                    for i in validate_project(
                        self.win.model.project.to_dict(), self.win.model.project.base_dir
                    )
                    if i.severity == "error"
                ]
            )
            if n:
                self.win.player.set_notice(
                    f"미리보기를 만들 수 없습니다: 문제 {n}개 (편집 ▸ 검사하기)"
                )
            QTimer.singleShot(0, lambda: self.auto.on_finished(key, False))
            return
        proj = self.win.model.project.copy()
        proj.dubbing.takes = []
        proj.subtitle = SubtitleSettings()
        proj.bgm.file = None
        for ts in (
            proj.tracks.clips,
            proj.tracks.subtitle,
            proj.tracks.dubbing,
            proj.tracks.bgm,
        ):
            ts.muted = ts.solo = False
        proj.tracks.subtitle.hidden = False
        self._preview_seq += 1
        out = cache_dir(proj) / f"preview_{self._preview_seq % 2}.mp4"
        if (
            self.win.player.media_path is not None
            and Path(self.win.player.media_path) == out
        ):
            self._preview_seq += 1
            out = cache_dir(proj) / f"preview_{self._preview_seq % 2}.mp4"
        worker = RenderWorker(proj, out, RenderSettings(draft=True, encoder="auto"))
        self._preview_worker = worker
        worker.progress.connect(lambda f, m: self._preview_progress(f))
        worker.finished_ok.connect(
            lambda res, k=key, o=out, w=worker: self._preview_done(w, k, o, True)
        )
        worker.failed.connect(
            lambda msg, log, k=key, o=out, w=worker: self._preview_failed(w, k, msg)
        )
        worker.cancelled.connect(
            lambda k=key, o=out, w=worker: self._preview_done(w, k, o, False)
        )
        self._preview_progress(0.0)
        worker.start()

    def _cancel_preview_worker(self):
        w = self._preview_worker
        if w is not None and w.isRunning():
            w.cancel()

    def _preview_progress(self, f: float):
        self.win.player.set_progress(f"미리보기 만드는 중… {int(f * 100)}%", f)

    def _preview_done(self, worker, key, out: Path, ok: bool):
        worker.wait(3000)
        if self._preview_worker is worker:
            self._preview_worker = None
        self.win.player.set_progress("", None)
        if ok and out.exists():
            old = self.win.player.media_path
            self.preview_is_direct = False
            self.preview_revision = key
            self.win.player.load(out, keep_position=True, resume=True)
            if old and Path(old) != out and Path(old).name.startswith("preview_"):
                QTimer.singleShot(
                    1500,
                    lambda p=Path(old): p.unlink(missing_ok=True)
                    if self.win.player.media_path != p
                    else None,
                )
            self.win.statusBar().showMessage("미리보기를 새로 만들었습니다", 3000)
        self.auto.on_finished(key, ok and out.exists())
        self._update_notice()

    def _preview_failed(self, worker, key, msg: str):
        worker.wait(3000)
        if self._preview_worker is worker:
            self._preview_worker = None
        self.win.player.set_progress("", None)
        self.win.statusBar().showMessage(f"미리보기 실패: {msg}", 10000)
        self.auto.on_finished(key, False)
        self.win.player.set_notice(
            "미리보기를 만들지 못했습니다 — 🔄 미리보기 새로 만들기 로 다시 시도 (자세한 내용은 상태 표시줄)"
        )

    def _update_notice(self):
        """플레이어 안내 문구 갱신 (main_window 의 로직을 위임)."""
        if hasattr(self.win, "_update_notice"):
            self.win._update_notice()
