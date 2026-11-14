"""더빙(녹음한 목소리)으로 자막 만들기 — Qt 없음.

1. 소리에서 '말한 구간'의 시간을 얻습니다.
   * faster-whisper(로컬 음성 인식, 한국어): 단어마다 시작/끝 시간 + 인식한 글
   * 대안: ffmpeg silencedetect 로 무음 구간을 찾아 그 사이를 말한 구간으로 (글은 모름)
2. 대본이 있으면 대본 문장을 그대로 자막 글로 쓰고, 시간만 인식 결과에서 가져옵니다
   (공백·문장부호를 뺀 글자 단위로 difflib 정렬). 대본이 없으면 인식한 글을 그대로 씁니다.
3. 문장/문장부호 기준으로 자막 줄을 나누고, 줄당 최대 글자 수·최대 줄 수를 넘으면 더 나눕니다.
   최소 표시 시간을 지키고 다음 자막과 겹치지 않게 맞춥니다.
"""
from __future__ import annotations

import difflib
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .probe import ffmpeg_path, popen_kwargs, probe
from .subtitles import Cue

ProgressFn = Callable[[float, str], None]

WHISPER_MODELS = {  # 이름: (Hugging Face 저장소, 대략 내려받기 크기)
    "tiny": ("Systran/faster-whisper-tiny", "약 75MB"),
    "base": ("Systran/faster-whisper-base", "약 145MB"),
    "small": ("Systran/faster-whisper-small", "약 485MB"),
    "medium": ("Systran/faster-whisper-medium", "약 1.5GB"),
}
DEFAULT_MODEL = "small"
SENT_END = ".?!。？！…"


class Cancelled(Exception):
    pass


class EngineUnavailable(RuntimeError):
    pass


@dataclass
class Word:
    start: float
    end: float
    text: str = ""


@dataclass
class AutoSubOptions:
    engine: str = "auto"  # auto | whisper | silence
    model: str = DEFAULT_MODEL
    language: str = "ko"
    max_chars: int = 20  # 줄당 글자 수
    max_lines: int = 2  # 자막 하나의 줄 수
    min_duration: float = 1.0  # 최소 표시 시간(초)
    gap: float = 0.05  # 다음 자막과의 최소 간격
    linger: float = 0.3  # 말이 끝난 뒤 조금 더 보여 주기
    noise_db: float = -35.0  # 무음 감지 기준
    min_silence: float = 0.35  # 이만큼 조용하면 끊음


@dataclass
class CueInfo:
    conf: Optional[float] = None  # 0~1, LOW_CONF(0.6) 미만이면 노란색
    heard: str = ""  # 음성 인식이 알아들은 글
    reason: str = ""  # 신뢰도가 낮은 까닭

    @property
    def low(self) -> bool:
        from .cue_edit import LOW_CONF
        return self.conf is not None and self.conf < LOW_CONF


@dataclass
class AutoSubResult:
    cues: list[Cue]
    engine: str  # whisper | silence
    note: str = ""
    recognized: str = ""  # 인식한 글 (대본과 비교용)
    speech_end: float = 0.0  # 마지막 말 끝(타임라인 초)
    words: list[Word] = field(default_factory=list)
    infos: list[CueInfo] = field(default_factory=list)  # cues 와 같은 순서
    regions: list[tuple[float, float]] = field(default_factory=list)  # 말한 구간(타임라인 초, 무음 감지)

    @property
    def low_count(self) -> int:
        from .cue_edit import LOW_CONF
        return sum(1 for i in self.infos if i.conf is not None and i.conf < LOW_CONF)


# ------------------------------------------------------------------ 글 다루기
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def norm_chars(text: str) -> list[tuple[str, int]]:
    """정렬용: 공백·문장부호를 뺀 (글자, 원래 위치) 목록. 영문은 소문자."""
    return [(ch.lower(), i) for i, ch in enumerate(text) if not _PUNCT.fullmatch(ch)]


