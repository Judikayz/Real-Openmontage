"""python -m director validate projects/<id>   |   python -m director scene-plan projects/<id>"""
import json, sys
from pathlib import Path
from director.storyboard import to_scene_plan, validate_file

def main(argv):
    if len(argv) != 2 or argv[0] not in ("validate", "scene-plan"):
        print(__doc__); return 2
    pdir = Path(argv[1]); sbp = pdir / "artifacts" / "storyboard.json"
    r = validate_file(sbp, pdir)
    if argv[0] == "validate":
        print(json.dumps(r, indent=2)); return 0 if r["ok"] else 1
    if not r["ok"]:
        print(json.dumps(r, indent=2)); return 1
    out = pdir / "artifacts" / "scene_plan.json"
    out.write_text(json.dumps(to_scene_plan(json.loads(sbp.read_text())), indent=2)); print(f"wrote {out}"); return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
