"""Word-by-word animated captions as an ASS subtitle file (burned in by FFmpeg/libass)."""
from __future__ import annotations

from pathlib import Path

SENTENCE_END = (".", "!", "?", "…", ",")


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS uses BGR order)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[dict], per_line: int, max_span: float = 2.6, gap: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (len(cur) >= per_line or w["start"] - cur[-1]["end"] > gap
                    or w["end"] - cur[0]["start"] > max_span):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["word"].endswith(SENTENCE_END):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def wrap_hook(text: str, max_chars: int = 22) -> str:
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > max_chars:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\\N".join(_escape(line) for line in lines[:3])


def build_ass(words: list[dict], clip_start: float, clip_end: float, style: dict, *,
              width: int = 1080, height: int = 1920, hook: str = "", credit: str = "") -> str:
    """Return ASS text. `words` use source-VOD timestamps; output is clip-relative."""
    dur = clip_end - clip_start
    rel = []
    for w in words:
        if w["end"] <= clip_start or w["start"] >= clip_end:
            continue
        text = w["word"].strip()
        if not text:
            continue
        if style.get("uppercase"):
            text = text.upper()
        rel.append({"start": max(0.0, w["start"] - clip_start), "end": min(dur, w["end"] - clip_start),
                    "word": text})

    primary = ass_color(style["color"])
    hl = ass_color(style["highlight"])
    outline = ass_color(style["outline_color"])
    hook_box = ass_color(style.get("hook_box", "#FFFFFF"))
    hook_col = ass_color(style.get("hook_color", "#000000"))
    font = style["font"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{style['size']},{primary},{hl},{outline},&H80000000,-1,0,0,0,100,100,1,0,1,{style['outline']},{style['shadow']},2,60,60,{style['margin_v']},1
Style: Hook,{font},{style.get('hook_size', 64)},{hook_col},{hook_col},{hook_box},{hook_box},-1,0,0,0,100,100,0,0,3,18,0,8,80,80,190,1
Style: Credit,Arial,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,8,40,40,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if hook.strip():
        hs = float(style.get("hook_seconds") or 0)
        h_end = dur if hs <= 0 else min(dur, hs)
        events.append(f"Dialogue: 2,{ass_time(0)},{ass_time(h_end)},Hook,,0,0,0,,"
                      f"{{\\fad(150,0)}}{wrap_hook(hook.upper() if style.get('uppercase') else hook)}")
    if credit.strip():
        events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Credit,,0,0,0,,{_escape(credit)}")

    groups = group_words(rel, int(style.get("words_per_line", 3)))
    pop = "\\fscx112\\fscy112" if style.get("pop") else ""
    for gi, g in enumerate(groups):
        group_end = g[-1]["end"]
        nxt = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else dur
        # Hold the line until the next one starts if the pause is short.
        if nxt - group_end < 0.4:
            group_end = nxt
        for wi, w in enumerate(g):
            a = w["start"]
            b = g[wi + 1]["start"] if wi + 1 < len(g) else group_end
            if b - a < 0.04:
                continue
            parts = []
            for wj, other in enumerate(g):
                t = _escape(other["word"])
                if wj == wi:
                    parts.append(f"{{\\c{hl}{pop}}}{t}{{\\c{primary}\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def write_ass(path: str | Path, *args, **kwargs) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(*args, **kwargs), encoding="utf-8")
    return path
