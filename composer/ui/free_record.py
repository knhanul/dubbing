"""더빙 패널 ▸ '자유 녹음 (대본 먼저)' 탭.

자막 시간에 맞추지 않고 대본을 보며 편하게 녹음(한 번에 길게 또는 문장마다 끊어서)한 뒤,
'🎙→📝 더빙으로 자막 만들기'로 녹음에 맞는 자막(SRT)을 만듭니다.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSpinBox,
                               QTextBrowser, QVBoxLayout, QWidget)

from ..autosub import (WHISPER_MODELS, AutoSubOptions, AutoSubResult, Cancelled, make_subtitles, model_downloaded,
                       script_from_file, split_sentences, whisper_available)
from ..schema import SubtitleItem
from .preview import fmt_time

ENGINES = [("auto", "자동 (음성 인식 → 안 되면 무음 감지)"), ("whisper", "음성 인식 (faster-whisper)"),
           ("silence", "무음 구간으로 나누기 (빠름, 글은 대본에서)")]


def edit_cues_from(res: AutoSubResult):
    from ..cue_edit import EditCue
    infos = res.infos if len(res.infos) == len(res.cues) else [None] * len(res.cues)
    return [EditCue(c.start, c.end, c.text, i.heard if i else "", i.conf if i else None, i.reason if i else "")
            for c, i in zip(res.cues, infos)]


class AutoSubWorker(QThread):
    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, sources, script: str, opts: AutoSubOptions):
        super().__init__()
        self.sources, self.script, self.opts = sources, script, opts
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            res = make_subtitles(self.sources, self.script, self.opts, lambda f, m: self.progress.emit(f, m),
                                 self.cancel_event)
        except Cancelled:
            self.cancelled.emit()
            return
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
            return
        if self.cancel_event.is_set():
            self.cancelled.emit()
        else:
            self.done.emit(res)


class FreeRecordPage(QWidget):
    status = Signal(str)

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel  # DubbingPanel (마이크·플레이어·모델 공유)
        self.model = panel.model
        self.player = panel.player
        self.last_result: Optional[AutoSubResult] = None
        # 테스트에서 바꿔 끼울 수 있는 것들
        self.confirm = lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.Yes
        self.ask_audio_file = self._ask_audio_file
        self.ask_script_file = self._ask_script_file
        self.ask_srt_path = self._ask_srt_path
        self.run_worker = self._run_worker_dialog
        self.open_aligner = self._open_aligner_dialog  # (EditCue 목록, 말한 구간, 소리들, 안내) → 자막 | None

        hint = QLabel("자막 시간에 맞추지 말고 <b>대본을 보며 편하게</b> 녹음하세요. 한 번에 길게, 또는 문장마다 끊어서 "
                      "여러 번 녹음해도 됩니다. 녹음은 <b>지금 재생 위치</b>부터 더빙 트랙에 놓입니다. "
                      "다 녹음했으면 <b>🎙→📝 더빙으로 자막 만들기</b>를 누르세요.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa0a6;")

        # --- 대본 ---
        g_script = QGroupBox("① 대본 (붙여 넣거나 불러오기 — 없어도 됨)")
        gs = QVBoxLayout(g_script)
        self.script = QPlainTextEdit()
        self.script.setPlaceholderText("여기에 대본을 붙여 넣으세요.\n문장마다 줄을 바꾸면 자막도 그 문장대로 나뉩니다.\n"
                                       "대본이 있으면 자막 글은 대본 그대로, 시간만 녹음에서 가져옵니다.")
        self.script.setFixedHeight(96)
        r = QHBoxLayout()
        self.btn_script_file = QPushButton("대본 파일 열기…")
        self.btn_script_file.setToolTip("TXT / SRT / VTT / Word(DOCX)")
        self.btn_script_subs = QPushButton("지금 자막에서 가져오기")
        r.addWidget(self.btn_script_file); r.addWidget(self.btn_script_subs); r.addStretch(1)
        gs.addWidget(self.script)
        gs.addLayout(r)

        # --- 텔레프롬프터 ---
        g_tp = QGroupBox("② 보면서 읽기 (텔레프롬프터)")
        gt = QVBoxLayout(g_tp)
        self.prompter = QTextBrowser()
        f = QFont(self.font()); f.setPointSize(17); f.setBold(True)
        self.prompter.setFont(f)
        self.prompter.setStyleSheet("QTextBrowser{background:#111317;color:#f1f3f4;border-radius:6px;padding:6px}")
        self.prompter.setFixedHeight(170)  # 크기가 변하지 않게(옆 미리보기 흔들림 방지)
        self.prompter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        rr = QHBoxLayout()
        rr.addWidget(QLabel("자동으로 넘기기"))
        self.scroll_speed = QSpinBox(); self.scroll_speed.setRange(0, 20); self.scroll_speed.setValue(0)
        self.scroll_speed.setSuffix(" 자/초"); self.scroll_speed.setSpecialValueText("끔 (직접 스크롤)")
        self.scroll_speed.setToolTip("녹음하는 동안 이 속도로 대본을 위로 올립니다. 보통 말하기는 5~7자/초")
        rr.addWidget(self.scroll_speed); rr.addStretch(1)
        gt.addWidget(self.prompter)
        gt.addLayout(rr)

        # --- 녹음 ---
        g_rec = QGroupBox("③ 녹음")
        gr = QVBoxLayout(g_rec)
        self.with_video = QCheckBox("영상을 재생하면서 녹음")
        self.with_video.setChecked(True)
        self.with_video.setToolTip("끄면 화면은 멈춘 채로 녹음합니다(녹음은 지금 재생 위치부터 놓임)")
        self.btn_rec = QPushButton("● 자유 녹음 시작")
        self.btn_rec.setStyleSheet("QPushButton{background:#b3261e;color:white;font-weight:bold;padding:6px}")
        self.btn_import = QPushButton("📂 음성 파일 불러오기…")
        self.btn_import.setToolTip("이미 녹음한 WAV/MP3 등을 지금 재생 위치에 더빙으로 놓습니다")
        self.lbl_pos = QLabel()
        self.lbl_pos.setFixedHeight(self.lbl_pos.sizeHint().height())
        r2 = QHBoxLayout(); r2.addWidget(self.btn_rec, 1); r2.addWidget(self.btn_import)
        gr.addWidget(self.with_video)
        gr.addLayout(r2)
        gr.addWidget(self.lbl_pos)

        # --- 자막 만들기 ---
        g_make = QGroupBox("④ 자막 만들기")
        gm = QVBoxLayout(g_make)
        self.btn_make = QPushButton("🎙→📝 더빙으로 자막 만들기")
        self.btn_make.setStyleSheet("QPushButton{background:#1a73e8;color:white;font-weight:bold;padding:8px}")
        self.btn_align = QPushButton("✏ 자막 맞추기…")
        self.btn_align.setToolTip("더빙 파형 위에서 자막 시간을 끌어서 맞추고, 나누기·합치기·탭으로 맞추기를 합니다")
        self.btn_save_srt = QPushButton("SRT 파일로 저장…")
        r3 = QHBoxLayout(); r3.addWidget(self.btn_make, 1); r3.addWidget(self.btn_align); r3.addWidget(self.btn_save_srt)
        gm.addLayout(r3)
        fo = QFormLayout()
        self.engine = QComboBox()
        for k, v in ENGINES:
            self.engine.addItem(v, k)
        self.whisper_model = QComboBox()
        for k, (_, size) in WHISPER_MODELS.items():
            self.whisper_model.addItem(f"{k} ({size}){' — 기본' if k == 'small' else ''}", k)
        self.whisper_model.setCurrentIndex(self.whisper_model.findData("small"))
        self.max_chars = QSpinBox(); self.max_chars.setRange(6, 60); self.max_chars.setValue(20); self.max_chars.setSuffix(" 자")
        self.max_lines = QSpinBox(); self.max_lines.setRange(1, 3); self.max_lines.setValue(2); self.max_lines.setSuffix(" 줄")
        self.min_dur = QSpinBox(); self.min_dur.setRange(3, 50); self.min_dur.setValue(10)
        self.min_dur.setSuffix(" × 0.1초"); self.min_dur.setToolTip("자막 하나를 최소 이만큼은 보여 줍니다")
        rl = QHBoxLayout(); rl.addWidget(self.max_chars); rl.addWidget(QLabel("×")); rl.addWidget(self.max_lines); rl.addStretch(1)
        fo.addRow("인식 방법", self.engine)
        fo.addRow("음성 인식 모델", self.whisper_model)
        fo.addRow("자막 한 개", rl)
        fo.addRow("최소 표시", self.min_dur)
        gm.addLayout(fo)
        self.lbl_engine = QLabel()
        self.lbl_engine.setWordWrap(True)
        self.lbl_engine.setStyleSheet("color:#9aa0a6;")
        self.lbl_result = QLabel()
        self.lbl_result.setWordWrap(True)
        gm.addWidget(self.lbl_engine)
        gm.addWidget(self.lbl_result)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(hint)
        lay.addWidget(g_script)
        lay.addWidget(g_tp)
        lay.addWidget(g_rec)
        lay.addWidget(g_make)
        lay.addStretch(1)

        self.script.textChanged.connect(self._update_prompter)
        self.btn_script_file.clicked.connect(self.load_script_file)
        self.btn_script_subs.clicked.connect(self.script_from_subtitles)
        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_import.clicked.connect(self.import_audio)
        self.btn_make.clicked.connect(self.make_subtitles)
        self.btn_save_srt.clicked.connect(self.save_srt)
        self.btn_align.clicked.connect(self.align_existing)
        self.engine.currentIndexChanged.connect(lambda _: self._update_engine_note())
        self.whisper_model.currentIndexChanged.connect(lambda _: self._update_engine_note())
        self.player.position_changed.connect(self._on_position)
        self.model.changed.connect(self.refresh)
        self._t0 = None
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._auto_scroll)
        self._update_prompter()
        self._update_engine_note()
        self.refresh()

    # ------------------------------------------------------------------ 대본
    def script_text(self) -> str:
        return self.script.toPlainText().strip()

    def _update_prompter(self):
        sents = split_sentences(self.script.toPlainText())
        if not sents:
            self.prompter.setHtml("<p style='color:#80868b'>대본을 넣으면 여기에 크게 보입니다.</p>")
            return
        import html
        body = "".join(f"<p style='margin:0 0 10px 0'>{html.escape(s)}</p>" for s in sents)
        self.prompter.setHtml(body + "<p>&nbsp;</p>" * 4)

    def _ask_script_file(self) -> Optional[Path]:
        f, _ = QFileDialog.getOpenFileName(self, "대본 파일 열기", str(self.model.project.base_dir),
                                           "대본 (*.txt *.srt *.vtt *.docx);;모든 파일 (*)")
        return Path(f) if f else None

    def load_script_file(self):
        p = self.ask_script_file()
        if not p:
            return
        try:
            self.script.setPlainText(script_from_file(p))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "대본", f"대본을 읽지 못했습니다:\n{e}")

    def script_from_subtitles(self):
        cues = self.model.cues
        if not cues:
            self.status.emit("가져올 자막이 없습니다.")
            return
        self.script.setPlainText("\n".join(c.text.replace("\n", " ") for c in cues))

    # ------------------------------------------------------------------ 녹음
    def free_takes(self) -> list[tuple[int, object]]:
        """자막에 묶이지 않은(절대 위치) 더빙 = 자유 녹음·불러온 음성."""
        return [(i, t) for i, t in enumerate(self.model.project.dubbing.takes) if t.subtitle is None]

    def toggle_record(self):
        pnl = self.panel
        if pnl.mode == "free":
            pnl.stop_recording()
            return
        if pnl.mode:
            pnl.stop_recording()
        pnl.start_free_recording(play_video=self.with_video.isChecked())
        if pnl.mode == "free":
            self._t0 = time.perf_counter()
            self._scroll_from = self.prompter.verticalScrollBar().value()
            self._timer.start()
        self.refresh()

    def recording_stopped(self):
        self._timer.stop()
        self._t0 = None
        self.refresh()
        self._warn_if_longer()

    def _auto_scroll(self):
        if self._t0 is None or self.scroll_speed.value() <= 0:
            return
        text = self.script.toPlainText()
        n = max(1, len(text))
        frac = (time.perf_counter() - self._t0) * self.scroll_speed.value() / n
        bar = self.prompter.verticalScrollBar()
        bar.setValue(int(self._scroll_from + frac * (bar.maximum() + self.prompter.viewport().height() * 0.5)))

    def _ask_audio_file(self) -> Optional[Path]:
        f, _ = QFileDialog.getOpenFileName(self, "음성 파일 불러오기", str(self.model.project.base_dir),
                                           "소리 (*.wav *.mp3 *.m4a *.flac *.ogg *.aac);;모든 파일 (*)")
        return Path(f) if f else None

    def import_audio(self):
        from ..dubbing.takes import set_take
        from ..importer import rel_path
        if self.model.project.tracks.dubbing.locked:
            self.status.emit("🔒 더빙 트랙이 잠겨 있습니다.")
            return
        p = self.ask_audio_file()
        if not p:
            return
        pos = round(self.player.position(), 3)
        self.model.edit(lambda pr: set_take(pr, None, rel_path(pr, p), pos))
        self.model.select("take", len(self.model.project.dubbing.takes) - 1)
        self.status.emit(f"더빙으로 놓았습니다: {p.name} ({fmt_time(pos)}부터)")
        self._warn_if_longer()

    def _take_ends(self) -> float:
        from ..probe import probe
        end = 0.0
        for i, t in self.free_takes():
            p = self.model.project.resolve(t.file)
            try:
                d = probe(p).duration if p and p.exists() else 0.0
            except Exception:  # noqa: BLE001
                d = 0.0
            end = max(end, t.offset + (d or 0.0))
        return end

    def _warn_if_longer(self, end: Optional[float] = None, append: bool = False):
        end = self._take_ends() if end is None else end
        total = self.model.timeline.total
        if self.model.project.clips and end > total + 0.05:
            msg = (f"⚠ 더빙({fmt_time(end)})이 영상({fmt_time(total)})보다 깁니다. 영상 길이는 바꾸지 않았습니다 — "
                   "클립 길이를 늘리거나 이미지를 더 넣으세요.")
            self.lbl_result.setText(f"{self.lbl_result.text()}\n{msg}" if append else msg)
            self.status.emit(msg)
            return True
        return False

    def _on_position(self, t: float):
        if self.panel.mode == "free":
            el = t - self.panel.rec_offset if self.player.is_playing() else (time.perf_counter() - (self._t0 or 0))
            self.lbl_pos.setText(f"● 녹음 중… {fmt_time(max(0.0, el))}  (다시 누르면 끝)")
        else:
            self.lbl_pos.setText(f"녹음 위치: 지금 재생 위치 {fmt_time(t)} 부터")

    def refresh(self):
        rec = self.panel.mode == "free"
        self.btn_rec.setText("■ 녹음 끝내기" if rec else "● 자유 녹음 시작")
        self.btn_import.setEnabled(not rec)
        n = len(self.free_takes())
        self.btn_make.setEnabled(n > 0 and not rec)
        self.btn_make.setToolTip("" if n else "먼저 자유 녹음을 하거나 음성 파일을 불러오세요")
        self.btn_save_srt.setEnabled(bool(self.model.cues))
        self.btn_align.setEnabled(bool(self.model.cues) and not rec)
        if not rec:
            self._on_position(self.player.position())

    def _update_engine_note(self):
        k = self.engine.currentData()
        m = self.whisper_model.currentData()
        self.whisper_model.setEnabled(k != "silence")
        if k == "silence":
            self.lbl_engine.setText("말 사이 쉬는 곳(무음)으로 나눕니다. 글은 대본에서 가져옵니다(대본이 없으면 '(N번 대사)').")
        elif not whisper_available():
            self.lbl_engine.setText("음성 인식(faster-whisper)이 설치되어 있지 않습니다 → 무음 구간으로 나눕니다. "
                                    "설치: pip install faster-whisper")
        elif not model_downloaded(m):
            self.lbl_engine.setText(f"처음 한 번 음성 인식 모델({m}, {WHISPER_MODELS[m][1]})을 내려받습니다. "
                                    "인터넷이 필요하고 진행률이 표시됩니다. 그다음부터는 바로 시작합니다.")
        else:
            self.lbl_engine.setText(f"음성 인식 모델 {m} 준비됨 (내 PC 에서 처리, 인터넷 필요 없음).")

    # ------------------------------------------------------------------ 자막 만들기
    def options(self) -> AutoSubOptions:
        return AutoSubOptions(engine=self.engine.currentData(), model=self.whisper_model.currentData(),
                              max_chars=self.max_chars.value(), max_lines=self.max_lines.value(),
                              min_duration=self.min_dur.value() / 10.0)

    def sources(self) -> list[tuple[Path, float]]:
        out = []
        pr = self.model.project
        for _, t in self.free_takes():
            p = pr.resolve(t.file)
            if p and p.exists():
                out.append((p, t.offset))
        return out

    def make_subtitles(self):
        from ..subtitles import has_subtitles
        src = self.sources()
        if not src:
            QMessageBox.information(self, "자막 만들기", "자막을 만들 녹음이 없습니다. 먼저 '● 자유 녹음 시작'으로 녹음하거나 "
                                                     "'📂 음성 파일 불러오기…'를 하세요.")
            return
        if self.model.project.tracks.subtitle.locked:
            self.status.emit("🔒 자막 트랙이 잠겨 있습니다.")
            return
        if has_subtitles(self.model.project) and not self.confirm(
                "자막 만들기", "지금 있는 자막을 새로 만든 자막으로 바꿀까요?\n(Ctrl+Z 로 되돌릴 수 있고, 원본 SRT 파일은 지워지지 않습니다)"):
            return
        worker = AutoSubWorker(src, self.script_text(), self.options())
        res = self.run_worker(worker)
        if isinstance(res, AutoSubResult):
            if not res.cues:
                self.apply_result(res)  # '말소리를 찾지 못함' 안내
            else:
                # 바로 '자막 맞추기' 화면 → 노란색(확인 필요) 자막을 보고 고친 뒤 '✔ 적용'
                cues = self.open_aligner(edit_cues_from(res), res.regions, src, self._result_note(res))
                if cues is None:
                    self.last_result = res
                    self.status.emit("자막을 넣지 않았습니다 (자막 맞추기에서 취소).")
                else:
                    self.apply_result(res, cues)
        self._update_engine_note()

    def _result_note(self, res: AutoSubResult) -> str:
        how = "음성 인식 + 무음 경계" if res.engine == "whisper" else "무음 구간"
        n = res.low_count
        msg = f"{how}으로 자막 {len(res.cues)}개를 만들었습니다." + (f" 노란색 {n}개를 확인해 주세요." if n else "")
        return f"{msg} {res.note}".strip()

    def _open_aligner_dialog(self, cues, regions, sources, note=""):
        from .sub_align import SubAlignDialog
        dlg = SubAlignDialog(cues, regions, sources, gap=self.options().gap, max_chars=self.max_chars.value(),
                             note=note, parent=self)
        ok = dlg.exec() == SubAlignDialog.Accepted
        return dlg.result_cues() if ok else None

    def align_existing(self):
        """지금 자막을 더빙 파형 위에서 다시 맞추기."""
        from ..autosub import detect_speech
        from ..cue_edit import EditCue
        if not self.model.cues:
            return
        if self.model.project.tracks.subtitle.locked:
            self.status.emit("🔒 자막 트랙이 잠겨 있습니다.")
            return
        src = self.sources()
        regions = []
        o = self.options()
        for p, pos in src:
            try:
                regions.extend((s + pos, e + pos) for s, e in detect_speech(p, o.noise_db, o.min_silence))
            except Exception:  # noqa: BLE001
                pass
        cues = [EditCue(c.start, c.end, c.text) for c in self.model.cues]
        note = "" if src else "자유 녹음·불러온 음성이 없어 파형 없이 시간만 고칩니다."
        out = self.open_aligner(cues, regions, src, note)
        if out is not None:
            self._set_items(out)
            msg = f"✔ 자막 맞추기를 적용했습니다 ({len(out)}개). Ctrl+Z 로 되돌릴 수 있습니다."
            self.lbl_result.setText(msg)
            self.status.emit(msg)

    def _run_worker_dialog(self, worker: AutoSubWorker):
        dlg = QProgressDialog("자막 만드는 중…", "취소", 0, 1000, self)
        dlg.setWindowTitle("🎙→📝 더빙으로 자막 만들기")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(460)
        box = {}
        worker.progress.connect(lambda f, m: (dlg.setValue(int(f * 1000)), dlg.setLabelText(m)))
        worker.done.connect(lambda r: box.setdefault("res", r))
        worker.failed.connect(lambda m: box.setdefault("err", m))
        worker.cancelled.connect(lambda: box.setdefault("cancel", True))
        worker.finished.connect(dlg.reset)
        dlg.canceled.connect(worker.cancel)
        worker.start()
        dlg.exec()
        if worker.isRunning() and dlg.wasCanceled():
            worker.cancel()
        worker.wait()
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        if "err" in box:
            QMessageBox.warning(self, "자막 만들기", f"자막을 만들지 못했습니다:\n{box['err']}")
            return None
        if "cancel" in box or "res" not in box:
            self.status.emit("자막 만들기를 취소했습니다.")
            return None
        return box["res"]

    def _set_items(self, cues):
        from ..subtitles import detach_takes

        def fn(p):
            detach_takes(p)  # 줄별 녹음이 있었다면 지금 위치에 고정 (번호가 바뀌어도 안 움직이게)
            p.subtitle.file = None
            p.subtitle.offset = 0.0
            p.subtitle.items = [SubtitleItem(k + 1, c.start, c.end, c.text) for k, c in enumerate(cues)]
        self.model.edit(fn)  # 되돌리기 한 번

    def apply_result(self, res: AutoSubResult, cues=None):
        if not res.cues:
            QMessageBox.information(self, "자막 만들기", "녹음에서 말소리를 찾지 못했습니다. 마이크 소리 크기를 확인해 주세요.")
            return
        cues = res.cues if cues is None else cues
        self._set_items(cues)
        self.last_result = res
        how = "음성 인식 + 무음 경계" if res.engine == "whisper" else "무음 구간"
        msg = f"✔ 자막 {len(cues)}줄을 넣었습니다 ({how}{', 대본 문장 사용' if self.script_text() else ''})."
        if res.note:
            msg += " " + res.note
        msg += " '✏ 자막 맞추기…'로 다시 고칠 수 있고, Ctrl+Z 로 되돌릴 수 있습니다."
        self.lbl_result.setText(msg)
        self.status.emit(msg)
        self._warn_if_longer(max(res.speech_end, self._take_ends()), append=True)
        self.refresh()

    def _ask_srt_path(self) -> Optional[Path]:
        base = self.model.path.with_suffix(".srt") if self.model.path else Path(self.model.project.base_dir) / "자막.srt"
        f, _ = QFileDialog.getSaveFileName(self, "SRT 파일로 저장", str(base), "자막 (*.srt)")
        if not f:
            return None
        p = Path(f)
        return p if p.suffix.lower() == ".srt" else p.with_suffix(".srt")

    def save_srt(self) -> Optional[Path]:
        from ..subtitles import to_srt
        if not self.model.cues:
            return None
        p = self.ask_srt_path()
        if not p:
            return None
        p.write_text(to_srt(self.model.cues), encoding="utf-8-sig")
        self.status.emit(f"SRT 로 저장했습니다: {p}")
        return p