def split_sentences(text: str) -> list[str]:
    """줄바꿈과 문장 끝 부호(. ? ! … 등) 기준으로 문장을 나눕니다."""
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        buf = ""
        i = 0
        while i < len(line):
            buf += line[i]
            if line[i] in SENT_END:
                while i + 1 < len(line) and (line[i + 1] in SENT_END or line[i + 1] in "\"'”’)」』"):
                    i += 1
                    buf += line[i]
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
            i += 1
        if buf.strip():
            out.append(buf.strip())
    return out


def _wrap_lines(sentence: str, max_chars: int) -> list[str]:
    """한 문장을 max_chars 이하의 줄들로 (띄어쓰기에서 끊고, 쉼표 뒤를 먼저 고려, 너무 긴 낱말은 자름)."""
    words = sentence.split()
    lines, cur = [], ""
    for w in words:
        while len(w) > max_chars:  # 띄어쓰기 없는 긴 글
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:max_chars])
            w = w[max_chars:]
        cand = f"{cur} {w}" if cur else w
        if len(cand) <= max_chars:
            cur = cand
            if cur.endswith((",", "，", "、")) and len(cur) >= max_chars * 0.6:
                lines.append(cur)  # 쉼표에서 끊으면 읽기 편함
                cur = ""
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def wrap_cue_texts(sentence: str, max_chars: int = 20, max_lines: int = 2) -> list[str]:
    """문장 하나 → 자막 글 목록 (각각 최대 max_lines 줄, 줄은 '\\n' 으로)."""
    max_chars = max(4, int(max_chars))
    max_lines = max(1, int(max_lines))
    lines = _wrap_lines(sentence.strip(), max_chars)
    if not lines:
        return []
    groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
    # 마지막 자막이 한 줄 짜리로 너무 짧으면 앞에서 한 줄 넘겨 받아 균형
    if len(groups) >= 2 and len(groups[-1]) == 1 and max_lines >= 2 and len(groups[-2]) == max_lines:
        if len(groups[-1][0]) < max_chars * 0.4:
            groups[-1].insert(0, groups[-2].pop())
    return ["\n".join(g) for g in groups if g]


def script_chunks(script: str, max_chars: int = 20, max_lines: int = 2) -> list[str]:
    return [t for t, _, _ in script_chunks_marked(script, max_chars, max_lines)]


def script_chunks_marked(script: str, max_chars: int = 20, max_lines: int = 2) -> list[tuple[str, bool, bool]]:
    """(자막 글, 문장의 시작인가, 문장의 끝인가). 문장 경계에서는 보통 숨을 쉬므로 무음 경계와 맞아야 함."""
    out = []
    for s in split_sentences(script):
        parts = wrap_cue_texts(s, max_chars, max_lines)
        for k, t in enumerate(parts):
            out.append((t, k == 0, k == len(parts) - 1))
    return out


# ------------------------------------------------------------------ 정렬
def char_times(words: list[Word]) -> list[tuple[str, float, float]]:
    """인식한 단어들 → 글자마다 (글자, 시작, 끝). 단어 시간을 글자 수로 고르게 나눔."""
    out = []
    for w in words:
        chars = [c for c, _ in norm_chars(w.text)]
        if not chars:
            continue
        step = max(0.0, w.end - w.start) / len(chars)
        for k, c in enumerate(chars):
            out.append((c, w.start + k * step, w.start + (k + 1) * step))
    return out


