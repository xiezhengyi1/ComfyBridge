"""ComfyBridge 配置模块。

首次启动时若 config.json 不存在会自动创建，并生成一个随机管理员 API Key。
所有可调项都集中在 config.json 中，改完重启服务生效。

v0.4 鉴权体系（凭据保护）：
- 管理员 Key 的**明文**只保存在本地 `admin-key.txt`（权限 600，已 gitignore）；
  config.json 的 `api_keys` 只存其 HMAC 摘要（`hmac$` 前缀条目），磁盘上不落明文。
- 管理员可用 POST /v1/admin/keys 批量生成“一次性激活 Key”分发给普通用户；
  每个激活 Key 只能用一次（只能绑定一个用户）。

云部署时支持环境变量覆盖（避免密钥进镜像/仓库）：
  COMFYBRIDGE_COMFYUI_URL   ComfyUI 地址
  COMFYBRIDGE_COMFYUI_WORKERS  多个 ComfyUI 实例（逗号分隔，用于多卡并行）
  COMFYBRIDGE_API_KEY       管理员 API Key（明文注入，启动时自动归档进 admin-key.txt 并转存摘要）
  COMFYBRIDGE_KEY_HASH_SECRET  Key/Session HMAC 服务端密钥
  COMFYBRIDGE_CORS_ORIGINS  允许跨域来源（逗号分隔，如公网访问地址）
  COMFYBRIDGE_HOST          监听地址（云上设 0.0.0.0）
  COMFYBRIDGE_PORT          监听端口
  COMFYBRIDGE_AUTH_DISABLED 1/true 关闭鉴权（不推荐）
  COMFYBRIDGE_COOKIE_SECURE 0/false 仅本地 HTTP 调试时关闭 Secure Cookie
"""
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
ADMIN_KEY_FILE = BASE_DIR / "admin-key.txt"

HASH_PREFIX = "hmac$"

