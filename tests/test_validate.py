import json
from pathlib import Path

import pytest

from composer.schema import JSON_SCHEMA, Project
from composer.validate import JsonExtractError, correction_prompt, extract_json, has_errors, validate_project

ROOT = Path(__file__).resolve().parent.parent


def test_extract_from_fence_with_extra_text():
    text = "좋아요! 아래가 결과입니다.\n```json\n{\"clips\": [{\"file\": \"a.png\", \"duration\": 3}]}\n```\n감사합니다."
    assert extract_json(text)["clips"][0]["file"] == "a.png"


def test_extract_plain_with_prefix_and_braces_in_strings():
    text = '설명 {이건 JSON 아님} 결과: {"title": "괄호 } 포함 {", "clips": []} 끝'
    assert extract_json(text)["title"] == "괄호 } 포함 {"


def test_extract_relaxed_trailing_commas_comments_smart_quotes():
    text = '```json\n{\n  // 주석\n  "clips": [ {"file": "a.png", "duration": 2,}, ],\n  /* 블록 */ "title": \u201c제목\u201d\n}\n```'
    d = extract_json(text)
    assert d["title"] == "제목" and len(d["clips"]) == 1


def test_extract_prefers_largest_object():
    text = '예: {"a": 1}\n```json\n{"clips": [{"file": "x.png", "duration": 1}], "title": "big"}\n```'
    assert extract_json(text)["title"] == "big"


def test_extract_errors():
    with pytest.raises(JsonExtractError):
        extract_json("")
    with pytest.raises(JsonExtractError, match="찾지 못했습니다"):
        extract_json("JSON 이 없습니다")
    with pytest.raises(JsonExtractError, match="JSON 형식 오류"):
        extract_json('{"clips": [1 2]}')


def test_example_answer_file():
    d = extract_json((ROOT / "examples" / "ai_answer_example.txt").read_text(encoding="utf-8"))
    assert d["output"]["height"] == 1920 and len(d["clips"]) == 2


def _media(tmp_path):
    for n in ("a.png", "b.mp4", "m.mp3", "s.srt", "t.wav"):
        (tmp_path / n).write_bytes(b"x")
    return tmp_path


def test_valid_project_has_no_errors(tmp_path):
    base = _media(tmp_path)
    data = {
        "version": 2, "output": {"width": 1920, "height": 1080, "fps": 30},
        "transition": {"type": "crossfade", "duration": 0.5},
        "clips": [{"file": "a.png", "type": "image", "duration": 3, "motion": "zoom_in"},
                  {"file": "b.mp4", "type": "video", "speed": 2, "trim_start": 1, "trim_end": 4}],
        "subtitle": {"file": "s.srt", "offset": -0.2}, "bgm": {"file": "m.mp3", "volume": 0.3},
        "dubbing": {"takes": [{"file": "t.wav", "subtitle": 1, "offset": -1.5}]},
    }
    assert validate_project(data, base) == []


def test_detects_problems_in_korean(tmp_path):
    base = _media(tmp_path)
    data = {
        "output": {"width": 1921, "height": "1080", "fps": 0},
        "clips": [
            {"file": "없는파일.png", "duration": 2},
            {"file": "a.png"},
            {"file": "b.mp4", "type": "image", "duration": 3},
            {"file": "b.mp4", "trim_start": 5, "trim_end": 2, "speed": 0, "motion": "zoom_in"},
            {"file": "a.png", "duration": -1, "motion": "spin", "motion_strength": 3},
            {"file": "a.png", "duration": 2, "transition": {"type": "explode", "duration": 1}, "colour": 1},
        ],
        "bgm": {"file": "music.mp3", "volume": "크게"},
    }
    issues = validate_project(data, base)
    msgs = "\n".join(map(str, issues))
    assert has_errors(issues)
    for needle in ("output.width: 짝수여야", "output.height: 숫자여야", "output.fps: 이상 1",
                   "clips[0].file: 파일 '없는파일.png'", "clips[1].duration: 이미지는 duration",
                   "clips[2].type: 파일 확장자로는 'video'", "clips[3].trim_end", "clips[3].speed",
                   "clips[3].motion: 움직임 효과는 이미지에만", "clips[4].duration", "clips[4].motion: 허용되지 않는 값 'spin'",
                   "clips[4].motion_strength", "clips[5].transition.type", "clips[5].colour: 알 수 없는 항목",
                   "bgm.file", "bgm.volume: 숫자여야"):
        assert needle in msgs, needle


def test_non_object_and_empty_clips():
    assert has_errors(validate_project([1, 2]))
    assert "clips" in str(validate_project({"clips": []})[0])


def test_correction_prompt_lists_errors_and_files(tmp_path):
    issues = validate_project({"clips": [{"file": "z.png"}]}, tmp_path)
    text = correction_prompt(issues, ["a.png", "b.mp4"])
    assert "JSON 만" in text and "clips[0].file" in text and "- a.png" in text
    t2 = correction_prompt([], ["a.png"], parse_error="JSON 형식 오류")
    assert "JSON 을 읽을 수 없었어" in t2


def test_schema_file_matches_code():
    shipped = json.loads((ROOT / "schema" / "project.schema.json").read_text(encoding="utf-8"))
    assert shipped == JSON_SCHEMA


def test_example_project_matches_schema_and_roundtrips():
    jsonschema = pytest.importorskip("jsonschema")
    data = json.loads((ROOT / "examples" / "project_example.json").read_text(encoding="utf-8"))
    jsonschema.validate(data, JSON_SCHEMA)
    assert not has_errors(validate_project(data, None))
    p = Project.from_dict(data)
    jsonschema.validate(p.to_dict(), JSON_SCHEMA)
    again = Project.from_dict(p.to_dict())
    assert again.to_dict() == p.to_dict()
    assert p.clips[2].transition.type == "fade"
