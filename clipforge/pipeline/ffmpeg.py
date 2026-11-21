"""Small FFmpeg/FFprobe helpers."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install it (Windows: winget install Gyan.FFmpeg).")
    return path


def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe not found on PATH (it ships with ffmpeg).")
    return path


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def ffprobe_duration(path: str | Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_size(path: str | Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            w, h = int(s["width"]), int(s["height"])
            rot = 0
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = abs(int(sd["rotation"]))
            if rot in (90, 270):
                w, h = h, w
            return w, h
    raise ValueError(f"No video stream in {path}")


def has_audio(path: str | Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path)["streams"])


@functools.lru_cache(maxsize=4)
def encoder_works(name: str) -> bool:
    """True if this ffmpeg build + hardware can actually encode with `name`."""
    vf = "format=nv12" if name.endswith("_qsv") else "format=yuv420p"
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
           "color=c=black:s=320x240:d=0.2", "-vf", vf, "-c:v", name, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pick_encoder(preference: str = "auto") -> str:
    if preference and preference != "auto":
        return preference
    # Intel Quick Sync (Core Ultra / Arc) first, then CPU.
    for name in ("h264_qsv", "libx264"):
        if encoder_works(name):
            return name
    return "libx264"
