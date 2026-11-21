"""Command line: python -m clipforge <command> ..."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from clipforge import db, services
from clipforge.config import load_config


def _ctx(args):
    cfg = load_config(args.config)
    conn = db.connect(cfg)
    db.init_db(conn)
    return cfg, conn


def cmd_init(args) -> int:
    cfg, conn = _ctx(args)
    for d in ("sources", "audio", "transcripts", "clips", "outbox", "review", "logs"):
        cfg.dir(d)
    print(f"Storage:  {cfg.storage}")
    print(f"Database: {cfg.db_path}")
    print("Niches:   " + ", ".join(cfg.niches))
    print("Next: python -m clipforge check   then   python -m clipforge dashboard")
    return 0


def cmd_check(args) -> int:
    cfg, _ = _ctx(args)
    ok = True

    def line(name, good, detail=""):
        nonlocal ok
        ok &= bool(good) or name.startswith("(optional)")
        print(f"  [{'OK' if good else '--'}] {name}{('  ' + detail) if detail else ''}")

    print("ClipForge environment check")
    line("ffmpeg", shutil.which("ffmpeg"), shutil.which("ffmpeg") or "install: winget install Gyan.FFmpeg")
    line("ffprobe", shutil.which("ffprobe"))
    try:
        from clipforge.pipeline.ffmpeg import encoder_works, pick_encoder
        enc = pick_encoder(cfg["render"]["encoder"])
        line("video encoder", True, enc + ("  (Intel Quick Sync)" if enc.endswith("_qsv") else "  (CPU)"))
        line("(optional) Intel Quick Sync h264_qsv", encoder_works("h264_qsv"))
    except Exception as exc:  # noqa: BLE001
        line("video encoder", False, str(exc))
    for mod, label in (("yt_dlp", "yt-dlp"), ("faster_whisper", "faster-whisper"), ("numpy", "numpy"),
                       ("fastapi", "fastapi")):
        try:
            __import__(mod)
            line(label, True)
        except ImportError:
            line(label, False, "pip install -r requirements.txt")
    from clipforge.pipeline import llm
    line("(optional) Ollama local AI", llm.available(cfg["llm"]["url"]),
         "enabled in config" if cfg["llm"]["enabled"] else "disabled in config")
    free = shutil.disk_usage(cfg.storage if cfg.storage.exists() else Path.cwd()).free / 1e9
    line("disk space", free > 50, f"{free:.0f} GB free at {cfg.storage}")
    return 0 if ok else 1


def cmd_dashboard(args) -> int:
    import uvicorn

    cfg = load_config(args.config)
    from clipforge.web.app import create_app

    host = args.host or cfg["dashboard"]["host"]
    port = args.port or int(cfg["dashboard"]["port"])
    print(f"ClipForge dashboard: http://{host}:{port}")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
    return 0


def cmd_worker(args) -> int:
    from clipforge import worker

    worker.run(load_config(args.config), once=args.once, periodic=not args.no_periodic)
    return 0


def cmd_submit(args) -> int:
    cfg, conn = _ctx(args)
    sid = services.submit_source(conn, cfg, args.url, creator_id=args.creator, own_content=args.own,
                                 niche=args.niche)
    print(f"Queued source #{sid}. Run the worker to process it.")
    return 0


def cmd_process(args) -> int:
    """One-shot: submit + process synchronously (the 'paste a link, get clips' flow)."""
    from clipforge import worker

    cfg, conn = _ctx(args)
    sid = services.submit_source(conn, cfg, args.url, creator_id=args.creator, own_content=args.own,
                                 niche=args.niche, priority=10)
    worker.setup_logging(cfg)
    while True:
        job = db.claim_job(conn)
        if not job:
            break
        try:
            worker.run_job(conn, cfg, job)
            db.update(conn, "jobs", job["id"], status="done", finished_at=db.now())
        except Exception as exc:  # noqa: BLE001
            worker._fail(conn, cfg, job, exc)
    rows = db.all_(conn, "SELECT * FROM candidates WHERE source_id=? ORDER BY rank", sid)
    print(f"\nSource #{sid}: {len(rows)} clips")
    for r in rows:
        print(f"  #{r['rank']:>2} score {r['score']:5.1f}  {r['start']:8.1f}-{r['end']:8.1f}s  "
              f"{r['status']:9}  {r['clip_path'] or ''}")
        print(f"       {r['title']}")
    return 0


def cmd_add_creator(args) -> int:
    cfg, conn = _ctx(args)
    facecam = json.loads(args.facecam) if args.facecam else None
    cid = services.add_creator(conn, name=args.name, niche=args.niche, platform=args.platform,
                               channel_url=args.channel or "", crop_mode=args.crop, facecam=facecam,
                               auto_discover=args.auto_discover)
    if args.granted_by:
        services.grant_permission(conn, cid, granted_by=args.granted_by, evidence=args.evidence or "")
    print(f"Creator #{cid} added{' with permission' if args.granted_by else ' (no permission recorded yet)'}.")
    return 0


def cmd_add_account(args) -> int:
    cfg, conn = _ctx(args)
    aid = services.add_account(conn, name=args.name, platform=args.platform, niche=args.niche, handle=args.handle or "")
    print(f"Account #{aid} added.")
    return 0


def cmd_claude_export(args) -> int:
    from clipforge.pipeline.claude_handoff import export_brief

    cfg, conn = _ctx(args)
    src = db.one(conn, "SELECT * FROM sources WHERE id=?", args.source_id)
    if not src:
        print("No such source", file=sys.stderr)
        return 1
    cands = [dict(r) for r in db.all_(conn, "SELECT * FROM candidates WHERE source_id=? ORDER BY rank", src["id"])]
    path = export_brief(cfg.dir("review") / f"source_{src['id']}.md", dict(src), cands,
                        cfg.niche(src["niche"]).get("name", src["niche"]))
    try:
        rel = path.relative_to(Path.cwd())
    except ValueError:
        rel = path
    print(f"Wrote {path}\nIn Claude Code, say:  Read {rel.as_posix()} and follow the instructions in it.")
    print(f"Then run:  python -m clipforge claude-import {src['id']}")
    return 0


def cmd_claude_import(args) -> int:
    from clipforge.pipeline.claude_handoff import load_picks

    cfg, conn = _ctx(args)
    path = Path(args.file) if args.file else cfg.dir("review") / f"source_{args.source_id}.picks.json"
    picks = load_picks(path)
    changed = 0
    for p in picks:
        cand = db.one(conn, "SELECT * FROM candidates WHERE id=? AND source_id=?", int(p["id"]), args.source_id)
        if not cand:
            continue
        score = float(p.get("score", 0))
        if 1 <= score <= 10:
            blended = round(0.4 * (cand["heuristic_score"] or cand["score"]) + 0.6 * score * 10, 2)
            db.update(conn, "candidates", cand["id"], score=blended, llm_score=score / 10,
                      llm_reason=("Claude: " + str(p.get("reason", "")))[:300])
        start = p.get("start", cand["start"])
        end = p.get("end", cand["end"])
        services.edit_candidate(conn, cand["id"], title=p.get("title") or cand["title"],
                                hook=p.get("hook") or cand["hook"], start=float(start), end=float(end))
        if p.get("keep") is False and cand["status"] not in ("approved",):
            services.reject(conn, cand["id"], notes="Rejected by Claude review: " + str(p.get("reason", "")))
        changed += 1
    print(f"Applied {changed} picks from {path}. Changed hooks/times were queued for re-render.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipforge", description="Local AI clipping studio")
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create storage folders and database").set_defaults(fn=cmd_init)
    sub.add_parser("check", help="verify ffmpeg, encoders, python packages, Ollama").set_defaults(fn=cmd_check)

    p = sub.add_parser("dashboard", help="run the review dashboard")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("worker", help="run the background job worker")
    p.add_argument("--once", action="store_true", help="exit when the queue is empty")
    p.add_argument("--no-periodic", action="store_true", help="don't schedule discovery/cleanup")
    p.set_defaults(fn=cmd_worker)

    for name, fn, helptext in (("submit", cmd_submit, "queue a VOD URL or local file"),
                               ("process", cmd_process, "submit and process right now (blocking)")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("url")
        p.add_argument("--creator", type=int, help="creator id (must have permission on file)")
        p.add_argument("--own", action="store_true", help="this is your own content")
        p.add_argument("--niche")
        p.set_defaults(fn=fn)

    p = sub.add_parser("add-creator", help="add a creator (optionally with permission)")
    p.add_argument("name")
    p.add_argument("--niche", required=True)
    p.add_argument("--platform", default="twitch")
    p.add_argument("--channel", help="channel/videos URL for auto-discovery")
    p.add_argument("--crop", choices=["center", "blur", "split"])
    p.add_argument("--facecam", help='JSON fractions, e.g. {"x":0.75,"y":0.7,"w":0.25,"h":0.3}')
    p.add_argument("--auto-discover", action="store_true")
    p.add_argument("--granted-by", help="who granted permission (records it)")
    p.add_argument("--evidence", help="link/path to the written permission")
    p.set_defaults(fn=cmd_add_creator)

    p = sub.add_parser("add-account", help="add one of your social accounts")
    p.add_argument("name")
    p.add_argument("--platform", required=True, choices=services.PLATFORMS)
    p.add_argument("--niche", required=True)
    p.add_argument("--handle")
    p.set_defaults(fn=cmd_add_account)

    p = sub.add_parser("claude-export", help="write a review brief for Claude Code")
    p.add_argument("source_id", type=int)
    p.set_defaults(fn=cmd_claude_export)
    p = sub.add_parser("claude-import", help="apply Claude Code's picks file")
    p.add_argument("source_id", type=int)
    p.add_argument("--file")
    p.set_defaults(fn=cmd_claude_import)

    args = ap.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except services.PermissionDenied as exc:
        print(f"Permission required: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
