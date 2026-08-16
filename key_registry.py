"""API Key 注册表 —— 一次性激活 Key 的生成、在线验证与用户绑定隔离。

体系（v0.4）：
- 管理员 Key：config.json 的 `api_keys`（或环境变量 COMFYBRIDGE_API_KEY 追加）。
  管理员可调用 POST /v1/admin/keys 批量生成“一次性激活 Key”，也可当普通用户使用
  （各自拥有独立、隔离的用户记录）。
- 激活 Key：**每个 Key 只能用一次**。第一次携带它请求任意 /v1 业务接口时，服务端
  在线校验并“激活”：创建用户记录、把 Key 标记为 used、绑定该用户（激活码模式）。
  此后同一把 Key 就是该用户的个人身份 Key，可继续用于后续请求；
  但不能再被第二个人激活。
- 用户记录隔离：任务 / 文件 / SSE 实时流全部按 user_id 隔离，用户只能看到自己的数据。

持久化（storage/ 下，均 gitignore）：
  keys.json    {key: {key, status: unused|used|revoked, bound_user, note, created_at, used_at}}
  users.json   {api_key: {user_id, api_key, role: admin|user, status: active|disabled,
                          activated_at, last_seen_at}}
"""
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

KEY_STATUS_UNUSED = "unused"
KEY_STATUS_USED = "used"
KEY_STATUS_REVOKED = "revoked"

USER_ROLE_ADMIN = "admin"
USER_ROLE_USER = "user"

USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class KeyRegistry:
    def __init__(self, storage_dir, cfg: dict):
        self.storage = Path(storage_dir)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.keys_file = self.storage / "keys.json"
        self.users_file = self.storage / "users.json"
        self._cfg = cfg
        self._lock = threading.Lock()
        self._keys = {}     # key -> record
        self._users = {}    # api_key -> user record
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        self._keys = self._read_json(self.keys_file)
        self._users = self._read_json(self.users_file)

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        for path, data in ((self.keys_file, self._keys), (self.users_file, self._users)):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    # ---------------- 管理员 Key ----------------
    def _admin_keys(self) -> list:
        return [k for k in (self._cfg.get("api_keys") or []) if k]

    def is_admin_key(self, key: str) -> bool:
        return key in self._admin_keys()

    def _admin_user_id(self, key: str) -> str:
        # 确定性 user_id：管理员 Key 重启后仍映射到同一个用户记录
        return "admin_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def admin_owner_id(self) -> str | None:
        """第一个管理员 Key 对应的用户 ID（用于把升级前遗留的无主任务划归管理员）。"""
        keys = self._admin_keys()
        return self._admin_user_id(keys[0]) if keys else None

    def _ensure_admin_user(self, key: str) -> dict:
        """惰性创建管理员用户记录（调用方须持有 self._lock）。"""
        user = self._users.get(key)
        if user is None:
            user = {
                "user_id": self._admin_user_id(key),
                "api_key": key,
                "role": USER_ROLE_ADMIN,
                "status": USER_STATUS_ACTIVE,
                "activated_at": time.time(),
                "last_seen_at": time.time(),
            }
            self._users[key] = user
            self._save()
        return user

    # ---------------- 生成 ----------------
    def generate_keys(self, count: int, note: str = "") -> list:
        """生成 count 个一次性激活 Key（status=unused），返回记录列表。"""
        count = max(1, min(int(count), 100))
        now = time.time()
        out = []
        with self._lock:
            for _ in range(count):
                key = "cb_" + secrets.token_urlsafe(20)
                while key in self._keys or self.is_admin_key(key):
                    key = "cb_" + secrets.token_urlsafe(20)
                rec = {
                    "key": key,
                    "status": KEY_STATUS_UNUSED,
                    "bound_user": None,
                    "note": note,
                    "created_at": now,
                    "used_at": None,
                }
                self._keys[key] = rec
                out.append(dict(rec))
            self._save()
        return out

    # ---------------- 在线验证（不消费，供 /v1/auth/verify 使用） ----------------
    def verify(self, key: str) -> dict:
        key = (key or "").strip()
        if not key:
            return {"valid": False, "status": "empty", "message": "未提供 API Key"}
        if self.is_admin_key(key):
            return {"valid": True, "status": "admin", "role": USER_ROLE_ADMIN,
                    "message": "管理员 Key，可直接使用"}
        with self._lock:
            rec = self._keys.get(key)
            if rec is not None and rec.get("status") == KEY_STATUS_USED:
                uid = rec.get("bound_user")
                user = next((u for u in self._users.values()
                             if u.get("user_id") == uid), None)
            else:
                user = None
        if rec is None:
            return {"valid": False, "status": "unknown", "message": "无效的 API Key"}
        st = rec.get("status")
        if st == KEY_STATUS_UNUSED:
            return {"valid": True, "status": KEY_STATUS_UNUSED,
                    "message": "有效 · 未使用（首次请求将自动激活并绑定为当前用户）"}
        if st == KEY_STATUS_REVOKED:
            return {"valid": False, "status": KEY_STATUS_REVOKED, "message": "该 Key 已被吊销"}
        if user is None or user.get("status") != USER_STATUS_ACTIVE:
            return {"valid": False, "status": KEY_STATUS_USED,
                    "message": "该用户已被停用，请联系管理员"}
        return {"valid": True, "status": KEY_STATUS_USED, "role": USER_ROLE_USER,
                "user_id": user["user_id"], "message": "有效 · 已激活"}

    # ---------------- 鉴权解析（激活 Key 首次使用即自动激活绑定） ----------------
    def resolve_user(self, key: str) -> dict | None:
        """把请求携带的 Key 解析为用户记录；无效/已吊销/被停用返回 None。"""
        key = (key or "").strip()
        if not key:
            return None
        if self.is_admin_key(key):
            with self._lock:
                user = self._ensure_admin_user(key)
            user["last_seen_at"] = time.time()
            return user

        with self._lock:
            rec = self._keys.get(key)
            if rec is None or rec.get("status") == KEY_STATUS_REVOKED:
                return None
            if rec.get("status") == KEY_STATUS_USED:
                uid = rec.get("bound_user")
                user = next((u for u in self._users.values()
                             if u.get("user_id") == uid), None)
                if user is None or user.get("status") != USER_STATUS_ACTIVE:
                    return None
                user["last_seen_at"] = time.time()
                return user
            # 未使用 -> 激活：创建用户、绑定、标记 used（每个 Key 只能激活一个用户）
            user = {
                "user_id": _new_id("u"),
                "api_key": key,
                "role": USER_ROLE_USER,
                "status": USER_STATUS_ACTIVE,
                "activated_at": time.time(),
                "last_seen_at": time.time(),
            }
            self._users[key] = user
            rec["status"] = KEY_STATUS_USED
            rec["bound_user"] = user["user_id"]
            rec["used_at"] = time.time()
            self._save()
        return user

    # ---------------- 管理（仅管理员调用） ----------------
    def list_keys(self) -> list:
        with self._lock:
            out = [dict(r) for r in self._keys.values()]
        return sorted(out, key=lambda r: r.get("created_at", 0), reverse=True)

    def list_users(self) -> list:
        with self._lock:
            out = [dict(u) for u in self._users.values()]
        return sorted(out, key=lambda u: u.get("activated_at", 0))

    def revoke_key(self, key: str) -> dict | None:
        """吊销激活 Key；若已绑定用户，一并停用该用户。"""
        with self._lock:
            rec = self._keys.get(key)
            if rec is None:
                return None
            rec["status"] = KEY_STATUS_REVOKED
            uid = rec.get("bound_user")
            if uid:
                for u in self._users.values():
                    if u.get("user_id") == uid:
                        u["status"] = USER_STATUS_DISABLED
                        break
            self._save()
            return dict(rec)

    def set_user_status(self, user_id: str, status: str) -> dict | None:
        if status not in (USER_STATUS_ACTIVE, USER_STATUS_DISABLED):
            return None
        with self._lock:
            for u in self._users.values():
                if u.get("user_id") == user_id:
                    u["status"] = status
                    self._save()
                    return dict(u)
        return None
