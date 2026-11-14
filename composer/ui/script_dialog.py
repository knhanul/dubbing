"""'대본 출력' 창: 자막으로 대본(표)을 만들어 미리 보고 인쇄 / PDF / DOCX / TXT / 클립보드로 내보냅니다."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMarginsF, QMimeData, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QPageLayout, QPageSize, QTextDocument
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QSplitter, QTextBrowser, QVBoxLayout, QWidget)

from ..script import COLUMN_LABELS, COLUMNS, TIME_FORMATS, Script, ScriptOptions, script_from_file, \
    script_from_project, to_docx, to_html, to_text, to_txt_file
from ..schema import Project

KOREAN_FONTS = ["Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "Noto Sans KR", "Apple SD Gothic Neo", "Nanum Gothic",
                "NanumGothic", "나눔고딕", "Gothic A1", "Gulim", "굴림", "Dotum", "돋움", "UnDotum"]


def korean_font() -> str:
    """인쇄·PDF 용 한글 글꼴 (맑은 고딕 → Noto CJK → 나눔고딕 … 순으로 설치된 것)."""
    fams = set(QFontDatabase.families())
    for f in KOREAN_FONTS:
        if f in fams:
            return f
    for f in QFontDatabase.families(QFontDatabase.Korean):
        return f
    return QFont().family()


def make_document(script: Script, opts: ScriptOptions) -> QTextDocument:
    fam = korean_font()
    doc = QTextDocument()
    f = QFont(fam)
    f.setPointSize(opts.font_size)
    doc.setDefaultFont(f)
    doc.setHtml(to_html(script, opts, fam))
    return doc


def _a4_printer(printer, opts: Optional[ScriptOptions] = None):
    """A4, 칸이 많으면(7개 이상) 가로 방향."""
    orient = QPageLayout.Landscape if opts is not None and len(opts.columns) >= 7 else QPageLayout.Portrait
    printer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), orient, QMarginsF(12, 12, 12, 12),
                                      QPageLayout.Millimeter))
    return printer


def save_pdf(script: Script, opts: ScriptOptions, path: Path) -> Path:
    """PDF 저장. Qt 가 글꼴을 PDF 안에 넣으므로(서브셋) 다른 PC 에서도 한글이 보입니다."""
    from PySide6.QtPrintSupport import QPrinter
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setDocName(script.title)
    printer.setCreator("영상 합성기")
    _a4_printer(printer, opts)
    make_document(script, opts).print_(printer)
    return Path(path)


class ScriptDialog(QDialog):
    def __init__(self, project: Project, title: str = "", parent=None, takes_ready: bool = True):
        super().__init__(parent)
        self.setWindowTitle("대본 출력")
        self.resize(1100, 720)
        self.project = project
        self.external: Optional[Path] = None
        self.script: Optional[Script] = None
        self.last_saved: Optional[Path] = None
        # 테스트에서 바꿔 끼울 수 있는 함수
        self.ask_save_path = self._ask_save_path
        self.ask_open_srt = self._ask_open_srt
        self.info = lambda text: QMessageBox.information(self, "대본 출력", text)

        # ---------------- 왼쪽: 설정
        left = QWidget()
        lf = QVBoxLayout(left)
        g_src = QGroupBox("자막")
        fs = QFormLayout(g_src)
        self.source = QComboBox()
        self.source.addItem("프로젝트 자막 (미세 조정·전체 오프셋 반영)", "project")
        self.source.addItem("다른 SRT/VTT 파일…", "file")
        self.lbl_src = QLabel()
        self.lbl_src.setWordWrap(True)
        self.btn_srt = QPushButton("SRT 파일 선택…")
        fs.addRow("가져올 곳", self.source)
        fs.addRow("", self.btn_srt)
        fs.addRow("", self.lbl_src)
        lf.addWidget(g_src)

        g_opt = QGroupBox("모양")
        fo = QFormLayout(g_opt)
        self.title = QLineEdit(title or project.title or "대본")
        self.font_size = QSpinBox(); self.font_size.setRange(6, 24); self.font_size.setValue(11); self.font_size.setSuffix(" pt")
        self.time_format = QComboBox()
        for k, v in TIME_FORMATS.items():
            self.time_format.addItem(v, k)
        fo.addRow("제목", self.title)
        fo.addRow("글자 크기", self.font_size)
        fo.addRow("시간 형식", self.time_format)
        lf.addWidget(g_opt)

        g_col = QGroupBox("넣을 칸")
        fc = QVBoxLayout(g_col)
        self.col_checks: dict[str, QCheckBox] = {}
        labels = dict(COLUMN_LABELS, index="번호", start="시작 시간", end="끝 시간", duration="길이", text="대본 내용",
                      recorded="녹음 상태 ✔", clip="클립 구간 (어느 클립에 나오는지)", speed="읽기 속도 (글자/초)",
                      memo="메모 칸 (빈 칸)")
        default_on = {"index", "start", "end", "duration", "text", "recorded"}
        for c in COLUMNS:
            cb = QCheckBox(labels[c])
            cb.setChecked(c in default_on)
            self.col_checks[c] = cb
            fc.addWidget(cb)
        r = QHBoxLayout()
        r.addWidget(QLabel("  빠르다고 표시:"))
        self.speed_limit = QDoubleSpinBox(); self.speed_limit.setRange(1, 30); self.speed_limit.setValue(7.0)
        self.speed_limit.setSingleStep(0.5); self.speed_limit.setSuffix(" 자/초 초과")
        r.addWidget(self.speed_limit); r.addStretch(1)
        fc.addLayout(r)
        lf.addWidget(g_col)
        lf.addStretch(1)

        # ---------------- 오른쪽: 미리보기
        self.preview = QTextBrowser()
        self.preview.setOpenLinks(False)
        self.preview.setStyleSheet("QTextBrowser{background:#ffffff;color:#000000;}")
        self.lbl_summary = QLabel()
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("<b>미리보기</b>"))
        rl.addWidget(self.preview, 1)
        rl.addWidget(self.lbl_summary)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left); split.addWidget(right)
        split.setSizes([340, 760])

        # ---------------- 아래: 내보내기 버튼
        self.btn_print = QPushButton("🖨 인쇄…")
        self.btn_preview = QPushButton("인쇄 미리 보기…")
        self.btn_pdf = QPushButton("PDF 저장…")
        self.btn_docx = QPushButton("Word(DOCX) 저장…")
        self.btn_txt = QPushButton("TXT 저장…")
        self.btn_copy = QPushButton("📋 클립보드에 복사")
        self.btn_close = QPushButton("닫기")
        row = QHBoxLayout()
        for b in (self.btn_print, self.btn_preview, self.btn_pdf, self.btn_docx, self.btn_txt, self.btn_copy):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_close)

        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(row)

        # ---------------- 연결
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh)
        for w in (self.title,):
            w.textChanged.connect(lambda *_: self._timer.start())
        for w in (self.font_size, self.speed_limit):
            w.valueChanged.connect(lambda *_: self._timer.start())
        self.time_format.currentIndexChanged.connect(lambda *_: self.refresh())
        for cb in self.col_checks.values():
            cb.toggled.connect(lambda *_: self.refresh())
        self.source.currentIndexChanged.connect(self._source_changed)
        self.btn_srt.clicked.connect(self.pick_srt)
        self.btn_print.clicked.connect(self.print_script)
        self.btn_preview.clicked.connect(self.print_preview)
        self.btn_pdf.clicked.connect(lambda: self.export("pdf"))
        self.btn_docx.clicked.connect(lambda: self.export("docx"))
        self.btn_txt.clicked.connect(lambda: self.export("txt"))
        self.btn_copy.clicked.connect(self.copy_to_clipboard)
        self.btn_close.clicked.connect(self.accept)

        from ..subtitles import has_subtitles
        if not has_subtitles(project):
            self.source.setItemText(0, "프로젝트 자막 (없음)")
            self.source.setCurrentIndex(1)  # 자막이 없으면 파일에서
        self._source_changed()

    # ------------------------------------------------------------------
    def options(self) -> ScriptOptions:
        return ScriptOptions(title=self.title.text().strip() or "대본",
                             columns=[c for c, cb in self.col_checks.items() if cb.isChecked()],
                             font_size=self.font_size.value(), time_format=self.time_format.currentData(),
                             speed_limit=self.speed_limit.value())

    def _source_changed(self, *_):
        is_file = self.source.currentData() == "file"
        self.btn_srt.setVisible(is_file)
        self.col_checks["recorded"].setEnabled(not is_file)
        self.refresh()

    def _ask_open_srt(self) -> Optional[Path]:
        f, _ = QFileDialog.getOpenFileName(self, "대본으로 만들 자막 파일", str(self.project.base_dir), "자막 (*.srt *.vtt)")
        return Path(f) if f else None

    def pick_srt(self):
        p = self.ask_open_srt()
        if p:
            self.external = p
            if self.title.text() in ("", "대본", self.project.title):
                self.title.setText(p.stem)
            self.refresh()

    def build(self) -> Optional[Script]:
        opts = self.options()
        if self.source.currentData() == "file":
            if not self.external:
                return None
            return script_from_file(self.external, opts.title, self.project)
        return script_from_project(self.project, opts.title)

    def refresh(self):
        opts = self.options()
        try:
            self.script = self.build()
        except Exception as e:  # noqa: BLE001
            self.script = None
            self.lbl_src.setText(f"❌ 자막을 읽지 못했습니다: {e}")
        if self.source.currentData() == "file":
            if not self.external:
                self.lbl_src.setText("SRT/VTT 파일을 고르세요.")
            elif self.script is not None:
                self.lbl_src.setText(f"파일: {self.external.name}")
        else:
            self.lbl_src.setText("")
        has = bool(self.script and self.script.lines)
        for b in (self.btn_print, self.btn_preview, self.btn_pdf, self.btn_docx, self.btn_txt, self.btn_copy):
            b.setEnabled(has)
        if not has:
            self.preview.setHtml("<p style='color:#666'>대본으로 만들 자막이 없습니다. "
                                 "프로젝트에 자막을 넣거나 'SRT 파일 선택…'으로 고르세요.</p>")
            self.lbl_summary.setText("")
            return
        sb = self.preview.verticalScrollBar().value()
        self.preview.setDocument(make_document(self.script, opts))
        self.preview.verticalScrollBar().setValue(sb)
        from ..script import summary
        self.lbl_summary.setText(summary(self.script, opts))

    # ------------------------------------------------------------------ 내보내기
    def _ask_save_path(self, kind: str) -> Optional[Path]:
        filt = {"pdf": "PDF (*.pdf)", "docx": "Word 문서 (*.docx)", "txt": "텍스트 (*.txt)"}[kind]
        default = Path(self.project.base_dir) / f"{self.options().title}.{kind}"
        f, _ = QFileDialog.getSaveFileName(self, "대본 저장", str(default), filt)
        if not f:
            return None
        p = Path(f)
        return p if p.suffix.lower() == f".{kind}" else p.with_suffix(f".{kind}")

    def export(self, kind: str) -> Optional[Path]:
        if not (self.script and self.script.lines):
            return None
        path = self.ask_save_path(kind)
        if not path:
            return None
        opts = self.options()
        try:
            if kind == "pdf":
                save_pdf(self.script, opts, path)
            elif kind == "docx":
                to_docx(self.script, opts, path)
            else:
                to_txt_file(self.script, opts, path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "대본 출력", f"저장하지 못했습니다:\n{e}")
            return None
        self.last_saved = path
        self.info(f"저장했습니다:\n{path}")
        return path

    def copy_to_clipboard(self):
        if not (self.script and self.script.lines):
            return
        opts = self.options()
        md = QMimeData()
        md.setText(to_text(self.script, opts))
        md.setHtml(to_html(self.script, opts, korean_font()))
        QApplication.clipboard().setMimeData(md)
        self.btn_copy.setText("✔ 복사했습니다")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText("📋 클립보드에 복사"))

    def print_script(self):
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        printer = _a4_printer(QPrinter(QPrinter.HighResolution), self.options())
        printer.setDocName(self.options().title)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("대본 인쇄")
        if dlg.exec() == QDialog.Accepted:
            make_document(self.script, self.options()).print_(printer)

    def print_preview(self):
        from PySide6.QtPrintSupport import QPrinter, QPrintPreviewDialog
        printer = _a4_printer(QPrinter(QPrinter.HighResolution), self.options())
        dlg = QPrintPreviewDialog(printer, self)
        dlg.setWindowTitle("대본 인쇄 미리 보기")
        doc = make_document(self.script, self.options())
        dlg.paintRequested.connect(doc.print_)
        dlg.exec()
