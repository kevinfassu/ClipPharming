import json
from pathlib import Path

import pytest

from clipforge import db, services, worker
from clipforge.pipeline import render
from clipforge.pipeline.ffmpeg import probe, video_size
from tests.conftest import write_transcript


def drain(conn, cfg):
    while (job := db.claim_job(conn)):
        try:
            worker.run_job(conn, cfg, job)
            db.update(conn, "jobs", job["id"], status="done", finished_at=db.now())
        except Exception as exc:  # noqa: BLE001
            worker._fail(conn, cfg, job, exc)


def test_end_to_end_local_file(cfg, conn, vod):
    sid = services.submit_source(conn, cfg, str(vod), own_content=True, niche="gaming")
    write_transcript(cfg, sid)
    drain(conn, cfg)
    src = db.one(conn, "SELECT * FROM sources WHERE id=?", sid)
    assert src["status"] == "done", src["error"]
    clips = db.all_(conn, "SELECT * FROM candidates WHERE source_id=? ORDER BY rank", sid)
    assert clips and all(c["status"] == "rendered" for c in clips)
    for c in clips:
        assert Path(c["clip_path"]).exists() and Path(c["thumb_path"]).exists()
        assert video_size(c["clip_path"]) == (360, 640)
        dur = float(probe(c["clip_path"])["format"]["duration"])
        assert abs(dur - (c["end"] - c["start"])) < 1.0

    # edit hook -> re-render queued and processed
    c = clips[0]
    services.edit_candidate(conn, c["id"], hook="New hook", start=c["start"], end=c["end"])
    drain(conn, cfg)
    assert db.one(conn, "SELECT status FROM candidates WHERE id=?", c["id"])["status"] == "rendered"

    # approve -> publish queue -> export
    acc = services.add_account(conn, name="Clutch Clips", platform="youtube", niche="gaming")
    (pid,) = services.approve(conn, c["id"], account_ids=[acc], scheduled_at="2026-12-01T18:00", caption="hi")
    out = services.export_publish(conn, cfg, pid)
    assert out.exists() and out.with_suffix(".txt").read_text().startswith("hi")
    services.mark_posted(conn, pid, "https://youtube.com/shorts/x")
    services.record_stats(conn, pid, views=1234, avg_view_pct=71.5)
    a = services.analytics(conn)
    assert a["by_account"][0]["views"] == 1234


def test_permission_required(cfg, conn, vod):
    cid = services.add_creator(conn, name="Streamer", niche="gaming")
    with pytest.raises(services.PermissionDenied):
        services.submit_source(conn, cfg, str(vod), creator_id=cid)
    pid = services.grant_permission(conn, cid, granted_by="@streamer", evidence="dm screenshot")
    sid = services.submit_source(conn, cfg, str(vod), creator_id=cid)
    # revoked before the worker gets to it -> ingest refuses
    services.revoke_permission(conn, pid)
    job = db.claim_job(conn)
    with pytest.raises(services.PermissionDenied):
        worker.run_job(conn, cfg, job)
    worker._fail(conn, cfg, job, services.PermissionDenied("x"))
    assert db.one(conn, "SELECT status FROM sources WHERE id=?", sid)["status"] == "failed"


def test_expired_permission(cfg, conn):
    cid = services.add_creator(conn, name="Old", niche="gaming")
    services.grant_permission(conn, cid, granted_by="x", expires_at="2001-01-01")
    assert services.active_permission(conn, cid) is None


@pytest.mark.parametrize("mode", ["center", "blur", "split"])
def test_render_modes(tmp_path, vod, mode):
    out = tmp_path / "c.mp4"
    rcfg = {"width": 360, "height": 640, "fps": 15, "encoder": "libx264", "crf": 30, "audio_bitrate": "96k"}
    render.render_clip(vod, 10, 14, out, rcfg, mode=mode,
                       facecam={"x": 0.7, "y": 0.7, "w": 0.3, "h": 0.3} if mode == "split" else None)
    assert video_size(out) == (360, 640)


def test_transcribe_with_stub_model(tmp_path, monkeypatch, vod):
    """faster-whisper API contract, without downloading a model."""
    from types import SimpleNamespace as NS

    from clipforge.pipeline import audio, transcribe

    class Stub:
        def transcribe(self, samples, **kw):
            assert kw["word_timestamps"] is True and samples.dtype.name == "float32"
            w = [NS(start=1.0, end=1.4, word=" hello", probability=0.9), NS(start=1.5, end=1.9, word=" chat!", probability=0.8)]
            return iter([NS(end=2.0, words=w)]), NS(language="en")

    monkeypatch.setattr(transcribe, "_model", lambda tcfg: Stub())
    wav = audio.extract_audio(vod, tmp_path / "a.wav")
    tcfg = {"mode": "hotspots", "hotspot_count": 2, "hotspot_seconds": 10, "language": "en"}
    data = transcribe.transcribe(wav, tmp_path / "t.json", tcfg, excite=audio.excitement(audio.loudness_curve(wav)))
    assert data["mode"] == "hotspots" and len(data["regions"]) == 2
    assert data["words"][0]["word"] == "hello"
    assert data["words"][0]["start"] >= data["regions"][0][0]   # offsets applied
    assert json.loads((tmp_path / "t.json").read_text())["words"]


def test_claude_handoff_roundtrip(cfg, conn, vod):
    from clipforge.cli import main

    sid = services.submit_source(conn, cfg, str(vod), own_content=True, niche="gaming")
    write_transcript(cfg, sid)
    drain(conn, cfg)
    cfg_path = str(cfg.root / "config.yaml")
    assert main(["--config", cfg_path, "claude-export", str(sid)]) == 0
    brief = cfg.dir("review") / f"source_{sid}.md"
    assert "Candidate id=" in brief.read_text()
    cands = db.all_(conn, "SELECT * FROM candidates WHERE source_id=?", sid)
    picks = [{"id": cands[0]["id"], "score": 9, "keep": True, "title": "Best one", "hook": "WAIT FOR IT"},
             {"id": cands[-1]["id"], "score": 2, "keep": False, "reason": "no payoff"}]
    brief.with_suffix(".picks.json").write_text(json.dumps(picks))
    assert main(["--config", cfg_path, "claude-import", str(sid)]) == 0
    top = db.one(conn, "SELECT * FROM candidates WHERE id=?", cands[0]["id"])
    assert top["title"] == "Best one" and top["llm_score"] == 0.9
    if len(cands) > 1:
        assert db.one(conn, "SELECT status FROM candidates WHERE id=?", cands[-1]["id"])["status"] == "rejected"


def test_missing_local_file_gives_clear_error(cfg, conn):
    from clipforge.pipeline.download import looks_like_path

    assert looks_like_path(r"C:\Users\YOU\Videos\test.mp4")
    assert looks_like_path("/home/me/v.mp4") and looks_like_path("file:///tmp/v.mp4")
    assert not looks_like_path("https://www.twitch.tv/videos/123")
    with pytest.raises(ValueError, match="Video file not found"):
        services.submit_source(conn, cfg, r"C:\Users\YOU\Videos\test.mp4", own_content=True)
    assert db.one(conn, "SELECT COUNT(*) n FROM sources")["n"] == 0


def test_quoted_local_path_accepted(cfg, conn, vod):
    sid = services.submit_source(conn, cfg, f'"{vod}"', own_content=True)
    assert db.one(conn, "SELECT url FROM sources WHERE id=?", sid)["url"] == str(vod)
