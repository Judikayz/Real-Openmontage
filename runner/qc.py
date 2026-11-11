"""Automated render QC using ffprobe/ffmpeg (real decode, not file-exists checks)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"error": (r.stderr or "ffprobe failed").strip()[:500]}
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("r_frame_rate", "0/0") not in ("0/0", ""):
        n, _, den = v["r_frame_rate"].partition("/")
        try: fps = round(float(n) / float(den or 1), 2)
        except Exception: pass
    return {"duration": float(d.get("format", {}).get("duration") or 0), "size": int(d.get("format", {}).get("size") or 0),
            "width": v and v.get("width"), "height": v and v.get("height"), "fps": fps,
            "has_video": v is not None, "has_audio": a is not None,
            "video_codec": v and v.get("codec_name"), "audio_codec": a and a.get("codec_name"),
            "audio_duration": float(a["duration"]) if a and a.get("duration") else None,
            "video_duration": float(v["duration"]) if v and v.get("duration") else None}


def qc_video(path: Path, expected_resolution: str | None = None, min_duration: float = 0.5) -> dict:
    errors, warnings = [], []
    if not path.is_file() or path.stat().st_size == 0:
        return {"ok": False, "errors": ["file missing or empty"], "warnings": [], "meta": {}}
    meta = probe(path)
    if "error" in meta:
        return {"ok": False, "errors": [f"not a readable media file: {meta['error']}"], "warnings": [], "meta": {}}
    if not meta["has_video"]: errors.append("no video stream")
    if meta["duration"] < min_duration: errors.append(f"duration too short: {meta['duration']:.2f}s")
    if not meta["has_audio"]: warnings.append("no audio stream")
    if meta["audio_duration"] and meta["video_duration"] and abs(meta["audio_duration"] - meta["video_duration"]) > 1.0:
        warnings.append(f"audio/video duration drift {abs(meta['audio_duration'] - meta['video_duration']):.2f}s")
    if expected_resolution and meta["width"]:
        if f"{meta['width']}x{meta['height']}" != expected_resolution:
            warnings.append(f"resolution {meta['width']}x{meta['height']} differs from configured {expected_resolution}")
    d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, timeout=1800)
    if d.returncode != 0 or d.stderr.strip():
        errors.append(f"decode errors: {d.stderr.strip()[:300] or 'ffmpeg returned ' + str(d.returncode)}")
    if meta["has_video"]:
        b = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vf", "blackdetect=d=1.5:pic_th=0.98", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=1800)
        for m in re.finditer(r"black_start:([\d.]+).*?black_duration:([\d.]+)", b.stderr):
            warnings.append(f"black frames at {float(m.group(1)):.1f}s for {float(m.group(2)):.1f}s")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "meta": meta}
