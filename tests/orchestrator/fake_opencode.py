"""STAND-IN for the opencode CLI, used only by tests. It mimics the verified CLI surface
(`run --format json --session`, `export`, `import`) and the JSON-lines event shape
{type, sessionID, part}. It does NOT call any LLM."""
import json, os, subprocess, sys, time
from pathlib import Path

HOME = Path(os.environ["FAKE_OC_HOME"]); HOME.mkdir(parents=True, exist_ok=True)
LOG = Path(os.environ["FAKE_OC_LOG"])
a = sys.argv[1:]
def log(d): open(LOG, "a").write(json.dumps(d) + "\n")

if a[0] == "export":
    f = HOME / f"{a[1]}.json"
    if not f.exists(): sys.exit(1)
    print(f.read_text()); sys.exit(0)
if a[0] == "import":
    d = json.loads(Path(a[1]).read_text()); (HOME / f"{d['info']['id']}.json").write_text(json.dumps(d)); sys.exit(0)

assert a[0] == "run"
session = a[a.index("--session") + 1] if "--session" in a else None
prompt = a[-1]; repo = Path(a[a.index("--dir") + 1])
pid = prompt.split("Project id: ")[1].split()[0]
pdir = repo / "projects" / pid
log({"session_arg": session, "had_notes": (pdir / "notes.txt").exists(),
     "uploads": sorted(p.name for p in (pdir / "inputs" / "uploads").glob("*")) if (pdir / "inputs" / "uploads").exists() else [],
     "model": a[a.index("-m") + 1] if "-m" in a else None, "prompt_has_history": "Conversation so far" in prompt})
def ev(**k): print(json.dumps({"timestamp": int(time.time() * 1000), "sessionID": sid, **k}), flush=True)
if session:
    if not (HOME / f"{session}.json").exists():
        sid = session; ev(type="error", error={"name": "NotFound", "data": {"message": "session not found"}}); sys.exit(1)
    sid = session
else:
    sid = f"ses_fake_{int(time.time()*1000)}"
ev(type="step_start")
request = prompt.split("User request:")[-1]
if "crash" in request: sys.exit(3)
if "sleep" in request:
    for _ in range(120): time.sleep(0.5)
ev(type="text", part={"type": "text", "text": "Plan: hook, demo, CTA. "})
pdir.mkdir(parents=True, exist_ok=True); (pdir / "notes.txt").write_text("storyboard v1")
if "render" in request or "Automated QC failed" in prompt:
    (pdir / "renders").mkdir(exist_ok=True)
    bad = "badrender" in request and "Automated QC failed" not in prompt
    out = pdir / "renders" / "final.mp4"
    if bad: out.write_bytes(b"not a video")
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(out)], check=True)
if "storyboard" in request or "failed validation" in prompt:
    ups = sorted((pdir / "inputs" / "uploads").glob("*.mp4")) if (pdir / "inputs" / "uploads").exists() else []
    av = str(ups[0].relative_to(pdir)) if ups else "inputs/uploads/missing.mp4"
    scenes = [{"id": "s1", "title": "Hook", "start_seconds": 0, "end_seconds": 5, "type": "talking_head", "layout": "fullscreen", "narration": "Hello",
               "visual_description": "Presenter close-up", "asset_refs": [{"role": "avatar_video", "source": "uploaded", "path": av}]},
              {"id": "s2", "title": "Demo", "start_seconds": 5, "end_seconds": 15, "type": "screen_recording", "layout": "fullscreen", "narration": "Watch",
               "visual_description": "Dashboard demo", "asset_refs": [{"role": "screen_recording", "source": "uploaded", "path": av}]}]
    if "badplan" in request and "failed validation" not in prompt:
        scenes[1]["asset_refs"] = [{"role": "screen_recording", "source": "generated", "prompt": "fake dashboard"}]  # fabricated demo
    (pdir / "artifacts").mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts" / "storyboard.json").write_text(json.dumps({"version": "1.0", "title": "T", "avatar_asset": av,
        "format": {"resolution": "1920x1080", "aspect_ratio": "16:9", "fps": 30}, "scenes": scenes}))
if "askuser" in request:
    (pdir / "artifacts").mkdir(parents=True, exist_ok=True)
    (pdir / "artifacts" / "needs_input.json").write_text(json.dumps({"question": "Which brand color?", "options": ["red", "blue"]}))
ev(type="text", part={"type": "text", "text": "Done."})
sess = HOME / f"{sid}.json"
msgs = json.loads(sess.read_text())["messages"] if sess.exists() else []
sess.write_text(json.dumps({"info": {"id": sid}, "messages": msgs + [request.strip()]}))
