from fastapi.testclient import TestClient

from clipforge import db, services
from clipforge.web.app import create_app


def test_pages_and_forms(cfg, conn, vod):
    client = TestClient(create_app(cfg))
    for path in ("/", "/sources", "/creators", "/accounts", "/review", "/publish", "/analytics", "/jobs"):
        r = client.get(path)
        assert r.status_code == 200, path

    r = client.post("/creators", data={"name": "Streamy", "niche": "gaming", "platform": "twitch",
                                       "facecam": "0.7,0.7,0.3,0.3", "granted_by": "@streamy"})
    assert r.status_code == 200 and "Added Streamy" in r.text
    cid = db.one(conn, "SELECT id FROM creators WHERE name='Streamy'")["id"]

    r = client.post("/sources", data={"url": str(vod), "creator_id": str(cid)})
    assert "queued" in r.text
    r = client.post("/sources", data={"url": str(vod)})          # no creator, not own -> refused
    assert "permission" in r.text.lower()

    r = client.post("/accounts", data={"name": "Clutch", "platform": "tiktok", "niche": "gaming"})
    assert "Added Clutch" in r.text
    assert client.get("/review?status=rendered&niche=gaming").status_code == 200
