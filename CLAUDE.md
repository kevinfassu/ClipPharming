# ClipForge - notes for Claude Code

Private, local-first AI clipping studio. Runs on one always-on Windows 11 laptop
(Core Ultra 7 155H, Intel Arc iGPU, 16 GB RAM). No paid APIs required.

## Layout
- `clipforge/pipeline/` - pure processing steps: `download` (yt-dlp), `audio` (ffmpeg extract + numpy loudness/excitement),
  `transcribe` (faster-whisper, word timestamps, "hotspots" mode for long VODs), `detect` (moment finding + scoring),
  `captions` (ASS word-by-word), `render` (ffmpeg 9:16: center/blur/split), `llm` (optional Ollama), `claude_handoff`.
- `clipforge/worker.py` - sequential job queue (ingest/render/discover/cleanup) stored in SQLite; resumable.
- `clipforge/services.py` - business ops shared by CLI + dashboard (permissions gate, approve, export, analytics).
- `clipforge/web/` - FastAPI + Jinja dashboard, no JS build.
- `config.yaml` - niches, weights, model and render settings. `config.local.yaml` overrides (git-ignored).

## Rules
- Never process a source without `own_content` or an active creator permission (`services.check_source_allowed`).
- One heavy job at a time - 16 GB RAM. Don't add parallel transcription/rendering.
- Keep the pipeline free of paid APIs; Ollama/Claude review are optional add-ons.
- ffmpeg on Windows: subtitle files are passed by bare filename with `cwd` = clip folder (drive-letter colons break libass).

## Commands
- Tests: `python -m pytest -q` (needs ffmpeg on PATH; no network/model downloads)
- Run: `python -m clipforge dashboard` + `python -m clipforge worker`
- One-shot: `python -m clipforge process <url-or-file> --own --niche gaming`

## Clip review handoff
When asked to review `storage/review/source_<id>.md`, follow the instructions inside it and write only
`storage/review/source_<id>.picks.json`.
