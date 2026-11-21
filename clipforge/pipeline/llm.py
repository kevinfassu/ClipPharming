"""Optional local-LLM re-ranking through Ollama (free, runs on your machine).

If Ollama isn't installed/running this quietly returns None and the
heuristic scores are used as-is.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

PROMPT = """You are the editor of a short-form video account in the "{niche}" niche.
Below is the transcript of a candidate {dur:.0f}-second clip cut from a longer livestream.
Rate how likely it is to hold a viewer's attention as a standalone YouTube Short / TikTok:
a strong hook in the first 3 seconds, a clear payoff (reaction, punchline, surprising statement),
and it must make sense without extra context.

Transcript:
\"\"\"{transcript}\"\"\"

Reply with ONLY a JSON object:
{{"score": <integer 1-10>, "title": "<catchy title, max 60 chars, no hashtags>",
"hook": "<on-screen hook text, max 8 words>", "reason": "<one short sentence>"}}"""


def available(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def rate(transcript: str, niche: str, duration: float, lcfg: dict) -> dict | None:
    body = json.dumps({
        "model": lcfg["model"],
        "prompt": PROMPT.format(niche=niche, dur=duration, transcript=transcript[:4000]),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 4096},
    }).encode()
    req = urllib.request.Request(f"{lcfg['url'].rstrip('/')}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=float(lcfg.get("timeout", 120))) as r:
            raw = json.loads(r.read().decode())
        data = json.loads(raw.get("response", "{}"))
        score = float(data.get("score", 0))
        if not 1 <= score <= 10:
            return None
        return {"score": score, "title": str(data.get("title", ""))[:80],
                "hook": str(data.get("hook", ""))[:80], "reason": str(data.get("reason", ""))[:300]}
    except Exception as exc:  # noqa: BLE001 - never let the optional LLM break ingestion
        log.warning("Ollama rating failed: %s", exc)
        return None
