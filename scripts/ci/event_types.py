"""Print the event types found in an `opencode run --format json` output file."""
import json, sys
types = []
for line in open(sys.argv[1]):
    try:
        types.append(json.loads(line).get("type"))
    except Exception:
        pass
print(types[:40])
