"""Source ingestion with yt-dlp (or a local file)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable


_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def looks_like_path(url: str) -> bool:
    """True for C:\\..., /home/..., relative paths and file:// - anything that isn't a web URL."""
    url = url.strip().strip('"').strip("'")
    return url.startswith("file://") or not _URL_RE.match(url)


def is_local(url: str) -> bool:
    if not looks_like_path(url):
        return False
    try:
        return local_path(url).exists()
    except OSError:
        return False


def check_local(url: str) -> None:
    """Raise a clear error for a local path that doesn't exist (instead of passing it to yt-dlp)."""
    if looks_like_path(url) and not is_local(url):
        raise FileNotFoundError(
            f"Video file not found: {url}\n"
            "Check the path. Tip: in File Explorer, Shift + right-click the video and choose "
            "'Copy as path', then paste it inside the quotes.")


def local_path(url: str) -> Path:
    url = url.strip().strip('"').strip("'")
    return Path(url[7:] if url.startswith("file://") else url).expanduser().resolve()


def download(url: str, out_dir: Path, *, max_height: int = 1080, cookies_from_browser: str | None = None,
             progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Download a VOD. Returns {path, title, duration, id}. Local files are used in place."""
    out_dir.mkdir(parents=True, exist_ok=True)
    check_local(url)
    if is_local(url):
        src = local_path(url)
        return {"path": str(src), "title": src.stem, "duration": None, "id": f"local:{src}"}

    import yt_dlp  # lazy: heavy import

    def hook(d: dict) -> None:
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(0.99, d.get("downloaded_bytes", 0) / total))

    opts: dict[str, Any] = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        "outtmpl": str(out_dir / "%(extractor)s_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            # merged output may have a different extension
            matches = sorted(out_dir.glob(path.stem + ".*"), key=lambda p: p.stat().st_size, reverse=True)
            if not matches:
                raise FileNotFoundError(f"yt-dlp reported success but no file found for {url}")
            path = matches[0]
    return {
        "path": str(path),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "id": f"{info.get('extractor_key', 'web').lower()}:{info.get('id')}",
    }


def list_channel(url: str, limit: int = 5) -> list[dict[str, Any]]:
    """Latest videos/VODs on a channel page, newest first, without downloading."""
    import yt_dlp

    opts = {"quiet": True, "extract_flat": "in_playlist", "playlistend": limit, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries") or []
    out = []
    for e in entries[:limit]:
        if not e:
            continue
        vid_url = e.get("url") or e.get("webpage_url")
        if vid_url and not vid_url.startswith("http"):
            vid_url = e.get("webpage_url") or vid_url
        out.append({
            "url": vid_url,
            "title": e.get("title"),
            "duration": e.get("duration"),
            "id": f"{(e.get('ie_key') or info.get('extractor_key') or 'web').lower()}:{e.get('id')}",
            "live": e.get("live_status") in ("is_live", "is_upcoming"),
        })
    return out


def delete_media(*paths: str | None) -> None:
    for p in paths:
        if not p:
            continue
        path = Path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
