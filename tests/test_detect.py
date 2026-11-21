import numpy as np

from clipforge.pipeline import detect
from clipforge.pipeline.audio import HOP, excitement
from clipforge.pipeline.transcribe import hotspot_regions, merge_regions
from tests.conftest import fake_words

PROFILE = {"clip_min": 10, "clip_max": 30, "clip_target": 18, "clips_per_source": 5, "min_score": 25,
           "weights": {"audio": 0.4, "keywords": 0.25, "laughter": 0.1, "speech_rate": 0.05, "hook": 0.1,
                       "complete": 0.05, "duration": 0.05, "arc": 0.1}, "keywords": []}


def loud_curve(seconds, hot):
    db = np.full(int(seconds / HOP), -30.0, dtype=np.float32)
    for a, b in hot:
        db[int(a / HOP):int(b / HOP)] = -6.0
    return excitement(db)


def test_finds_both_hot_moments_with_buildup():
    hot = ((40, 46), (90, 95))
    cands = detect.detect_candidates(fake_words(120, hot), loud_curve(120, hot), 120, PROFILE)
    assert len(cands) >= 2
    top = sorted(cands[:2], key=lambda c: c.start)
    for c, (a, b) in zip(top, hot):
        assert c.start < a <= c.end, (c, a)          # moment is inside the clip
        assert a - c.start >= 2                      # with some lead-in
        assert 10 <= c.duration <= 30
        assert c.title and c.transcript


def test_no_overlapping_clips():
    hot = ((40, 46), (52, 56), (90, 95))
    cands = detect.detect_candidates(fake_words(120, hot), loud_curve(120, hot), 120, PROFILE)
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            assert not (a.start <= b.peak <= a.end)


def test_quiet_vod_gives_nothing_or_little():
    cands = detect.detect_candidates(fake_words(120, ()), loud_curve(120, ()), 120, PROFILE)
    assert len(cands) <= 1


def test_too_short_source():
    assert detect.detect_candidates([], np.zeros(10, dtype=np.float32), 5, PROFILE) == []


def test_keyword_hits_strong_phrase():
    words = [{"start": i, "end": i + 0.3, "word": w} for i, w in enumerate(["oh", "my", "god", "hello"])]
    hits = detect.keyword_hits(words, [])
    assert hits and hits[0][1] == 2.0


def test_hotspot_regions_cover_loud_parts():
    ex = loud_curve(3600, ((600, 610), (2000, 2010)))
    regions = hotspot_regions(ex, 3600, count=2, seconds=60)
    assert any(a <= 600 <= b for a, b in regions)
    assert any(a <= 2000 <= b for a, b in regions)
    assert sum(b - a for a, b in regions) <= 130


def test_merge_regions():
    assert merge_regions([(0, 10), (12, 20), (40, 50)]) == [(0, 20), (40, 50)]