def align_chunks(chunks: list[str], words: list[Word]) -> list[tuple[float, float]]:
    """대본 자막 글(chunks)마다 (시작, 끝) — 인식 결과와 글자 단위로 맞춘 시간."""
    rec = char_times(words)
    sc: list[tuple[str, int]] = []  # (글자, 몇 번째 chunk)
    for ci, ch in enumerate(chunks):
        sc.extend((c, ci) for c, _ in norm_chars(ch))
    if not sc:
        return []
    n = len(sc)
    st: list[Optional[float]] = [None] * n
    en: list[Optional[float]] = [None] * n
    if rec:
        sm = difflib.SequenceMatcher(None, [c for c, _ in sc], [c for c, _, _ in rec], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    st[i1 + k], en[i1 + k] = rec[j1 + k][1], rec[j1 + k][2]
            elif tag == "replace":  # 다르게 인식된 부분: 구간을 비율로 나눔
                t0, t1 = rec[j1][1], rec[j2 - 1][2]
                m = i2 - i1
                for k in range(m):
                    st[i1 + k] = t0 + (t1 - t0) * k / m
                    en[i1 + k] = t0 + (t1 - t0) * (k + 1) / m
    _fill_gaps(st, en)
    res = []
    for ci in range(len(chunks)):
        idx = [i for i in range(n) if sc[i][1] == ci]
        if not idx:  # 글자가 없는 chunk (부호만)
            prev = res[-1][1] if res else (st[0] or 0.0)
            res.append((prev, prev))
            continue
        res.append((st[idx[0]], en[idx[-1]]))
    return res


def align_detail(chunks: list[str], words: list[Word]) -> list[tuple[float, float, float, str]]:
    """align_chunks + 자막마다 (인식 일치율 0~1, 알아들은 글)."""
    times = align_chunks(chunks, words)
    rec: list[tuple[str, int]] = []  # (글자, 몇 번째 단어)
    for wi, w in enumerate(words):
        rec.extend((c, wi) for c, _ in norm_chars(w.text))
    sc: list[int] = []  # 글자마다 몇 번째 chunk
    for ci, ch in enumerate(chunks):
        sc.extend(ci for _ in norm_chars(ch))
    matched = [0] * len(chunks)
    votes: list[dict] = [dict() for _ in chunks]  # chunk → {단어: 글자 수}
    if rec and sc:
        sm = difflib.SequenceMatcher(None, [c for c, _ in _sc_chars(chunks)], [c for c, _ in rec], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            for j in range(j1, j2):
                if tag == "equal":
                    ci = sc[i1 + (j - j1)]
                    matched[ci] += 1
                elif tag == "replace":
                    ci = sc[min(i2 - 1, i1 + (j - j1) * (i2 - i1) // max(1, j2 - j1))]
                elif tag == "insert":  # 대본에 없는 말: 앞 자막에 붙임
                    ci = sc[max(0, min(len(sc) - 1, i1 - 1))]
                else:
                    continue
                wi = rec[j][1]
                votes[ci][wi] = votes[ci].get(wi, 0) + 1
    owner: dict[int, int] = {}
    for ci, v in enumerate(votes):
        for wi, n in v.items():
            if wi not in owner or n > votes[owner[wi]].get(wi, 0):
                owner[wi] = ci
    heard = [" ".join(words[wi].text for wi in sorted(w for w, c in owner.items() if c == ci)) for ci in range(len(chunks))]
    out = []
    for ci, (s, e) in enumerate(times):
        n = len(norm_chars(chunks[ci]))
        out.append((s, e, (matched[ci] / n) if n else 1.0, heard[ci]))
    return out


def _sc_chars(chunks: list[str]) -> list[tuple[str, int]]:
    out = []
    for ci, ch in enumerate(chunks):
        out.extend((c, ci) for c, _ in norm_chars(ch))
    return out


SNAP_TOL = 0.4  # 음성 인식 시각과 무음 경계가 이만큼 안이면 경계에 붙임(초)


def hybrid_snap(aligned: list[tuple[float, float, float, str]], marks: list[tuple[str, bool, bool]],
                regions: list[tuple[float, float]], tol: float = SNAP_TOL) -> list[tuple[float, float, CueInfo]]:
    """음성 인식 시각 + 무음 경계 섞기: 문장 시작/끝은 가까운 '말 시작/말 끝'에 붙이고,
    붙을 곳이 없거나(두 방식이 어긋남) 인식 일치율이 낮으면 신뢰도를 낮춤."""
    from .cue_edit import snap
    starts, ends = [s for s, _ in regions], [e for _, e in regions]
    out = []
    for (s, e, ratio, heard), (_, st_mark, en_mark) in zip(aligned, marks):
        conf, why = ratio, []
        if ratio < 0.6:
            why.append(f"인식 일치 {ratio:.0%}")
        if regions:
            if st_mark:
                s2 = snap(s, starts, tol)
                if s2 == s and (not starts or min(abs(x - s) for x in starts) > tol):
                    conf *= 0.55  # 두 방식이 어긋남 → 노란색
                    why.append(f"시작이 무음 경계와 {min(abs(x - s) for x in starts):.1f}초 어긋남")
                s = s2
            if en_mark:
                e2 = snap(e, ends, tol)
                if e2 == e and (not ends or min(abs(x - e) for x in ends) > tol):
                    conf *= 0.55
                    why.append(f"끝이 무음 경계와 {min(abs(x - e) for x in ends):.1f}초 어긋남")
                e = e2
        if e <= s:
            e = s + 0.2
        out.append((s, e, CueInfo(round(conf, 2), heard, ", ".join(why))))
    return out


def distribute_by_silence(regions: list[tuple[float, float]], marks: list[tuple[str, bool, bool]],
                          gap: float = 0.05) -> list[tuple[float, float, CueInfo]]:
    """음성 인식 없이: 말한 구간들에 대본 자막을 순서대로 배분.
    개수가 같으면 1:1, 다르면 글자 수 비례 위치에서 가까운 쉬는 곳(무음)을 골라 합치거나, 쉬는 곳이 없으면 구간 안을 나눔."""
    chunks = [t for t, _, _ in marks]
    n = len(chunks)
    if not n:
        return []
    if not regions:
        return [(0.0, 0.0, CueInfo(0.0, "", "말소리를 못 찾음")) for _ in chunks]
    lens = [max(1, len(norm_chars(c))) for c in chunks]
    if len(regions) == n:
        rates = [(e - s) / L for (s, e), L in zip(regions, lens)]
        med = sorted(rates)[len(rates) // 2] or 1e-6
        out = []
        for (s, e), r in zip(regions, rates):
            odd = not (0.4 <= r / med <= 2.5)
            out.append((s, e, CueInfo(0.5 if odd else 0.8, "", "말 길이와 글 길이가 잘 안 맞음" if odd else "")))
        return out
    total_chars = sum(lens)
    durs = [e - s for s, e in regions]
    total = sum(durs)
    cum_r, acc = [], 0.0  # 쉬는 곳 k(구간 k 뒤)의 '말한 시간 축' 위치
    for d in durs[:-1]:
        acc += d
        cum_r.append(acc)

    def at(x: float) -> float:
        a = 0.0
        for (s, e), d in zip(regions, durs):
            if x <= a + d + 1e-9:
                return s + (x - a)
            a += d
        return regions[-1][1]

    starts = [regions[0][0]] + [0.0] * (n - 1)
    ends = [0.0] * (n - 1) + [regions[-1][1]]
    on_silence = [True] * (n + 1)  # 경계 b(0..n): 자막 b-1 과 b 사이
    used = -1
    acc_c = 0
    for b in range(1, n):
        acc_c += lens[b - 1]
        x = total * acc_c / total_chars
        tol = 0.5 * min(total * lens[b - 1] / total_chars, total * lens[b] / total_chars)
        cand = [k for k in range(used + 1, len(cum_r)) if abs(cum_r[k] - x) <= tol]
        # 남은 자막 수만큼 쉬는 곳을 남겨 두지 않아도 됨(모자라면 구간 안을 나눔)
        if cand:
            k = min(cand, key=lambda k: abs(cum_r[k] - x))
            used = k
            ends[b - 1], starts[b] = regions[k][1], regions[k + 1][0]
        else:
            p = at(x)
            if p <= starts[b - 1] + 0.2:
                p = starts[b - 1] + 0.2
            ends[b - 1], starts[b] = p - gap / 2, p + gap / 2
            on_silence[b] = False
    out = []
    for j in range(n):
        conf, why = 0.75, ""
        for b, mark in ((j, marks[j][1]), (j + 1, marks[j][2])):
            if 0 < b < n and not on_silence[b]:
                if mark:  # 문장 사이인데 쉬는 곳이 없음 → 확인 필요
                    conf, why = min(conf, 0.45), "문장 사이에 쉬는 곳이 없어 글자 수로 나눔"
                else:  # 한 문장을 줄 길이로 나눈 곳 → 보통 쉬지 않음
                    conf = min(conf, 0.65)
        out.append((starts[j], max(ends[j], starts[j] + 0.2), CueInfo(conf, "", why)))
    return out


def _fill_gaps(st: list, en: list):
    """맞춰지지 않은 글자(인식 못한 부분)는 앞뒤 맞춘 글자 사이에 고르게 끼워 넣음."""
    n = len(st)
    known = [i for i in range(n) if st[i] is not None]
    if not known:
        for i in range(n):
            st[i], en[i] = 0.0, 0.0
        return
    first, last = known[0], known[-1]
    for i in range(first):
        st[i] = en[i] = st[first]
    for i in range(last + 1, n):
        st[i] = en[i] = en[last]
    i = first
    while i < last:
        j = i + 1
        while st[j] is None:
            j += 1
        if j > i + 1:
            a, b = en[i], st[j]
            m = j - i - 1
            for k in range(1, j - i):
                st[i + k] = a + (b - a) * (k - 1) / m
                en[i + k] = a + (b - a) * k / m
        i = j


def proportional_words(regions: list[tuple[float, float]], chunks: list[str]) -> list[tuple[float, float]]:
    """글을 모를 때(무음 감지): 말한 구간 수 == 자막 수면 1:1, 아니면 말한 시간 전체를 글자 수 비율로 나눔."""
    if not chunks:
        return []
    if not regions:
        return [(0.0, 0.0)] * len(chunks)
    if len(regions) == len(chunks):
        return list(regions)
    lens = [max(1, len(norm_chars(c))) for c in chunks]
    total_chars = sum(lens)
    total_speech = sum(e - s for s, e in regions)

    def at(x: float) -> float:  # 말한 시간 축의 x 초 → 실제 시각
        acc = 0.0
        for s, e in regions:
            d = e - s
            if x <= acc + d + 1e-9:
                return s + (x - acc)
            acc += d
        return regions[-1][1]

    out, acc = [], 0
    for L in lens:
        a = at(total_speech * acc / total_chars)
        acc += L
        b = at(total_speech * acc / total_chars)
        # 구간 경계에 걸쳐 끝나면 그 구간 끝에 맞춤(무음 쪽으로 늘어나지 않게)
        out.append((a, b))
    return out


def finalize(timed: list[tuple[str, float, float]], opts: AutoSubOptions) -> list[Cue]:
    """번호 매기기 + 최소 표시 시간 + 겹치지 않게."""
    return finalize_with_info([(t, s, e, None) for t, s, e in timed], opts)[0]


def finalize_with_info(timed, opts: AutoSubOptions) -> tuple[list[Cue], list]:
    """timed: [(글, 시작, 끝, 정보)] → (자막들, 같은 순서의 정보들)."""
    items = sorted([x for x in timed if x[0].strip()], key=lambda x: x[1])
    infos = [x[3] for x in items]
    items = [x[:3] for x in items]
    cues = []
    for k, (text, s, e) in enumerate(items):
        nxt = items[k + 1][1] if k + 1 < len(items) else None
        s = max(0.0, s)
        if cues and s < cues[-1].end + opts.gap:
            s = cues[-1].end + opts.gap
        e = max(e + opts.linger, s + opts.min_duration)
        if nxt is not None:
            e = min(e, max(s + 0.2, nxt - opts.gap))
        cues.append(Cue(k + 1, round(s, 3), round(max(e, s + 0.2), 3), text.strip()))
    return cues, infos


# ------------------------------------------------------------------ 무음 감지
def detect_speech(path: Path, noise_db: float = -35.0, min_silence: float = 0.35, min_speech: float = 0.15,
                  cancel: Optional[threading.Event] = None) -> list[tuple[float, float]]:
    """ffmpeg silencedetect: 소리 파일 안에서 말한 구간 [(시작, 끝)] (파일 기준 초)."""
    dur = probe(path).duration or 0.0
    args = [ffmpeg_path(), "-hide_banner", "-nostdin", "-i", str(path), "-af",
            f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **popen_kwargs())
    silences, start = [], None
    for line in proc.stderr:
        if cancel is not None and cancel.is_set():
            proc.kill()
            raise Cancelled()
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    proc.wait()
    if start is not None:
        silences.append((start, dur))
    speech, t = [], 0.0
    for s, e in silences:
        if s - t >= min_speech:
            speech.append((round(t, 3), round(s, 3)))
        t = e
    if dur - t >= min_speech:
        speech.append((round(t, 3), round(dur, 3)))
    return speech


# ------------------------------------------------------------------ faster-whisper
_MODEL_CACHE: dict = {}


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def models_dir() -> Path:
    from .subtitle_presets import data_dir
    return data_dir() / "whisper"


def model_downloaded(size: str) -> bool:
    d = models_dir() / size
    return (d / "model.bin").exists()


def _download(size: str, progress: ProgressFn, cancel: threading.Event) -> str:
    """처음 쓸 때 모델을 내려받습니다(진행률 표시). 이미 있으면 바로 경로."""
    repo, approx = WHISPER_MODELS.get(size, (size, ""))
    target = models_dir() / size
    if (target / "model.bin").exists():
        return str(target)
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm as _tqdm

    class _Bar(_tqdm):  # huggingface_hub 가 바이트 진행률을 여기로 알려 줌
        def update(self, n=1):
            if cancel.is_set():
                raise Cancelled()
            r = super().update(n)
            if self.total and getattr(self, "unit", "B") == "B":
                progress(min(0.99, self.n / self.total) * 0.3,
                         f"음성 인식 모델 내려받는 중 ({size}, {approx}) … {self.n / 1e6:.0f}/{self.total / 1e6:.0f}MB")
            return r

    progress(0.0, f"음성 인식 모델 내려받는 중 ({size}, {approx}) — 처음 한 번만")
    snapshot_download(repo, local_dir=str(target), tqdm_class=_Bar,
                      allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json",
                                      "vocabulary.*"])
    return str(target)


def _load_model(size: str, progress: ProgressFn, cancel: threading.Event):
    if size in _MODEL_CACHE:
        return _MODEL_CACHE[size]
    from faster_whisper import WhisperModel
    path = _download(size, progress, cancel)
    progress(0.3, "음성 인식 모델 불러오는 중…")
    model = None
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:  # NVIDIA GPU 가 있으면
            model = WhisperModel(path, device="cuda", compute_type="float16")
    except Exception:  # noqa: BLE001  (CUDA 라이브러리 없음 등 → CPU)
        model = None
    if model is None:
        model = WhisperModel(path, device="cpu", compute_type="int8")
    _MODEL_CACHE[size] = model
    return model


def transcribe(path: Path, opts: AutoSubOptions, progress: ProgressFn, cancel: threading.Event,
               p0: float = 0.0, p1: float = 1.0) -> list[Word]:
    """faster-whisper 로 단어 시간 얻기 (파일 기준 초). 진행률은 p0~p1 사이로 알림."""
    if not whisper_available():
        raise EngineUnavailable("faster-whisper 가 설치되어 있지 않습니다")
    model = _load_model(opts.model, lambda f, m: progress(p0 + (p1 - p0) * f, m), cancel)
    audio = load_audio_16k(path, cancel)  # PyAV 판 차이에 흔들리지 않게 프로그램의 ffmpeg 로 직접 읽음
    segments, info = model.transcribe(audio, language=opts.language or None, word_timestamps=True,
                                      vad_filter=True, beam_size=5)
    dur = info.duration or 1.0
    words = []
    for seg in segments:
        if cancel.is_set():
            raise Cancelled()
        ws = list(seg.words or [])
        if ws:
            words.extend(Word(float(w.start), float(w.end), w.word.strip()) for w in ws)
        else:
            words.append(Word(float(seg.start), float(seg.end), seg.text.strip()))
        progress(p0 + (p1 - p0) * (0.35 + 0.65 * min(1.0, seg.end / dur)), f"음성 인식 중… {fmt_t(seg.end)} / {fmt_t(dur)}")
        _sentence_marks(words, seg.text)
    return words


def load_audio_16k(path: Path, cancel: Optional[threading.Event] = None):
    """음성 인식용: 16kHz 모노 float32 numpy 배열."""
    import numpy as np
    args = [ffmpeg_path(), "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    res = subprocess.run(args, capture_output=True, **popen_kwargs())
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if res.returncode != 0:
        raise RuntimeError(f"소리 파일을 읽지 못했습니다: {res.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(res.stdout, np.float32).copy()


def mix_sources(sources: list[tuple[Path, float]], out: Path) -> Path:
    """더빙 녹음들을 타임라인 위치대로 한 파일(WAV)로 — '자막 맞추기' 화면에서 듣고 파형을 그리는 용도."""
    sources = [(Path(p), float(pos)) for p, pos in sources if Path(p).exists()]
    if not sources:
        raise ValueError("들을 녹음이 없습니다")
    args = [ffmpeg_path(), "-v", "error", "-y", "-nostdin"]
    for p, _ in sources:
        args += ["-i", str(p)]
    parts = []
    for k, (_, pos) in enumerate(sources):
        ms = max(0, int(round(pos * 1000)))
        parts.append(f"[{k}:a]aformat=sample_rates=22050:channel_layouts=mono,adelay={ms}:all=1[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(len(sources)))
    graph = ";".join(parts) + f";{mix}amix=inputs={len(sources)}:normalize=0:dropout_transition=0[out]"
    args += ["-filter_complex", graph, "-map", "[out]", "-ar", "22050", "-ac", "1", str(out)]
    res = subprocess.run(args, capture_output=True, **popen_kwargs())
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace")[-300:])
    return out


def wave_peaks(path: Path, per_sec: int = 100):
    """파형 그리기용: 1/per_sec 초마다 가장 큰 소리(0~1)."""
    import numpy as np
    rate = 8000
    args = [ffmpeg_path(), "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    res = subprocess.run(args, capture_output=True, **popen_kwargs())
    a = np.abs(np.frombuffer(res.stdout, np.float32)) if res.returncode == 0 else np.zeros(0, np.float32)
    step = rate // per_sec
    n = len(a) // step
    if n == 0:
        return np.zeros(0, np.float32)
    pk = a[:n * step].reshape(n, step).max(axis=1)
    top = float(np.percentile(pk, 99)) or 1.0
    return np.clip(pk / top, 0, 1).astype(np.float32)


def _sentence_marks(words: list[Word], seg_text: str):
    """세그먼트 끝에 문장부호가 있으면 마지막 단어에도 붙여 두기(대본 없을 때 문장 나누기용)."""
    t = seg_text.strip()
    if words and t and t[-1] in SENT_END and not words[-1].text.endswith(tuple(SENT_END)):
        words[-1].text += t[-1]


def fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


# ------------------------------------------------------------------ 전체
def make_subtitles(sources: list[tuple[Path, float]], script: str, opts: AutoSubOptions,
                   progress: Optional[ProgressFn] = None, cancel: Optional[threading.Event] = None) -> AutoSubResult:
    """sources: (소리 파일, 타임라인에서 그 파일이 시작하는 초). script: 대본(없으면 '')."""
    progress = progress or (lambda f, m: None)
    cancel = cancel or threading.Event()
    sources = sorted([(Path(p), float(pos)) for p, pos in sources if Path(p).exists()], key=lambda x: x[1])
    if not sources:
        raise ValueError("자막을 만들 녹음(더빙)이 없습니다.")
    script = (script or "").strip()
    note = ""
    engine = opts.engine
    words: list[Word] = []
    regions: list[tuple[float, float]] = []
    if engine in ("auto", "whisper"):
        try:
            n = len(sources)
            for k, (p, pos) in enumerate(sources):
                ws = transcribe(p, opts, progress, cancel, k / n * 0.95, (k + 1) / n * 0.95)
                words.extend(Word(w.start + pos, w.end + pos, w.text) for w in ws)
            engine = "whisper"
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if opts.engine == "whisper":
                raise
            note = (f"음성 인식을 쓸 수 없어 무음 구간으로 나눴습니다 ({e})." if not isinstance(e, EngineUnavailable)
                    else "음성 인식(faster-whisper)이 설치되어 있지 않아 무음 구간으로 나눴습니다.")
            engine = "silence"
            words = []
    if engine == "whisper" and opts.engine == "auto" and _poor_match(script, words):
        # 말소리를 거의 못 알아들었으면(작은 소리·잡음·다른 언어) 시간은 무음 구간에서
        note = ("음성 인식이 말을 거의 알아듣지 못해 무음 구간으로 시간을 맞췄습니다." if script else
                "음성 인식이 말을 알아듣지 못해 무음 구간으로 나눴습니다.")
        engine, words = "silence", []
    # 무음 감지는 늘 함께 (음성 인식 시각을 '말 시작/끝' 경계에 붙이고, 어긋나면 노란색 표시)
    base = 0.9 if engine == "silence" else 0.1
    p0 = 0.0 if engine == "silence" else 0.85
    for k, (p, pos) in enumerate(sources):
        progress(p0 + k / len(sources) * base, f"무음 구간 찾는 중… ({k + 1}/{len(sources)})")
        regions.extend((s + pos, e + pos) for s, e in detect_speech(p, opts.noise_db, opts.min_silence,
                                                                 cancel=cancel))
    regions.sort()
    progress(0.96, "자막 줄 나누는 중…")
    recognized = " ".join(w.text for w in words).strip()
    if engine == "whisper":
        marks = script_chunks_marked(script or _recognized_text(words), opts.max_chars, opts.max_lines)
        aligned = align_detail([t for t, _, _ in marks], words)
        if not script:  # 대본 없이 인식한 글을 그대로 쓰면 '인식 일치'는 의미 없음 → 경계만 봄
            aligned = [(s, e, 1.0, t) for (s, e, _, _), (t, _, _) in zip(aligned, marks)]
        timed = [(m[0], s, e, info) for m, (s, e, info) in zip(marks, hybrid_snap(aligned, marks, regions))]
    elif script:
        marks = script_chunks_marked(script, opts.max_chars, opts.max_lines)
        timed = [(m[0], s, e, info) for m, (s, e, info) in zip(marks, distribute_by_silence(regions, marks, opts.gap))]
    else:  # 대본도 없고 인식도 못함 → 말한 구간마다 빈 자막(나중에 글 넣기)
        timed = [(f"({k + 1}번 대사)", s, e, CueInfo(0.3, "", "글을 넣어 주세요")) for k, (s, e) in enumerate(regions)]
        if regions:
            note = (note + " " if note else "") + "대본이 없어 자막 글은 '(N번 대사)'로 넣었습니다. 자막 맞추기 화면에서 고쳐 주세요."
    cues, infos = finalize_with_info(timed, opts)
    end = max([w.end for w in words] + [e for _, e in regions] + [0.0])
    progress(1.0, f"자막 {len(cues)}줄을 만들었습니다")
    return AutoSubResult(cues, engine, note.strip(), recognized, end, words, infos, regions)


def _poor_match(script: str, words: list[Word]) -> bool:
    rec = "".join(c for c, _, _ in char_times(words))
    if not rec:
        return True
    if not script:
        return False
    sc = "".join(c for c, _ in norm_chars(script))
    if not sc:
        return False
    m = difflib.SequenceMatcher(None, sc, rec, autojunk=False)
    matched = sum(b.size for b in m.get_matching_blocks())
    return matched / len(sc) < 0.3


def _recognized_text(words: list[Word]) -> str:
    """인식한 단어 → 문장 부호/긴 쉼에서 줄바꿈한 글 (대본 대신 사용)."""
    out, prev_end = "", None
    for w in words:
        if prev_end is not None and w.start - prev_end > 0.8 and not out.endswith("\n"):
            out += "\n"  # 1초 가까이 쉬면 문장이 끝난 것으로
        out += ("" if out.endswith("\n") or not out else " ") + w.text
        if w.text.endswith(tuple(SENT_END)):
            out += "\n"
        prev_end = w.end
    return out


def script_from_file(path: Path) -> str:
    """대본 파일 읽기: .txt / .srt / .vtt / .docx"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".srt", ".vtt"):
        from .subtitles import load_subtitles
        return "\n".join(c.text.replace("\n", " ") for c in load_subtitles(path))
    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
    from .subtitles import decode_bytes
    return decode_bytes(path.read_bytes())
