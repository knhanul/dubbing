"""crop_anchor 테스트."""
from composer.render.ffmpeg_builder import _fit_filters
from composer.schema import Clip, Project


def test_fit_filters_anchor_expressions():
    w, h, bg = 1920, 1080, "#000000"
    cases = {
        "center": "crop=1920:1080:(iw-ow)/2:(ih-oh)/2",
        "top": "crop=1920:1080:(iw-ow)/2:0",
        "bottom": "crop=1920:1080:(iw-ow)/2:ih-oh",
        "left": "crop=1920:1080:0:(ih-oh)/2",
        "right": "crop=1920:1080:iw-ow:(ih-oh)/2",
    }
    for anchor, expected in cases.items():
        filters = _fit_filters("cover", w, h, bg, anchor=anchor)
        assert any(expected in f for f in filters), f"{anchor} missing {expected} in {filters}"
    # contain ignores anchor
    filters = _fit_filters("contain", w, h, bg, anchor="top")
    assert all("crop=" not in f for f in filters)


def test_old_json_without_crop_anchor_defaults_to_center():
    data = {"file": "a.png", "type": "image", "duration": 4.0}
    c = Clip.from_dict(data)
    assert c.crop_anchor == "center"
    # full project
    p = Project.from_dict({"version": 2, "clips": [data]})
    assert p.clips[0].crop_anchor == "center"
