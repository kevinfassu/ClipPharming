"""Business operations shared by the dashboard, the CLI and the worker."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from clipforge import db
from clipforge.config import Config

PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"]
SOURCE_PLATFORMS = ["twitch", "youtube", "kick", "other"]


class PermissionDenied(Exception):
    pass


# ---------------------------------------------------------------- creators
def add_creator(conn: sqlite3.Connection, *, name: str, niche: str, platform: str = "twitch",
                channel_url: str = "", crop_mode: str | None = None, facecam: dict | None = None,
                crop_x: float = 0.5, auto_discover: bool = False, notes: str = "") -> int:
    return db.insert(conn, "creators", name=name.strip(), niche=niche, platform=platform,
                     channel_url=channel_url.strip() or None, crop_mode=crop_mode or None,
                     facecam=json.dumps(facecam) if facecam else None, crop_x=float(crop_x),
                     auto_discover=int(bool(auto_discover)), notes=notes or None, created_at=db.now())


def grant_permission(conn: sqlite3.Connection, creator_id: int, *, granted_by: str, evidence: str = "",
                     expires_at: str | None = None, allows_monetization: bool = True, scope: str = "clips",
                     notes: str = "") -> int:
    if not granted_by.strip():
        raise ValueError("Record who granted permission")
    return db.insert(conn, "permissions", creator_id=creator_id, granted_by=granted_by.strip(),
                     granted_at=db.now(), expires_at=expires_at or None, scope=scope,
                     allows_monetization=int(bool(allows_monetization)), evidence=evidence or None,
                     notes=notes or None)


def revoke_permission(conn: sqlite3.Connection, permission_id: int) -> None:
    db.update(conn, "permissions", permission_id, revoked_at=db.now())


def active_permission(conn: sqlite3.Connection, creator_id: int | None) -> sqlite3.Row | None:
    if not creator_id:
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    return db.one(conn, """SELECT * FROM permissions WHERE creator_id = ? AND revoked_at IS NULL
                           AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)
                           ORDER BY id DESC LIMIT 1""", creator_id, today)


def check_source_allowed(conn: sqlite3.Connection, source: sqlite3.Row) -> None:
    if source["own_content"]:
        return
    if not active_permission(conn, source["creator_id"]):
        raise PermissionDenied(
            "No active permission on file for this creator. Add one on the Creators page "
            "(or mark the source as your own content).")


# ---------------------------------------------------------------- accounts
def add_account(conn: sqlite3.Connection, *, name: str, platform: str, niche: str, handle: str = "",
                show_credit: bool = True) -> int:
    return db.insert(conn, "accounts", name=name.strip(), platform=platform, niche=niche,
                     handle=handle.strip() or None, show_credit=int(bool(show_credit)), created_at=db.now())


# ---------------------------------------------------------------- sources
def submit_source(conn: sqlite3.Connection, cfg: Config, url: str, *, creator_id: int | None = None,
                  own_content: bool = False, niche: str | None = None, priority: int = 0,
                  title: str | None = None, external_id: str | None = None) -> int:
    url = url.strip().strip('"').strip("'")
    if not url:
        raise ValueError("URL or file path required")
    from clipforge.pipeline.download import check_local, is_local, local_path

    try:
        check_local(url)
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from None
    if is_local(url):
        url = str(local_path(url))
    creator = db.one(conn, "SELECT * FROM creators WHERE id = ?", creator_id) if creator_id else None
    if creator_id and not creator:
        raise ValueError(f"Creator {creator_id} not found")
    if not own_content and not active_permission(conn, creator_id):
        raise PermissionDenied("This creator has no active permission on file - add it first, "
                               "or tick 'my own content'.")
    niche = niche or (creator["niche"] if creator else None) or next(iter(cfg.niches))
    if niche not in cfg.niches:
        raise ValueError(f"Unknown niche '{niche}'. Configured: {', '.join(cfg.niches)}")
    with db.tx(conn):
        sid = db.insert(conn, "sources", creator_id=creator_id, own_content=int(bool(own_content)), url=url,
                        niche=niche, title=title, external_id=external_id, status="queued",
                        created_at=db.now(), updated_at=db.now())
        db.enqueue(conn, "ingest", source_id=sid, priority=priority)
    return sid


def retry_source(conn: sqlite3.Connection, source_id: int) -> None:
    with db.tx(conn):
        db.update(conn, "sources", source_id, status="queued", error=None)
        db.enqueue(conn, "ingest", source_id=source_id, priority=1)


# ---------------------------------------------------------------- review
def approve(conn: sqlite3.Connection, candidate_id: int, *, account_ids: list[int], scheduled_at: str | None,
            caption: str = "", notes: str = "") -> list[int]:
    cand = db.one(conn, "SELECT * FROM candidates WHERE id = ?", candidate_id)
    if not cand or not cand["clip_path"]:
        raise ValueError("Clip not rendered yet")
    ids = []
    with db.tx(conn):
        db.update(conn, "candidates", candidate_id, status="approved", reviewed_at=db.now(),
                  review_notes=notes or None)
        for acc in account_ids:
            ids.append(db.insert(conn, "publish_queue", candidate_id=candidate_id, account_id=acc,
                                 scheduled_at=scheduled_at or None, caption=caption or cand["title"],
                                 status="pending", created_at=db.now()))
    return ids


def reject(conn: sqlite3.Connection, candidate_id: int, notes: str = "") -> None:
    db.update(conn, "candidates", candidate_id, status="rejected", reviewed_at=db.now(), review_notes=notes or None)


def edit_candidate(conn: sqlite3.Connection, candidate_id: int, *, title: str | None = None,
                   hook: str | None = None, start: float | None = None, end: float | None = None,
                   rerender: bool = True) -> None:
    cand = db.one(conn, "SELECT * FROM candidates WHERE id = ?", candidate_id)
    if not cand:
        raise ValueError("Candidate not found")
    values: dict = {}
    if title is not None:
        values["title"] = title.strip()
    if hook is not None:
        values["hook"] = hook.strip()
    if start is not None and end is not None:
        if end - start < 3:
            raise ValueError("Clip must be at least 3 seconds")
        values["start"], values["end"] = float(start), float(end)
    visual_change = any(k in values and values[k] != cand[k] for k in ("hook", "start", "end"))
    with db.tx(conn):
        if values:
            db.update(conn, "candidates", candidate_id, **values)
        if rerender and visual_change:
            db.update(conn, "candidates", candidate_id, status="rendering")
            db.enqueue(conn, "render", candidate_id=candidate_id, source_id=cand["source_id"], priority=5)


# ---------------------------------------------------------------- publishing
def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:n] or "clip"


def export_publish(conn: sqlite3.Connection, cfg: Config, publish_id: int) -> Path:
    """Copy the clip + a caption sidecar to storage/outbox/<account>/ for manual upload."""
    row = db.one(conn, """SELECT p.*, c.clip_path, c.title, a.name AS account_name, a.platform,
                                 s.url AS source_url, cr.name AS creator_name
                          FROM publish_queue p JOIN candidates c ON c.id = p.candidate_id
                          JOIN accounts a ON a.id = p.account_id JOIN sources s ON s.id = c.source_id
                          LEFT JOIN creators cr ON cr.id = s.creator_id WHERE p.id = ?""", publish_id)
    if not row:
        raise ValueError("Publish item not found")
    folder = cfg.dir("outbox") / _slug(f"{row['account_name']}-{row['platform']}")
    folder.mkdir(parents=True, exist_ok=True)
    when = (row["scheduled_at"] or db.now())[:16].replace(":", "").replace("T", "_")
    base = folder / f"{when}_{publish_id:05d}_{_slug(row['title'])}"
    shutil.copy2(row["clip_path"], base.with_suffix(".mp4"))
    credit = f"\n\nCredit: {row['creator_name']} ({row['source_url']})" if row["creator_name"] else ""
    base.with_suffix(".txt").write_text(f"{row['caption'] or row['title']}{credit}\n", encoding="utf-8")
    db.update(conn, "publish_queue", publish_id, status="exported", exported_path=str(base.with_suffix(".mp4")))
    return base.with_suffix(".mp4")


def mark_posted(conn: sqlite3.Connection, publish_id: int, post_url: str = "") -> None:
    db.update(conn, "publish_queue", publish_id, status="posted", post_url=post_url or None, posted_at=db.now())


def record_stats(conn: sqlite3.Connection, publish_id: int, **stats) -> int:
    clean = {k: (None if v in ("", None) else v) for k, v in stats.items()}
    return db.insert(conn, "post_stats", publish_id=publish_id, recorded_at=db.now(), **clean)


def analytics(conn: sqlite3.Connection) -> dict:
    latest = """SELECT ps.* FROM post_stats ps JOIN (SELECT publish_id, MAX(id) mid FROM post_stats GROUP BY publish_id)
                x ON x.mid = ps.id"""
    by_account = db.all_(conn, f"""
        SELECT a.id, a.name, a.platform, a.niche, COUNT(p.id) AS posts,
               COALESCE(SUM(l.views),0) AS views, ROUND(AVG(l.avg_view_pct),1) AS avg_view_pct,
               COALESCE(SUM(l.followers_gained),0) AS followers, ROUND(COALESCE(SUM(l.revenue),0),2) AS revenue
        FROM accounts a LEFT JOIN publish_queue p ON p.account_id = a.id AND p.status = 'posted'
        LEFT JOIN ({latest}) l ON l.publish_id = p.id GROUP BY a.id ORDER BY views DESC""")
    by_length = db.all_(conn, f"""
        SELECT s.niche, CASE WHEN c.end - c.start < 30 THEN '<30s' WHEN c.end - c.start < 60 THEN '30-60s'
                             ELSE '60s+' END AS bucket,
               COUNT(*) AS posts, ROUND(AVG(l.views)) AS avg_views, ROUND(AVG(l.avg_view_pct),1) AS avg_view_pct
        FROM publish_queue p JOIN candidates c ON c.id = p.candidate_id JOIN sources s ON s.id = c.source_id
        JOIN ({latest}) l ON l.publish_id = p.id GROUP BY s.niche, bucket ORDER BY s.niche, bucket""")
    by_creator = db.all_(conn, f"""
        SELECT COALESCE(cr.name, 'Own content') AS creator, COUNT(*) AS posts, ROUND(AVG(l.views)) AS avg_views
        FROM publish_queue p JOIN candidates c ON c.id = p.candidate_id JOIN sources s ON s.id = c.source_id
        LEFT JOIN creators cr ON cr.id = s.creator_id JOIN ({latest}) l ON l.publish_id = p.id
        GROUP BY creator ORDER BY avg_views DESC""")
    return {"by_account": by_account, "by_length": by_length, "by_creator": by_creator}
