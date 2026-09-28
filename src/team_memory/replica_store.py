"""Read and delete Litestream replica objects: an S3-compatible bucket (AWS Signature V4) or a local directory.

Litestream uploads and restores on its own; the team server only needs to list what a replica holds
(`tam-team replication status` / `restore`) and to drop the replica of a deleted workspace
(`tam-team replication drop`). Credentials come from the standard AWS variables that Litestream reads too.
"""
import hashlib
import hmac
import json
import logging
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from team_memory.contracts import DTO, Conflict, Unavailable

LOGGER = logging.getLogger(__name__)
S3_TIMEOUT_SECONDS = 30
LIST_PAGE_SIZE = 1000
AWS_ENDPOINT_TEMPLATE = "https://s3.{region}.amazonaws.com"
DEFAULT_BUCKET_REGION = "us-east-1"
S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
SESSION_TOKEN_ENV = "AWS_SESSION_TOKEN"


class ReplicaObject(DTO):
    key: str
    size: int
    modified: datetime


class ObjectStore(Protocol):
    def list(self, prefix: str) -> list[ReplicaObject]: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def close(self) -> None: ...


class Credentials(DTO):
    access_key: str
    secret_key: str
    session_token: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Credentials":
        access, secret = environ.get(ACCESS_KEY_ENV, ""), environ.get(SECRET_KEY_ENV, "")
        if not access or not secret:
            raise Conflict(f"Set {ACCESS_KEY_ENV} and {SECRET_KEY_ENV} for the S3 replica")
        return cls(access_key=access, secret_key=secret, session_token=environ.get(SESSION_TOKEN_ENV) or None)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign_v4(method: str, host: str, path: str, query: Mapping[str, str], headers: Mapping[str, str],
            payload_sha256: str, credentials: Credentials, region: str, now: datetime) -> dict[str, str]:
    """Return the request headers with an AWS Signature Version 4 `Authorization` header for the `s3` service."""
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    signed = {k.lower(): str(v).strip() for k, v in headers.items()}
    signed.update({"host": host, "x-amz-date": stamp, "x-amz-content-sha256": payload_sha256})
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    names = sorted(signed)
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
    canonical = "\n".join([method, quote(path, safe="/-_.~"), canonical_query,
                           "".join(f"{name}:{signed[name]}\n" for name in names), ";".join(names), payload_sha256])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + credentials.secret_key).encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    signed["authorization"] = (f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
                               f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return signed


def _error_code(response: httpx.Response) -> str:
    try:
        return ElementTree.fromstring(response.content).findtext("Code") or ""
    except ElementTree.ParseError:
        return ""


class S3Store:
    def __init__(self, bucket: str, region: str, endpoint: str | None, path_style: bool, credentials: Credentials,
                 transport: httpx.BaseTransport | None = None):
        base = httpx.URL(endpoint or AWS_ENDPOINT_TEMPLATE.format(region=region))
        if base.scheme not in ("http", "https") or not base.host:
            raise Conflict("The S3 endpoint must be an http(s) URL")
        self.bucket, self.region, self.credentials, self.path_style = bucket, region, credentials, path_style
        port = f":{base.port}" if base.port else ""
        self.host = (base.host if path_style else f"{bucket}.{base.host}") + port
        self.origin = f"{base.scheme}://{self.host}"
        self.client = httpx.Client(timeout=S3_TIMEOUT_SECONDS, transport=transport, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def _path(self, key: str) -> str:
        return (f"/{self.bucket}/" if self.path_style else "/") + key

    def _send(self, method: str, path: str, query: Mapping[str, str] | None = None, body: bytes = b"") -> httpx.Response:
        query = dict(query or {})
        headers = sign_v4(method, self.host, path, query, {}, hashlib.sha256(body).hexdigest(), self.credentials,
                          self.region, datetime.now(UTC))
        encoded = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
        url = self.origin + quote(path, safe="/-_.~") + ("?" + encoded if encoded else "")
        try:
            return self.client.request(method, url, headers=headers, content=body or None)
        except httpx.HTTPError as exc:
            raise Unavailable(f"S3 replica unreachable: {type(exc).__name__}") from exc

    def _request(self, method: str, key: str, query: Mapping[str, str] | None = None) -> httpx.Response:
        response = self._send(method, self._path(key), query)
        if response.status_code >= 300:
            raise Unavailable(f"S3 {method} failed: HTTP {response.status_code} {_error_code(response)}".rstrip())
        return response

    def create_bucket(self) -> None:
        """Create the bucket; an existing bucket owned by these credentials is fine."""
        body = b"" if self.region == DEFAULT_BUCKET_REGION else (
            f'<CreateBucketConfiguration xmlns="{S3_NAMESPACE[1:-1]}"><LocationConstraint>{self.region}'
            "</LocationConstraint></CreateBucketConfiguration>").encode()
        response = self._send("PUT", self._path("").rstrip("/") or "/", body=body)
        if response.status_code in (200, 204):
            return
        if response.status_code == 409 and _error_code(response) == "BucketAlreadyOwnedByYou":
            return
        raise Unavailable(f"S3 bucket creation failed: HTTP {response.status_code} {_error_code(response)}".rstrip())

    def list(self, prefix: str) -> list[ReplicaObject]:
        found, token = [], None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": str(LIST_PAGE_SIZE)}
            if token:
                query["continuation-token"] = token
            tree = ElementTree.fromstring(self._request("GET", "", query).content)
            namespace = S3_NAMESPACE if tree.tag.startswith(S3_NAMESPACE) else ""
            for item in tree.iter(namespace + "Contents"):
                modified = datetime.fromisoformat(item.findtext(namespace + "LastModified"))
                found.append(ReplicaObject(key=item.findtext(namespace + "Key"),
                                           size=int(item.findtext(namespace + "Size") or 0), modified=modified))
            if tree.findtext(namespace + "IsTruncated") != "true":
                return found
            token = tree.findtext(namespace + "NextContinuationToken")
            if not token:
                raise Unavailable("S3 listing was truncated without a continuation token")

    def delete_prefix(self, prefix: str) -> int:
        if not prefix.endswith("/"):
            raise Conflict("Refusing to delete a replica prefix that is not a directory")
        objects = self.list(prefix)
        for item in objects:
            self._request("DELETE", item.key)
        LOGGER.info(json.dumps({"event": "replica_prefix_deleted", "backend": "s3", "bucket": self.bucket,
                                "prefix": prefix, "objects": len(objects)}))
        return len(objects)


class FileStore:
    """A Litestream `file` replica: the same key layout under a local or mounted directory."""

    def __init__(self, base: Path):
        self.base = base.resolve()

    def close(self) -> None:
        return None

    def _resolve(self, prefix: str) -> Path:
        target = (self.base / prefix).resolve()
        if target != self.base and self.base not in target.parents:
            raise Conflict("Replica path must stay inside the replica directory")
        return target

    def list(self, prefix: str) -> list[ReplicaObject]:
        start = self._resolve(prefix.rsplit("/", 1)[0] if "/" in prefix else "")
        if not start.is_dir():
            return []
        found = []
        for path in sorted(start.rglob("*")):
            key = path.relative_to(self.base).as_posix()
            if path.is_file() and not path.is_symlink() and key.startswith(prefix):
                info = path.stat()
                found.append(ReplicaObject(key=key, size=info.st_size,
                                           modified=datetime.fromtimestamp(info.st_mtime, UTC)))
        return found

    def delete_prefix(self, prefix: str) -> int:
        if not prefix.endswith("/"):
            raise Conflict("Refusing to delete a replica prefix that is not a directory")
        target = self._resolve(prefix)
        if target == self.base or not target.is_dir():
            return 0
        count = sum(1 for path in target.rglob("*") if path.is_file())
        shutil.rmtree(target)
        LOGGER.info(json.dumps({"event": "replica_prefix_deleted", "backend": "file", "prefix": prefix,
                                "objects": count}))
        return count

