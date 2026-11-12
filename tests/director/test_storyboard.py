import copy, json, subprocess, sys
from pathlib import Path
import pytest
from director.storyboard import to_scene_plan, validate

ROOT = Path(__file__).resolve().parents[2]


def project(tmp_path):
    up = tmp_path / "inputs" / "uploads"; up.mkdir(parents=True)
    for n in ("avatar.mp4", "voice.wav", "demo.mp4", "logo.png"): (up / n).write_bytes(b"x")
    return tmp_path


def base():
    A, V, D = "inputs/uploads/avatar.mp4", "inputs/uploads/voice.wav", "inputs/uploads/demo.mp4"
    return {"version": "1.0", "title": "Tool review", "format": {"resolution": "1920x1080", "aspect_ratio": "16:9", "fps": 30},
            "avatar_asset": A, "voice_asset": V, "scenes": [
        {"id": "s1", "title": "Hook", "start_seconds": 0, "end_seconds": 6, "type": "talking_head", "layout": "fullscreen", "narrative_role": "introduce_subject",
         "narration": "Stop wasting hours editing.", "visual_description": "Presenter, close-up, direct to camera",
         "asset_refs": [{"role": "avatar_video", "source": "uploaded", "path": A}, {"role": "voice", "source": "uploaded", "path": V}], "status": "planned"},
        {"id": "s2", "title": "Demo", "start_seconds": 6, "end_seconds": 26, "type": "screen_recording", "layout": "screen_with_pip",
         "narration": "Here is how it works.", "visual_description": "Walkthrough of the dashboard with zoom on the export button",
         "avatar": {"position": "bottom-right", "size": "20%"}, "overlays": ["callout: Export"], "depends_on": ["s1"],
         "asset_refs": [{"role": "screen_recording", "source": "uploaded", "path": D, "in_seconds": 3, "out_seconds": 23}, {"role": "avatar_video", "source": "uploaded", "path": A}]},
        {"id": "s3", "title": "CTA", "start_seconds": 26, "end_seconds": 32, "type": "text_card", "layout": "text_only", "narrative_role": "call_to_action",
         "visual_description": "Large text: Try it free", "captions": "Try it free", "transition_in": "fade"}]}


def test_valid_storyboard_passes_and_derives_upstream_scene_plan(tmp_path):
    r = validate(base(), project(tmp_path))
    assert r["ok"], r
    assert r["scenes"] == 3 and r["duration"] == 32
    sp = to_scene_plan(base())
    assert [s["type"] for s in sp["scenes"]] == ["talking_head", "screen_recording", "text_card"]
    assert sp["scenes"][1]["required_assets"][0]["source"] == "provided"


def test_fabricated_screen_recording_is_rejected(tmp_path):
    sb = base(); sb["scenes"][1]["asset_refs"] = [{"role": "screen_recording", "source": "generated", "prompt": "a dashboard"}]
    r = validate(sb, project(tmp_path))
    assert not r["ok"] and any("never generate" in e or "cannot be generated" in e for e in r["errors"])


def test_screen_scene_without_any_recording_rejected(tmp_path):
    sb = base(); sb["scenes"][1]["asset_refs"] = [{"role": "avatar_video", "source": "uploaded", "path": sb["avatar_asset"]}]
    assert any("no real recording" in e for e in validate(sb, project(tmp_path))["errors"])


def test_captured_recording_must_exist_on_disk(tmp_path):
    sb = base(); sb["scenes"][1]["asset_refs"][0] = {"role": "screen_recording", "source": "captured", "path": "captures/run1.mp4"}
    assert any("missing captured asset" in e for e in validate(sb, project(tmp_path))["errors"])


def test_missing_uploaded_asset_rejected(tmp_path):
    sb = base(); sb["scenes"][0]["asset_refs"][0]["path"] = "inputs/uploads/nope.mp4"
    assert not validate(sb, project(tmp_path))["ok"]


def test_avatar_and_voice_identity_must_stay_consistent(tmp_path):
    p = project(tmp_path); (p / "inputs/uploads/other.mp4").write_bytes(b"x"); (p / "inputs/uploads/other.wav").write_bytes(b"x")
    sb = base(); sb["scenes"][0]["asset_refs"][0]["path"] = "inputs/uploads/other.mp4"
    assert any("avatar differs" in e for e in validate(sb, p)["errors"])
    sb = base(); sb["scenes"][0]["asset_refs"][1]["path"] = "inputs/uploads/other.wav"
    assert any("voice differs" in e for e in validate(sb, p)["errors"])


def test_talking_head_must_use_supplied_avatar(tmp_path):
    sb = base(); sb["scenes"][0]["asset_refs"] = []
    assert any("must use the supplied avatar_asset" in e for e in validate(sb, project(tmp_path))["errors"])


def test_timeline_overlap_gap_and_order(tmp_path):
    p = project(tmp_path)
    sb = base(); sb["scenes"][1]["start_seconds"] = 4
    assert any("overlaps" in e for e in validate(sb, p)["errors"])
    sb = base(); sb["scenes"][2]["start_seconds"] = 28
    r = validate(sb, p); assert r["ok"] and any("gap" in w for w in r["warnings"])
    sb = base(); sb["scenes"][0]["end_seconds"] = 0
    assert any("end must be after start" in e for e in validate(sb, p)["errors"])


def test_duplicate_ids_and_bad_dependencies(tmp_path):
    p = project(tmp_path)
    sb = base(); sb["scenes"][2]["id"] = "s1"
    assert any("duplicate" in e for e in validate(sb, p)["errors"])
    sb = base(); sb["scenes"][0]["depends_on"] = ["s3"]
    assert any("not earlier" in e for e in validate(sb, p)["errors"])
    sb = base(); sb["scenes"][0]["depends_on"] = ["zzz"]
    assert any("unknown scene" in e for e in validate(sb, p)["errors"])


def test_stock_and_generated_need_queries(tmp_path):
    sb = base(); sb["scenes"].append({"id": "s4", "title": "b", "start_seconds": 32, "end_seconds": 36, "type": "broll", "layout": "fullscreen", "visual_description": "city",
                                      "narration": "x", "asset_refs": [{"role": "footage", "source": "stock"}]})
    assert any("search query" in e for e in validate(sb, project(tmp_path))["errors"])


def test_schema_errors_reported(tmp_path):
    sb = base(); sb["scenes"][0]["layout"] = "diagonal"; del sb["format"]["fps"]
    r = validate(sb, project(tmp_path)); assert not r["ok"] and any("schema" in e for e in r["errors"])


def test_cli_validate_and_scene_plan(tmp_path):
    p = project(tmp_path / "projects" / "demo"); (p / "artifacts").mkdir()
    (p / "artifacts" / "storyboard.json").write_text(json.dumps(base()))
    r = subprocess.run([sys.executable, "-m", "director", "validate", str(p)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-m", "director", "scene-plan", str(p)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads((p / "artifacts" / "scene_plan.json").read_text())["scenes"]
