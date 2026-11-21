from clipforge.pipeline.captions import ass_color, ass_time, build_ass, group_words
from clipforge.config import DEFAULT_CAPTION_STYLE


def test_ass_color_is_bgr():
    assert ass_color("#FF8800") == "&H000088FF"


def test_ass_time():
    assert ass_time(3723.456) == "1:02:03.46"


def test_build_ass_highlights_each_word_relative_to_clip():
    words = [{"start": 100 + i * 0.5, "end": 100 + i * 0.5 + 0.4, "word": w}
             for i, w in enumerate(["no", "way", "{bro}!", "that", "was", "insane."])]
    ass = build_ass(words, 99.0, 105.0, DEFAULT_CAPTION_STYLE, hook="Watch this", credit="Credit: X")
    lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert len(lines) == 6
    assert lines[0].split(",")[1] == "0:00:01.00"   # 100s in source == 1s in clip
    assert "{bro}" not in ass and "(BRO)!" in ass   # braces escaped, uppercase on
    assert "Hook" in ass and "WATCH THIS" in ass and "Credit: X" in ass


def test_group_words_breaks_on_punctuation():
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.2, "word": w} for i, w in enumerate(["a", "b.", "c", "d", "e", "f"])]
    groups = group_words(words, 3)
    assert [len(g) for g in groups] == [2, 3, 1]


def test_hook_wraps_with_real_ass_newlines():
    from clipforge.pipeline.captions import wrap_hook
    out = wrap_hook("bro what the oh my god this is insane", 12)
    assert "\\N" in out and "\\\\N" not in out
