"""Moment detection and heuristic scoring - runs fully offline, no API needed.

Signals (per second of the VOD):
  audio        - loudness spikes above the local baseline (screams, hype, crowd)
  keywords     - reaction phrases ("no way", "oh my god", niche-specific terms)
  laughter     - "haha", "lmao", "(laughs)", etc.
  speech_rate  - bursts of fast talking
Windows are grown around peaks, snapped to sentence boundaries so clips start
and end cleanly, and scored with the niche's weights.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from clipforge.pipeline.audio import HOP

BASE_KEYWORDS = [
    "oh my god", "oh my gosh", "no way", "what the", "let's go", "lets go", "are you kidding",
    "holy", "insane", "crazy", "bro", "dude", "wait wait", "no no no", "stop", "get out",
    "help", "run", "clip that", "clip it", "i'm dead", "screaming", "yes yes",
    "unbelievable", "wow", "omg", "wtf", "oh no", "how did", "did you see",
]
STRONG_KEYWORDS = {"oh my god", "no way", "what the", "let's go", "lets go", "clip that", "clip it",
                   "are you kidding", "oh no", "no no no", "unbelievable", "holy"}
LAUGH_RE = re.compile(r"\b(ha(ha)+|he(he)+|lol|lmao|lmfao|rofl)\b|\((laugh|laughs|laughing|laughter)\)|\[laugh", re.I)
SENTENCE_END = (".", "!", "?", "…")


@dataclass
class Candidate:
    start: float
    end: float
    peak: float
    score: float
    features: dict[str, float] = field(default_factory=dict)
    transcript: str = ""
    title: str = ""
    hook: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "peak": self.peak, "score": self.score,
                "features": self.features, "transcript": self.transcript, "title": self.title,
                "hook": self.hook}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).strip()


def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or x.size == 0:
        return x
    k = np.ones(width, dtype=np.float32) / width
    return np.convolve(x, k, mode="same")


def _scale(x: np.ndarray) -> np.ndarray:
    """Map to 0..1 using the 98th percentile of the non-zero values as 'max'."""
    if x.size == 0:
        return x
    nz = x[x > 0]
    hi = float(np.percentile(nz, 98)) if nz.size else 0.0
    if hi <= 1e-9:
        return np.zeros_like(x)
    return np.clip(x / hi, 0, 1)


def keyword_hits(words: list[dict], extra_keywords: list[str] = ()) -> list[tuple[float, float]]:
    """(time, weight) for every reaction keyword/phrase occurrence (base list + niche extras)."""
    phrases = sorted({_norm(k) for k in BASE_KEYWORDS + list(extra_keywords) if k.strip()}, key=len, reverse=True)
    toks = [_norm(w["word"]) for w in words]
    hits: list[tuple[float, float]] = []
    for i in range(len(words)):
        for ph in phrases:
            parts = ph.split()
            n = len(parts)
            if i + n <= len(toks) and toks[i:i + n] == parts:
                weight = 2.0 if ph in STRONG_KEYWORDS else 1.0
                hits.append((words[i]["start"], weight))
                break
        raw = words[i]["word"]
        if raw.endswith("!"):
            hits.append((words[i]["start"], 0.6))
        if len(raw) > 2 and raw.isupper() and raw.isalpha():
            hits.append((words[i]["start"], 0.5))
    return hits


def build_signals(words: list[dict], excite: np.ndarray, duration: float,
                  keywords: list[str]) -> dict[str, np.ndarray]:
    n = max(1, int(np.ceil(duration)))
    audio = np.zeros(n, dtype=np.float32)
    if excite.size:
        per = int(round(1.0 / HOP))
        m = min(len(excite) // per, n)
        if m:
            audio[:m] = np.clip(excite[: m * per].reshape(m, per).max(axis=1), 0, None)
    kw = np.zeros(n, dtype=np.float32)
    laugh = np.zeros(n, dtype=np.float32)
    rate = np.zeros(n, dtype=np.float32)
    speech = np.zeros(n, dtype=np.float32)
    for t, wgt in keyword_hits(words, keywords):
        kw[min(n - 1, int(t))] += wgt
    for w in words:
        s = min(n - 1, int(w["start"]))
        rate[s] += 1
        e = min(n, int(np.ceil(w["end"])))
        speech[s:max(s + 1, e)] = 1
        if LAUGH_RE.search(w["word"]):
            laugh[s] += 1
    rate_s = _smooth(rate, 5)
    spoken = rate_s[speech > 0]
    if spoken.size > 10:
        rate_z = np.clip((rate_s - np.median(spoken)) / (np.std(spoken) + 1e-6), 0, None)
    else:
        rate_z = np.zeros(n, dtype=np.float32)
    return {
        "audio": _scale(_smooth(audio, 3)),
        "keywords": _scale(_smooth(kw, 4)),
        "laughter": _scale(_smooth(laugh, 4)),
        "speech_rate": _scale(rate_z),
        "speech": speech,
    }


def interest_curve(signals: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    total = np.zeros_like(signals["audio"])
    wsum = 0.0
    for key in ("audio", "keywords", "laughter", "speech_rate"):
        w = float(weights.get(key, 0))
        total += w * signals[key]
        wsum += w
    return total / wsum if wsum else total


def _boundaries(words: list[dict]) -> tuple[list[float], list[float]]:
    """Good clip start times (sentence starts) and end times (sentence ends)."""
    starts, ends = [], []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        if prev is None or prev["word"].endswith(SENTENCE_END) or w["start"] - prev["end"] > 0.7:
            starts.append(w["start"])
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or w["word"].endswith(SENTENCE_END) or nxt["start"] - w["end"] > 0.7:
            ends.append(w["end"])
    return starts, ends


def _snap(t: float, options: list[float], lo: float, hi: float, prefer: str) -> tuple[float, bool]:
    best, best_d = t, None
    for o in options:
        if lo <= o <= hi:
            d = abs(o - t)
            # small bias so starts prefer earlier context and ends prefer finishing the thought
            if prefer == "earlier" and o > t:
                d *= 1.5
            if prefer == "later" and o < t:
                d *= 1.5
            if best_d is None or d < best_d:
                best, best_d = o, d
    return best, best_d is not None


def _pick_peaks(curve: np.ndarray, count: int, min_gap: int, radius: int = 5) -> list[int]:
    """Strongest local maxima, at least `min_gap` seconds apart."""
    if curve.size == 0:
        return []
    padded = np.pad(curve, radius, mode="constant")
    local_max = np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1).max(axis=1)
    floor = max(0.05, float(np.percentile(curve, 60)))
    order = np.argsort(curve)[::-1]
    picked: list[int] = []
    for idx in order:
        if curve[idx] <= floor:
            break
        if curve[idx] < local_max[idx]:
            continue
        if all(abs(int(idx) - p) >= min_gap for p in picked):
            picked.append(int(idx))
            if len(picked) >= count:
                break
    return picked


def _overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def suggest_title(words: list[dict], start: float, end: float, curve: np.ndarray) -> tuple[str, str]:
    """Title = the most intense sentence; hook = a short punchy version for the overlay."""
    inside = [w for w in words if start <= w["start"] < end]
    if not inside:
        return "", ""
    sentences: list[list[dict]] = [[]]
    for w in inside:
        sentences[-1].append(w)
        if w["word"].endswith(SENTENCE_END):
            sentences.append([])
    sentences = [s for s in sentences if s]

    def intensity(s: list[dict]) -> float:
        a, b = int(s[0]["start"]), int(s[-1]["end"]) + 1
        seg = curve[a:b] if a < len(curve) else np.zeros(1)
        return float(seg.max()) if seg.size else 0.0

    best = max(sentences, key=lambda s: (intensity(s), len(s) >= 3))
    text = " ".join(w["word"] for w in best).strip()
    text = re.sub(r"\s+", " ", text)
    title = text if len(text) <= 70 else text[:67].rsplit(" ", 1)[0] + "..."
    hook_words = text.split()
    hook = " ".join(hook_words[:8]) + ("..." if len(hook_words) > 8 else "")
    return title, hook


def detect_candidates(words: list[dict], excite: np.ndarray, duration: float, profile: dict) -> list[Candidate]:
    clip_min = float(profile.get("clip_min", 20))
    clip_max = float(profile.get("clip_max", 60))
    target = float(profile.get("clip_target", (clip_min + clip_max) / 2))
    want = int(profile.get("clips_per_source", 10))
    weights = profile.get("weights", {})
    duration = float(duration)
    if duration < clip_min:
        return []

    signals = build_signals(words, excite, duration, profile.get("keywords", []))
    curve = interest_curve(signals, weights)
    starts, ends = _boundaries(words)

    peaks = _pick_peaks(curve, want * 3, min_gap=int(max(clip_min, target * 0.6)))
    best_per_peak: list[Candidate] = []
    for p in peaks:
        variants = []
        # Try a long build-up, a balanced cut and a cold open; keep whichever scores best.
        for lead, tail in ((0.65, 0.35), (0.45, 0.35), (0.3, 0.3)):
            c = _window(p, lead, tail, target, clip_min, clip_max, duration, starts, ends)
            c.features = score_features(c, signals, curve, words, target, c.features.pop("_s_ok"),
                                        c.features.pop("_e_ok"))
            c.score = combine_score(c.features, weights)
            variants.append(c)
        best_per_peak.append(max(variants, key=lambda c: c.score))

    best_per_peak.sort(key=lambda c: c.score, reverse=True)
    chosen: list[Candidate] = []
    floor = max(float(profile.get("min_score", 25)),
                (best_per_peak[0].score * 0.4) if best_per_peak else 0.0)
    for c in best_per_peak:
        if c.score < floor:
            break
        if all(_overlap(c, o) < 0.2 and not (o.start <= c.peak <= o.end) for o in chosen):
            chosen.append(c)
        if len(chosen) >= want:
            break
    for c in chosen:
        c.transcript = " ".join(w["word"] for w in words if c.start <= w["start"] < c.end)
        c.title, c.hook = suggest_title(words, c.start, c.end, curve)
    return chosen


CONTENT_KEYS = ("audio", "keywords", "laughter", "speech_rate")
STRUCTURE_KEYS = ("hook", "complete", "duration", "arc")


def combine_score(features: dict[str, float], weights: dict[str, float]) -> float:
    """0-100. Content (is something happening?) gates structure (is it a well-cut clip?),
    so a perfectly-cut clip of nothing still scores near zero."""
    def avg(keys):
        ws = [(float(weights.get(k, 0)), float(features.get(k, 0))) for k in keys]
        tot = sum(w for w, _ in ws)
        return sum(w * v for w, v in ws) / tot if tot else 0.0
    arc = float(features.get("arc", 1.0))
    return round(100 * avg(CONTENT_KEYS) * (0.5 + 0.5 * avg(STRUCTURE_KEYS)) * (0.6 + 0.4 * arc), 2)


def _window(p: int, lead: float, tail: float, target: float, clip_min: float, clip_max: float,
            duration: float, starts: list[float], ends: list[float]) -> Candidate:
    raw_start = p - target * lead
    raw_end = p + target * tail
    start, s_ok = _snap(raw_start, starts, raw_start - 8, min(p - 1.0, raw_start + 6), "earlier")
    end, e_ok = _snap(raw_end, ends, max(p + 1.5, raw_end - 5), raw_end + 10, "later")
    start = max(0.0, start - 0.15)
    end = min(duration, end + 0.35)
    if end - start < clip_min:
        # grow the build-up first; only extend past the payoff if we hit the start of the VOD
        start = max(0.0, end - clip_min)
        end = min(duration, max(end, start + clip_min))
        s_ok = False
    if end - start > clip_max:
        end = start + clip_max
        e_ok = False
    c = Candidate(start=round(start, 2), end=round(end, 2), peak=float(p), score=0.0)
    c.features = {"_s_ok": s_ok, "_e_ok": e_ok}
    return c


def score_features(c: Candidate, signals: dict[str, np.ndarray], curve: np.ndarray, words: list[dict],
                   target: float, start_ok: bool, end_ok: bool) -> dict[str, float]:
    a, b = int(c.start), max(int(c.start) + 1, int(np.ceil(c.end)))

    def peak(key: str) -> float:
        seg = signals[key][a:b]
        return float(seg.max()) if seg.size else 0.0

    def mean_top(key: str, frac: float = 0.25) -> float:
        seg = np.sort(signals[key][a:b])[::-1]
        if not seg.size:
            return 0.0
        k = max(1, int(len(seg) * frac))
        return float(seg[:k].mean())

    hook_seg = curve[a:a + 3]
    first_words = [w for w in words if c.start <= w["start"] < c.start + 3.5]
    hook_text = " ".join(w["word"] for w in first_words)
    hook = float(hook_seg.max()) if hook_seg.size else 0.0
    if hook_text.rstrip().endswith(("?", "!")) or any(w["word"].endswith(("?", "!")) for w in first_words):
        hook = min(1.0, hook + 0.3)
    if first_words:
        hook = min(1.0, hook + 0.1)  # someone talking immediately > dead air
    speech_cov = float(signals["speech"][a:b].mean()) if b > a else 0.0
    # Arc: the payoff should land after some build-up and not be followed by a long dull tail.
    pos = (c.peak - c.start) / max(1e-6, c.duration)
    tail = c.end - c.peak
    arc = 1.0
    if pos < 0.15:
        arc -= 0.3
    if tail > 0.5 * c.duration:
        arc -= min(0.7, (tail - 0.5 * c.duration) / c.duration * 2)
    return {
        "arc": round(max(0.0, arc), 4),
        "audio": round(0.6 * peak("audio") + 0.4 * mean_top("audio"), 4),
        "keywords": round(0.5 * peak("keywords") + 0.5 * mean_top("keywords"), 4),
        "laughter": round(peak("laughter"), 4),
        "speech_rate": round(mean_top("speech_rate"), 4),
        "hook": round(hook, 4),
        "complete": round((0.5 if start_ok else 0.0) + (0.5 if end_ok else 0.0), 4),
        "duration": round(max(0.0, 1 - abs(c.duration - target) / target), 4),
        "speech_coverage": round(speech_cov, 4),
    }
