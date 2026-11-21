"""Use your Claude Code subscription as the editor - no API key needed.

`python -m clipforge claude-export <source_id>` writes storage/review/source_<id>.md:
a ready-to-run brief listing every candidate with its transcript. Open Claude Code
in this repo and say:

    Read storage/review/source_12.md and follow the instructions in it.

Claude writes storage/review/source_12.picks.json; then run
`python -m clipforge claude-import 12` to apply its scores/titles/hooks.
"""
from __future__ import annotations

import json
from pathlib import Path


def _fmt(t: float) -> str:
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def export_brief(path: Path, source: dict, candidates: list[dict], niche_name: str) -> Path:
    picks_path = path.with_suffix(".picks.json")
    lines = [
        f"# Clip review brief - source #{source['id']}",
        "",
        f"**Source:** {source.get('title') or source['url']}  ",
        f"**Niche / account style:** {niche_name}",
        "",
        "## Instructions for Claude",
        "",
        "You are the editor of a short-form account. For every candidate below decide whether it",
        "works as a standalone Short/TikTok: strong hook in the first 3 seconds, clear payoff,",
        "understandable without the rest of the stream. You may also tighten the start/end",
        "(times are in seconds from the start of the VOD; keep clips within 15-90 seconds).",
        "",
        f"Write a JSON array to `{picks_path.as_posix()}` with one object per candidate:",
        "",
        "```json",
        '[{"id": 123, "score": 8, "keep": true, "title": "max 60 chars", "hook": "max 8 words",',
        '  "start": 1234.5, "end": 1270.2, "reason": "one sentence"}]',
        "```",
        "",
        "`score` is 1-10. Omit `start`/`end` to keep the detected times. Write only the file - do not",
        "modify anything else in the repository.",
        "",
        "## Candidates",
        "",
    ]
    for c in candidates:
        feats = json.loads(c["features"]) if isinstance(c.get("features"), str) else (c.get("features") or {})
        lines += [
            f"### Candidate id={c['id']}  ({_fmt(c['start'])} -> {_fmt(c['end'])}, {c['end'] - c['start']:.0f}s,"
            f" heuristic score {c['heuristic_score'] or c['score']:.0f})",
            "",
            "Signals: " + ", ".join(f"{k}={v:.2f}" for k, v in feats.items()),
            "",
            "> " + (c.get("transcript") or "(no speech detected - likely a gameplay/audio moment)"),
            "",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_picks(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("picks") or data.get("candidates") or []
    out = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue
        out.append(item)
    return out
