# Pi Session Viewer service.
#
# JupyterHub config fragment that writes /srv/jupyterhub/pi_session_viewer.py
# and registers the service as a Hub-managed app.
import os
import textwrap
from pathlib import Path

pi_session_viewer_py = Path("/srv/jupyterhub/pi_session_viewer.py")
pi_session_viewer_py.write_text(
    textwrap.dedent(
        r'''
        import base64
        import datetime
        import gzip
        import hashlib
        import html
        import hmac
        import json
        import os
        import re
        import secrets
        import sqlite3
        import subprocess
        import tempfile
        from pathlib import Path
        from typing import Any, Dict, List, Optional, Tuple
        from urllib.parse import quote, urlencode, urlsplit

        import requests
        from jupyterhub.services.auth import HubOAuthenticated, HubOAuthCallbackHandler
        from tornado.ioloop import IOLoop, PeriodicCallback
        from tornado.web import Application, HTTPError, RequestHandler, authenticated

        API_URL = os.environ["JUPYTERHUB_API_URL"].rstrip("/")
        API_TOKEN = os.environ["JUPYTERHUB_API_TOKEN"]

        SERVICE_PREFIX = os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/").rstrip("/")
        if not SERVICE_PREFIX.startswith("/"):
            SERVICE_PREFIX = "/" + SERVICE_PREFIX

        PUBLIC_HOST = (os.environ.get("PUBLIC_HOST", "") or "").strip().rstrip("/")

        SHARES_BUCKET = (os.environ.get("PI_SESSION_VIEWER_S3_BUCKET", "") or "").strip()
        SHARES_PREFIX = (os.environ.get("PI_SESSION_VIEWER_S3_PREFIX", "pi-shares") or "pi-shares").strip().strip("/")
        S3_REGION = (os.environ.get("PI_SESSION_VIEWER_S3_REGION", "us-east-1") or "us-east-1").strip()
        S3_ENDPOINT = (os.environ.get("PI_SESSION_VIEWER_S3_ENDPOINT", "") or "").strip()
        S3_ACCESS_KEY = (os.environ.get("PI_SESSION_VIEWER_S3_ACCESS_KEY_ID", "") or "").strip()
        S3_SECRET_KEY = (os.environ.get("PI_SESSION_VIEWER_S3_SECRET_ACCESS_KEY", "") or "").strip()
        S3_SESSION_TOKEN = (os.environ.get("PI_SESSION_VIEWER_S3_SESSION_TOKEN", "") or "").strip()
        S3_AUTO_CREATE_BUCKET = (os.environ.get("PI_SESSION_VIEWER_S3_AUTO_CREATE_BUCKET", "0") or "0").strip() in (
            "1",
            "true",
            "yes",
            "on",
        )

        DB_PATH = (os.environ.get("PI_SESSION_VIEWER_DB_PATH", "/tmp/pi-session-viewer/shares.db") or "/tmp/pi-session-viewer/shares.db").strip()
        API_AUTH_TOKEN = (os.environ.get("PI_SESSION_VIEWER_API_TOKEN", "") or "").strip()
        DEFAULT_EXPIRES_HOURS = int(os.environ.get("PI_SESSION_VIEWER_DEFAULT_EXPIRES_HOURS", "720") or "720")
        MAX_EXPIRES_HOURS = int(os.environ.get("PI_SESSION_VIEWER_MAX_EXPIRES_HOURS", "2160") or "2160")
        MAX_SESSION_BYTES = int(os.environ.get("PI_SESSION_VIEWER_MAX_SESSION_BYTES", "10485760") or "10485760")
        CLEANUP_INTERVAL_SECONDS = int(os.environ.get("PI_SESSION_VIEWER_CLEANUP_INTERVAL_SECONDS", "600") or "600")
        HARD_DELETE_GRACE_HOURS = int(os.environ.get("PI_SESSION_VIEWER_HARD_DELETE_GRACE_HOURS", "168") or "168")

        SHARE_ID_RE = re.compile(r"^[a-z0-9-]{6,64}$")
        VIEW_LINK_TTL_SECONDS = int(os.environ.get("PI_SESSION_VIEWER_VIEW_LINK_TTL_SECONDS", "600") or "600")
        VIEWER_SIGNING_KEY = hashlib.sha256(f"{API_TOKEN}|{API_AUTH_TOKEN}|{SERVICE_PREFIX}".encode("utf-8")).digest()


        def utcnow() -> datetime.datetime:
            return datetime.datetime.now(datetime.timezone.utc)


        def isoformat(dt: datetime.datetime) -> str:
            return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


        def parse_iso(raw: str):
            if not raw:
                return None
            text = str(raw).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.datetime.fromisoformat(text).astimezone(datetime.timezone.utc)
            except Exception:
                return None


        def parse_targets(value) -> List[str]:
            if value is None:
                return []
            if isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            elif isinstance(value, list):
                items = [str(part).strip() for part in value]
            else:
                return []
            out = []
            seen = set()
            for item in items:
                if not item:
                    continue
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
            return out


        def normalize_share_id(raw: str) -> str:
            value = (raw or "").strip().lower()
            if not SHARE_ID_RE.fullmatch(value):
                return ""
            return value


        def _b64url_decode(text: str) -> bytes:
            pad = "=" * ((4 - (len(text) % 4)) % 4)
            return base64.urlsafe_b64decode((text + pad).encode("ascii"))


        def make_view_token(share_id: str) -> str:
            exp = int(utcnow().timestamp()) + max(30, VIEW_LINK_TTL_SECONDS)
            payload = f"{share_id}:{exp}"
            sig = hmac.new(VIEWER_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            raw = f"{exp}:{sig}".encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


        def verify_view_token(share_id: str, token: str) -> bool:
            try:
                raw = _b64url_decode((token or "").strip()).decode("utf-8")
                exp_s, sig = raw.split(":", 1)
                exp = int(exp_s)
            except Exception:
                return False
            if exp < int(utcnow().timestamp()):
                return False
            payload = f"{share_id}:{exp}"
            expect = hmac.new(VIEWER_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expect)


        def hub_request(method: str, path: str, *, json_body=None):
            return requests.request(
                method,
                f"{API_URL}{path}",
                headers={"Authorization": f"token {API_TOKEN}", "Content-Type": "application/json"},
                json=json_body,
                timeout=20,
            )


        def get_user(username: str) -> Dict[str, Any]:
            response = hub_request("GET", f"/users/{quote(username)}")
            if response.status_code == 404:
                # Unknown users should not crash requests; treat as non-admin with no groups.
                return {"name": username, "groups": [], "admin": False}
            if response.status_code != 200:
                raise RuntimeError(f"Unable to resolve user {username!r}: status={response.status_code} body={response.text}")
            return response.json()


        def user_groups(username: str) -> List[str]:
            user = get_user(username)
            groups = []
            for g in user.get("groups") or []:
                if isinstance(g, dict):
                    name = g.get("name")
                else:
                    name = str(g or "")
                if isinstance(name, str) and name.strip():
                    groups.append(name.strip())
            return sorted(set(groups))


        def is_admin(username: str) -> bool:
            user = get_user(username)
            return bool(user.get("admin"))


        def ensure_s3_configured():
            if not SHARES_BUCKET:
                raise RuntimeError("PI_SESSION_VIEWER_S3_BUCKET is not configured.")


        def _s3_base_url() -> str:
            if S3_ENDPOINT:
                return S3_ENDPOINT.rstrip("/")
            return f"https://s3.{S3_REGION}.amazonaws.com"


        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


        def _s3_authorization_headers(method: str, url: str, body: bytes, extra_headers: Dict[str, str]) -> Dict[str, str]:
            if not (S3_ACCESS_KEY and S3_SECRET_KEY):
                return dict(extra_headers)

            now = datetime.datetime.utcnow()
            amz_date = now.strftime("%Y%m%dT%H%M%SZ")
            date_stamp = now.strftime("%Y%m%d")
            parsed = urlsplit(url)
            canonical_uri = parsed.path or "/"
            canonical_query = parsed.query or ""
            payload_hash = hashlib.sha256(body).hexdigest()

            headers = {
                "host": parsed.netloc,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
            }
            if S3_SESSION_TOKEN:
                headers["x-amz-security-token"] = S3_SESSION_TOKEN
            for k, v in (extra_headers or {}).items():
                lk = k.lower().strip()
                if lk and v is not None:
                    headers[lk] = str(v).strip()

            signed_header_names = sorted(headers.keys())
            canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
            signed_headers = ";".join(signed_header_names)

            canonical_request = "\n".join(
                [
                    method.upper(),
                    canonical_uri,
                    canonical_query,
                    canonical_headers,
                    signed_headers,
                    payload_hash,
                ]
            )
            algorithm = "AWS4-HMAC-SHA256"
            credential_scope = f"{date_stamp}/{S3_REGION}/s3/aws4_request"
            string_to_sign = "\n".join(
                [
                    algorithm,
                    amz_date,
                    credential_scope,
                    hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
                ]
            )

            k_date = _hmac_sha256(("AWS4" + S3_SECRET_KEY).encode("utf-8"), date_stamp)
            k_region = hmac.new(k_date, S3_REGION.encode("utf-8"), hashlib.sha256).digest()
            k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
            k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
            signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

            auth = (
                f"{algorithm} Credential={S3_ACCESS_KEY}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            )

            out = {}
            for k, v in headers.items():
                if k == "host":
                    continue
                out[k] = v
            out["Authorization"] = auth
            return out


        def _s3_request(
            method: str,
            path: str,
            *,
            body: bytes = b"",
            headers: Dict[str, str] = None,
            expected=(200, 204),
            allow_404: bool = False,
        ):
            ensure_s3_configured()
            clean_path = path if path.startswith("/") else "/" + path
            url = f"{_s3_base_url()}{clean_path}"
            signed_headers = _s3_authorization_headers(method, url, body, headers or {})
            response = requests.request(method.upper(), url, data=body, headers=signed_headers, timeout=30)
            if allow_404 and response.status_code == 404:
                return response
            if response.status_code not in tuple(expected):
                raise RuntimeError(
                    f"s3 request failed method={method.upper()} path={clean_path} "
                    f"status={response.status_code} body={response.text[:300]}"
                )
            return response


        def s3_ensure_bucket():
            if not S3_AUTO_CREATE_BUCKET:
                return
            head = _s3_request("HEAD", f"/{SHARES_BUCKET}", expected=(200, 301, 403, 404), allow_404=True)
            if head.status_code != 404:
                return
            if S3_REGION and S3_REGION != "us-east-1":
                xml = (
                    "<CreateBucketConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                    f"<LocationConstraint>{S3_REGION}</LocationConstraint>"
                    "</CreateBucketConfiguration>"
                ).encode("utf-8")
                _s3_request(
                    "PUT",
                    f"/{SHARES_BUCKET}",
                    body=xml,
                    headers={"Content-Type": "application/xml"},
                    expected=(200,),
                )
            else:
                _s3_request("PUT", f"/{SHARES_BUCKET}", expected=(200,))


        def db_conn():
            path = Path(DB_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            return conn


        def init_db():
            with db_conn() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shares (
                        id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        title TEXT NOT NULL,
                        acl_users_json TEXT NOT NULL,
                        acl_groups_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        revoked_at TEXT,
                        s3_key TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        entry_count INTEGER NOT NULL,
                        leaf_ids_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        last_accessed_at TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS shares_owner_idx ON shares(owner)")
                conn.execute("CREATE INDEX IF NOT EXISTS shares_expires_idx ON shares(expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS shares_revoked_idx ON shares(revoked_at)")
                conn.commit()


        def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
            return {
                "id": row["id"],
                "owner": row["owner"],
                "title": row["title"],
                "acl_users": json.loads(row["acl_users_json"] or "[]"),
                "acl_groups": json.loads(row["acl_groups_json"] or "[]"),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "s3_key": row["s3_key"],
                "size_bytes": int(row["size_bytes"] or 0),
                "sha256": row["sha256"],
                "entry_count": int(row["entry_count"] or 0),
                "leaf_ids": json.loads(row["leaf_ids_json"] or "[]"),
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "last_accessed_at": row["last_accessed_at"],
            }


        def get_share(share_id: str) -> Optional[Dict[str, Any]]:
            sid = normalize_share_id(share_id)
            if not sid:
                return None
            with db_conn() as conn:
                row = conn.execute("SELECT * FROM shares WHERE id = ?", (sid,)).fetchone()
                if not row:
                    return None
                return row_to_dict(row)


        def list_all_shares() -> List[Dict[str, Any]]:
            with db_conn() as conn:
                rows = conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
            return [row_to_dict(r) for r in rows]


        def update_last_accessed(share_id: str):
            with db_conn() as conn:
                conn.execute(
                    "UPDATE shares SET last_accessed_at = ? WHERE id = ?",
                    (isoformat(utcnow()), share_id),
                )
                conn.commit()


        def store_share(record: Dict[str, Any]):
            with db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO shares (
                        id, owner, title, acl_users_json, acl_groups_json,
                        created_at, expires_at, revoked_at, s3_key,
                        size_bytes, sha256, entry_count, leaf_ids_json,
                        metadata_json, last_accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["owner"],
                        record["title"],
                        json.dumps(record["acl_users"]),
                        json.dumps(record["acl_groups"]),
                        record["created_at"],
                        record["expires_at"],
                        record.get("revoked_at"),
                        record["s3_key"],
                        int(record["size_bytes"]),
                        record["sha256"],
                        int(record["entry_count"]),
                        json.dumps(record["leaf_ids"]),
                        json.dumps(record.get("metadata", {})),
                        record.get("last_accessed_at"),
                    ),
                )
                conn.commit()


        def revoke_share(share_id: str):
            with db_conn() as conn:
                conn.execute(
                    "UPDATE shares SET revoked_at = ? WHERE id = ?",
                    (isoformat(utcnow()), share_id),
                )
                conn.commit()


        def delete_share_row(share_id: str):
            with db_conn() as conn:
                conn.execute("DELETE FROM shares WHERE id = ?", (share_id,))
                conn.commit()


        def share_expired(record: Dict[str, Any]) -> bool:
            expires_at = parse_iso(record.get("expires_at", ""))
            if not expires_at:
                return False
            return utcnow() >= expires_at


        def share_revoked(record: Dict[str, Any]) -> bool:
            return bool(record.get("revoked_at"))


        def can_view(record: Dict[str, Any], username: str, groups: List[str], admin: bool = False) -> bool:
            if admin:
                return True
            owner = str(record.get("owner") or "").strip()
            if username == owner:
                return True
            if username in set(parse_targets(record.get("acl_users"))):
                return True
            allowed_groups = set(parse_targets(record.get("acl_groups")))
            return bool(allowed_groups.intersection(set(groups)))


        def parse_jsonl_entries(raw_text: str) -> List[Dict[str, Any]]:
            entries: List[Dict[str, Any]] = []
            line_no = 0
            for line in (raw_text or "").splitlines():
                line_no += 1
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    obj = {
                        "id": f"raw-{line_no}",
                        "type": "raw_text",
                        "text": line,
                    }
                if not isinstance(obj, dict):
                    obj = {
                        "id": f"value-{line_no}",
                        "type": "raw_value",
                        "value": obj,
                    }
                if not obj.get("id"):
                    obj["id"] = f"entry-{line_no}"
                entries.append(obj)
            return entries


        def compute_leaf_ids(entries: List[Dict[str, Any]]) -> List[str]:
            ids = []
            parent_ids = set()
            for entry in entries:
                eid = str(entry.get("id") or "").strip()
                if eid:
                    ids.append(eid)
                pid = str(entry.get("parentId") or "").strip()
                if pid:
                    parent_ids.add(pid)
            leafs = []
            seen = set()
            for eid in ids:
                if eid in parent_ids:
                    continue
                if eid in seen:
                    continue
                seen.add(eid)
                leafs.append(eid)
            if not leafs and ids:
                leafs = [ids[-1]]
            return leafs


        def build_chain(entries: List[Dict[str, Any]], leaf_id: str) -> List[str]:
            by_id = {str(e.get("id")): e for e in entries if str(e.get("id") or "")}
            chain = []
            seen = set()
            current = leaf_id
            while current and current not in seen:
                seen.add(current)
                chain.append(current)
                node = by_id.get(current)
                if not node:
                    break
                current = str(node.get("parentId") or "").strip()
            chain.reverse()
            return chain


        def filter_branch(entries: List[Dict[str, Any]], leaf_id: str) -> List[Dict[str, Any]]:
            target = str(leaf_id or "").strip()
            if not target:
                return entries
            chain = build_chain(entries, target)
            if not chain:
                return entries
            pos = {eid: idx for idx, eid in enumerate(chain)}
            subset = [e for e in entries if str(e.get("id") or "") in pos]
            subset.sort(key=lambda e: pos.get(str(e.get("id") or ""), 10**9))
            return subset


        def preview_text(entry: Dict[str, Any], max_len: int = 140) -> str:
            text = ""
            if isinstance(entry.get("text"), str):
                text = entry["text"]
            elif isinstance(entry.get("message"), dict):
                msg = entry.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    chunks = []
                    for part in content:
                        if isinstance(part, dict):
                            t = part.get("text")
                            if isinstance(t, str):
                                chunks.append(t)
                    text = "\n".join(chunks)
            if not text:
                text = json.dumps(entry, ensure_ascii=False)
            text = " ".join(text.split())
            if len(text) <= max_len:
                return text
            return text[: max_len - 1] + "…"


        def _s3_object_path(s3_key: str) -> str:
            encoded_key = quote(s3_key, safe="/-_.~")
            return f"/{SHARES_BUCKET}/{encoded_key}"


        def upload_object(s3_key: str, payload: bytes, metadata: Dict[str, str]):
            ensure_s3_configured()
            headers = {
                "Content-Type": "application/gzip",
            }
            for k, v in (metadata or {}).items():
                clean_key = re.sub(r"[^a-zA-Z0-9_-]", "-", str(k))
                headers[f"x-amz-meta-{clean_key}"] = str(v)
            _s3_request("PUT", _s3_object_path(s3_key), body=payload, headers=headers, expected=(200,))


        def download_object(s3_key: str) -> bytes:
            ensure_s3_configured()
            response = _s3_request("GET", _s3_object_path(s3_key), expected=(200,))
            return response.content


        def delete_object(s3_key: str):
            ensure_s3_configured()
            _s3_request("DELETE", _s3_object_path(s3_key), expected=(200, 204, 404), allow_404=True)


        def load_entries_for_share(record: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
            compressed = download_object(record["s3_key"])
            raw = gzip.decompress(compressed)
            text = raw.decode("utf-8", errors="replace")
            entries = parse_jsonl_entries(text)
            return text, entries


        def create_share_record(
            *,
            owner: str,
            title: str,
            acl_users: List[str],
            acl_groups: List[str],
            expires_hours: int,
            session_path: str,
            content: bytes,
        ) -> Dict[str, Any]:
            if not owner:
                raise RuntimeError("owner is required")
            if not acl_users and not acl_groups:
                raise RuntimeError("Provide at least one user or group in ACL.")
            if expires_hours <= 0 or expires_hours > MAX_EXPIRES_HOURS:
                raise RuntimeError(f"expires_hours must be between 1 and {MAX_EXPIRES_HOURS}.")
            if len(content) <= 0:
                raise RuntimeError("session content is empty")
            if len(content) > MAX_SESSION_BYTES:
                raise RuntimeError(
                    f"Session size {len(content)} bytes exceeds limit {MAX_SESSION_BYTES} bytes."
                )

            text = content.decode("utf-8", errors="replace")
            entries = parse_jsonl_entries(text)
            leaf_ids = compute_leaf_ids(entries)

            sid = secrets.token_hex(6)
            created_at_dt = utcnow()
            expires_at_dt = created_at_dt + datetime.timedelta(hours=int(expires_hours))
            s3_key = f"{SHARES_PREFIX}/{owner}/{sid}/session.jsonl.gz"
            digest = hashlib.sha256(content).hexdigest()
            compressed = gzip.compress(content, compresslevel=6)

            upload_object(
                s3_key,
                compressed,
                metadata={
                    "owner": owner,
                    "share-id": sid,
                    "sha256": digest,
                },
            )

            record = {
                "id": sid,
                "owner": owner,
                "title": (title or "").strip() or f"Pi Session Share {sid}",
                "acl_users": parse_targets(acl_users),
                "acl_groups": parse_targets(acl_groups),
                "created_at": isoformat(created_at_dt),
                "expires_at": isoformat(expires_at_dt),
                "revoked_at": None,
                "s3_key": s3_key,
                "size_bytes": len(content),
                "sha256": digest,
                "entry_count": len(entries),
                "leaf_ids": leaf_ids,
                "metadata": {
                    "session_path": session_path,
                },
                "last_accessed_at": None,
            }
            store_share(record)
            return record


        def cleanup_expired():
            now = utcnow()
            hard_cutoff = now - datetime.timedelta(hours=max(HARD_DELETE_GRACE_HOURS, 1))
            removed = 0
            for record in list_all_shares():
                revoked_at = parse_iso(record.get("revoked_at") or "")
                expires_at = parse_iso(record.get("expires_at") or "")
                should_delete = False
                if revoked_at and revoked_at <= hard_cutoff:
                    should_delete = True
                elif expires_at and expires_at <= hard_cutoff:
                    should_delete = True
                if not should_delete:
                    continue
                try:
                    delete_object(record["s3_key"])
                except Exception as exc:
                    print(f"WARN cleanup: failed deleting object {record['s3_key']}: {exc}")
                delete_share_row(record["id"])
                removed += 1
            if removed:
                print(f"pi-session-viewer cleanup removed={removed}")


        def share_view_url(share_id: str) -> str:
            if PUBLIC_HOST:
                return f"{PUBLIC_HOST}{SERVICE_PREFIX}/s/{share_id}"
            return f"{SERVICE_PREFIX}/s/{share_id}"


        class BaseHandler(HubOAuthenticated, RequestHandler):
            def write_json(self, payload: Dict[str, Any], status: int = 200):
                self.set_status(status)
                self.set_header("Content-Type", "application/json; charset=utf-8")
                self.set_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.set_header("Pragma", "no-cache")
                self.write(json.dumps(payload, ensure_ascii=False))

            def auth_header_token(self) -> str:
                raw = (self.request.headers.get("Authorization", "") or "").strip()
                if not raw:
                    return ""
                parts = raw.split(None, 1)
                if len(parts) == 2 and parts[0].lower() in ("token", "bearer"):
                    return parts[1].strip()
                return raw

            def service_token_user(self) -> Optional[Dict[str, Any]]:
                if not API_AUTH_TOKEN:
                    return None
                token = self.auth_header_token()
                if not token:
                    return None
                if not hmac.compare_digest(token, API_AUTH_TOKEN):
                    raise HTTPError(401, "invalid service token")
                username = (self.request.headers.get("X-PI-USER", "") or "").strip()
                if not username:
                    raise HTTPError(400, "X-PI-USER header is required with service token auth")
                return {
                    "name": username,
                    "groups": user_groups(username),
                    "admin": is_admin(username),
                    "auth": "token",
                }

            def browser_user(self) -> Optional[Dict[str, Any]]:
                raw = self.current_user
                username = ""
                if raw:
                    if isinstance(raw, dict):
                        username = str(raw.get("name") or "").strip()
                    else:
                        username = str(raw).strip()
                # Fallback for proxied authenticated requests where service oauth
                # cookies are missing but JupyterHub proxy forwards identity headers.
                if not username:
                    username = (
                        (self.request.headers.get("X-Forwarded-User", "") or "").strip()
                        or (self.request.headers.get("X-JupyterHub-User", "") or "").strip()
                    )
                if not username:
                    return None
                return {
                    "name": username,
                    "groups": user_groups(username),
                    "admin": is_admin(username),
                    "auth": "browser",
                }

            def resolve_user(self) -> Dict[str, Any]:
                token_user = self.service_token_user()
                if token_user:
                    return token_user
                browser = self.browser_user()
                if browser:
                    return browser
                raise HTTPError(401, "authentication required")

            def require_share_access(self, share_id: str, user_ctx: Dict[str, Any], *, owner_only: bool = False) -> Dict[str, Any]:
                record = get_share(share_id)
                if not record:
                    raise HTTPError(404, "share not found")
                if share_revoked(record):
                    raise HTTPError(410, "share revoked")
                if share_expired(record):
                    raise HTTPError(410, "share expired")
                if owner_only:
                    if user_ctx.get("name") != record.get("owner") and not user_ctx.get("admin"):
                        raise HTTPError(403, "owner access required")
                    return record
                allowed = can_view(
                    record,
                    user_ctx.get("name", ""),
                    user_ctx.get("groups", []),
                    bool(user_ctx.get("admin")),
                )
                if not allowed:
                    raise HTTPError(403, "not allowed")
                return record


        class RootHandler(BaseHandler):
            @authenticated
            def get(self):
                user = self.browser_user()
                username = user["name"] if user else ""
                page = f"""<!doctype html>
                <html><head>
                  <meta charset=\"utf-8\" />
                  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
                  <title>Pi Session Viewer</title>
                  <style>
                    body {{ font-family: Inter, system-ui, sans-serif; margin: 24px; color: #0f172a; }}
                    .card {{ border: 1px solid #cbd5e1; border-radius: 10px; padding: 16px; max-width: 900px; }}
                    code {{ background:#f1f5f9; padding:2px 6px; border-radius:6px; }}
                    a {{ color:#2563eb; }}
                  </style>
                </head><body>
                  <div class=\"card\">
                    <h2>Pi Session Viewer</h2>
                    <p>Signed in as <b>{html.escape(username)}</b>.</p>
                    <p>Use <code>/session-share</code> in Pi to create shares.</p>
                    <p><a href=\"{SERVICE_PREFIX}/api/shares?scope=mine\">My shares (JSON)</a> ·
                       <a href=\"{SERVICE_PREFIX}/api/shares?scope=with-me\">Shared with me (JSON)</a></p>
                  </div>
                </body></html>"""
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(page)


        class ViewerPageHandler(BaseHandler):
            @authenticated
            def get(self, share_id: str):
                sid = normalize_share_id(share_id)
                if not sid:
                    raise HTTPError(404)
                user = self.resolve_user()
                self.require_share_access(sid, user)
                view_token = make_view_token(sid)
                page = f"""<!DOCTYPE html>
                <html lang=\"en\" data-theme=\"dark\">
                <head>
                  <meta charset=\"UTF-8\" />
                  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
                  <title>Session Preview – pi.dev</title>
                  <link rel=\"stylesheet\" type=\"text/css\" href=\"https://pi.dev/style.css\" />
                  <style>
                    .loading {{
                      display: flex;
                      flex-direction: column;
                      align-items: center;
                      justify-content: center;
                      height: 100vh;
                      gap: 1.5rem;
                    }}
                    .spinner {{
                      width: 48px;
                      height: 48px;
                      border: 3px solid var(--terminal-border);
                      border-top-color: var(--accent);
                      border-radius: 50%;
                      animation: spin 1s linear infinite;
                    }}
                    @keyframes spin {{
                      to {{
                        transform: rotate(360deg);
                      }}
                    }}
                    .loading p {{
                      font-family: var(--font-mono);
                      font-size: 14px;
                      color: var(--dimmed-text-color);
                      margin: 0;
                    }}
                    .error-container {{
                      display: flex;
                      flex-direction: column;
                      align-items: center;
                      justify-content: center;
                      min-height: 100vh;
                      padding: 2rem;
                    }}
                    .error-box {{
                      background: var(--terminal-bg);
                      border: 1px solid var(--terminal-border);
                      border-radius: 12px;
                      padding: 2.5rem;
                      max-width: 480px;
                      width: 100%;
                      text-align: center;
                      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                    }}
                    .error-icon {{
                      width: 64px;
                      height: 64px;
                      margin: 0 auto 1.5rem auto;
                      background: rgba(239, 68, 68, 0.1);
                      border-radius: 50%;
                      display: flex;
                      align-items: center;
                      justify-content: center;
                    }}
                    .error-icon svg {{
                      width: 32px;
                      height: 32px;
                      color: #ef4444;
                    }}
                    .error-box h1 {{
                      font-family: var(--font-sans);
                      font-size: 1.5rem;
                      font-weight: 700;
                      color: #fff;
                      margin-bottom: 1rem;
                    }}
                    .error-box p {{
                      color: var(--dimmed-text-color);
                      margin-bottom: 0;
                      line-height: 1.6;
                    }}
                    .error-box code {{
                      background: rgba(255, 255, 255, 0.1);
                      padding: 0.125rem 0.375rem;
                      border-radius: 4px;
                      font-size: 0.875rem;
                      border: none;
                    }}
                    .error-home-link {{
                      display: inline-block;
                      margin-top: 1.5rem;
                      color: var(--accent);
                      font-family: var(--font-sans);
                      font-size: 0.875rem;
                      text-decoration: none;
                      border-bottom: 1px solid transparent;
                      transition: border-color 0.2s;
                    }}
                    .error-home-link:hover {{
                      border-bottom-color: var(--accent);
                    }}
                    .preview-frame {{
                      position: fixed;
                      top: 0;
                      left: 0;
                      width: 100%;
                      height: 100%;
                      border: none;
                      background: white;
                    }}
                    .hidden {{
                      display: none !important;
                    }}
                  </style>
                </head>
                <body>
                  <div id=\"loading\" class=\"loading\">
                    <div class=\"spinner\"></div>
                    <p>Loading session...</p>
                  </div>

                  <div id=\"error\" class=\"error-container hidden\">
                    <div class=\"error-box\">
                      <div class=\"error-icon\">
                        <svg xmlns=\"http://www.w3.org/2000/svg\" fill=\"none\" viewBox=\"0 0 24 24\" stroke-width=\"1.5\" stroke=\"currentColor\">
                          <path stroke-linecap=\"round\" stroke-linejoin=\"round\" d=\"M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z\" />
                        </svg>
                      </div>
                      <h1>Failed to load session</h1>
                      <p id=\"error-message\"></p>
                      <a href=\"/\" class=\"error-home-link\">← Back to home</a>
                    </div>
                  </div>

                  <iframe id=\"preview\" class=\"preview-frame hidden\" sandbox=\"allow-scripts allow-downloads\"></iframe>

                  <script>
                    (function() {{
                      const SHARE_ID = {json.dumps(sid)};
                      const SERVICE_PREFIX = {json.dumps(SERVICE_PREFIX)};
                      const VIEW_TOKEN = {json.dumps(view_token)};

                      const loadingEl = document.getElementById("loading");
                      const errorEl = document.getElementById("error");
                      const errorMessageEl = document.getElementById("error-message");
                      const previewEl = document.getElementById("preview");

                      function showError(message) {{
                        loadingEl.classList.add("hidden");
                        errorMessageEl.innerHTML = message;
                        errorEl.classList.remove("hidden");
                      }}

                      function showPreview(content, shareId, urlParams) {{
                        loadingEl.classList.add("hidden");
                        const baseUrl = location.origin + SERVICE_PREFIX + "/s/" + shareId + "?" + shareId;
                        const metaTags = '<meta name="pi-share-base-url" content="' + baseUrl + '">' +
                                         '<meta name="pi-url-params" content="' + urlParams + '">';
                        content = content.replace(/<head[^>]*>/i, "$&" + metaTags);
                        previewEl.srcdoc = content;
                        previewEl.classList.remove("hidden");
                      }}

                      const urlParams = location.search ? location.search.substring(1) : "";
                      const exportUrl = SERVICE_PREFIX + "/api/shares/" + encodeURIComponent(SHARE_ID) + "/export/session.html?vt=" + encodeURIComponent(VIEW_TOKEN);

                      fetch(exportUrl)
                        .then(function(res) {{
                          if (!res.ok) {{
                            if (res.status === 404) throw new Error("Session not found. It may have been deleted.");
                            if (res.status === 410) throw new Error("Session expired or revoked.");
                            if (res.status === 403) throw new Error("You do not have access to this session.");
                            throw new Error("Viewer API error: " + res.status);
                          }}
                          return res.text();
                        }})
                        .then(function(content) {{
                          showPreview(content, SHARE_ID, urlParams);
                        }})
                        .catch(function(err) {{
                          showError(err.message || "Unknown error while loading session");
                        }});
                    }})();
                  </script>
                </body>
                </html>"""
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(page)


        class SharesApiHandler(BaseHandler):
            def get(self):
                user = self.resolve_user()
                scope = (self.get_argument("scope", "with-me") or "with-me").strip().lower()
                all_shares = list_all_shares()
                visible = []
                for rec in all_shares:
                    expired = share_expired(rec)
                    revoked = share_revoked(rec)
                    if scope == "mine":
                        if rec.get("owner") != user["name"]:
                            continue
                        if revoked:
                            continue
                    elif scope == "all" and user.get("admin"):
                        if revoked:
                            continue
                    else:
                        if revoked or expired:
                            continue
                        if not can_view(rec, user["name"], user.get("groups", []), bool(user.get("admin"))):
                            continue
                    visible.append(
                        {
                            "id": rec["id"],
                            "title": rec["title"],
                            "owner": rec["owner"],
                            "created_at": rec["created_at"],
                            "expires_at": rec["expires_at"],
                            "entry_count": rec["entry_count"],
                            "size_bytes": rec["size_bytes"],
                            "leaf_ids": rec["leaf_ids"],
                            "viewer_url": share_view_url(rec["id"]),
                            "revoked_at": rec.get("revoked_at"),
                        }
                    )
                self.write_json({"ok": True, "scope": scope, "shares": visible})

            def post(self):
                user = self.resolve_user()
                try:
                    body = json.loads(self.request.body.decode("utf-8")) if self.request.body else {}
                except Exception:
                    raise HTTPError(400, "invalid json")

                owner = (body.get("owner") or user.get("name") or "").strip()
                if user.get("auth") != "token":
                    owner = user.get("name", "")
                if not owner:
                    raise HTTPError(400, "owner is required")

                if user.get("auth") == "token":
                    # Service token can only create shares for itself unless caller is admin.
                    if owner != user.get("name") and not user.get("admin"):
                        raise HTTPError(403, "cannot create share for another user")

                session_path = (body.get("session_path") or "").strip()
                title = (body.get("title") or "").strip()
                acl_users = parse_targets(body.get("share_with_users"))
                acl_groups = parse_targets(body.get("share_with_groups"))
                expires_hours = int(body.get("expires_hours") or DEFAULT_EXPIRES_HOURS)
                content_base64 = (body.get("content_base64") or "").strip()
                if not content_base64:
                    raise HTTPError(400, "content_base64 is required")
                try:
                    content = base64.b64decode(content_base64)
                except Exception:
                    raise HTTPError(400, "content_base64 is invalid")

                try:
                    record = create_share_record(
                        owner=owner,
                        title=title,
                        acl_users=acl_users,
                        acl_groups=acl_groups,
                        expires_hours=expires_hours,
                        session_path=session_path,
                        content=content,
                    )
                except Exception as exc:
                    raise HTTPError(400, str(exc))

                self.write_json(
                    {
                        "ok": True,
                        "share": {
                            "id": record["id"],
                            "owner": record["owner"],
                            "title": record["title"],
                            "created_at": record["created_at"],
                            "expires_at": record["expires_at"],
                            "entry_count": record["entry_count"],
                            "size_bytes": record["size_bytes"],
                            "viewer_url": share_view_url(record["id"]),
                        },
                    },
                    status=201,
                )


        class ShareMetaHandler(BaseHandler):
            def get(self, share_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user)
                self.write_json(
                    {
                        "ok": True,
                        "share": {
                            "id": rec["id"],
                            "owner": rec["owner"],
                            "title": rec["title"],
                            "created_at": rec["created_at"],
                            "expires_at": rec["expires_at"],
                            "entry_count": rec["entry_count"],
                            "size_bytes": rec["size_bytes"],
                            "leaf_ids": rec["leaf_ids"],
                            "viewer_url": share_view_url(rec["id"]),
                        },
                    }
                )


        class ShareContentHandler(BaseHandler):
            def get(self, share_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user)
                leaf_id = (self.get_argument("leafId", "") or "").strip()
                target_id = (self.get_argument("targetId", "") or "").strip()
                try:
                    _text, entries = load_entries_for_share(rec)
                except Exception as exc:
                    raise HTTPError(500, f"failed to load session payload: {exc}")

                leaf_ids = rec.get("leaf_ids") or compute_leaf_ids(entries)
                selected_leaf = ""
                if leaf_id and leaf_id in leaf_ids:
                    selected_leaf = leaf_id
                    entries = filter_branch(entries, leaf_id)

                leafs = []
                by_id = {str(e.get("id")): e for e in entries if str(e.get("id") or "")}
                for lid in leaf_ids:
                    entry = by_id.get(lid) or {}
                    leafs.append({"id": lid, "preview": preview_text(entry)})

                update_last_accessed(rec["id"])
                self.write_json(
                    {
                        "ok": True,
                        "share": {
                            "id": rec["id"],
                            "owner": rec["owner"],
                            "title": rec["title"],
                            "created_at": rec["created_at"],
                            "expires_at": rec["expires_at"],
                            "entry_count": rec["entry_count"],
                            "size_bytes": rec["size_bytes"],
                        },
                        "entries": entries,
                        "leafs": leafs,
                        "selected_leaf": selected_leaf,
                        "target_id": target_id,
                    }
                )


        class ShareLeafsHandler(BaseHandler):
            def get(self, share_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user)
                try:
                    _text, entries = load_entries_for_share(rec)
                except Exception as exc:
                    raise HTTPError(500, f"failed to load session payload: {exc}")
                leaf_ids = rec.get("leaf_ids") or compute_leaf_ids(entries)
                by_id = {str(e.get("id")): e for e in entries if str(e.get("id") or "")}
                payload = [
                    {
                        "id": lid,
                        "preview": preview_text(by_id.get(lid) or {}),
                        "url": f"{share_view_url(rec['id'])}?{urlencode({'leafId': lid})}",
                    }
                    for lid in leaf_ids
                ]
                self.write_json({"ok": True, "leafs": payload})


        class ShareEntryHandler(BaseHandler):
            def get(self, share_id: str, entry_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user)
                try:
                    _text, entries = load_entries_for_share(rec)
                except Exception as exc:
                    raise HTTPError(500, f"failed to load session payload: {exc}")
                target = None
                for e in entries:
                    if str(e.get("id") or "") == entry_id:
                        target = e
                        break
                if not target:
                    raise HTTPError(404, "entry not found")

                leaf_ids = rec.get("leaf_ids") or compute_leaf_ids(entries)
                preferred_leaf = ""
                for lid in leaf_ids:
                    chain = build_chain(entries, lid)
                    if entry_id in chain:
                        preferred_leaf = lid
                        break
                params = {}
                if preferred_leaf:
                    params["leafId"] = preferred_leaf
                params["targetId"] = entry_id
                deep = share_view_url(rec["id"])
                if params:
                    deep = f"{deep}?{urlencode(params)}"
                self.write_json({"ok": True, "entry": target, "deep_link": deep})


        class ShareDownloadHandler(BaseHandler):
            def get(self, share_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user)
                try:
                    text, _entries = load_entries_for_share(rec)
                except Exception as exc:
                    raise HTTPError(500, f"failed to load session payload: {exc}")
                filename = f"pi-session-{rec['id']}.jsonl"
                self.set_header("Content-Type", "application/jsonl; charset=utf-8")
                self.set_header("Content-Disposition", f"attachment; filename={filename}")
                self.write(text)


        class ShareExportHtmlHandler(BaseHandler):
            def get(self, share_id: str):
                sid = normalize_share_id(share_id)
                if not sid:
                    raise HTTPError(404)
                rec = get_share(sid)
                if not rec:
                    raise HTTPError(404, "share not found")
                if share_revoked(rec):
                    raise HTTPError(410, "share revoked")
                if share_expired(rec):
                    raise HTTPError(410, "share expired")

                # Preferred path for browser viewer page: short-lived signed token
                # minted by ViewerPageHandler after ACL check.
                view_token = (self.get_argument("vt", "") or "").strip()
                if view_token:
                    if not verify_view_token(sid, view_token):
                        raise HTTPError(401, "invalid viewer token")
                else:
                    # Fallback for API/service-token callers.
                    user = self.resolve_user()
                    self.require_share_access(sid, user)

                try:
                    text, _entries = load_entries_for_share(rec)
                except Exception as exc:
                    raise HTTPError(500, f"failed to load session payload: {exc}")

                try:
                    with tempfile.TemporaryDirectory(prefix="pi-share-export-") as temp_dir:
                        in_path = os.path.join(temp_dir, f"{rec['id']}.jsonl")
                        out_path = os.path.join(temp_dir, f"{rec['id']}.html")
                        with open(in_path, "w", encoding="utf-8") as f:
                            f.write(text)

                        proc = subprocess.run(
                            [
                                "npx",
                                "-y",
                                "@mariozechner/pi-coding-agent",
                                "--export",
                                in_path,
                                out_path,
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=180,
                        )
                        if proc.returncode != 0:
                            raise RuntimeError(
                                "pi export failed: "
                                f"exit={proc.returncode} stderr={proc.stderr.strip()[:500]}"
                            )

                        with open(out_path, "r", encoding="utf-8") as f:
                            html_doc = f.read()
                except Exception as exc:
                    raise HTTPError(500, f"failed to render session.html export: {exc}")

                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(html_doc)


        class ShareRevokeHandler(BaseHandler):
            def post(self, share_id: str):
                user = self.resolve_user()
                rec = self.require_share_access(share_id, user, owner_only=True)
                revoke_share(rec["id"])
                self.write_json({"ok": True, "id": rec["id"], "revoked_at": isoformat(utcnow())})


        class HealthHandler(RequestHandler):
            def get(self):
                try:
                    init_db()
                    self.set_header("Content-Type", "application/json")
                    self.write(json.dumps({"ok": True, "service": "pi-session-viewer"}))
                except Exception as exc:
                    self.set_status(500)
                    self.write(json.dumps({"ok": False, "error": str(exc)}))


        def periodic_cleanup():
            try:
                cleanup_expired()
            except Exception as exc:
                print(f"WARN cleanup failed: {exc}")


        def make_app() -> Application:
            return Application(
                [
                    (rf"{SERVICE_PREFIX}/healthz", HealthHandler),
                    (rf"{SERVICE_PREFIX}/oauth_callback", HubOAuthCallbackHandler),
                    (rf"{SERVICE_PREFIX}/", RootHandler),
                    (rf"{SERVICE_PREFIX}/s/([a-z0-9-]+)", ViewerPageHandler),
                    (rf"{SERVICE_PREFIX}/api/shares", SharesApiHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)", ShareMetaHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/content", ShareContentHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/download", ShareDownloadHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/leafs", ShareLeafsHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/entries/([^/]+)", ShareEntryHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/export/session\.html", ShareExportHtmlHandler),
                    (rf"{SERVICE_PREFIX}/api/shares/([a-z0-9-]+)/revoke", ShareRevokeHandler),
                ],
                cookie_secret=os.urandom(32),
            )


        def main():
            init_db()
            if SHARES_BUCKET:
                try:
                    s3_ensure_bucket()
                    print(f"pi-session-viewer using S3 bucket={SHARES_BUCKET} prefix={SHARES_PREFIX} endpoint={S3_ENDPOINT or 'aws'}")
                except Exception as exc:
                    print(f"WARN: unable to initialize s3 client now: {exc}")
            else:
                print("WARN: PI_SESSION_VIEWER_S3_BUCKET not configured; share creation will fail")

            app = make_app()
            port = int(os.environ.get("PORT", "10400"))
            app.listen(port, address="0.0.0.0")

            if CLEANUP_INTERVAL_SECONDS > 0:
                callback = PeriodicCallback(periodic_cleanup, CLEANUP_INTERVAL_SECONDS * 1000)
                callback.start()

            IOLoop.current().start()


        if __name__ == "__main__":
            main()
        '''
    )
)

