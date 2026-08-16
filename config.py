"""ComfyBridge 配置模块。

首次启动时若 config.json 不存在会自动创建，并生成一个随机管理员 API Key（打印在控制台）。
所有可调项都集中在 config.json 中，改完重启服务生效。

v0.4 鉴权体系：
- config.json 的 `api_keys`（含自动生成的）是**管理员 Key**：可调用
  POST /v1/admin/keys 批量生成“一次性激活 Key”分发给普通用户。
- 普通用户拿到激活 Key 后，首次请求任意 /v1 接口即在线校验并自动激活绑定；
  每个激活 Key 只能用一次（只能绑定一个用户）。

云部署时支持环境变量覆盖（避免密钥进镜像/仓库）：
  COMFYBRIDGE_COMFYUI_URL   ComfyUI 地址
  COMFYBRIDGE_API_KEY       管理员 API Key（追加进 api_keys）
  COMFYBRIDGE_CORS_ORIGINS  允许跨域来源（逗号分隔，如公网访问地址）
  COMFYBRIDGE_HOST          监听地址（云上设 0.0.0.0）
  COMFYBRIDGE_PORT          监听端口
  COMFYBRIDGE_AUTH_DISABLED 1/true 关闭鉴权（不推荐）
"""
import json
import os
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULTS = {
    # 你的云上 ComfyUI 地址（不带末尾斜杠）
    "comfyui_base_url": "https://8188-cpod-1u2zhjzg91gm.pod.compshare.cn",

    # 管理员 Key 列表（v0.4：可调 /v1/admin/keys 批量生成一次性激活 Key 分发给用户）。
    # 留空数组且 auth_disabled=false 时，启动会自动生成一个并写入本文件。
    "api_keys": [],
    "auth_disabled": False,          # 调试期可临时设为 true 关闭鉴权（不推荐）

    "cors_origins": [                # 允许跨域访问的页面来源
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],

    # 限流：每个 Key 每分钟最多请求数
    "rate_limit_per_minute": 10,

    # 并发任务数（同时跑几个生成）
    "max_concurrent_jobs": 2,

    # ComfyUI 轮询间隔（秒）与单任务超时（秒）
    "poll_interval": 1.0,
    "job_timeout": 900,

    # 提示词限制
    "max_prompt_len": 4000,

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
    if env.get("COMFYBRIDGE_API_KEY"):
        k = env["COMFYBRIDGE_API_KEY"].strip()
        if k and k not in cfg.get("api_keys", []):
            cfg.setdefault("api_keys", []).append(k)
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
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def bootstrap(cfg: dict) -> str:
    """确保存在管理员 API Key，返回用于展示的 key（可能为 None 表示未启用鉴权）。"""
    if cfg.get("auth_disabled"):
        return None
    keys = cfg.get("api_keys") or []
    if not keys:
        key = uuid.uuid4().hex
        cfg["api_keys"] = [key]
        save_config(cfg)
        return key
    return keys[0]
