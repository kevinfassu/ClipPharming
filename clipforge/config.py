"""Configuration loading.

Defaults live in DEFAULTS below. `config.yaml` (repo root, or the path in the
CLIPFORGE_CONFIG env var) overrides them, and `config.local.yaml` next to it
overrides that (git-ignored, for machine-specific paths).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CAPTION_STYLE: dict[str, Any] = {
    "font": "Arial Black",
    "size": 78,
    "color": "#FFFFFF",
    "highlight": "#FFE400",
    "outline_color": "#000000",
    "outline": 6,
    "shadow": 2,
    "margin_v": 560,          # distance from bottom of the 1920px frame
    "words_per_line": 3,
    "uppercase": True,
    "pop": True,               # scale the active word up slightly
    "hook_size": 64,
    "hook_color": "#000000",
    "hook_box": "#FFFFFF",
    "hook_seconds": 0,         # 0 = keep hook text on screen the whole clip
}

DEFAULTS: dict[str, Any] = {
    "storage_dir": "storage",
    "database": None,               # default: <storage_dir>/clipforge.db
    "retention_days": 14,           # delete source VOD video/audio after this
    "download": {
        "max_height": 1080,
        "cookies_from_browser": None,  # e.g. "chrome" for sub-only VODs you have access to
    },
    "transcription": {
        "model": "small.en",
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 6,
        "language": "en",
        "vad_filter": True,
        # full     -> transcribe the whole VOD
        # hotspots -> transcribe only the loudest/most active regions (much faster on 10h streams)
        # auto     -> hotspots when the VOD is longer than hotspot_after_minutes
        "mode": "auto",
        "hotspot_after_minutes": 90,
        "hotspot_count": 40,
        "hotspot_seconds": 120,
    },
    "render": {
        "encoder": "auto",          # auto | h264_qsv | libx264
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "crf": 20,
        "qsv_quality": 22,
        "audio_bitrate": "160k",
        "loudnorm": True,
        "threads": 0,               # 0 = ffmpeg decides
    },
    "llm": {
        "enabled": False,           # local model via Ollama, optional
        "url": "http://localhost:11434",
        "model": "qwen2.5:3b-instruct",
        "top_n": 15,
        "weight": 0.5,              # blend: final = (1-w)*heuristic + w*llm
        "timeout": 120,
    },
    "discovery": {
        "enabled": True,
        "interval_hours": 3,
        "max_per_channel": 3,
        "min_duration_minutes": 10,
    },
    "worker": {
        "poll_seconds": 5,
        "max_attempts": 2,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "niches": {
        "gaming": {
            "name": "Gaming highlights",
            "clip_min": 18,
            "clip_max": 60,
            "clip_target": 32,
            "clips_per_source": 10,
            "min_score": 25,           # skip weak moments instead of padding the count
            "crop": "blur",            # center | blur | split
            "weights": {"audio": 0.40, "keywords": 0.22, "laughter": 0.10,
                        "speech_rate": 0.08, "hook": 0.10, "complete": 0.05, "duration": 0.05, "arc": 0.10},
            "keywords": ["clutch", "no way", "let's go", "gg", "insane", "what the", "bro", "run", "oh my god"],
            "caption_style": {},
        },
        "podcast": {
            "name": "Podcast moments",
            "clip_min": 30,
            "clip_max": 90,
            "clip_target": 55,
            "clips_per_source": 8,
            "crop": "center",
            "weights": {"audio": 0.12, "keywords": 0.30, "laughter": 0.15,
                        "speech_rate": 0.08, "hook": 0.20, "complete": 0.10, "duration": 0.05, "arc": 0.08},
            "keywords": ["honestly", "the truth is", "nobody talks about", "the craziest", "i swear",
                         "story", "never", "secret", "biggest mistake"],
            "caption_style": {"highlight": "#3DF5A7"},
        },
        "irl": {
            "name": "Internet culture / IRL",
            "clip_min": 18,
            "clip_max": 60,
            "clip_target": 35,
            "clips_per_source": 10,
            "crop": "center",
            "weights": {"audio": 0.32, "keywords": 0.22, "laughter": 0.15,
                        "speech_rate": 0.08, "hook": 0.13, "complete": 0.05, "duration": 0.05, "arc": 0.10},
            "keywords": ["chat", "look at this", "no way", "what is", "excuse me", "oh my god"],
            "caption_style": {"highlight": "#FF5CA8"},
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    # ---- paths -------------------------------------------------------
    def _resolve(self, value: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(value)))
        return p if p.is_absolute() else (self.root / p)

    @property
    def storage(self) -> Path:
        return self._resolve(self.data["storage_dir"])

    @property
    def db_path(self) -> Path:
        db = self.data.get("database")
        return self._resolve(db) if db else self.storage / "clipforge.db"

    def dir(self, name: str) -> Path:
        """Named storage sub-folder (sources, audio, transcripts, clips, outbox, review, logs)."""
        path = self.storage / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- niches ------------------------------------------------------
    @property
    def niches(self) -> dict[str, dict]:
        return self.data["niches"]

    def niche(self, slug: str | None) -> dict:
        niches = self.niches
        if slug in niches:
            prof = niches[slug]
        else:
            prof = next(iter(niches.values()))
        prof = copy.deepcopy(prof)
        prof["caption_style"] = deep_merge(DEFAULT_CAPTION_STYLE, prof.get("caption_style") or {})
        return prof


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = path or os.environ.get("CLIPFORGE_CONFIG")
    if path:
        cfg_path = Path(path)
    else:
        cfg_path = Path.cwd() / "config.yaml"
        if not cfg_path.exists():
            cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    data = copy.deepcopy(DEFAULTS)
    root = cfg_path.parent if cfg_path.exists() else Path.cwd()
    for candidate in (cfg_path, cfg_path.with_name("config.local.yaml")):
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            # Niches in a user config replace the defaults' niche *set* but are
            # merged per niche so partial definitions still work.
            if "niches" in loaded and isinstance(loaded["niches"], dict):
                merged_niches = {}
                for slug, prof in loaded["niches"].items():
                    merged_niches[slug] = deep_merge(DEFAULTS["niches"].get(slug, DEFAULTS["niches"]["gaming"]), prof or {})
                loaded = dict(loaded)
                loaded["niches"] = merged_niches
                data["niches"] = {}
            data = deep_merge(data, loaded)
    return Config(data, root)
