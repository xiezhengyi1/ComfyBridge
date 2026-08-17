"""Credential registry for activation keys, short-lived browser sessions and audit events.

Raw API keys and session tokens are deliberately never written to ``storage/``.
Only HMAC digests (keyed with ``key_hash_secret``) are persisted. Raw values are
returned exactly once when an administrator creates or rotates a credential.

Admin keys live in ``config.json`` as ``hmac$<digest>`` entries (plaintext is
archived by ``config.bootstrap`` into local ``admin-key.txt``); ``is_admin_key``
accepts both the digest form and a legacy plaintext entry.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from config import HASH_PREFIX


KEY_STATUS_UNUSED = "unused"
KEY_STATUS_USED = "used"
KEY_STATUS_REVOKED = "revoked"
KEY_STATUS_EXPIRED = "expired"

USER_ROLE_ADMIN = "admin"
USER_ROLE_USER = "user"

USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"


def _new_id(prefix: str, bytes_: int = 8) -> str:
    return f"{prefix}_{secrets.token_urlsafe(bytes_)}"


class KeyRegistry:
    """Single-process credential store.

    The service currently runs one Uvicorn process. If it is later scaled to
    multiple processes or replicas, move this registry to a transactional shared
    store before enabling that topology.
    """

    def __init__(self, storage_dir, cfg: dict):
        self.storage = Path(storage_dir)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.keys_file = self.storage / "keys.json"
        self.users_file = self.storage / "users.json"
        self.sessions_file = self.storage / "sessions.json"
        self.audit_file = self.storage / "audit.jsonl"
        self._cfg = cfg
        secret = str(cfg.get("key_hash_secret") or "").strip()
        if not secret:
            raise ValueError("key_hash_secret is required before KeyRegistry starts")
        self._pepper = secret.encode("utf-8")
        self._lock = threading.RLock()
        self._keys: dict[str, dict] = {}
        self._users: dict[str, dict] = {}
        self._sessions: dict[str, dict] = {}
        self._load()

    # ---------------- Cryptography and persistence ----------------
    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(self._pepper, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()

    def _key_hash(self, key: str) -> str:
        return self._digest("api-key", key)

    def _session_hash(self, token: str) -> str:
        return self._digest("session", token)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _save_locked(self) -> None:
        self._write_json(self.keys_file, self._keys)
        self._write_json(self.users_file, self._users)
        self._write_json(self.sessions_file, self._sessions)

    def _audit_locked(self, action: str, *, actor: str | None = None,
                      subject: str | None = None, detail: dict | None = None) -> None:
        """Append a secret-free audit record and rotate it before it grows indefinitely."""
        event = {
            "at": time.time(),
            "action": action,
            "actor": actor,
            "subject": subject,
            "detail": detail or {},
        }
        try:
            if self.audit_file.exists() and self.audit_file.stat().st_size >= 1_048_576:
                backup = self.storage / "audit.1.jsonl"
                if backup.exists():
                    backup.unlink()
                self.audit_file.replace(backup)
                try:
                    os.chmod(backup, 0o600)
                except OSError:
                    pass
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            try:
                os.chmod(self.audit_file, 0o600)
            except OSError:
                pass
        except OSError:
            # An unavailable audit disk must not turn a normal revocation into a failed one.
            pass

    def _load(self) -> None:
        with self._lock:
            self._keys = self._read_json(self.keys_file)
            self._users = self._read_json(self.users_file)
            self._sessions = self._read_json(self.sessions_file)
            changed = self._migrate_legacy_locked()
            changed = self._expire_records_locked() or changed
            if changed:
                self._save_locked()

    def _migrate_legacy_locked(self) -> bool:
        """Convert v0.4 plaintext storage in place without ever logging a key."""
        changed = False
        now = time.time()
        keys: dict[str, dict] = {}
        for map_key, original in self._keys.items():
            if not isinstance(original, dict):
                changed = True
                continue
            rec = dict(original)
            raw = str(rec.pop("key", "") or "")
            digest = self._key_hash(raw) if raw else str(rec.pop("key_hash", "") or map_key)
            if not digest:
                changed = True
                continue
            if raw or map_key != digest or "id" not in rec:
                changed = True
            rec["id"] = str(rec.get("id") or _new_id("key"))
            rec["status"] = rec.get("status", KEY_STATUS_UNUSED)
            rec.setdefault("bound_user", None)
            rec.setdefault("note", "")
            rec.setdefault("created_at", now)
            rec.setdefault("used_at", None)
            rec.setdefault("revoked_at", None)
            # Existing deployments acquire an explicit expiry from the upgrade time.
            if rec.get("expires_at") is None:
                hours = self._activation_ttl_hours() if rec["status"] == KEY_STATUS_UNUSED else self._user_key_ttl_hours()
                rec["expires_at"] = now + hours * 3600
                changed = True
            keys[digest] = rec
        self._keys = keys

        users: dict[str, dict] = {}
        for map_key, original in self._users.items():
            if not isinstance(original, dict):
                changed = True
                continue
            user = dict(original)
            raw = str(user.pop("api_key", "") or "")
            digest = self._key_hash(raw) if raw else str(user.pop("key_hash", "") or map_key)
            if not digest:
                changed = True
                continue
            if raw or map_key != digest:
                changed = True
            user["user_id"] = str(user.get("user_id") or _new_id("u"))
            user["role"] = user.get("role", USER_ROLE_USER)
            user["status"] = user.get("status", USER_STATUS_ACTIVE)
            user.setdefault("activated_at", now)
            user.setdefault("last_seen_at", now)
            user.setdefault("credential_version", 1)
            rec = self._keys.get(digest)
            user.setdefault("key_id", rec.get("id") if rec else None)
            if user.get("role") == USER_ROLE_USER:
                user.setdefault("key_expires_at", rec.get("expires_at") if rec else now + self._user_key_ttl_hours() * 3600)
            else:
                user["key_expires_at"] = None
            # 管理员 user_id 统一按当前摘要派生，保证与 admin_owner_id()/任务归属一致
            # （修复旧版 sha1 派生的历史残留）。
            if user.get("role") == USER_ROLE_ADMIN:
                want = "admin_" + digest[:12]
                if user.get("user_id") != want:
                    user["user_id"] = want
                    changed = True
            users[digest] = user
        self._users = users

        # Sessions only ever use the new hash-only schema. Drop malformed legacy data.
        sessions = {}
        for digest, rec in self._sessions.items():
            if isinstance(rec, dict) and rec.get("user_id") and rec.get("expires_at"):
                sessions[str(digest)] = dict(rec)
            else:
                changed = True
        self._sessions = sessions
        return changed

    def _activation_ttl_hours(self) -> int:
        return max(1, min(int(self._cfg.get("activation_key_ttl_hours", 168)), 24 * 90))

    def _user_key_ttl_hours(self) -> int:
        return max(1, min(int(self._cfg.get("user_key_ttl_days", 30)), 365)) * 24

    def _session_ttl_seconds(self) -> int:
        return max(1, min(int(self._cfg.get("session_ttl_hours", 12)), 24 * 7)) * 3600

    def _expire_records_locked(self) -> bool:
        now = time.time()
        changed = False
        for rec in self._keys.values():
            if rec.get("status") in (KEY_STATUS_UNUSED, KEY_STATUS_USED):
                if float(rec.get("expires_at") or 0) <= now:
                    rec["status"] = KEY_STATUS_EXPIRED
                    changed = True
        stale_sessions = [digest for digest, rec in self._sessions.items()
                          if float(rec.get("expires_at") or 0) <= now]
        for digest in stale_sessions:
            self._sessions.pop(digest, None)
            changed = True
        return changed

    # ---------------- Admin identities ----------------
    def _admin_keys(self) -> list[str]:
        return [str(k).strip() for k in (self._cfg.get("api_keys") or []) if str(k).strip()]

    def _admin_digest(self, entry: str) -> str:
        """管理员 Key 条目规范化为摘要：hmac$<digest> 直接取摘要，明文则现场计算。"""
        entry = str(entry or "").strip()
        if entry.startswith(HASH_PREFIX):
            return entry[len(HASH_PREFIX):]
        return self._key_hash(entry)

    def is_admin_key(self, key: str) -> bool:
        candidate = self._key_hash(key)
        return any(hmac.compare_digest(candidate, self._admin_digest(admin_key))
                   for admin_key in self._admin_keys())

    def _admin_user_id(self, key: str) -> str:
        return "admin_" + self._key_hash(key)[:12]

    def admin_owner_id(self) -> str | None:
        """第一个管理员 Key 对应的用户 ID（与登录后 _admin_user_id 派生一致）。"""
        keys = self._admin_keys()
        return ("admin_" + self._admin_digest(keys[0])[:12]) if keys else None

    def _ensure_admin_user_locked(self, key: str) -> dict:
        digest = self._key_hash(key)
        user = self._users.get(digest)
        if user is None:
            now = time.time()
            user = {
                "user_id": self._admin_user_id(key),
                "key_id": "admin_" + digest[:16],
                "role": USER_ROLE_ADMIN,
                "status": USER_STATUS_ACTIVE,
                "activated_at": now,
                "last_seen_at": now,
                "key_expires_at": None,
                "credential_version": 1,
            }
            self._users[digest] = user
            self._save_locked()
            self._audit_locked("admin_identity_created", actor=user["user_id"])
        return user

    # ---------------- Public-safe projections ----------------
    @staticmethod
    def _public_key(rec: dict) -> dict:
        return {
            "id": rec.get("id"),
            "status": rec.get("status"),
            "bound_user": rec.get("bound_user"),
            "note": rec.get("note", ""),
            "created_at": rec.get("created_at"),
            "used_at": rec.get("used_at"),
            "expires_at": rec.get("expires_at"),
            "revoked_at": rec.get("revoked_at"),
        }

    @staticmethod
    def _public_user(user: dict) -> dict:
        return {
            "user_id": user.get("user_id"),
            "role": user.get("role"),
            "status": user.get("status"),
            "key_id": user.get("key_id"),
            "activated_at": user.get("activated_at"),
            "last_seen_at": user.get("last_seen_at"),
            "key_expires_at": user.get("key_expires_at"),
        }

    # ---------------- Key lifecycle ----------------
    def generate_keys(self, count: int, note: str = "", expires_in_hours: int | None = None,
                      actor: str | None = None) -> list[dict]:
        """Issue activation keys. Each raw key is included only in this return value."""
        count = max(1, min(int(count), 100))
        ttl = self._activation_ttl_hours() if expires_in_hours is None else max(1, min(int(expires_in_hours), 24 * 90))
        now = time.time()
        out = []
        with self._lock:
            self._expire_records_locked()
            for _ in range(count):
                raw = "cb_" + secrets.token_urlsafe(32)
                digest = self._key_hash(raw)
                while digest in self._keys or self.is_admin_key(raw):
                    raw = "cb_" + secrets.token_urlsafe(32)
                    digest = self._key_hash(raw)
                rec = {
                    "id": _new_id("key"),
                    "status": KEY_STATUS_UNUSED,
                    "bound_user": None,
                    "note": (note or "")[:200],
                    "created_at": now,
                    "used_at": None,
                    "expires_at": now + ttl * 3600,
                    "revoked_at": None,
                }
                self._keys[digest] = rec
                out.append({**self._public_key(rec), "key": raw})
            self._save_locked()
            self._audit_locked("activation_keys_issued", actor=actor, detail={"count": count, "ttl_hours": ttl})
        return out

    def resolve_user(self, key: str) -> dict | None:
        """Resolve a raw API key; the raw value is never persisted or returned."""
        key = (key or "").strip()
        if not key:
            return None
        with self._lock:
            changed = self._expire_records_locked()
            if self.is_admin_key(key):
                user = self._ensure_admin_user_locked(key)
                user["last_seen_at"] = time.time()
                if changed:
                    self._save_locked()
                return dict(user)

            digest = self._key_hash(key)
            rec = self._keys.get(digest)
            if rec is None or rec.get("status") in (KEY_STATUS_REVOKED, KEY_STATUS_EXPIRED):
                if changed:
                    self._save_locked()
                return None
            now = time.time()
            if rec.get("status") == KEY_STATUS_UNUSED:
                user = {
                    "user_id": _new_id("u"),
                    "key_id": rec["id"],
                    "role": USER_ROLE_USER,
                    "status": USER_STATUS_ACTIVE,
                    "activated_at": now,
                    "last_seen_at": now,
                    "key_expires_at": now + self._user_key_ttl_hours() * 3600,
                    "credential_version": 1,
                }
                rec["status"] = KEY_STATUS_USED
                rec["bound_user"] = user["user_id"]
                rec["used_at"] = now
                rec["expires_at"] = user["key_expires_at"]
                self._users[digest] = user
                self._save_locked()
                self._audit_locked("activation_key_redeemed", actor=user["user_id"], subject=rec["id"])
                return dict(user)

            user = self._users.get(digest)
            if user is None or user.get("status") != USER_STATUS_ACTIVE:
                if changed:
                    self._save_locked()
                return None
            if float(user.get("key_expires_at") or 0) <= now:
                rec["status"] = KEY_STATUS_EXPIRED
                self._save_locked()
                self._audit_locked("api_key_expired", actor=user["user_id"], subject=rec["id"])
                return None
            user["last_seen_at"] = now
            if changed:
                self._save_locked()
            return dict(user)

    def _find_user_locked(self, user_id: str) -> tuple[str, dict] | tuple[None, None]:
        for digest, user in self._users.items():
            if user.get("user_id") == user_id:
                return digest, user
        return None, None

    def rotate_user_key(self, user_id: str, *, expires_in_hours: int | None = None,
                        actor: str | None = None) -> dict | None:
        """Replace a user's API key and invalidate every existing browser session."""
        ttl = self._user_key_ttl_hours() if expires_in_hours is None else max(1, min(int(expires_in_hours), 24 * 365))
        now = time.time()
        with self._lock:
            old_digest, user = self._find_user_locked(user_id)
            if user is None or user.get("role") != USER_ROLE_USER:
                return None
            old_rec = self._keys.get(old_digest)
            if old_rec is not None:
                old_rec["status"] = KEY_STATUS_REVOKED
                old_rec["revoked_at"] = now
            raw = "cb_" + secrets.token_urlsafe(32)
            digest = self._key_hash(raw)
            while digest in self._keys or self.is_admin_key(raw):
                raw = "cb_" + secrets.token_urlsafe(32)
                digest = self._key_hash(raw)
            rec = {
                "id": _new_id("key"),
                "status": KEY_STATUS_USED,
                "bound_user": user_id,
                "note": "rotated credential",
                "created_at": now,
                "used_at": now,
                "expires_at": now + ttl * 3600,
                "revoked_at": None,
            }
            user["key_id"] = rec["id"]
            user["key_expires_at"] = rec["expires_at"]
            user["credential_version"] = int(user.get("credential_version", 1)) + 1
            user["last_seen_at"] = now
            self._keys[digest] = rec
            self._users.pop(old_digest, None)
            self._users[digest] = user
            self._save_locked()
            self._audit_locked("api_key_rotated", actor=actor, subject=user_id, detail={"ttl_hours": ttl})
            return {**self._public_key(rec), "key": raw}

    def list_keys(self) -> list[dict]:
        with self._lock:
            changed = self._expire_records_locked()
            if changed:
                self._save_locked()
            out = [self._public_key(rec) for rec in self._keys.values()]
        return sorted(out, key=lambda rec: rec.get("created_at") or 0, reverse=True)

    def list_users(self) -> list[dict]:
        with self._lock:
            out = [self._public_user(user) for user in self._users.values()]
        return sorted(out, key=lambda user: user.get("activated_at") or 0)

    def revoke_key(self, key_id: str, *, actor: str | None = None) -> dict | None:
        """Revoke by opaque record ID, never by a secret in the request path."""
        now = time.time()
        with self._lock:
            digest = next((d for d, rec in self._keys.items() if rec.get("id") == key_id), None)
            if digest is None:
                return None
            rec = self._keys[digest]
            rec["status"] = KEY_STATUS_REVOKED
            rec["revoked_at"] = now
            uid = rec.get("bound_user")
            if uid:
                _, user = self._find_user_locked(uid)
                # A historical key stays revocable for audit, but revoking it must
                # not disable a user who has already received a replacement key.
                if user is not None and user.get("key_id") == rec.get("id"):
                    user["status"] = USER_STATUS_DISABLED
                    user["credential_version"] = int(user.get("credential_version", 1)) + 1
            self._save_locked()
            self._audit_locked("api_key_revoked", actor=actor, subject=key_id, detail={"user_id": uid})
            return self._public_key(rec)

    def set_user_status(self, user_id: str, status: str, *, actor: str | None = None) -> dict | None:
        if status not in (USER_STATUS_ACTIVE, USER_STATUS_DISABLED):
            return None
        with self._lock:
            _, user = self._find_user_locked(user_id)
            if user is None:
                return None
            user["status"] = status
            if status == USER_STATUS_DISABLED:
                user["credential_version"] = int(user.get("credential_version", 1)) + 1
            self._save_locked()
            self._audit_locked("user_status_changed", actor=actor, subject=user_id, detail={"status": status})
            return self._public_user(user)

    def audit_login_failure(self, *, client_ip: str = "") -> None:
        """Record a failed login without retaining the submitted credential."""
        with self._lock:
            self._audit_locked("login_failed", detail={"client_ip": client_ip})

    # ---------------- Browser sessions ----------------
    def create_session(self, user: dict, *, client_ip: str = "") -> tuple[str, dict]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        digest = self._session_hash(token)
        with self._lock:
            self._expire_records_locked()
            rec = {
                "id": _new_id("sess"),
                "user_id": user["user_id"],
                "credential_version": int(user.get("credential_version", 1)),
                "created_at": now,
                "last_seen_at": now,
                "expires_at": now + self._session_ttl_seconds(),
            }
            self._sessions[digest] = rec
            self._save_locked()
            self._audit_locked("session_created", actor=user["user_id"], subject=rec["id"], detail={"client_ip": client_ip})
        return token, dict(rec)

    def resolve_session(self, token: str) -> dict | None:
        token = (token or "").strip()
        if not token:
            return None
        now = time.time()
        with self._lock:
            changed = self._expire_records_locked()
            session = self._sessions.get(self._session_hash(token))
            if session is None:
                if changed:
                    self._save_locked()
                return None
            _, user = self._find_user_locked(session.get("user_id", ""))
            if user is None or user.get("status") != USER_STATUS_ACTIVE:
                return None
            if int(session.get("credential_version", 0)) != int(user.get("credential_version", 1)):
                return None
            if user.get("role") == USER_ROLE_USER and float(user.get("key_expires_at") or 0) <= now:
                return None
            session["last_seen_at"] = now
            user["last_seen_at"] = now
            return dict(user)

    def revoke_session(self, token: str, *, actor: str | None = None) -> None:
        token = (token or "").strip()
        if not token:
            return
        with self._lock:
            rec = self._sessions.pop(self._session_hash(token), None)
            if rec is None:
                return
            self._save_locked()
            self._audit_locked("session_revoked", actor=actor or rec.get("user_id"), subject=rec.get("id"))
