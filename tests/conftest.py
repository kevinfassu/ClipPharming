import json
import subprocess
from pathlib import Path

import pytest

from clipforge import db
from clipforge.config import load_config


def make_vod(path: Path, seconds: int = 120, size: str = "1280x720", loud=((40, 44), (90, 93))) -> Path:
    expr = "+".join(f"between(t,{a},{b})" for a, b in loud) or "0"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=f=220:d={seconds}",
        "-filter_complex", f"[1:a]volume='if({expr},1.0,0.04)':eval=frame[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
        "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def fake_words(seconds: int, hot=((40, 46), (90, 95))) -> list[dict]:
    words, t, i = [], 0.5, 0
    filler = "so we are just walking around here looking for loot".split()
    while t < seconds - 1:
        if any(a <= t < b for a, b in hot):
            seq = ["oh", "my", "god!", "no", "way!", "let's", "go!"]
        else:
            seq = [filler[(i + k) % len(filler)] for k in range(6)]
            seq[-1] += "."
        for w in seq:
            words.append({"start": round(t, 2), "end": round(t + 0.3, 2), "word": w, "p": 0.9})
            t += 0.4
        t += 0.7
        i += 1
    return words


@pytest.fixture
def cfg(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"storage_dir: {tmp_path.as_posix()}/storage\n"
        "discovery: {enabled: false}\n"
        "render: {encoder: libx264, width: 360, height: 640, fps: 15}\n"
        "niches:\n  gaming: {clips_per_source: 3, clip_min: 10, clip_max: 30, clip_target: 18}\n"
        "  podcast: {clips_per_source: 3}\n",
        encoding="utf-8")
    return load_config(cfg_file)


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(scope="session")
def vod(tmp_path_factory):
    return make_vod(tmp_path_factory.mktemp("media") / "vod.mp4")


def write_transcript(cfg, source_id: int, seconds: int = 120) -> Path:
    path = cfg.dir("transcripts") / f"source_{source_id}.json"
    path.write_text(json.dumps({"language": "en", "duration": seconds, "mode": "full",
                                "regions": [[0, seconds]], "words": fake_words(seconds)}))
    return path
