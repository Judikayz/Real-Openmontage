"""Storyboard = editable, time-based production plan (superset of upstream scene_plan).

Checks (each returns an error or a warning):
  structure, ordered non-overlapping timeline, asset existence on disk, screen-recording provenance
  (no fabricated demos), avatar/voice consistency, dependency order, derivation into a valid scene_plan.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parent.parent
SCENE_TYPES = ["talking_head", "broll", "animation", "character_scene", "diagram", "text_card", "transition", "generated", "screen_recording"]
LAYOUTS = ["fullscreen", "avatar_pip", "split_screen", "screen_with_pip", "image_text", "chart", "text_only"]
ASSET_SOURCES = ["uploaded", "captured", "stock", "generated"]
ASSET_ROLES = ["avatar_video", "voice", "screen_recording", "footage", "image", "document", "music", "sfx", "logo"]
STATUSES = ["planned", "assets_ready", "rendered", "failed", "skipped"]
SRC_MAP = {"uploaded": "provided", "captured": "record", "stock": "source", "generated": "generate"}
TOL = 0.05  # seconds tolerated for gaps/overlaps

STORYBOARD_SCHEMA = {
    "type": "object",
    "required": ["version", "title", "format", "scenes"],
    "properties": {
        "version": {"type": "string"},
        "title": {"type": "string", "minLength": 1},
        "format": {"type": "object", "required": ["resolution", "aspect_ratio", "fps"], "properties": {
            "resolution": {"type": "string", "pattern": r"^\d{3,4}x\d{3,4}$"}, "aspect_ratio": {"type": "string"}, "fps": {"type": "number", "minimum": 1}}},
        "structure": {"type": "object"},
        "pipeline": {"type": "string"},
        "avatar_asset": {"type": "string"},
        "voice_asset": {"type": "string"},
        "music": {"type": "object"},
        "scenes": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["id", "title", "start_seconds", "end_seconds", "type", "visual_description", "layout"],
            "properties": {
                "id": {"type": "string", "minLength": 1}, "title": {"type": "string"},
                "start_seconds": {"type": "number", "minimum": 0}, "end_seconds": {"type": "number", "minimum": 0},
                "type": {"enum": SCENE_TYPES}, "layout": {"enum": LAYOUTS},
                "narrative_role": {"type": "string"}, "narration": {"type": "string"},
                "visual_description": {"type": "string", "minLength": 1},
                "asset_refs": {"type": "array", "items": {"type": "object", "required": ["role", "source"], "properties": {
                    "role": {"enum": ASSET_ROLES}, "source": {"enum": ASSET_SOURCES}, "path": {"type": "string"}, "asset_id": {"type": "string"},
                    "in_seconds": {"type": "number"}, "out_seconds": {"type": "number"}, "query": {"type": "string"}, "prompt": {"type": "string"}}}},
                "avatar": {"type": "object"}, "composition": {"type": "string"}, "editing": {"type": "string"},
                "captions": {"type": "string"}, "overlays": {"type": "array", "items": {"type": "string"}},
                "transition_in": {"type": "string"}, "transition_out": {"type": "string"},
                "audio": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "status": {"enum": STATUSES}}}}},
}


def to_scene_plan(sb: dict) -> dict:
    """Derive an upstream-valid scene_plan so existing pipelines can consume the storyboard unchanged."""
    scenes = []
    for s in sb["scenes"]:
        notes = "; ".join(x for x in [s.get("captions", ""), *s.get("overlays", [])] if x)
        sc = {"id": s["id"], "type": s["type"], "description": s["visual_description"],
              "start_seconds": s["start_seconds"], "end_seconds": s["end_seconds"]}
        if s.get("narrative_role"): sc["narrative_role"] = s["narrative_role"]
        if s.get("transition_in"): sc["transition_in"] = s["transition_in"]
        if s.get("transition_out"): sc["transition_out"] = s["transition_out"]
        if notes: sc["overlay_notes"] = notes
        ra = [{"type": a["role"], "description": a.get("path") or a.get("query") or a.get("prompt") or a["role"], "source": SRC_MAP[a["source"]]}
              for a in s.get("asset_refs", [])]
        if ra: sc["required_assets"] = ra
        scenes.append(sc)
    return {"version": "1.0", "scenes": scenes, "metadata": {"title": sb["title"], "derived_from": "storyboard.json"}}


def _scene_plan_schema():
    return json.loads((REPO / "schemas" / "artifacts" / "scene_plan.schema.json").read_text())


def validate(sb: dict, project_dir: Path | None = None) -> dict:
    errors, warnings = [], []
    v = jsonschema.Draft202012Validator(STORYBOARD_SCHEMA)
    for e in sorted(v.iter_errors(sb), key=lambda e: list(e.path)):
        errors.append(f"schema: {'/'.join(map(str, e.path)) or '<root>'}: {e.message[:200]}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "scenes": 0, "duration": 0}

    scenes, ids = sb["scenes"], [s["id"] for s in sb["scenes"]]
    for i in {x for x in ids if ids.count(x) > 1}: errors.append(f"duplicate scene id: {i}")
    prev_end = 0.0
    for s in scenes:
        sid = s["id"]
        if s["end_seconds"] <= s["start_seconds"]: errors.append(f"{sid}: end must be after start")
        if s["start_seconds"] < prev_end - TOL: errors.append(f"{sid}: overlaps previous scene by {prev_end - s['start_seconds']:.2f}s (scenes must be in order)")
        elif s["start_seconds"] > prev_end + TOL: warnings.append(f"{sid}: gap of {s['start_seconds'] - prev_end:.2f}s before this scene")
        prev_end = max(prev_end, s["end_seconds"])
        for d in s.get("depends_on", []):
            if d not in ids: errors.append(f"{sid}: depends_on unknown scene {d}")
            elif ids.index(d) >= ids.index(sid): errors.append(f"{sid}: depends_on {d}, which is not earlier in the timeline")
        refs = s.get("asset_refs", [])
        for a in refs:
            if a["source"] in ("uploaded", "captured"):
                if not a.get("path"): errors.append(f"{sid}: {a['source']} {a['role']} needs a path")
                elif project_dir is not None and not (project_dir / a["path"]).is_file() and not (REPO / a["path"]).is_file():
                    errors.append(f"{sid}: missing {a['source']} asset on disk: {a['path']}")
            if a["source"] == "stock" and not a.get("query"): errors.append(f"{sid}: stock asset needs a search query")
            if a["source"] == "generated" and not a.get("prompt"): errors.append(f"{sid}: generated asset needs a prompt")
            if "in_seconds" in a and "out_seconds" in a and a["out_seconds"] <= a["in_seconds"]: errors.append(f"{sid}: asset trim out<=in")
        # anti-fabrication: a screen recording must come from a real upload or a real capture, never from generation
        if s["type"] == "screen_recording" or s["layout"] in ("screen_with_pip",):
            real = [a for a in refs if a["role"] == "screen_recording" and a["source"] in ("uploaded", "captured")]
            if not real: errors.append(f"{sid}: screen demo scene has no real recording (uploaded or captured). Capture it with the browser-capture tools or ask the user; never generate it")
            if any(a["role"] == "screen_recording" and a["source"] in ("generated", "stock") for a in refs):
                errors.append(f"{sid}: a screen recording cannot be generated or stock footage")
        if s["type"] == "talking_head" and not any(a["role"] in ("avatar_video",) for a in refs) and sb.get("avatar_asset"):
            errors.append(f"{sid}: talking_head scene must use the supplied avatar_asset ({sb['avatar_asset']})")
        for a in refs:
            if a["role"] == "avatar_video" and sb.get("avatar_asset") and a.get("path") != sb["avatar_asset"]:
                errors.append(f"{sid}: avatar differs from the project's avatar_asset (identity must stay consistent)")
            if a["role"] == "voice" and sb.get("voice_asset") and a.get("path") != sb["voice_asset"]:
                errors.append(f"{sid}: voice differs from the project's voice_asset (voice must stay consistent)")
        if s["type"] != "text_card" and not s.get("narration") and not s.get("audio"): warnings.append(f"{sid}: no narration or audio instruction")
        if s["layout"] in ("avatar_pip", "screen_with_pip") and not s.get("avatar"): warnings.append(f"{sid}: PiP layout without avatar placement instructions")
    if project_dir is not None:
        for key in ("avatar_asset", "voice_asset"):
            if sb.get(key) and not (project_dir / sb[key]).is_file(): errors.append(f"{key} not found on disk: {sb[key]}")
    if scenes and scenes[0]["start_seconds"] > TOL: warnings.append("timeline does not start at 0")
    try:
        jsonschema.validate(to_scene_plan(sb), _scene_plan_schema())
    except jsonschema.ValidationError as e:
        errors.append(f"derived scene_plan invalid against upstream schema: {e.message[:200]}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "scenes": len(scenes), "duration": round(prev_end, 2)}


def validate_file(path: Path, project_dir: Path | None = None) -> dict:
    try:
        sb = json.loads(Path(path).read_text())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errors": [f"unreadable storyboard: {e}"], "warnings": [], "scenes": 0, "duration": 0}
    return validate(sb, project_dir)
