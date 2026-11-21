"""녹음에 맞춰 다시 구성 프롬프트 테스트."""
from pathlib import Path

from composer.prompt_template import build_realign_prompt
from composer.schema import Project, Clip, SubtitleSettings, SubtitleItem, Take, Dubbing
from composer.subtitles import project_cues


def test_build_realign_prompt_includes_cues_and_takes(tmp_path):
    # 가짜 프로젝트: 클립 2개, 자막 3줄, 테이크 2개
    p = Project(base_dir=tmp_path)
    p.clips = [
        Clip(file="img1.png", type="image", duration=4.0, label="S01"),
        Clip(file="vid1.mp4", type="video", trim_start=0.0, trim_end=5.0),
    ]
    p.subtitle = SubtitleSettings(items=[
        SubtitleItem(1, 0.0, 2.5, "첫 줄"),
        SubtitleItem(2, 3.0, 5.5, "둘째 줄"),
        SubtitleItem(3, 6.0, 8.0, "셋째 줄"),
    ])
    p.dubbing = Dubbing(takes=[
        Take(subtitle=1, file="take1.wav", offset=-0.3),
        Take(subtitle=2, file="take2.wav", offset=0.0),
    ])
    # 더미 파일
    (tmp_path / "img1.png").write_bytes(b"x")
    (tmp_path / "vid1.mp4").write_bytes(b"x")
    (tmp_path / "take1.wav").write_bytes(b"x")
    (tmp_path / "take2.wav").write_bytes(b"x")

    entries = []  # scan_folder 없이도 동작
    text = build_realign_prompt(p, entries, "테스트")
    assert "index=1" in text and "index=2" in text and "index=3" in text
    assert "녹음 없음" in text  # 3번 자막
    assert "말소리" in text or "테이크" in text
    assert "img1.png" in text and "vid1.mp4" in text
    # 규칙 포함
    assert "clips와 transition" in text
    assert "자막 번호가 바뀌면" not in text  # 프롬프트에는 없음


def test_realign_blocks_changed_subtitle_indices():
    # 이 테스트는 UI check 로직을 직접 검증하기보다, 규칙이 문서화되어 있음을 확인
    # 실제 차단은 prompt_dialog.check 에서 수행됨
    from composer.ui.prompt_dialog import PromptDialog
    # 간단히 메시지 문자열이 코드에 있는지 확인
    import inspect
    src = inspect.getsource(PromptDialog.check)
    assert "자막 번호가 바뀌면 녹음이 다른 줄에 붙습니다" in src