import z2jh

enabled = bool(z2jh.get_config("custom.pi-session-viewer-enabled", False))
if not enabled:
    print("pi-session-viewer disabled via custom.pi-session-viewer-enabled")
else:
    port = int(z2jh.get_config("custom.pi-session-viewer-port", 10400))
    service_name = str(z2jh.get_config("custom.pi-session-viewer-service-name", "pi-session-viewer") or "pi-session-viewer")
    public_host = str(z2jh.get_config("custom.external-url") or "").strip().rstrip("/")
    if public_host and not public_host.startswith(("http://", "https://")):
        scheme = str(z2jh.get_config("custom.external-url-scheme", "https") or "https").strip()
        public_host = f"{scheme}://{public_host}"

    oauth_redirect_uri = f"{public_host}/services/{service_name}/oauth_callback"

    env = {
        "PORT": str(port),
        "PUBLIC_HOST": public_host,
        "JUPYTERHUB_OAUTH_CALLBACK_URL": oauth_redirect_uri,
        "PI_SESSION_VIEWER_S3_BUCKET": str(z2jh.get_config("custom.pi-session-viewer-s3-bucket", "") or "").strip(),
        "PI_SESSION_VIEWER_S3_PREFIX": str(z2jh.get_config("custom.pi-session-viewer-s3-prefix", "pi-shares") or "pi-shares").strip(),
        "PI_SESSION_VIEWER_S3_REGION": str(z2jh.get_config("custom.pi-session-viewer-s3-region", "us-east-1") or "us-east-1").strip(),
        "PI_SESSION_VIEWER_S3_ENDPOINT": str(z2jh.get_config("custom.pi-session-viewer-s3-endpoint", "") or "").strip(),
        "PI_SESSION_VIEWER_S3_ACCESS_KEY_ID": str(z2jh.get_config("custom.pi-session-viewer-s3-access-key-id", "") or "").strip(),
        "PI_SESSION_VIEWER_S3_SECRET_ACCESS_KEY": str(z2jh.get_config("custom.pi-session-viewer-s3-secret-access-key", "") or "").strip(),
        "PI_SESSION_VIEWER_S3_SESSION_TOKEN": str(z2jh.get_config("custom.pi-session-viewer-s3-session-token", "") or "").strip(),
        "PI_SESSION_VIEWER_S3_AUTO_CREATE_BUCKET": "1" if bool(z2jh.get_config("custom.pi-session-viewer-s3-auto-create-bucket", False)) else "0",
        "PI_SESSION_VIEWER_DB_PATH": str(z2jh.get_config("custom.pi-session-viewer-db-path", "/tmp/pi-session-viewer/shares.db") or "/tmp/pi-session-viewer/shares.db").strip(),
        "PI_SESSION_VIEWER_DEFAULT_EXPIRES_HOURS": str(z2jh.get_config("custom.pi-session-viewer-default-expires-hours", 720)),
        "PI_SESSION_VIEWER_MAX_EXPIRES_HOURS": str(z2jh.get_config("custom.pi-session-viewer-max-expires-hours", 2160)),
        "PI_SESSION_VIEWER_MAX_SESSION_BYTES": str(z2jh.get_config("custom.pi-session-viewer-max-session-bytes", 10485760)),
        "PI_SESSION_VIEWER_CLEANUP_INTERVAL_SECONDS": str(z2jh.get_config("custom.pi-session-viewer-cleanup-interval-seconds", 600)),
        "PI_SESSION_VIEWER_HARD_DELETE_GRACE_HOURS": str(z2jh.get_config("custom.pi-session-viewer-hard-delete-grace-hours", 168)),
    }

    api_token = str(z2jh.get_config("custom.pi-session-viewer-api-token", "") or "").strip()
    if not api_token:
        api_token = (os.environ.get("PI_M4_TOOLS_API_TOKEN") or "").strip()
    if api_token:
        env["PI_SESSION_VIEWER_API_TOKEN"] = api_token

    c.JupyterHub.services.append(
        {
            "name": service_name,
            "url": f"http://hub:{port}",
            "command": ["python", str(pi_session_viewer_py)],
            "environment": env,
            "oauth_redirect_uri": oauth_redirect_uri,
            "oauth_no_confirm": True,
            "display": False,
        }
    )

    viewer_role = {
        "name": "pi-session-viewer-service-role",
        "services": [service_name],
        "scopes": [
            "read:users",
            "read:users:name",
            "read:groups",
            "list:groups",
            "access:services",
            "read:services",
            "list:services",
        ],
    }
    if not c.JupyterHub.load_roles:
        c.JupyterHub.load_roles = []
    if isinstance(c.JupyterHub.load_roles, list):
        c.JupyterHub.load_roles.append(viewer_role)
    else:
        c.JupyterHub.load_roles = [viewer_role]
