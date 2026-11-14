"""AI 프롬프트 템플릿 + 응답 붙여넣기/검증 + 수정 요청 프롬프트."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from ..prompt_template import build_prompt, media_info_text, scan_folder
from ..schema import JSON_SCHEMA, Project
from ..validate import JsonExtractError, correction_prompt, extract_json, has_errors, validate_project


class PromptDialog(QDialog):
    project_ready = Signal(object, object)  # Project, folder Path

    def __init__(self, folder: Optional[Path] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 프롬프트 템플릿 — AI 에게 프로젝트 JSON 받기")
        self.resize(980, 720)
        self.folder: Optional[Path] = Path(folder) if folder else None
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
        l2.addWidget(QLabel("AI 의 답변을 통째로 붙여넣으세요. ```json 코드블록이나 설명 문장이 섞여 있어도 JSON 만 찾아냅니다."))
        split = QSplitter(Qt.Vertical)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("여기에 AI 답변 붙여넣기")
        self.issues = QListWidget()
        split.addWidget(self.answer); split.addWidget(self.issues)
        split.setSizes([400, 200])
        l2.addWidget(split, 1)
        row2 = QHBoxLayout()
        b_paste = QPushButton("📋 클립보드에서 붙여넣기")
        b_check = QPushButton("검사하기")
        self.b_fix = QPushButton("수정 요청 프롬프트 복사")
        self.b_fix.setEnabled(False)
        self.b_open = QPushButton("✔ 이 프로젝트 열기")
        self.b_open.setEnabled(False)
        for b in (b_paste, b_check, self.b_fix):
            row2.addWidget(b)
        row2.addStretch(1)
        row2.addWidget(self.b_open)
        l2.addLayout(row2)
        self.fix_text = ""
        tabs.addTab(w2, "② AI 답변 붙여넣기")
        self.tabs = tabs

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)

        b_folder.clicked.connect(self._pick_folder)
        b_gen.clicked.connect(self.generate)
        b_copy.clicked.connect(lambda: self._copy(self.prompt.toPlainText(), "프롬프트를 복사했습니다. Grok 등 AI 에 붙여넣으세요."))
        b_paste.clicked.connect(lambda: self.answer.setPlainText(QGuiApplication.clipboard().text()))
        b_check.clicked.connect(self.check)
        self.b_fix.clicked.connect(lambda: self._copy(self.fix_text, "수정 요청을 복사했습니다. AI 에게 보내고 새 답변을 다시 붙여넣으세요."))
        self.b_open.clicked.connect(self._open)
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
        self.prompt.setPlainText(build_prompt(self.entries, self.request.toPlainText(), self.w.value(), self.h.value(),
                                              self.fps.value(), self.target.value() or None, schema))

    def check(self):
        self.issues.clear()
        self.parsed = None
        self.b_open.setEnabled(False)
        folder = self._folder()
        if not folder:
            return
        names = [e.name for e in (self.entries or scan_folder(folder))]
        try:
            data = extract_json(self.answer.toPlainText())
        except JsonExtractError as e:
            self._add(f"[오류] {e}", "#ff6b6b")
            self.fix_text = correction_prompt([], names, parse_error=str(e))
            self.b_fix.setEnabled(True)
            return
        issues = validate_project(data, folder)
        if not issues:
            self._add("✔ 문제 없음! '이 프로젝트 열기'를 누르세요.", "#3fa36b")
        for i in issues:
            self._add(str(i), "#ff6b6b" if i.severity == "error" else "#ffd166")
        self.fix_text = correction_prompt(issues, names) if issues else ""
        self.b_fix.setEnabled(bool(issues))
        if not has_errors(issues):
            self.parsed = data
            self.b_open.setEnabled(True)
            n = len(data.get("clips", []))
            self._add(f"클립 {n}개를 읽었습니다.", "#9aa0aa")

    def _add(self, text, color):
        it = QListWidgetItem(text)
        it.setForeground(QColor(color))
        self.issues.addItem(it)

    def _open(self):
        if self.parsed is None or self.folder is None:
            return
        self.project_ready.emit(Project.from_dict(self.parsed, base_dir=self.folder), self.folder)
        self.accept()
