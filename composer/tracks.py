"""트랙(행) 켜고 끄기 — 👁 감추기 · 🔇 소리 끄기 · S 솔로 · 🔒 잠금 (Qt 없음).

* 오디오 트랙: 클립(원본 소리), 더빙, 배경음악. 솔로가 하나라도 켜져 있으면 솔로 켠 트랙만 들림.
  소리 끄기는 솔로보다 우선(솔로+소리 끄기 = 안 들림).
* 감추기: 클립 → 화면을 배경색(검정)으로, 자막 → 화면 표시·새기기·소프트 자막 모두 없음.
* 잠금: 화면에서 그 트랙 항목의 편집·끌기를 막음 (내보내기에는 영향 없음).
"""
from __future__ import annotations

from .schema import Project

AUDIO_TRACKS = ("clips", "dubbing", "bgm")
# 트랙마다 쓸 수 있는 단추
TRACK_FLAGS = {
    "clips": ("hidden", "muted", "solo", "locked"),
    "subtitle": ("hidden", "locked"),
    "dubbing": ("muted", "solo", "locked"),
    "bgm": ("muted", "solo", "locked"),
}
TRACK_LABELS = {"clips": "클립", "subtitle": "자막", "dubbing": "더빙", "bgm": "배경음악"}
# 타임라인 항목 종류 → 트랙
KIND_TRACK = {"clip": "clips", "subtitle": "subtitle", "take": "dubbing", "bgm": "bgm"}


def any_solo(project: Project) -> bool:
    return any(project.tracks.get(k).solo for k in AUDIO_TRACKS)


def audible(project: Project, key: str) -> bool:
    ts = project.tracks.get(key)
    if ts.muted:
        return False
    return ts.solo or not any_solo(project)


def visible(project: Project, key: str) -> bool:
    return not project.tracks.get(key).hidden


def locked(project: Project, key: str) -> bool:
    return project.tracks.get(KIND_TRACK.get(key, key)).locked


def dimmed(project: Project, key: str) -> bool:
    """타임라인에서 흐리게 그릴지 (감춤 / 안 들림)."""
    if key == "subtitle":
        return not visible(project, key)
    if key == "clips":
        return not visible(project, key) and not audible(project, key)
    return not audible(project, key)


def off_tracks(project: Project) -> list[str]:
    """내보내기 경고용: 실제로 결과에 영향이 있는 꺼진 트랙 설명."""
    out = []
    p = project
    if p.clips and not visible(p, "clips"):
        out.append("클립 화면 감춤(검은 화면)")
    if p.clips and not audible(p, "clips"):
        out.append("클립 원본 소리 꺼짐")
    from .subtitles import has_subtitles
    if has_subtitles(p) and not visible(p, "subtitle"):
        out.append("자막 감춤")
    if p.dubbing.takes and not audible(p, "dubbing"):
        out.append("더빙 소리 꺼짐")
    if p.bgm.file and not audible(p, "bgm"):
        out.append("배경음악 소리 꺼짐")
    return out


def effective_project(project: Project) -> Project:
    """감추기·소리 끄기·솔로를 적용한 복사본 (내보내기용). 클립 감추기는 렌더러가 tracks.clips.hidden 을 봄."""
    p = project.copy()
    if not audible(p, "clips"):
        for c in p.clips:
            c.mute = True
    if not visible(p, "subtitle"):
        from .subtitles import clear_subtitles
        clear_subtitles(p)  # 자막에 붙은 더빙은 제자리에 고정
    if not audible(p, "dubbing"):
        p.dubbing.takes = []
    if not audible(p, "bgm"):
        p.bgm.file = None
    # 적용했으니 음소거/솔로 표시는 지움 (감추기 hidden 은 렌더러용으로 남김)
    for k in AUDIO_TRACKS:
        ts = p.tracks.get(k)
        ts.muted = ts.solo = False
    p.tracks.subtitle.hidden = False
    return p
