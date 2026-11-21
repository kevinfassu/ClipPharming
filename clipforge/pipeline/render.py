"""Vertical 9:16 rendering with FFmpeg.

Crop modes
  center - fill the frame from the middle (or `crop_x` focus) of the source.
  blur   - full source frame in the middle over a blurred, zoomed copy (safe for gameplay/HUDs).
  split  - streamer facecam on top (40%), gameplay underneath (needs a facecam rectangle).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from clipforge.pipeline.ffmpeg import ffmpeg_bin, has_audio, pick_encoder, video_size


def _fc_crop(facecam: dict) -> str:
    fx, fy = float(facecam["x"]), float(facecam["y"])
    fw, fh = float(facecam["w"]), float(facecam["h"])
    return f"crop=w=iw*{fw:.4f}:h=ih*{fh:.4f}:x=iw*{fx:.4f}:y=ih*{fy:.4f}"


def build_filter(mode: str, W: int, H: int, fps: int, *, crop_x: float = 0.5,
                 facecam: dict | None = None, ass_name: str | None = None, pix_fmt: str = "yuv420p") -> str:
    cx = min(1.0, max(0.0, float(crop_x)))
    if mode == "split" and facecam:
        top_h = int(H * 0.4) // 2 * 2
        bot_h = H - top_h
        graph = (
            f"[0:v]split=2[cam][game];"
            f"[cam]{_fc_crop(facecam)},scale={W}:{top_h}:force_original_aspect_ratio=increase,"
            f"crop={W}:{top_h},setsar=1[top];"
            f"[game]scale={W}:{bot_h}:force_original_aspect_ratio=increase,"
            f"crop={W}:{bot_h}:(iw-ow)*{cx:.3f}:(ih-oh)/2,setsar=1[bot];"
            f"[top][bot]vstack=inputs=2[v0]"
        )
    elif mode == "blur":
        graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"gblur=sigma=12,eq=brightness=-0.10:saturation=1.2,scale={W}:{H},setsar=1[bg];"
            f"[fgsrc]scale={W}:-2,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2-120[v0]"
        )
    else:  # center
        graph = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H}:(iw-ow)*{cx:.3f}:(ih-oh)/2,setsar=1[v0]"
        )
    tail = f"[v0]fps={fps}"
    if ass_name:
        tail += f",ass={ass_name}"
    tail += f",format={pix_fmt}[v]"
    return graph + ";" + tail


def render_clip(source: str | Path, start: float, end: float, out_path: str | Path, rcfg: dict, *,
                mode: str = "center", crop_x: float = 0.5, facecam: dict | None = None,
                ass_path: str | Path | None = None) -> Path:
    source = Path(source).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    W, H, fps = int(rcfg["width"]), int(rcfg["height"]), int(rcfg["fps"])
    encoder = pick_encoder(rcfg.get("encoder", "auto"))
    if mode == "split" and not facecam:
        mode = "blur"
    src_w, src_h = video_size(source)
    if src_w / src_h <= W / H + 0.01 and mode != "split":
        mode = "center"  # already vertical: just fit it

    # libass paths are painful on Windows (drive-letter colons), so run ffmpeg
    # from the clip's folder and reference the subtitle file by bare name.
    ass_name = None
    if ass_path:
        ass_path = Path(ass_path).resolve()
        if ass_path.parent != out_path.parent:
            raise ValueError("ASS file must live next to the output clip")
        ass_name = ass_path.name
    pix_fmt = "nv12" if encoder.endswith("_qsv") else "yuv420p"
    graph = build_filter(mode, W, H, fps, crop_x=crop_x, facecam=facecam, ass_name=ass_name, pix_fmt=pix_fmt)

    duration = max(0.1, end - start)
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
           "-filter_complex", graph, "-map", "[v]"]
    if has_audio(source):
        cmd += ["-map", "0:a:0"]
        afilters = ["aresample=48000"]
        if rcfg.get("loudnorm", True):
            afilters.insert(0, "loudnorm=I=-14:TP=-1.5:LRA=11")
        cmd += ["-af", ",".join(afilters), "-c:a", "aac", "-b:a", str(rcfg.get("audio_bitrate", "160k")), "-ac", "2"]
    else:
        cmd += ["-an"]
    if encoder.endswith("_qsv"):
        cmd += ["-c:v", encoder, "-preset", "medium", "-global_quality", str(rcfg.get("qsv_quality", 22)),
                "-look_ahead", "0"]
    else:
        cmd += ["-c:v", encoder, "-preset", "veryfast", "-crf", str(rcfg.get("crf", 20)), "-profile:v", "high"]
    if int(rcfg.get("threads", 0)):
        cmd += ["-threads", str(int(rcfg["threads"]))]
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    cmd += ["-r", str(fps), "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, cwd=str(out_path.parent), capture_output=True, text=True)
    if proc.returncode != 0 and encoder.endswith("_qsv"):
        # Hardware encoder hiccup -> retry on CPU rather than failing the clip.
        return render_clip(source, start, end, out_path, {**rcfg, "encoder": "libx264"}, mode=mode,
                           crop_x=crop_x, facecam=facecam, ass_path=ass_path)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    tmp.replace(out_path)
    return out_path


def thumbnail(clip: str | Path, out_jpg: str | Path, at: float = 1.0) -> Path:
    out_jpg = Path(out_jpg)
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}",
           "-i", str(clip), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(out_jpg)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_jpg
