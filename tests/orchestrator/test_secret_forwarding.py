"""What reaches the Docker container: tool/provider keys yes, infrastructure secrets never, values never on the command line."""
import json, os, stat, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_script(tmp, secrets, extra_env=None):
    bindir = tmp / "bin"; bindir.mkdir(exist_ok=True)
    out = tmp / "docker_call.json"
    stub = bindir / "docker"  # records its argv and the VALUES of the env vars named by each `-e NAME`
    stub.write_text(f"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
names = [a[i + 1] for i, x in enumerate(a) if x == "-e"]
json.dump({{"argv": a, "env": {{n: os.environ.get(n) for n in names}}}}, open({str(out)!r}, "w"))
""")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SECRETS_JSON": json.dumps(secrets), "JOB_ID": "job_x", **(extra_env or {})}
    r = subprocess.run([str(ROOT / "scripts/ci/run_in_container.sh"), "python", "-m", "runner.run_job"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text()), r.stdout


def test_tool_and_provider_keys_are_forwarded_but_infra_secrets_are_not(tmp_path):
    secrets = {"PEXELS_API_KEY": "pex-VALUE", "UNSPLASH_ACCESS_KEY": "uns-VALUE", "REPLICATE_API_TOKEN": "rep-VALUE", "OPENROUTER_API_KEY": "or-VALUE",
               "HF_TOKEN": "hf-VALUE", "CLOUDFLARE_API_TOKEN": "cf-SECRET", "GH_DISPATCH_TOKEN": "gh-SECRET", "ORCH_ADMIN_TOKEN": "admin-SECRET",
               "R2_S3_SECRET_ACCESS_KEY": "r2-SECRET", "GITHUB_TOKEN": "ghs-SECRET", "ELEVENLABS_API_KEY": ""}
    call, stdout = run_script(tmp_path, secrets)
    fwd = call["env"]
    assert fwd["PEXELS_API_KEY"] == "pex-VALUE" and fwd["UNSPLASH_ACCESS_KEY"] == "uns-VALUE" and fwd["REPLICATE_API_TOKEN"] == "rep-VALUE" and fwd["HF_TOKEN"] == "hf-VALUE"
    for infra in ("CLOUDFLARE_API_TOKEN", "GH_DISPATCH_TOKEN", "ORCH_ADMIN_TOKEN", "R2_S3_SECRET_ACCESS_KEY", "GITHUB_TOKEN"): assert infra not in fwd, infra
    assert "ELEVENLABS_API_KEY" not in fwd  # empty/unset secrets are not forwarded
    blob = json.dumps(call["argv"]) + stdout
    for v in ("pex-VALUE", "uns-VALUE", "or-VALUE", "cf-SECRET", "gh-SECRET"): assert v not in blob, "secret value leaked into argv/log"
    assert "forwarded 5 tool/provider key(s)" in stdout


def test_allowlist_covers_every_key_openmontage_documents_and_stays_in_sync_with_backlot():
    allow = [l.strip() for l in (ROOT / "config/runner_secrets.txt").read_text().splitlines() if l.strip()]
    import re
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{3,})=", (ROOT / ".env.example").read_text(), re.M))
    assert documented <= set(allow), sorted(documented - set(allow))
    assert {"PEXELS_API_KEY", "PIXABAY_API_KEY", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY"} <= set(allow)
    assert json.loads((ROOT / "worker/src/runner_secrets.json").read_text()) == allow  # Backlot's Worker form uses the same list
    assert not {"CLOUDFLARE_API_TOKEN", "GH_DISPATCH_TOKEN", "ORCH_ADMIN_TOKEN", "ORCH_RUNNER_SECRET", "GITHUB_TOKEN"} & set(allow)
