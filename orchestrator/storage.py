"""Object storage: S3-compatible (Cloudflare R2) with presigned URLs, or local (dev/tests)."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import quote


class StorageError(Exception):
    pass


class LocalStorage:
    """Dev/test backend. URLs are HMAC-signed, expiring, and served by this app."""

    kind = "local"

    def __init__(self, root: str, secret: str, base_url: str = ""):
        self.root, self.secret, self.base_url = Path(root), secret or "dev", base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError("invalid key")
        return p

    def _sig(self, op: str, key: str, exp: int) -> str:
        return hmac.new(self.secret.encode(), f"{op}:{key}:{exp}".encode(), hashlib.sha256).hexdigest()

    def verify(self, op: str, key: str, exp: int, sig: str) -> bool:
        return exp > time.time() and hmac.compare_digest(sig, self._sig(op, key, exp))

    def _url(self, op: str, key: str, ttl: int) -> str:
        exp = int(time.time()) + ttl
        return f"{self.base_url}/api/v1/storage/{op}?key={quote(key)}&exp={exp}&sig={self._sig(op, key, exp)}"

    def presign_put(self, key, mime="application/octet-stream", ttl=3600): return self._url("put", key, ttl)
    def presign_get(self, key, ttl=3600): return self._url("get", key, ttl)

    def head(self, key):
        p = self._path(key)
        return {"size": p.stat().st_size} if p.is_file() else None

    def write_stream(self, key, chunks):
        p = self._path(key); p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        n = 0
        with open(tmp, "wb") as f:
            for c in chunks:
                f.write(c); n += len(c)
        os.replace(tmp, p)
        return n

    def open_path(self, key): return self._path(key)
    def delete(self, key):
        p = self._path(key)
        if p.is_file(): p.unlink()

    def status(self):
        return {"backend": "local", "ok": True, "note": "dev/test only; use s3 (R2) in production"}


class S3Storage:
    kind = "s3"

    def __init__(self, endpoint, bucket, key_id, secret, region="auto"):
        import boto3
        from botocore.config import Config as BotoConfig
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key_id,
                                   aws_secret_access_key=secret, region_name=region,
                                   config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}))

    def presign_put(self, key, mime="application/octet-stream", ttl=3600):
        return self.client.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": mime}, ExpiresIn=ttl)

    def presign_get(self, key, ttl=3600):
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl)

    def head(self, key):
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
            return {"size": r["ContentLength"]}
        except Exception:
            return None

    def delete(self, key): self.client.delete_object(Bucket=self.bucket, Key=key)

    def status(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return {"backend": "s3", "ok": True, "bucket": self.bucket}
        except Exception as e:
            return {"backend": "s3", "ok": False, "error": str(e)[:300]}


def build_storage(cfg):
    if cfg.storage == "s3":
        if not cfg.storage_configured():
            raise StorageError("S3 storage selected but S3_* variables are incomplete")
        return S3Storage(cfg.s3_endpoint, cfg.s3_bucket, cfg.s3_access_key_id, cfg.s3_secret_access_key, cfg.s3_region)
    return LocalStorage(cfg.local_storage_dir, cfg.session_secret or cfg.runner_secret, cfg.public_url)
