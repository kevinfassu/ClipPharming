# ClipForge

Your private AI clipping studio. Paste a Twitch/YouTube VOD (from a creator who gave you permission, or your own
recording) and ClipForge finds the best moments, cuts them into vertical 9:16 Shorts with word-by-word captions, and
puts them in a review dashboard. You approve, pick which of your accounts each clip goes to, export, post, and log
the results. Over time the analytics show which niches, lengths and creators work.

Everything runs on your own computer with free, open-source tools. No API keys.

```
VOD URL / file
  -> yt-dlp            download (or use a local file)
  -> ffmpeg + numpy    audio loudness / "excitement" curve
  -> faster-whisper    transcript with word timestamps (long VODs: only the loudest regions)
  -> detector          finds moments from screams, reaction phrases, laughter, fast talking
                       and cuts them on sentence boundaries with a build-up before the payoff
  -> (optional)        local LLM via Ollama, or your Claude Code subscription, re-ranks + writes titles/hooks
  -> ffmpeg            9:16 render (center / blurred background / facecam split) + captions + hook + credit
  -> dashboard         review, edit, approve -> publishing queue -> export -> stats
```

## Setup (Windows 11)

1. Install [Git](https://git-scm.com/download/win), clone this repo, open PowerShell in the folder.
2. Run the installer (installs Python 3.12, FFmpeg and Deno with winget if missing, then the Python packages and the
   Whisper model):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

   If it says it installed new tools, open a new PowerShell window and run it again.

3. Start ClipForge (worker + dashboard, then opens http://127.0.0.1:8765):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-clipforge.ps1
```

4. To make it always-on (starts at logon, no sleep while plugged in):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register-startup.ps1
```

Stop it with `scripts\stop-clipforge.ps1`. Unfinished jobs resume automatically on the next start.

## First run: prove the pipeline on one video

Start with a 20-30 minute video before a 10-hour stream. The quickest test is your own recording:

```powershell
.\.venv\Scripts\python.exe -m clipforge process "C:\Videos\my-recording.mp4" --own --niche gaming
```

Clips land in `storage\clips\source_<id>\clip01_*.mp4` and in the dashboard's Review tab.

## Daily workflow

1. **Creators and permissions**: add each creator, who gave permission, and a link to the evidence (DM, email,
   contract). Optional: their channel URL plus **Auto-discover**, so new VODs are picked up every few hours.
   Sources can't be queued for a creator without an active permission. Revoking a permission stops any new
   processing for them.
2. **My accounts**: add your YouTube, TikTok and Instagram accounts, one niche each.
3. **Sources**: paste VOD links, or let auto-discovery fill the queue.
4. **Review**: watch each clip, fix the title, hook or timing (this re-renders automatically), then approve it
   to one or more accounts with a schedule, or reject it with a reason.
5. **Publishing**: **Export** copies the MP4 and a caption `.txt` (with creator credit) to
   `storage\outbox\<account>\`. Upload it, paste the post URL, and later record views, retention and revenue.
6. **Analytics**: views by account, clip length and creator, so you know what to make more of.

## Niches and tuning (`config.yaml`)

Each niche (gaming, podcast, irl, or any you add) has its own settings:

| setting | meaning |
|---|---|
| `clip_min` / `clip_max` / `clip_target` | clip length in seconds |
| `clips_per_source` | maximum clips per VOD |
| `min_score` | weaker moments are skipped instead of padding the count |
| `crop` | `center`, `blur` (whole frame over a blurred background, good for gameplay with a HUD), or `split` (facecam on top, gameplay below) |
| `weights` | how much each signal counts: `audio`, `keywords`, `laughter`, `speech_rate`, `hook`, `complete`, `duration`, `arc` |
| `keywords` | extra reaction phrases for this niche |
| `caption_style` | font, size, colors, highlight color, position, words per line, uppercase |

Per creator (Creators page), you can override the crop mode, set the facecam box for split layout (`x,y,w,h` as
fractions of the frame; for example `0.74,0.68,0.25,0.30` for a bottom-right cam), and set the horizontal focus for
center crops.

Machine-specific settings, such as storage on a second drive, go in `config.local.yaml`:

```yaml
storage_dir: D:/ClipForge/Storage
```

### Settings chosen for your laptop (Core Ultra 7 155H, Arc iGPU, 16 GB)

- **Transcription**: faster-whisper `small.en`, CPU, int8, 6 threads. faster-whisper can't use Intel Arc
  (its GPU path is NVIDIA CUDA only), so the CPU is the right choice.
- **Long VODs**: `transcription.mode: auto` switches to *hotspots* above 90 minutes. Only the 40 loudest 2-minute
  regions are transcribed, which turns a 10-hour transcription into roughly an hour and a half of audio.
- **Rendering**: `encoder: auto` tries Intel Quick Sync (`h264_qsv`) and falls back to CPU `libx264` if it fails.
- **One job at a time**, so transcription, the optional LLM and rendering never compete for 16 GB of RAM.
- **Retention**: source video and audio are deleted after `retention_days` (14). Clips, transcripts and the
  database are kept.

## Optional: smarter ranking

**Local LLM (free, runs on the laptop):** install [Ollama](https://ollama.com), then run:

```powershell
ollama pull qwen2.5:3b-instruct
```

Then set `llm.enabled: true` in `config.yaml`. The top 15 moments per VOD are rated and given titles and hooks.
A 3B model fits in 16 GB next to everything else. Larger models will run out of memory.

**Claude Code (uses your subscription, not the API):**

```powershell
.\.venv\Scripts\python.exe -m clipforge claude-export 12
```

This writes `storage\review\source_12.md`. In Claude Code, in this folder, say:

```text
Read storage/review/source_12.md and follow the instructions in it.
```

Claude writes `source_12.picks.json`, with a score, keep or reject, title, hook and adjusted timing for each
candidate. Apply it:

```powershell
.\.venv\Scripts\python.exe -m clipforge claude-import 12
```

Changed hooks and timings re-render automatically.

## CLI reference

```text
python -m clipforge init | check | dashboard | worker [--once]
python -m clipforge submit  <url|file> (--creator ID | --own) [--niche gaming]
python -m clipforge process <url|file> (--creator ID | --own) [--niche gaming]   # blocking one-shot
python -m clipforge add-creator NAME --niche gaming [--channel URL --auto-discover --granted-by @them --evidence LINK]
python -m clipforge add-account NAME --platform youtube --niche gaming
python -m clipforge claude-export SOURCE_ID / claude-import SOURCE_ID
```

## Monetization reality check

- Get **written** permission and store it with the creator. Every clip keeps a link to its source and permission
  record, which you'll need if a takedown request comes in.
- Platforms judge *reused content*. Permission alone doesn't make a clip eligible for YouTube Partner Program or
  TikTok Creator Rewards. Add your own value: a hook, context, a story arc, commentary, and a distinct style per
  account. Don't post the same clip unchanged on several of your own accounts in one niche.
- TikTok's Creator Rewards program pays only for videos over one minute, which is why the podcast niche defaults to
  30-90 seconds. You can raise `clip_min` for the niches you want to monetize there.
- Publishing is manual export for now. Direct upload through the YouTube Data API or TikTok Content Posting API
  needs app approval, and is the next milestone once the clips are consistently good.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q
```

The tests generate synthetic videos with ffmpeg and stub the Whisper model, so they don't need network access.
