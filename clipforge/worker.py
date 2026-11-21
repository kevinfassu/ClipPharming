"""Background worker: one job at a time (sized for a 16 GB laptop).

Job types
  ingest   - download -> audio -> loudness -> transcribe -> detect -> (LLM) -> queue renders
  render   - captions + 9:16 render + thumbnail for one candidate
  discover - check auto-discover creators for new VODs
  cleanup  - delete old source video/audio (transcripts and clips are kept)

Every ingest stage skips work whose output already exists, so a crash or
reboot resumes where it left off.
"""
from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipforge import db, services
from clipforge.config import Config
from clipforge.pipeline import audio as audio_mod
from clipforge.pipeline import captions, detect, download, llm, render, transcribe

log = logging.getLogger("clipforge.worker")


def setup_logging(cfg: Config) -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(cfg.dir("logs") / "worker.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


# ------------------------------------------------------------------ ingest
def _stage(conn: sqlite3.Connection, sid: int, stage: str, progress: float | None = None) -> None:
    db.update(conn, "sources", sid, status="processing", stage=stage, progress=progress)
    log.info("source %s: %s", sid, stage)


def ingest(conn: sqlite3.Connection, cfg: Config, source_id: int) -> None:
    src = db.one(conn, "SELECT * FROM sources WHERE id = ?", source_id)
    if not src:
        raise ValueError(f"source {source_id} missing")
    services.check_source_allowed(conn, src)
    profile = cfg.niche(src["niche"])
    sdir = cfg.dir("sources")

    # 1. download
    video = src["video_path"]
    if not video or not Path(video).exists():
        _stage(conn, source_id, "downloading", 0)
        last = [0.0]

        def prog(p: float) -> None:
            if p - last[0] >= 0.02:
                last[0] = p
                db.update(conn, "sources", source_id, progress=round(p, 3))

        dl = cfg["download"]
        info = download.download(src["url"], sdir, max_height=int(dl["max_height"]),
                                 cookies_from_browser=dl.get("cookies_from_browser"), progress=prog)
        video = info["path"]
        db.update(conn, "sources", source_id, video_path=video, title=src["title"] or info["title"],
                  external_id=src["external_id"] or info["id"], media_deleted=0)

    duration = audio_mod.ffprobe_duration(video)
    db.update(conn, "sources", source_id, duration=duration)

    # 2. audio + loudness
    wav = src["audio_path"]
    if not wav or not Path(wav).exists():
        _stage(conn, source_id, "extracting audio")
        wav = str(audio_mod.extract_audio(video, cfg.dir("audio") / f"source_{source_id}.wav"))
        db.update(conn, "sources", source_id, audio_path=wav)
    curve_path = cfg.dir("audio") / f"source_{source_id}.loudness.npy"
    if not curve_path.exists():
        _stage(conn, source_id, "analysing audio")
        audio_mod.save_curve(curve_path, audio_mod.loudness_curve(wav))
    excite = audio_mod.excitement(audio_mod.load_curve(curve_path))

    # 3. transcription
    tpath = cfg.dir("transcripts") / f"source_{source_id}.json"
    if not tpath.exists():
        _stage(conn, source_id, "transcribing", 0)
        last = [0.0]

        def tprog(p: float) -> None:
            if p - last[0] >= 0.01:
                last[0] = p
                db.update(conn, "sources", source_id, progress=round(p, 3))

        transcribe.transcribe(wav, tpath, cfg["transcription"], excite=excite, progress=tprog)
    db.update(conn, "sources", source_id, transcript_path=str(tpath), loudness_path=str(curve_path))
    tdata = transcribe.load_transcript(tpath)

    # 4. moment detection
    _stage(conn, source_id, "finding moments")
    cands = detect.detect_candidates(tdata["words"], excite, duration, profile)
    heuristic = {id(c): c.score for c in cands}

    # 5. optional local LLM re-rank
    lcfg = cfg["llm"]
    if lcfg.get("enabled") and cands and llm.available(lcfg["url"]):
        _stage(conn, source_id, "local AI review")
        w = float(lcfg.get("weight", 0.5))
        for c in cands[: int(lcfg.get("top_n", 15))]:
            r = llm.rate(c.transcript, profile.get("name", src["niche"]), c.duration, lcfg)
            if r:
                c.features["llm"] = r["score"] / 10
                c.features["_llm_reason"] = r["reason"]  # type: ignore[assignment]
                c.score = round((1 - w) * c.score + w * r["score"] * 10, 2)
                c.title = r["title"] or c.title
                c.hook = r["hook"] or c.hook
        cands.sort(key=lambda c: c.score, reverse=True)

    # 6. store + queue renders (replace any unreviewed candidates from a previous run)
    stale = db.all_(conn, """SELECT * FROM candidates WHERE source_id = ? AND status IN
                             ('new','failed','rendering','rendered')""", source_id)
    for old in stale:
        for p in (old["clip_path"], old["thumb_path"]):
            if p:
                Path(p).unlink(missing_ok=True)
            if p and p.endswith(".mp4"):
                Path(p).with_suffix(".ass").unlink(missing_ok=True)
    with db.tx(conn):
        conn.execute("""DELETE FROM jobs WHERE type='render' AND status='queued' AND candidate_id IN
                        (SELECT id FROM candidates WHERE source_id = ? AND status IN
                         ('new','failed','rendering','rendered'))""", (source_id,))
        conn.execute("""DELETE FROM candidates WHERE source_id = ? AND status IN
                        ('new','failed','rendering','rendered')
                        AND id NOT IN (SELECT candidate_id FROM publish_queue)""", (source_id,))
        for rank, c in enumerate(cands, 1):
            reason = c.features.pop("_llm_reason", None)
            cid = db.insert(conn, "candidates", source_id=source_id, rank=rank, start=c.start, end=c.end,
                            peak=c.peak, score=c.score,
                            heuristic_score=heuristic[id(c)],
                            llm_score=c.features.get("llm"), llm_reason=reason,
                            features=json.dumps(c.features), transcript=c.transcript, title=c.title,
                            hook=c.hook, status="rendering", created_at=db.now())
            db.enqueue(conn, "render", candidate_id=cid, source_id=source_id, priority=2)
        db.update(conn, "sources", source_id, status="rendering" if cands else "done",
                  stage=f"{len(cands)} moments found", progress=1.0)
    log.info("source %s: %d candidates queued for render", source_id, len(cands))


# ------------------------------------------------------------------ render
def render_candidate(conn: sqlite3.Connection, cfg: Config, candidate_id: int) -> None:
    cand = db.one(conn, "SELECT * FROM candidates WHERE id = ?", candidate_id)
    if not cand:
        return
    src = db.one(conn, "SELECT * FROM sources WHERE id = ?", cand["source_id"])
    if not src["video_path"] or not Path(src["video_path"]).exists():
        raise FileNotFoundError("Source video was deleted (retention cleanup). Re-ingest the source to re-render.")
    creator = db.one(conn, "SELECT * FROM creators WHERE id = ?", src["creator_id"]) if src["creator_id"] else None
    profile = cfg.niche(src["niche"])
    words = transcribe.load_transcript(src["transcript_path"])["words"] if src["transcript_path"] else []

    out_dir = cfg.dir("clips") / f"source_{src['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"clip{cand['rank'] or 0:02d}_c{candidate_id}"
    clip_path = out_dir / f"{stem}.mp4"
    ass_path = out_dir / f"{stem}.ass"
    credit = f"Credit: {creator['name']}" if creator else ""
    if src["test_only"]:
        credit = "PRIVATE TEST - DO NOT POST"
    captions.write_ass(ass_path, words, cand["start"], cand["end"], profile["caption_style"],
                       width=int(cfg["render"]["width"]), height=int(cfg["render"]["height"]),
                       hook=cand["hook"] or "", credit=credit)
    mode = (creator["crop_mode"] if creator and creator["crop_mode"] else None) or profile.get("crop", "center")
    facecam = json.loads(creator["facecam"]) if creator and creator["facecam"] else None
    crop_x = creator["crop_x"] if creator else 0.5
    render.render_clip(src["video_path"], cand["start"], cand["end"], clip_path, cfg["render"],
                       mode=mode, crop_x=crop_x, facecam=facecam, ass_path=ass_path)
    thumb = render.thumbnail(clip_path, out_dir / f"{stem}.jpg",
                             at=max(0.5, min((cand["peak"] or cand["start"]) - cand["start"], cand["end"] - cand["start"] - 0.5)))
    with db.tx(conn):
        db.update(conn, "candidates", candidate_id, status="rendered", clip_path=str(clip_path),
                  thumb_path=str(thumb), rendered_at=db.now())
        left = db.one(conn, "SELECT COUNT(*) n FROM candidates WHERE source_id=? AND status='rendering'", src["id"])
        if left["n"] == 0 and src["status"] in ("rendering", "processing"):
            db.update(conn, "sources", src["id"], status="done", stage="ready for review")


# ------------------------------------------------------------------ discover / cleanup
def discover(conn: sqlite3.Connection, cfg: Config) -> int:
    dcfg = cfg["discovery"]
    added = 0
    for cr in db.all_(conn, "SELECT * FROM creators WHERE auto_discover = 1 AND channel_url IS NOT NULL"):
        if not services.active_permission(conn, cr["id"]):
            log.info("discover: skipping %s (no active permission)", cr["name"])
            continue
        try:
            entries = download.list_channel(cr["channel_url"], int(dcfg["max_per_channel"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("discover: %s failed: %s", cr["name"], exc)
            continue
        for e in entries:
            if e["live"] or not e["url"]:
                continue
            if e["duration"] and e["duration"] < float(dcfg["min_duration_minutes"]) * 60:
                continue
            if db.one(conn, "SELECT 1 FROM sources WHERE external_id = ? OR url = ?", e["id"], e["url"]):
                continue
            services.submit_source(conn, cfg, e["url"], creator_id=cr["id"], title=e["title"], external_id=e["id"])
            added += 1
            log.info("discover: queued %s - %s", cr["name"], e["title"])
    return added


def cleanup(conn: sqlite3.Connection, cfg: Config) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(cfg["retention_days"]))).isoformat()
    storage = cfg.storage.resolve()
    n = 0
    for s in db.all_(conn, """SELECT * FROM sources WHERE media_deleted = 0 AND status = 'done'
                              AND updated_at < ?""", cutoff):
        pending = db.one(conn, "SELECT COUNT(*) n FROM candidates WHERE source_id=? AND status IN ('rendering','new')",
                         s["id"])
        if pending["n"]:
            continue
        for p in (s["video_path"], s["audio_path"]):
            # never delete files that live outside storage (e.g. your own local recordings)
            if p and Path(p).resolve().is_relative_to(storage):
                download.delete_media(p)
        db.update(conn, "sources", s["id"], media_deleted=1, video_path=None, audio_path=None)
        n += 1
    return n


# ------------------------------------------------------------------ loop
def run_job(conn: sqlite3.Connection, cfg: Config, job: sqlite3.Row) -> None:
    t = job["type"]
    if t == "ingest":
        ingest(conn, cfg, job["source_id"])
    elif t == "render":
        render_candidate(conn, cfg, job["candidate_id"])
    elif t == "discover":
        discover(conn, cfg)
    elif t == "cleanup":
        cleanup(conn, cfg)
    else:
        raise ValueError(f"unknown job type {t}")


def _fail(conn: sqlite3.Connection, cfg: Config, job: sqlite3.Row, exc: BaseException) -> None:
    msg = f"{type(exc).__name__}: {exc}"
    retry = job["attempts"] < int(cfg["worker"]["max_attempts"]) and not isinstance(
        exc, (services.PermissionDenied, FileNotFoundError))
    with db.tx(conn):
        db.update(conn, "jobs", job["id"], status="queued" if retry else "failed", error=msg,
                  finished_at=None if retry else db.now())
        if not retry:
            if job["type"] == "ingest" and job["source_id"]:
                db.update(conn, "sources", job["source_id"], status="failed", error=msg)
            if job["type"] == "render" and job["candidate_id"]:
                db.update(conn, "candidates", job["candidate_id"], status="failed", review_notes=msg)
                left = db.one(conn, "SELECT COUNT(*) n FROM candidates WHERE source_id=? AND status='rendering'",
                              job["source_id"])
                if job["source_id"] and left["n"] == 0:
                    conn.execute("UPDATE sources SET status='done', stage='ready for review' "
                                 "WHERE id=? AND status='rendering'", (job["source_id"],))
    log.error("job %s (%s) failed%s: %s", job["id"], job["type"], ", will retry" if retry else "", msg)
    log.debug(traceback.format_exc())


def schedule_periodic(conn: sqlite3.Connection, cfg: Config) -> None:
    now = datetime.now(timezone.utc)

    def due(key: str, hours: float) -> bool:
        last = db.kv_get(conn, key)
        return not last or now - datetime.fromisoformat(last) >= timedelta(hours=hours)

    if cfg["discovery"].get("enabled") and due("last_discover", float(cfg["discovery"]["interval_hours"])):
        if not db.one(conn, "SELECT 1 FROM jobs WHERE type='discover' AND status IN ('queued','running')"):
            db.enqueue(conn, "discover", priority=-1)
        db.kv_set(conn, "last_discover", now.isoformat())
    if due("last_cleanup", 24):
        db.enqueue(conn, "cleanup", priority=-2)
        db.kv_set(conn, "last_cleanup", now.isoformat())


def recover(conn: sqlite3.Connection) -> None:
    """Jobs left 'running' by a crash/reboot go back in the queue."""
    conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def run(cfg: Config, *, once: bool = False, periodic: bool = True) -> None:
    setup_logging(cfg)
    conn = db.connect(cfg)
    db.init_db(conn)
    recover(conn)
    stop = {"flag": False}

    def handle(_sig, _frm):
        stop["flag"] = True
        log.info("stopping after current job...")

    signal.signal(signal.SIGINT, handle)
    try:
        signal.signal(signal.SIGTERM, handle)
    except (AttributeError, ValueError):
        pass
    log.info("ClipForge worker started (db=%s)", cfg.db_path)
    while not stop["flag"]:
        if periodic:
            schedule_periodic(conn, cfg)
        job = db.claim_job(conn)
        if not job:
            if once:
                break
            time.sleep(float(cfg["worker"]["poll_seconds"]))
            continue
        log.info("job %s: %s source=%s candidate=%s", job["id"], job["type"], job["source_id"], job["candidate_id"])
        try:
            run_job(conn, cfg, job)
            db.update(conn, "jobs", job["id"], status="done", error=None, finished_at=db.now())
        except Exception as exc:  # noqa: BLE001
            _fail(conn, cfg, job, exc)
    conn.close()
