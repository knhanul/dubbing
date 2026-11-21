"""AI 로 구성하기: 프롬프트 만들기 → AI 답변 붙여넣기(자동 검사) → 적용."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ..prompt_template import build_prompt, build_realign_prompt, media_info_text, scan_folder
from ..schema import JSON_SCHEMA
from ..validate import JsonExtractError, correction_prompt, extract_json, validate_project


class PromptDialog(QDialog):
    IDLE = "AI 답변을 붙여넣으면 자동으로 검사합니다."
    OK_TEXT = "✔ 문제 없음 — '적용'을 누르면 타임라인에 반영됩니다."

    apply_requested = Signal(object, object)  # 검사를 통과한 JSON(dict), 작업 폴더 Path

    def __init__(self, folder: Optional[Path] = None, parent=None, project=None, mode: str = "initial"):
        super().__init__(parent)
        self.setWindowTitle("AI 로 구성하기 — 프롬프트 복사 → AI 답변 붙여넣기 → 적용")
        self.resize(980, 720)
        self.folder: Optional[Path] = Path(folder) if folder else None
        self.project = project
        self.mode = mode  # "initial" or "realign"
        self.entries = []
        self.parsed: Optional[dict] = None

        tabs = QTabWidget()
        # ---- 1단계 ----
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        top = QHBoxLayout()
        self.ed_folder = QLineEdit(str(self.folder) if self.folder else "")
        self.ed_folder.setPlaceholderText("이미지·영상·음악·자막이 들어 있는 폴더")
        b_folder = QPushButton("폴더 선택…")
        top.addWidget(QLabel("작업 폴더")); top.addWidget(self.ed_folder, 1); top.addWidget(b_folder)
        l1.addLayout(top)
        form = QFormLayout()
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText("예) 30초짜리 여행 브이로그. 사진은 천천히 확대, 영상은 소리 끄고, 잔잔한 배경음악, 자막 포함.")
        self.request.setMaximumHeight(80)
        self.w = QSpinBox(); self.w.setRange(16, 7680); self.w.setValue(1920); self.w.setSingleStep(2)
        self.h = QSpinBox(); self.h.setRange(16, 4320); self.h.setValue(1080); self.h.setSingleStep(2)
        self.fps = QDoubleSpinBox(); self.fps.setRange(1, 120); self.fps.setValue(30)
        self.target = QDoubleSpinBox(); self.target.setRange(0, 36000); self.target.setSuffix(" 초"); self.target.setSpecialValueText("지정 안 함")
        self.chk_schema = QCheckBox("JSON Schema 전문도 포함 (긴 프롬프트)")
        self.chk_info = QCheckBox("영상 길이·해상도 정보 포함"); self.chk_info.setChecked(True)
        size = QHBoxLayout(); size.addWidget(self.w); size.addWidget(QLabel("×")); size.addWidget(self.h)
        size.addWidget(QLabel("fps")); size.addWidget(self.fps); size.addStretch(1)
        form.addRow("원하는 영상 설명", self.request)
        form.addRow("출력 크기", size)
        form.addRow("목표 길이", self.target)
        form.addRow("", self.chk_info)
        form.addRow("", self.chk_schema)
        # 모드 선택
        mode_box = QHBoxLayout()
        self.rad_initial = QRadioButton("처음 구성")
        self.rad_realign = QRadioButton("녹음에 맞춰 다시 구성")
        self.rad_initial.setChecked(self.mode != "realign")
        self.rad_realign.setChecked(self.mode == "realign")
        can_realign = bool(self.project and self.project.clips and self.project.dubbing.takes)
        self.rad_realign.setEnabled(can_realign)
        tip = "녹음이 길면 장면과 목소리가 어긋날 수 있습니다.\nAI가 클립 길이만 다시 맞춰 줍니다."
        self.rad_realign.setToolTip(tip if can_realign else "클립과 녹음이 하나 이상 있어야 사용할 수 있습니다")
        mode_box.addWidget(self.rad_initial)
        mode_box.addWidget(self.rad_realign)
        mode_box.addStretch(1)
        form.addRow("모드", mode_box)
        # 단계 안내
        steps = QLabel(
            "<b>①</b> 프롬프트 만들기 → 복사<br>"
            "<b>②</b> ChatGPT / Claude 에 붙여넣기<br>"
            "<b>③</b> 답변을 아래 탭에 붙여넣기 → 적용"
        )
        steps.setWordWrap(True)
        steps.setStyleSheet("color:#9aa0aa;font-size:11px;padding:4px 0;")
        form.addRow("순서", steps)
        l1.addLayout(form)
        b_gen = QPushButton("프롬프트 만들기")
        self.prompt = QPlainTextEdit(); self.prompt.setReadOnly(False)
        b_copy = QPushButton("📋 프롬프트 복사 (AI 에 붙여넣기)")
        row = QHBoxLayout(); row.addWidget(b_gen); row.addStretch(1); row.addWidget(b_copy)
        l1.addLayout(row)
        l1.addWidget(self.prompt, 1)
        tabs.addTab(w1, "① 프롬프트 만들기")

        # ---- 2단계 ----
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        l2.addWidget(QLabel("AI 의 답변을 통째로 붙여넣으세요. 붙여넣으면 바로 검사합니다. "
                            "```json 코드블록이나 설명 문장이 섞여 있어도 JSON 만 찾아냅니다."))
        split = QSplitter(Qt.Vertical)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("여기에 AI 답변 붙여넣기")
        self.issues = QListWidget()
        split.addWidget(self.answer); split.addWidget(self.issues)
        split.setSizes([400, 200])
        l2.addWidget(split, 1)
        self.status = QLabel(self.IDLE)
        self.status.setWordWrap(True)
        l2.addWidget(self.status)
        row2 = QHBoxLayout()
        b_paste = QPushButton("📋 클립보드에서 붙여넣기")
        self.b_fix = QPushButton("수정 요청 프롬프트 복사")
        self.b_fix.setEnabled(False)
        self.b_apply = QPushButton("✔ 적용")
        self.b_apply.setEnabled(False)
        self.b_apply.setToolTip("현재 타임라인을 AI 결과로 바꿉니다 (Ctrl+Z 로 되돌리기)")
        self.b_apply.setStyleSheet("QPushButton:enabled{background:#2e7d4f;color:white;font-weight:bold;padding:6px 18px}")
        for b in (b_paste, self.b_fix):
            row2.addWidget(b)
        row2.addStretch(1)
        row2.addWidget(self.b_apply)
        l2.addLayout(row2)
        self.fix_text = ""
        tabs.addTab(w2, "② AI 답변 붙여넣기")
        self.tabs = tabs

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)

        b_folder.clicked.connect(self._pick_folder)
        b_gen.clicked.connect(self.generate)
        b_copy.clicked.connect(lambda: self._copy(self.prompt.toPlainText(), "프롬프트를 복사했습니다. Grok 등 AI 에 붙여넣으세요."))
        b_paste.clicked.connect(self.paste_from_clipboard)
        # 입력이 멈추고 0.4초 뒤 자동 검사
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self.check)
        self.answer.textChanged.connect(self._text_changed)
        self.b_fix.clicked.connect(lambda: self._copy(self.fix_text, "수정 요청을 복사했습니다. AI 에게 보내고 새 답변을 다시 붙여넣으세요."))
        self.b_apply.clicked.connect(self._apply)
        if self.folder:
            self.generate()

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.ed_folder.text())
        if d:
            self.ed_folder.setText(d)
            self.generate()

    def _copy(self, text, msg):
        QGuiApplication.clipboard().setText(text)
        QMessageBox.information(self, "복사됨", msg)

    def _folder(self) -> Optional[Path]:
        t = self.ed_folder.text().strip()
        if not t or not Path(t).is_dir():
            QMessageBox.warning(self, "폴더", "작업 폴더를 선택해 주세요.")
            return None
        self.folder = Path(t)
        return self.folder

    def generate(self):
        folder = self._folder()
        if not folder:
            return
        info = media_info_text if self.chk_info.isChecked() else None
        self.entries = scan_folder(folder, info)
        schema = json.dumps(JSON_SCHEMA, ensure_ascii=False, indent=1) if self.chk_schema.isChecked() else None
        if self.rad_realign.isChecked() and self.project:
            text = build_realign_prompt(self.project, self.entries, self.request.toPlainText(),
                                        self.w.value(), self.h.value(), self.fps.value())
        else:
            text = build_prompt(self.entries, self.request.toPlainText(), self.w.value(), self.h.value(),
                                self.fps.value(), self.target.value() or None, schema)
        self.prompt.setPlainText(text)

    def _text_changed(self):
        # 내용이 바뀌면 이전 검사 결과로 적용하지 못하게 막고 잠시 뒤 다시 검사
        self.parsed = None
        self.b_apply.setEnabled(False)
        self.status.setText("검사 중…" if self.answer.toPlainText().strip() else self.IDLE)
        self._debounce.start()

    def paste_from_clipboard(self):
        self.answer.setPlainText(QGuiApplication.clipboard().text())
        self._debounce.stop()
        self.check()

    def _set_status(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color}; font-weight:bold")

    def check(self):
        self._debounce.stop()
        self.issues.clear()
        self.parsed = None
        self.b_apply.setEnabled(False)
        self.b_fix.setEnabled(False)
        text = self.answer.toPlainText()
        if not text.strip():
            self._set_status(self.IDLE, "#9aa0aa")
            return
        t = self.ed_folder.text().strip()
        if not t or not Path(t).is_dir():
            self._set_status("작업 폴더를 먼저 선택해 주세요(① 탭).", "#ffd166")
            return
        folder = self.folder = Path(t)
        names = [e.name for e in (self.entries or scan_folder(folder))]
        try:
            data = extract_json(text)
        except JsonExtractError as e:
            self._add(f"[오류] {e}", "#ff6b6b")
            self.fix_text = correction_prompt([], names, parse_error=str(e))
            self.b_fix.setEnabled(True)
            self._set_status("✖ JSON 을 읽을 수 없습니다 — '수정 요청 프롬프트 복사'로 AI 에게 다시 요청하세요.", "#ff6b6b")
            return
        # 다시 구성 모드 안전장치
        if self.rad_realign.isChecked() and self.project:
            from ..subtitles import project_cues
            cur_indices = sorted(c.index for c in project_cues(self.project))
            items = (data.get("subtitle") or {}).get("items")
            if isinstance(items, list) and items:
                new_indices = sorted(it.get("index") for it in items if isinstance(it, dict) and "index" in it)
                if new_indices != cur_indices:
                    self._add("[오류] 자막 번호가 바뀌면 녹음이 다른 줄에 붙습니다. 자막은 빼고 다시 받아 주세요.", "#ff6b6b")
                    self._set_status("✖ 자막 번호가 바뀌었습니다 — 자막은 빼고 다시 받아 주세요.", "#ff6b6b")
                    return
            # 클립 개수/순서 경고
            if "clips" in data:
                cur_files = [c.file for c in self.project.clips]
                new_files = [c.get("file") for c in data["clips"] if isinstance(c, dict)]
                if len(new_files) != len(cur_files) or new_files != cur_files:
                    self._add("[경고] 클립 개수나 파일 순서가 바뀌었습니다. 의도한 것인지 확인하세요.", "#ffd166")
        issues = validate_project(data, folder)
        for i in issues:
            self._add(str(i), "#ff6b6b" if i.severity == "error" else "#ffd166")
        self.fix_text = correction_prompt(issues, names) if issues else ""
        self.b_fix.setEnabled(bool(issues))
        n_err = sum(1 for i in issues if i.severity == "error")
        if n_err:
            self._set_status(f"✖ 오류 {n_err}개 — 아래 목록을 확인하거나 '수정 요청 프롬프트 복사'로 AI 에게 고쳐 달라고 하세요.",
                             "#ff6b6b")
            return
        self.parsed = data
        self.b_apply.setEnabled(True)
        n = len(data.get("clips", []))
        warn = f" (경고 {len(issues)}개는 무시해도 됩니다)" if issues else ""
        self._set_status(self.OK_TEXT, "#3fa36b")
        self._add(f"클립 {n}개를 읽었습니다.{warn}", "#9aa0aa")

    def _add(self, text, color):
        it = QListWidgetItem(text)
        it.setForeground(QColor(color))
        self.issues.addItem(it)

    def _apply(self):
        if self.parsed is None or self.folder is None:
            return
        self.apply_requested.emit(self.parsed, self.folder)
        self.accept()
