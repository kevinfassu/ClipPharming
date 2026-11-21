"""Review dashboard (FastAPI + server-rendered templates, no JS build step)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from clipforge import __version__, db, services
from clipforge.config import Config, load_config

HERE = Path(__file__).parent


def _fmt_time(t: Optional[float]) -> str:
    if t is None:
        return ""
    t = int(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _ThreadConn:
    """One SQLite connection per server thread (FastAPI runs sync handlers in a pool)."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._local = threading.local()

    def _get(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect(self._cfg)
        return c

    def __getattr__(self, name):
        return getattr(self._get(), name)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    conn = _ThreadConn(cfg)
    db.init_db(conn)
    storage = cfg.storage.resolve()
    storage.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="ClipForge", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media", StaticFiles(directory=storage), name="media")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def media_url(path: Optional[str]) -> str:
        if not path:
            return ""
        try:
            return "/media/" + Path(path).resolve().relative_to(storage).as_posix()
        except ValueError:
            return ""

    templates.env.filters["media"] = media_url
    templates.env.filters["hms"] = _fmt_time
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
    templates.env.globals["niches"] = cfg.niches
    templates.env.globals["platforms"] = services.PLATFORMS
    templates.env.globals["source_platforms"] = services.SOURCE_PLATFORMS
    templates.env.globals["version"] = __version__

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        counts = db.one(conn, """SELECT
            (SELECT COUNT(*) FROM candidates WHERE status='rendered') AS review,
            (SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')) AS jobs,
            (SELECT COUNT(*) FROM publish_queue WHERE status IN ('pending','exported')) AS publish""")
        return templates.TemplateResponse(request, name, {"counts": counts, "msg": request.query_params.get("msg"),
                                                          "err": request.query_params.get("err"), **ctx})

    def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
        from urllib.parse import urlencode

        q = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        sep = "&" if "?" in url else "?"
        return RedirectResponse(url + (sep + q if q else ""), status_code=303)

    # ---------------------------------------------------------------- overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        stats = db.one(conn, """SELECT
            (SELECT COUNT(*) FROM sources) AS sources,
            (SELECT COUNT(*) FROM candidates) AS clips,
            (SELECT COUNT(*) FROM candidates WHERE status='approved') AS approved,
            (SELECT COUNT(*) FROM publish_queue WHERE status='posted') AS posted,
            (SELECT COALESCE(SUM(views),0) FROM post_stats ps WHERE ps.id IN
                (SELECT MAX(id) FROM post_stats GROUP BY publish_id)) AS views""")
        sources = db.all_(conn, """SELECT s.*, c.name AS creator FROM sources s LEFT JOIN creators c
                                   ON c.id = s.creator_id ORDER BY s.id DESC LIMIT 8""")
        jobs = db.all_(conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 10")
        worker_seen = db.one(conn, "SELECT MAX(started_at) AS t FROM jobs")["t"]
        return page(request, "overview.html", stats=stats, sources=sources, jobs=jobs, worker_seen=worker_seen)

    # ---------------------------------------------------------------- sources
    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        rows = db.all_(conn, """SELECT s.*, c.name AS creator,
                (SELECT COUNT(*) FROM candidates WHERE source_id = s.id) AS n_clips
                FROM sources s LEFT JOIN creators c ON c.id = s.creator_id ORDER BY s.id DESC LIMIT 200""")
        creators = db.all_(conn, "SELECT * FROM creators ORDER BY name")
        allowed = {c["id"] for c in creators if services.active_permission(conn, c["id"])}
        return page(request, "sources.html", sources=rows, creators=creators, allowed=allowed)

    @app.post("/sources")
    def sources_add(url: str = Form(...), creator_id: str = Form(""), own_content: str = Form(""),
                    test_only: str = Form(""), niche: str = Form("")):
        try:
            sid = services.submit_source(conn, cfg, url, creator_id=int(creator_id) if creator_id else None,
                                         own_content=bool(own_content), test_only=bool(test_only),
                                         niche=niche or None)
        except (services.PermissionDenied, ValueError) as exc:
            return back("/sources", err=str(exc))
        return back("/sources", msg=f"Source #{sid} queued")

    @app.post("/sources/{sid}/retry")
    def sources_retry(sid: int):
        services.retry_source(conn, sid)
        return back("/sources", msg=f"Source #{sid} re-queued")

    # ---------------------------------------------------------------- creators
    @app.get("/creators", response_class=HTMLResponse)
    def creators_page(request: Request):
        creators = db.all_(conn, "SELECT * FROM creators ORDER BY name")
        perms = {c["id"]: db.all_(conn, "SELECT * FROM permissions WHERE creator_id=? ORDER BY id DESC", c["id"])
                 for c in creators}
        active = {c["id"]: services.active_permission(conn, c["id"]) for c in creators}
        return page(request, "creators.html", creators=creators, perms=perms, active=active)

    @app.post("/creators")
    def creators_add(name: str = Form(...), niche: str = Form(...), platform: str = Form("twitch"),
                     channel_url: str = Form(""), crop_mode: str = Form(""), facecam: str = Form(""),
                     crop_x: float = Form(0.5), auto_discover: str = Form(""), notes: str = Form(""),
                     granted_by: str = Form(""), evidence: str = Form(""), expires_at: str = Form("")):
        fc = None
        if facecam.strip():
            try:
                x, y, w, h = (float(v) for v in facecam.replace(" ", "").split(","))
                fc = {"x": x, "y": y, "w": w, "h": h}
            except ValueError:
                return back("/creators", err="Facecam must be four numbers: x,y,w,h (fractions 0-1)")
        cid = services.add_creator(conn, name=name, niche=niche, platform=platform, channel_url=channel_url,
                                   crop_mode=crop_mode or None, facecam=fc, crop_x=crop_x,
                                   auto_discover=bool(auto_discover), notes=notes)
        if granted_by.strip():
            services.grant_permission(conn, cid, granted_by=granted_by, evidence=evidence, expires_at=expires_at or None)
        return back("/creators", msg=f"Added {name}")

    @app.post("/creators/{cid}/permission")
    def creators_permit(cid: int, granted_by: str = Form(...), evidence: str = Form(""), expires_at: str = Form(""),
                        allows_monetization: str = Form("1"), notes: str = Form("")):
        try:
            services.grant_permission(conn, cid, granted_by=granted_by, evidence=evidence,
                                      expires_at=expires_at or None, allows_monetization=allows_monetization == "1",
                                      notes=notes)
        except ValueError as exc:
            return back("/creators", err=str(exc))
        return back("/creators", msg="Permission recorded")

    @app.post("/permissions/{pid}/revoke")
    def permission_revoke(pid: int):
        services.revoke_permission(conn, pid)
        return back("/creators", msg="Permission revoked - no new VODs will be processed for this creator")

    @app.post("/creators/{cid}/toggle-discover")
    def creators_toggle(cid: int):
        conn.execute("UPDATE creators SET auto_discover = 1 - auto_discover WHERE id = ?", (cid,))
        return back("/creators")

    # ---------------------------------------------------------------- accounts
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(request: Request):
        return page(request, "accounts.html", accounts=db.all_(conn, "SELECT * FROM accounts ORDER BY niche, name"))

    @app.post("/accounts")
    def accounts_add(name: str = Form(...), platform: str = Form(...), niche: str = Form(...),
                     handle: str = Form("")):
        services.add_account(conn, name=name, platform=platform, niche=niche, handle=handle)
        return back("/accounts", msg=f"Added {name}")

    # ---------------------------------------------------------------- review
    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, status: str = "rendered", niche: str = "", source: str = ""):
        where, args = ["c.status = ?"], [status]
        if niche:
            where.append("s.niche = ?")
            args.append(niche)
        if source:
            where.append("s.id = ?")
            args.append(int(source))
        clips = db.all_(conn, f"""SELECT c.*, s.niche, s.title AS source_title, s.url AS source_url, s.test_only,
                                  cr.name AS creator FROM candidates c JOIN sources s ON s.id = c.source_id
                                  LEFT JOIN creators cr ON cr.id = s.creator_id WHERE {' AND '.join(where)}
                                  ORDER BY c.score DESC LIMIT 60""", *args)
        accounts = db.all_(conn, "SELECT * FROM accounts WHERE active=1 ORDER BY niche, name")
        return page(request, "review.html", clips=clips, accounts=accounts, status=status, niche=niche,
                    source=source)

    @app.post("/clips/{cid}/approve")
    async def clip_approve(request: Request, cid: int):
        form = await request.form()
        account_ids = [int(a) for a in form.getlist("account_ids")]
        if not account_ids:
            return back(request.headers.get("referer", "/review"), err="Pick at least one account")
        try:
            services.approve(conn, cid, account_ids=account_ids, scheduled_at=str(form.get("scheduled_at") or ""),
                             caption=str(form.get("caption") or ""))
        except ValueError as exc:
            return back("/review", err=str(exc))
        return back(request.headers.get("referer", "/review"), msg=f"Clip #{cid} approved")

    @app.post("/clips/{cid}/reject")
    def clip_reject(request: Request, cid: int, notes: str = Form("")):
        services.reject(conn, cid, notes)
        return back(request.headers.get("referer", "/review"), msg=f"Clip #{cid} rejected")

    @app.post("/clips/{cid}/edit")
    def clip_edit(request: Request, cid: int, title: str = Form(""), hook: str = Form(""),
                  start: float = Form(...), end: float = Form(...)):
        try:
            services.edit_candidate(conn, cid, title=title, hook=hook, start=start, end=end)
        except ValueError as exc:
            return back("/review", err=str(exc))
        return back(request.headers.get("referer", "/review"), msg=f"Clip #{cid} saved (re-render queued if needed)")

    @app.post("/clips/{cid}/restore")
    def clip_restore(request: Request, cid: int):
        c = db.one(conn, "SELECT * FROM candidates WHERE id=?", cid)
        if c and c["clip_path"]:
            db.update(conn, "candidates", cid, status="rendered")
        return back(request.headers.get("referer", "/review"), msg="Moved back to review")

    # ---------------------------------------------------------------- publishing
    @app.get("/publish", response_class=HTMLResponse)
    def publish_page(request: Request):
        rows = db.all_(conn, """SELECT p.*, c.title, c.clip_path, c.thumb_path, a.name AS account, a.platform,
                (SELECT views FROM post_stats WHERE publish_id = p.id ORDER BY id DESC LIMIT 1) AS views
                FROM publish_queue p JOIN candidates c ON c.id = p.candidate_id JOIN accounts a ON a.id = p.account_id
                WHERE p.status != 'cancelled' ORDER BY CASE p.status WHEN 'pending' THEN 0 WHEN 'exported' THEN 1
                ELSE 2 END, COALESCE(p.scheduled_at, p.created_at) LIMIT 200""")
        return page(request, "publish.html", rows=rows)

    @app.post("/publish/{pid}/export")
    def publish_export(pid: int):
        path = services.export_publish(conn, cfg, pid)
        return back("/publish", msg=f"Exported to {path}")

    @app.post("/publish/{pid}/posted")
    def publish_posted(pid: int, post_url: str = Form("")):
        services.mark_posted(conn, pid, post_url)
        return back("/publish", msg="Marked as posted")

    @app.post("/publish/{pid}/cancel")
    def publish_cancel(pid: int):
        db.update(conn, "publish_queue", pid, status="cancelled")
        return back("/publish", msg="Cancelled")

    @app.post("/publish/{pid}/stats")
    def publish_stats(pid: int, views: str = Form(""), likes: str = Form(""), comments: str = Form(""),
                      shares: str = Form(""), avg_view_pct: str = Form(""), followers_gained: str = Form(""),
                      revenue: str = Form("")):
        def num(v, f=int):
            try:
                return f(v) if v.strip() else None
            except ValueError:
                return None
        services.record_stats(conn, pid, views=num(views), likes=num(likes), comments=num(comments),
                              shares=num(shares), avg_view_pct=num(avg_view_pct, float),
                              followers_gained=num(followers_gained), revenue=num(revenue, float))
        return back("/publish", msg="Stats saved")

    # ---------------------------------------------------------------- analytics / jobs
    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request):
        return page(request, "analytics.html", a=services.analytics(conn))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return page(request, "jobs.html", jobs=db.all_(conn, "SELECT * FROM jobs ORDER BY id DESC LIMIT 200"))

    @app.post("/jobs/discover")
    def jobs_discover():
        db.enqueue(conn, "discover", priority=1)
        return back("/jobs", msg="Discovery queued")

    @app.get("/api/status")
    def api_status():
        rows = db.all_(conn, "SELECT id, status, stage, progress FROM sources WHERE status IN ('queued','processing','rendering')")
        return {"active": [dict(r) for r in rows]}

    return app
