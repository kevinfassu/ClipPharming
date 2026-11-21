"""ClipForge - a private, local-first AI clipping studio.

Pipeline: yt-dlp -> faster-whisper -> audio + transcript moment detection ->
(optional local LLM / Claude Code review) -> FFmpeg 9:16 render with captions ->
human review -> publishing queue -> performance tracking.
"""

__version__ = "0.1.0"
