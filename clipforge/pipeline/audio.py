"""Audio extraction and excitement analysis (pure numpy, streams the WAV so
10-hour VODs never need to fit in RAM)."""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from clipforge.pipeline.ffmpeg import ffmpeg_bin, ffprobe_duration

SAMPLE_RATE = 16000
HOP = 0.5  # seconds per loudness frame


def extract_audio(video: str | Path, wav: str | Path) -> Path:
    wav = Path(wav)
    wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav.with_suffix(".part.wav")
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.replace(wav)
    return wav


def wav_duration(wav: str | Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def loudness_curve(wav: str | Path, hop: float = HOP) -> np.ndarray:
    """RMS loudness in dBFS per `hop` seconds."""
    out: list[np.ndarray] = []
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        hop_n = int(sr * hop)
        chunk_hops = 2400  # 20 minutes per read at 0.5s hops
        while True:
            raw = w.readframes(hop_n * chunk_hops)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            n = len(x) // hop_n
            if n == 0:
                break
            frames = x[: n * hop_n].reshape(n, hop_n)
            rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
            out.append(20 * np.log10(rms + 1e-9))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def excitement(db: np.ndarray, hop: float = HOP, baseline_seconds: float = 90.0) -> np.ndarray:
    """How much louder each frame is than its local baseline, in robust z-units.

    Streamers vary in mic level across a long stream, so we compare each frame
    with the rolling median of the surrounding `baseline_seconds` rather than
    a global average. Silence is never "exciting".
    """
    if db.size == 0:
        return db
    win = max(3, int(baseline_seconds / hop) | 1)
    pad = win // 2
    padded = np.pad(db, pad, mode="edge")
    # rolling median via strided view, done in blocks to cap memory
    baseline = np.empty_like(db)
    view = np.lib.stride_tricks.sliding_window_view(padded, win)
    block = 20000
    for i in range(0, len(db), block):
        baseline[i:i + block] = np.median(view[i:i + block], axis=1)
    diff = db - baseline
    mad = np.median(np.abs(diff - np.median(diff))) * 1.4826 + 1e-6
    z = diff / max(mad, 1.5)  # floor so near-constant audio doesn't explode
    z[db < -50] = 0.0
    return np.clip(z, -5, 8).astype(np.float32)


def read_segment(wav: str | Path, start: float, end: float) -> np.ndarray:
    """Float32 mono samples for [start, end) - fed straight into faster-whisper."""
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        total = w.getnframes()
        a = max(0, int(start * sr))
        b = min(total, int(end * sr))
        w.setpos(a)
        raw = w.readframes(max(0, b - a))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def save_curve(path: str | Path, curve: np.ndarray) -> None:
    np.save(str(path), curve)


def load_curve(path: str | Path) -> np.ndarray:
    return np.load(str(path))


__all__ = ["extract_audio", "wav_duration", "loudness_curve", "excitement", "read_segment",
           "save_curve", "load_curve", "ffprobe_duration", "HOP", "SAMPLE_RATE"]
