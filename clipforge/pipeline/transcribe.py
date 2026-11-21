"""faster-whisper transcription with word-level timestamps.

Transcript JSON format (also what tests and `claude-import` rely on):
{
  "language": "en",
  "duration": 1234.5,
  "mode": "full" | "hotspots",
  "regions": [[start, end], ...],          # parts of the VOD that were transcribed
  "words": [{"start": 1.2, "end": 1.5, "word": "hello", "p": 0.98}, ...]
}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np

from clipforge.pipeline.audio import HOP, read_segment, wav_duration

_MODEL_CACHE: dict[tuple, Any] = {}


def _model(tcfg: dict) -> Any:
    key = (tcfg["model"], tcfg["device"], tcfg["compute_type"], tcfg["cpu_threads"])
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel  # lazy: heavy import

        _MODEL_CACHE.clear()  # only ever keep one model in RAM (16 GB machine)
        _MODEL_CACHE[key] = WhisperModel(
            tcfg["model"], device=tcfg["device"], compute_type=tcfg["compute_type"],
            cpu_threads=int(tcfg["cpu_threads"]), num_workers=1,
        )
    return _MODEL_CACHE[key]


def hotspot_regions(excite: np.ndarray, duration: float, count: int, seconds: float,
                    hop: float = HOP) -> list[tuple[float, float]]:
    """Pick the `count` most energetic, non-overlapping windows of `seconds` length."""
    if excite.size == 0:
        return [(0.0, duration)]
    win = max(1, int(seconds / hop))
    pos = np.clip(excite, 0, None)
    kernel = np.ones(win, dtype=np.float32)
    energy = np.convolve(pos, kernel, mode="same")
    order = np.argsort(energy)[::-1]
    taken = np.zeros(len(energy), dtype=bool)
    regions: list[tuple[float, float]] = []
    for idx in order:
        if len(regions) >= count:
            break
        a, b = max(0, idx - win // 2), min(len(energy), idx + win // 2)
        if taken[a:b].any():
            continue
        taken[max(0, a - win // 4):min(len(energy), b + win // 4)] = True
        regions.append((a * hop, min(duration, b * hop)))
    return merge_regions(sorted(regions))


def merge_regions(regions: list[tuple[float, float]], gap: float = 5.0) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(regions):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def transcribe(wav: str | Path, out_json: str | Path, tcfg: dict, *, excite: np.ndarray | None = None,
               progress: Callable[[float], None] | None = None) -> dict:
    duration = wav_duration(wav)
    mode = tcfg.get("mode", "auto")
    if mode == "auto":
        mode = "hotspots" if duration > float(tcfg.get("hotspot_after_minutes", 90)) * 60 else "full"
    if mode == "hotspots" and excite is not None:
        regions = hotspot_regions(excite, duration, int(tcfg.get("hotspot_count", 40)),
                                  float(tcfg.get("hotspot_seconds", 120)))
    else:
        mode, regions = "full", [(0.0, duration)]

    model = _model(tcfg)
    words: list[dict] = []
    language = tcfg.get("language") or None
    total = sum(b - a for a, b in regions) or 1.0
    done = 0.0
    for a, b in regions:
        # Feed audio in <=30 min pieces so RAM stays flat on long VODs.
        piece = 1800.0
        t = a
        while t < b:
            u = min(b, t + piece)
            samples = read_segment(wav, t, u)
            segments, info = model.transcribe(
                samples, language=language, word_timestamps=True,
                vad_filter=bool(tcfg.get("vad_filter", True)), beam_size=5,
                condition_on_previous_text=False,
            )
            language = language or info.language
            for seg in segments:
                for w in seg.words or []:
                    words.append({"start": round(t + w.start, 3), "end": round(t + w.end, 3),
                                  "word": w.word.strip(), "p": round(float(w.probability), 3)})
                if progress:
                    progress(min(0.999, (done + (seg.end or 0)) / total))
            done += u - t
            t = u
    data = {"language": language or "en", "duration": duration, "mode": mode,
            "regions": [list(r) for r in regions], "words": words}
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_json.with_suffix(".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(out_json)
    return data


def load_transcript(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def words_between(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if w["end"] > start and w["start"] < end]


def text_between(words: list[dict], start: float, end: float) -> str:
    return " ".join(w["word"] for w in words_between(words, start, end)).strip()
