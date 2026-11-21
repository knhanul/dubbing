from composer.prompt_template import build_prompt, scan_folder
from composer.validate import extract_json


def test_scan_and_build_prompt(tmp_path):
    for n in ("01.png", "02.MP4", "bgm.mp3", "sub.srt", "notes.txt", ".hidden.png"):
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "takes").mkdir()
    (tmp_path / "takes" / "line.wav").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "03.jpg").write_bytes(b"x")
    entries = scan_folder(tmp_path, info_fn=lambda p: "정보" if p.suffix == ".mp3" else "")
    names = [e.name for e in entries]
    assert names == ["01.png", "02.MP4", "bgm.mp3", "sub.srt", "sub/03.jpg"]
    assert [e.kind for e in entries] == ["image", "video", "audio", "subtitle", "image"]
    text = build_prompt(entries, "여름 여행 영상", 1080, 1920, 30, 45)
    assert "- 01.png — 이미지" in text and "- bgm.mp3 — 음악/소리 (정보)" in text
    assert "1080x1920" in text and "약 45초" in text and "여름 여행 영상" in text
    assert "JSON 하나만" in text and "zoom_in" in text and "crossfade" in text
    # 예시 JSON 도 우리 추출기로 읽힘
    assert extract_json(text)["clips"]


def test_prompt_mentions_subtitle_items_and_label(tmp_path):
    """프롬프트에 subtitle.items 와 label 안내가 포함되어 있는지 확인."""
    from composer.prompt_template import build_prompt, scan_folder
    entries = scan_folder(tmp_path)
    text = build_prompt(entries, "테스트", 1920, 1080, 30)
    assert "subtitle.items" in text
    assert "label" in text
    assert "장면 이름" in text or "S04" in text
    # 첫 번째 예시는 여전히 extract_json 으로 정상 추출
    from composer.validate import extract_json
    data = extract_json(text)
    assert "clips" in data
    assert data["subtitle"].get("file") == "subtitle.srt"
