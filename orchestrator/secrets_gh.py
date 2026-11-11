"""Write GitHub Actions secrets server-side (libsodium sealed box). Values are never stored here."""
from __future__ import annotations

import base64

import httpx

from .dispatch import GH_API, gh_headers

ALLOWED_SECRET_NAMES = {
    "ORCH_RUNNER_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY",
    "PIXABAY_API_KEY", "HEYGEN_API_KEY", "RUNWAY_API_KEY", "OPENCODE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
}


def seal(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()


def list_secret_names(cfg, transport=None) -> list[str]:
    with httpx.Client(timeout=20, transport=transport) as c:
        r = c.get(f"{GH_API}/repos/{cfg.gh_repo}/actions/secrets", headers=gh_headers(cfg.gh_token), params={"per_page": 100})
    r.raise_for_status()
    return sorted(s["name"] for s in r.json().get("secrets", []))


def put_secret(cfg, name: str, value: str, transport=None) -> None:
    if name not in ALLOWED_SECRET_NAMES:
        raise ValueError(f"secret name not allowed: {name}")
    h = gh_headers(cfg.gh_token)
    with httpx.Client(timeout=20, transport=transport) as c:
        k = c.get(f"{GH_API}/repos/{cfg.gh_repo}/actions/secrets/public-key", headers=h)
        k.raise_for_status()
        k = k.json()
        r = c.put(f"{GH_API}/repos/{cfg.gh_repo}/actions/secrets/{name}", headers=h,
                  json={"encrypted_value": seal(k["key"], value), "key_id": k["key_id"]})
    r.raise_for_status()