DEFAULTS = {
    # 你的云上 ComfyUI 地址（不带末尾斜杠）
    "comfyui_base_url": "https://8188-cpod-1u2zhjzg91gm.pod.compshare.cn",

    # 多 ComfyUI 实例（多卡并行）。显式列表优先级最高；留空则按 auto_discover
    # 自动扫描本机端口。例如：["http://127.0.0.1:8189", "http://127.0.0.1:8190"]
    "comfyui_workers": [],
    "auto_discover": True,           # 自动扫描本机端口发现 ComfyUI 实例
    "discover_host": "127.0.0.1",
    "discover_port_start": 8188,
    "discover_port_end": 8200,
    "discover_exclude_ports": [],    # 排除的端口（例如 CPU 控制器 8188）

    # 显存感知调度：按工作流声明显存需求（MB）与 worker 亲和。
    "workflow_vram_mb": {},          # 例 {"h3_video": 24000}，把大任务路由到装得下的卡
    "default_vram_mb": 8192,         # 未声明工作流的默认需求；0 = 不做显存过滤
    "workflow_worker": {},           # 工作流硬亲和：例 {"h3_video": "http://127.0.0.1:8189"}

    # 管理员 Key 列表（v0.4：可调 /v1/admin/keys 批量生成一次性激活 Key 分发给用户）。
    # 留空数组且 auth_disabled=false 时，启动会自动生成一个并写入本文件。
    "api_keys": [],
    "auth_disabled": False,          # 调试期可临时设为 true 关闭鉴权（不推荐）

    # 凭据保护：只保存 API Key / Session 的 HMAC 摘要。生产环境应通过
    # COMFYBRIDGE_KEY_HASH_SECRET 注入一个高熵值；留空时首次启动会生成并写入
    # 私有 config.json（绝不打印到日志）。更换该值会使所有已发 Key 与会话失效。
    "key_hash_secret": "",
    "activation_key_ttl_hours": 168,  # 未使用激活码有效期（7 天）
    "user_key_ttl_days": 30,          # 激活后的 API Key 有效期
    "session_ttl_hours": 12,          # 浏览器 HttpOnly 会话有效期
    "session_cookie_secure": True,    # 公网 HTTPS 必须为 true；仅本地 HTTP 调试时可设 false

    "cors_origins": [                # 允许跨域访问的页面来源
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],

    # 限流：每个 Key 每分钟最多请求数（网页端健康检查/工作流刷新/SSE 兜底轮询 + 上传/生成会叠加，
    # 过小会把正常操作误判为“请求过于频繁”，故默认放宽到 60）
    "rate_limit_per_minute": 60,
    "login_rate_limit_per_minute": 5,       # 未认证登录尝试：按源 IP 限流
    # 浏览器刷新/断线重试时，旧连接需要极短时间才能在服务端释放；为正常的
    # EventSource 自动重连留出余量，同时仍限制单用户持续创建连接的行为。
    "sse_connection_limit_per_user": 4,     # 每用户最多同时保持的 SSE 连接数
    "sse_connection_rate_per_minute": 15,   # 每用户每分钟新建 SSE 数

    # 并发任务数（同时跑几个生成）
    "max_concurrent_jobs": 2,

    # ComfyUI 轮询间隔（秒）与单任务超时（秒）
    "poll_interval": 1.0,
    "job_timeout": 900,

    # 提示词限制
    "max_prompt_len": 4000,

    # 图生视频等：单次最多上传的图片数
    "max_upload_images": 9,

    # 可选：提示词智能优化用的 LLM（OpenAI 兼容接口，如 DeepSeek/通义/OpenAI）
    # 留空 api_key 时 /v1/enhance 仅使用内置规则增强
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "deepseek-chat",
        "moderation_enabled": True,   # 配置了 api_key 后，生成/优化请求叠加 AI 内容审核
    },

    # 安全过滤阈值
    "hard_reject_threshold": 1,      # 高危词命中 >= 该值直接拒绝
    "soft_reject_threshold": 3,      # 普通敏感词命中 >= 该值拒绝

    # 服务监听
    "host": "127.0.0.1",
    "port": 8000,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(user_cfg)
        except json.JSONDecodeError:
            print(f"[config] 警告: {CONFIG_FILE} 不是合法 JSON，已使用默认配置")

    # 环境变量覆盖（云部署：密钥走环境变量，config.json 可不入镜像）
    env = os.environ
    if env.get("COMFYBRIDGE_COMFYUI_URL"):
        cfg["comfyui_base_url"] = env["COMFYBRIDGE_COMFYUI_URL"].strip().rstrip("/")
    if env.get("COMFYBRIDGE_COMFYUI_WORKERS"):
        cfg["comfyui_workers"] = [
            u.strip().rstrip("/") for u in env["COMFYBRIDGE_COMFYUI_WORKERS"].split(",")
            if u.strip()
        ]
    if env.get("COMFYBRIDGE_API_KEY"):
        k = env["COMFYBRIDGE_API_KEY"].strip()
        if k and k not in cfg.get("api_keys", []):
            cfg.setdefault("api_keys", []).append(k)
    if env.get("COMFYBRIDGE_KEY_HASH_SECRET"):
        cfg["key_hash_secret"] = env["COMFYBRIDGE_KEY_HASH_SECRET"].strip()
    if env.get("COMFYBRIDGE_CORS_ORIGINS"):
        cfg["cors_origins"] = [o.strip() for o in env["COMFYBRIDGE_CORS_ORIGINS"].split(",") if o.strip()]
    if env.get("COMFYBRIDGE_HOST"):
        cfg["host"] = env["COMFYBRIDGE_HOST"].strip()
    if env.get("COMFYBRIDGE_PORT"):
        try:
            cfg["port"] = int(env["COMFYBRIDGE_PORT"].strip())
        except ValueError:
            pass
    if env.get("COMFYBRIDGE_AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        cfg["auth_disabled"] = True
    if env.get("COMFYBRIDGE_COOKIE_SECURE", "").strip().lower() in ("0", "false", "no"):
        cfg["session_cookie_secure"] = False
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _hmac_hex(secret: str, purpose: str, value: str) -> str:
    """与 key_registry._digest 完全一致的 HMAC-SHA256 摘要。"""
    return hmac.new(secret.encode("utf-8"), f"{purpose}:{value}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _read_admin_keys() -> list:
    """读取 admin-key.txt 中的明文管理员 Key（跳过注释与空行）。"""
    try:
        lines = ADMIN_KEY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and s not in out:
            out.append(s)
    return out


def _write_admin_keys(keys: list) -> None:
    """把明文管理员 Key 归档到 admin-key.txt（去重合并，权限 600）。"""
    merged = []
    for k in list(_read_admin_keys()) + list(keys):
        k = (k or "").strip()
        if k and k not in merged:
            merged.append(k)
    content = (
        "# ComfyBridge 管理员 API Key（明文，请勿外传 / 不要提交到仓库）\n"
        "# 对应 config.json 中 api_keys 的 hmac$ 摘要条目。\n"
        "# 每行一个 Key；用它与网页登录门 / Authorization: Bearer <key> 鉴权。\n\n"
        + "\n".join(merged) + "\n"
    )
    ADMIN_KEY_FILE.write_text(content, encoding="utf-8")
    try:
        os.chmod(ADMIN_KEY_FILE, 0o600)
    except OSError:
        pass


def bootstrap(cfg: dict) -> bool:
    """确保 key_hash_secret 与管理员 Key 存在；管理员 Key 明文归档进 admin-key.txt，
    config.json 的 api_keys 只保留 HMAC 摘要（hmac$ 前缀）。不把密钥写入控制台或日志。"""
    if cfg.get("auth_disabled"):
        return False
    changed = False
    if not cfg.get("key_hash_secret"):
        cfg["key_hash_secret"] = uuid.uuid4().hex + uuid.uuid4().hex
        changed = True
    secret = str(cfg["key_hash_secret"])
    entries = [str(k).strip() for k in (cfg.get("api_keys") or []) if str(k).strip()]
    plain = [e for e in entries if not e.startswith(HASH_PREFIX)]
    hashed = [e for e in entries if e.startswith(HASH_PREFIX)]
    if not entries:
        # 首次启动：生成随机管理员 Key，明文进 admin-key.txt，config 只存摘要
        plain = [uuid.uuid4().hex + uuid.uuid4().hex]
    if plain:
        # 迁移旧明文（config.json 或环境变量注入）：归档明文 + 转为 hmac$ 摘要
        _write_admin_keys(plain)
        for k in plain:
            digest = _hmac_hex(secret, "api-key", k)
            if f"{HASH_PREFIX}{digest}" not in hashed:
                hashed.append(f"{HASH_PREFIX}{digest}")
        changed = True
    cfg["api_keys"] = hashed
    if changed:
        save_config(cfg)
    return changed
