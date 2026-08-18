"""ComfyBridge —— 连接“提示词”与“云上 ComfyUI 工作流”的安全中间 API。

启动：
    cd comfybridge
    pip install -r requirements.txt
    python -m uvicorn app:app --host 127.0.0.1 --port 8000

鉴权体系（v0.4，激活码模式）：
- 管理员 Key：config.json 的 api_keys（或环境变量 COMFYBRIDGE_API_KEY）。
  管理员可 POST /v1/admin/keys 批量生成一次性激活 Key 分发给用户。
- 激活 Key：每个 Key 只能用一次 —— 第一次携带它请求任意 /v1 接口时在线校验并激活，
  绑定为当前用户的个人身份 Key；此后该 Key 继续可用，但不能被第二个人激活。
  生成时可选择有效期：once=仅一次请求有效、1h/1d/1m=激活后有效 1 小时/1 天/1 个月。
- 用户数据隔离：每个用户只能看到自己的任务、文件与 SSE 实时流。
- 脚本鉴权：Authorization: Bearer <key> 或 X-API-Key: <key>。
- 浏览器鉴权：登录时将 Key 交换为短期 HttpOnly/Secure/SameSite Cookie，
  绝不在 URL、localStorage 或前端代码中保存 Key。
"""
import asyncio
import json
import queue
import time
import threading
import urllib.parse
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import config as cfg_mod
import prompt_enhance
import safety
import workflow_engine
from comfy_client import ComfyListener
from job_manager import JobManager
from key_registry import KeyRegistry
from worker_pool import ComfyPool, _probe

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CLIENT_ID = "comfybridge"
SESSION_COOKIE = "cb_session"
cfg = cfg_mod.load_config()

pool = None
listeners = []
job_manager = None
registry = None


def _build_pool(cfg: dict):
    """按优先级构造 ComfyUI worker 池：显式列表 > 自动发现 > 单例 base_url。

    显式列表若全部离线（例如部署脚本把桥指向了一个没起来的 worker 端口），
    会回退到自动发现，避免桥拿着一个死地址傻等。
    """
    urls = [str(u).strip().rstrip("/") for u in (cfg.get("comfyui_workers") or [])
            if u and str(u).strip()]
    if urls:
        alive = [u for u in urls if _probe(u)]
        if alive:
            return ComfyPool.from_urls(alive)
        print("[ComfyBridge] 显式 ComfyUI 列表全部离线，回退到自动发现: " + ", ".join(urls))

    if cfg.get("auto_discover", True):
        host = str(cfg.get("discover_host", "127.0.0.1"))
        start = int(cfg.get("discover_port_start", 8188))
        end = int(cfg.get("discover_port_end", 8200))
        exclude = {int(p) for p in (cfg.get("discover_exclude_ports") or [])}
        discovered = ComfyPool.discover(host, start, end, exclude=exclude)
        if len(discovered) > 0:
            return discovered

    base = str(cfg.get("comfyui_base_url", "")).strip().rstrip("/")
    return ComfyPool.from_urls([base] if base else [])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, listeners, job_manager, registry
    cfg_mod.bootstrap(cfg)
    registry = KeyRegistry(BASE_DIR / "storage", cfg)
    if not cfg.get("auth_disabled"):
        keyfile = BASE_DIR / "admin-key.txt"
        if keyfile.exists():
            print(f"[ComfyBridge] 鉴权已启用；管理员 Key 明文见 {keyfile}（config.json 仅存 HMAC 摘要）")
        else:
            print("[ComfyBridge] 警告：config.json 的 api_keys 只有 hmac$ 摘要但找不到 admin-key.txt；"
                  "请用 COMFYBRIDGE_API_KEY 环境变量注入明文管理员 Key，或清空 api_keys 让服务重新生成。")
    pool = _build_pool(cfg)
    print(f"[ComfyBridge] worker 池（{len(pool)} 个）: " + ", ".join(pool.urls) or "(空)")
    job_manager = JobManager(pool, BASE_DIR / "storage", cfg,
                             default_owner=registry.admin_owner_id())
    listeners = [
        ComfyListener(c.base, CLIENT_ID, job_manager.on_ws_event,
                      is_busy=job_manager.has_running)
        for c in pool.clients
    ]
    yield
    for l in listeners:
        l.close()
    job_manager._exec.shutdown(wait=False)


app = FastAPI(title="ComfyBridge", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.get("cors_origins", []),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    allow_credentials=False,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Prevent authenticated API responses from being retained by browsers/proxies."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    if request.url.path.startswith("/v1/"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response

# ---------------- 鉴权 & 限流 ----------------
_rate = defaultdict(deque)
_login_rate = defaultdict(deque)
_sse_counts = defaultdict(int)
_rate_lock = threading.Lock()
_sse_lock = threading.Lock()


def _extract_key(request: Request) -> str:
    """只接受请求头中的 Key；禁止把长期凭据放进 URL、日志或 Referrer。"""
    if "key" in request.query_params:
        raise HTTPException(400, "不支持在 URL 查询参数中传递 API Key，请使用会话 Cookie 或 Authorization 请求头")
    header = request.headers.get("Authorization", "")
    key = header.removeprefix("Bearer ").strip() if header.startswith("Bearer") else ""
    if not key:
        key = request.headers.get("X-API-Key", "").strip()
    return key


def _resolve(request: Request) -> dict | None:
    """优先使用短期 HttpOnly 会话，其次支持受限的命令行 Bearer Key。"""
    if cfg.get("auth_disabled"):
        return None
    # 即使 Cookie 已有效，也拒绝 URL Key，避免调用方误以为该方式仍受支持。
    key = _extract_key(request)
    session = request.cookies.get(SESSION_COOKIE, "")
    if session:
        user = registry.resolve_session(session)
        if user is not None:
            return user
    if not key:
        raise HTTPException(401, "缺失 API Key")
    user = registry.resolve_user(key)
    if user is None:
        raise HTTPException(401, "无效或已失效的 API Key")
    return user


def require_auth(request: Request):
    user = _resolve(request)
    if user is None:
        return None  # auth_disabled：不做用户隔离
    identity = user["user_id"]
    now = time.time()
    with _rate_lock:
        q = _rate[identity]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= int(cfg.get("rate_limit_per_minute", 10)):
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        q.append(now)
    return user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _require_login_rate(request: Request) -> None:
    """Limit unauthenticated login attempts by direct peer address."""
    now = time.time()
    ip = _client_ip(request)
    limit = int(cfg.get("login_rate_limit_per_minute", 5))
    with _rate_lock:
        q = _login_rate[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
        q.append(now)


def require_admin(request: Request):
    user = require_auth(request)
    if user is not None and user.get("role") == "admin":
        return user
    raise HTTPException(403, "需要管理员 Key")


def _owner(user: dict | None) -> str | None:
    """请求对应的用户 ID（auth_disabled 时返回 None = 不做隔离）。"""
    return user["user_id"] if user else None


# ---------------- 请求模型 ----------------
class GenerateRequest(BaseModel):
    workflow_id: str
    prompt: str = Field(min_length=1)
    aspect_ratio: str = "1:1"
    resolution: str = "1080p"
    seed: int | None = None
    batch_size: int | None = Field(default=None, ge=1, le=8,
                                   description="批量张数 1-8；留空使用工作流默认值")
    duration_s: int | None = Field(default=None, ge=1, le=60,
                                   description="文生视频时长（秒）；留空使用工作流默认值")
    images: list[str] = Field(default_factory=list,
                              description="图生视频的上游图片（/v1/upload 返回的 view_url 列表）")
    dry_run: bool = False


# ---------------- 业务接口 ----------------
def _llm_moderation_check(prompt: str) -> None:
    """若配置了 llm 且开启 moderation，则叠加 AI 语义审核（规则层的兜底）。"""
    llm_cfg = cfg.get("llm") or {}
    if not (llm_cfg.get("api_key") and llm_cfg.get("base_url")):
        return
    if not llm_cfg.get("moderation_enabled", True):
        return
    verdict = safety.llm_moderate(prompt, llm_cfg)
    if verdict is not None and not verdict["allowed"]:
        raise HTTPException(422, {
            "error": "提示词未通过 AI 内容审核",
            "reason": verdict["reason"],
            "category": "llm",
        })


@app.post("/v1/generate")
def generate(req: GenerateRequest, user=Depends(require_auth)):
    try:
        manifest = workflow_engine.load_manifest(req.workflow_id)
    except workflow_engine.WorkflowError:
        raise HTTPException(404, f"未知工作流: {req.workflow_id}")

    if req.aspect_ratio not in workflow_engine.ASPECT_RATIOS:
        raise HTTPException(422, f"不支持的宽高比: {req.aspect_ratio}，可选 {workflow_engine.ASPECT_RATIOS}")
    if req.resolution not in workflow_engine.RESOLUTIONS:
        raise HTTPException(422, f"不支持的分辨率: {req.resolution}，可选 {workflow_engine.RESOLUTIONS}")

    images = [str(i).strip() for i in (req.images or []) if i and str(i).strip()]
    if manifest.get("image_slots"):
        min_n = int(manifest.get("min_images", 1) or 1)
        max_n = int(manifest.get("max_images", 0) or 0)
        if len(images) < min_n:
            raise HTTPException(422, f"工作流「{manifest.get('name', req.workflow_id)}」需要至少 {min_n} 张图片（当前 {len(images)} 张）")
        if max_n and len(images) > max_n:
            raise HTTPException(422, f"工作流「{manifest.get('name', req.workflow_id)}」最多支持 {max_n} 张图片（当前 {len(images)} 张）")

    prompt = safety.sanitize(req.prompt, int(cfg.get("max_prompt_len", 4000)))
    verdict = safety.check_prompt(prompt, cfg)
    if not verdict["allowed"]:
        raise HTTPException(422, {
            "error": "提示词未通过安全审查",
            "reason": verdict["reason"],
            "category": verdict["category"],
            "hits": verdict["hits"],
        })
    _llm_moderation_check(prompt)

    if req.dry_run:
        if user is not None and user.get("role") != "admin":
            raise HTTPException(403, "仅管理员可查看工作流 dry-run 结果")
        wf, seed = workflow_engine.build_workflow(
            manifest, prompt, req.aspect_ratio, req.resolution, req.seed,
            req.batch_size, req.duration_s, images)
        return {"dry_run": True, "seed": seed, "workflow": wf}

    job = job_manager.create(req.workflow_id, {
        "prompt": prompt,
        "aspect_ratio": req.aspect_ratio,
        "resolution": req.resolution,
        "seed": req.seed,
        "batch_size": req.batch_size,
        "duration_s": req.duration_s,
        "images": images,
    }, owner=_owner(user))
    job_manager.submit(job["id"])
    return {"job_id": job["id"], "status": "queued", "poll": f"/v1/jobs/{job['id']}"}


_ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp"}


def _view_url(info: dict) -> str:
    """由 ComfyUI /upload/image 的返回 {name, subfolder, type} 拼出 ComfyUI 的 /view 地址（生成用）。"""
    qs = urllib.parse.urlencode({
        "filename": info.get("name", ""),
        "subfolder": info.get("subfolder", ""),
        "type": info.get("type", "input"),
    })
    return "/view?" + qs


def _preview_url(info: dict) -> str:
    """拼出经桥代理的同源 /v1/view 地址（网页缩略图用，可跨刷新复用）。"""
    qs = urllib.parse.urlencode({
        "filename": info.get("name", ""),
        "subfolder": info.get("subfolder", ""),
        "type": info.get("type", "input"),
    })
    return "/v1/view?" + qs


@app.post("/v1/upload")
async def upload_images(images: list[UploadFile] = File(...), user=Depends(require_auth)):
    """把用户上传的图片送到 ComfyUI input 目录，返回可直接写入工作流的 view_url 列表。

    图片按上传顺序编号（写入工作流后对应 image0/image1/…），供图生视频工作流
    按「图片数量」选择对应的组。
    """
    max_n = int(cfg.get("max_upload_images", 9))
    if not images:
        raise HTTPException(422, "请至少上传一张图片")
    if len(images) > max_n:
        raise HTTPException(422, f"最多上传 {max_n} 张图片")
    if pool is None or not pool.clients:
        raise HTTPException(503, "没有可用的 ComfyUI 实例（请确认 ComfyUI 已启动）")
    client = pool.any()
    if client is None:
        raise HTTPException(503, "没有可用的 ComfyUI 实例（请确认 ComfyUI 已启动）")

    out = []
    for f in images:
        raw = await f.read()
        if not raw:
            raise HTTPException(422, f"图片 {f.filename} 内容为空")
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(422, f"图片 {f.filename} 超过 20MB")
        name = Path(f.filename or "image.png").name
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _ALLOWED_IMAGE_EXTS:
            raise HTTPException(422, f"不支持的图片格式: {f.filename}（支持 png/jpg/jpeg/webp/bmp）")
        unique_name = f"cb_{uuid.uuid4().hex[:10]}_{name}"
        info = client.upload_image(raw, unique_name)
        info.setdefault("subfolder", "")
        info.setdefault("type", "input")
        out.append({
            "filename": info.get("name", unique_name),
            "subfolder": info.get("subfolder", ""),
            "type": info.get("type", "input"),
            "view_url": _view_url(info),
            "preview_url": _preview_url(info),
        })
    return {"images": out}


@app.get("/v1/view")
def proxy_comfy_view(filename: str, subfolder: str = "", type: str = "output",
                     _=Depends(require_auth)):
    """同源代理 ComfyUI 的 /view 文件（网页端缩略图预览用，避免跨域/CSP 问题）。"""
    name = Path(filename).name  # 防路径穿越
    if not name:
        raise HTTPException(404, "文件不存在")
    if pool is None or not pool.clients:
        raise HTTPException(503, "没有可用的 ComfyUI 实例")
    client = pool.any()
    try:
        data = client.view(name, subfolder, type)
    except Exception:
        raise HTTPException(404, "文件不存在")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    media = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif",
        "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    }.get(ext, "application/octet-stream")
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "no-store, private"})


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, user=Depends(require_auth)):
    job = job_manager.get(job_id, owner=_owner(user))
    if job is None:
        raise HTTPException(404, f"任务不存在: {job_id}")
    return job


@app.get("/v1/jobs")
def list_jobs(limit: int = 100, user=Depends(require_auth)):
    return job_manager.list(min(limit, 200), owner=_owner(user))


@app.get("/v1/files/{job_id}/{filename}")
def get_file(job_id: str, filename: str, user=Depends(require_auth)):
    """下载任务产物。只允许任务归属者访问，浏览器通过同源 HttpOnly Cookie 鉴权。"""
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, "文件不存在")
    if user is not None and job.get("owner") != user["user_id"]:
        raise HTTPException(404, "文件不存在")
    base = (BASE_DIR / "storage" / "jobs" / job_id).resolve()
    target = (base / Path(filename).name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@app.get("/v1/workflows")
def list_workflows(_=Depends(require_auth)):
    return workflow_engine.list_workflows()


@app.get("/v1/workflows/video-backends")
def list_video_backends(_=Depends(require_auth)):
    """列出 ComfyTV.VideoStage 的 workflow 下拉可选值（用于核对图生视频的后端标签）。"""
    if pool is None or not pool.clients:
        raise HTTPException(503, "没有可用的 ComfyUI 实例")
    client = pool.any()
    if client is None:
        raise HTTPException(503, "没有可用的 ComfyUI 实例")
    try:
        info = client.object_info("ComfyTV.VideoStage")
    except Exception as e:
        raise HTTPException(502, f"获取 VideoStage 节点信息失败: {e}")
    node = (info or {}).get("ComfyTV.VideoStage") or {}
    inputs = (node.get("input") or {}).get("required") or {}
    spec = inputs.get("workflow")
    options = []
    if isinstance(spec, (list, tuple)) and spec:
        first = spec[0]
        if isinstance(first, (list, tuple)):
            options = [str(x) for x in first]
        elif isinstance(first, dict):
            options = [str(k) for k in first.keys()]
    return {"workflow_options": options, "count": len(options), "raw": spec}


@app.get("/v1/health")
def health(_=Depends(require_auth)):
    workers = pool.status() if pool else []
    sys_info = None
    if pool:
        for c in pool.clients:
            try:
                sys_info = c.system_stats().get("system", {})
                break
            except Exception:
                continue
    if sys_info is not None:
        return {
            "ok": True,
            "version": app.version,
            "comfyui_version": sys_info.get("comfyui_version"),
            "ram_free_gb": round(sys_info.get("ram_free", 0) / 1024**3, 1),
            "workers": workers,
        }
    return {"ok": False, "version": app.version, "workers": workers}


class EnhanceRequest(BaseModel):
    prompt: str = Field(min_length=1)
    style: str = "cinematic"
    media: str = Field(default="image", description="image=文生图 / video=文生视频")


@app.post("/v1/enhance")
def enhance_prompt(req: EnhanceRequest, _=Depends(require_auth)):
    """提示词优化：规则增强内置可用；配置了 llm 则优先智能改写（输出同样过安全审查）。"""
    prompt = safety.sanitize(req.prompt, int(cfg.get("max_prompt_len", 4000)))
    verdict = safety.check_prompt(prompt, cfg)
    if not verdict["allowed"]:
        raise HTTPException(422, {
            "error": "提示词未通过安全审查",
            "reason": verdict["reason"],
            "category": verdict["category"],
            "hits": verdict["hits"],
        })
    if req.media not in ("image", "video"):
        raise HTTPException(422, f"未知媒体类型: {req.media}，可选 image/video")
    if req.style not in prompt_enhance._blocks(req.media):
        raise HTTPException(422, f"未知风格: {req.style}，可选 {[s['id'] for s in prompt_enhance.list_styles(req.media)]}")

    engine = "rules"
    out = prompt_enhance.enhance(prompt, req.style, req.media)

    llm_cfg = cfg.get("llm") or {}
    llm_ready = bool(llm_cfg.get("api_key") and llm_cfg.get("base_url"))
    if llm_ready:
        try:
            cand = prompt_enhance.llm_enhance(prompt, req.style, llm_cfg, req.media)
            if cand and safety.check_prompt(cand, cfg)["allowed"]:
                out, engine = cand, "llm"
        except Exception:
            pass  # LLM 失败静默回退到规则增强
        # 最终输出（无论规则增强还是 LLM 改写）统一过 LLM 审核
        mv = safety.llm_moderate(out, llm_cfg)
        if mv is not None and not mv["allowed"]:
            raise HTTPException(422, {
                "error": "提示词未通过 AI 内容审核",
                "reason": mv["reason"],
                "category": "llm",
            })

    return {"original": prompt, "enhanced": out, "style": req.style,
            "media": req.media,
            "style_name": prompt_enhance.get_style_name(req.style, req.media), "engine": engine}


# ---------------- Browser login: API Key -> short-lived HttpOnly session ----------------
class LoginRequest(BaseModel):
    key: str = Field(min_length=16, max_length=512)


def _session_view(user: dict) -> dict:
    return {
        "authenticated": True,
        "user_id": user["user_id"],
        "role": user.get("role"),
        "key_expires_at": user.get("key_expires_at"),
    }


def _set_session_cookie(response: Response, token: str) -> None:
    max_age = max(1, min(int(cfg.get("session_ttl_hours", 12)), 24 * 7)) * 3600
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=bool(cfg.get("session_cookie_secure", True)),
        samesite="strict",
        path="/",
    )


@app.post("/v1/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    """Exchange a Key for a short-lived, same-origin HttpOnly session cookie."""
    if cfg.get("auth_disabled"):
        raise HTTPException(404, "当前服务未启用鉴权")
    _require_login_rate(request)
    user = registry.resolve_user(req.key)
    if user is None:
        registry.audit_login_failure(client_ip=_client_ip(request))
        raise HTTPException(401, "登录凭据无效、已过期或已被吊销")
    token, _ = registry.create_session(user, client_ip=_client_ip(request))
    _set_session_cookie(response, token)
    return _session_view(user)


@app.get("/v1/auth/session")
def session_status(user=Depends(require_auth)):
    """Return the current authenticated identity without exposing any credential."""
    if user is None:
        return {"authenticated": False}
    return _session_view(user)


@app.post("/v1/auth/logout")
def logout(request: Request, response: Response, user=Depends(require_auth)):
    registry.revoke_session(request.cookies.get(SESSION_COOKIE, ""), actor=_owner(user))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------- 管理员接口：激活 Key 生成与管理 ----------------
class GenerateKeysRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100, description="一次生成的数量 1-100")
    note: str = Field(default="", description="备注（如客户名/用途），可选")
    expires_in_hours: int | None = Field(default=None, ge=1, le=2160,
                                          description="激活码有效期（小时），默认使用配置")
    validity: Literal["once", "1h", "1d", "1m"] | None = Field(
        default=None,
        description="有效期预设：once=仅一次请求有效；1h/1d/1m=激活后有效 1 小时/1 天/1 个月；默认按配置")


@app.post("/v1/admin/keys")
def admin_generate_keys(req: GenerateKeysRequest, user=Depends(require_admin)):
    """批量生成一次性激活 Key；原始 Key 只在本次响应中返回一次。"""
    return {"keys": registry.generate_keys(
        req.count, req.note.strip(), req.expires_in_hours, req.validity,
        actor=user["user_id"])}


@app.get("/v1/admin/keys")
def admin_list_keys(user=Depends(require_admin)):
    """已发放 Key 的元数据；历史明文 Key 永不回显。"""
    return {"keys": registry.list_keys()}


class RevokeKeyRequest(BaseModel):
    key_id: str = Field(min_length=8, max_length=128)


@app.post("/v1/admin/keys/revoke")
def admin_revoke_key(req: RevokeKeyRequest, user=Depends(require_admin)):
    """按非敏感的 Key 记录 ID 吊销；已绑定用户会同时停用。"""
    rec = registry.revoke_key(req.key_id, actor=user["user_id"])
    if rec is None:
        raise HTTPException(404, "Key 不存在")
    return {"revoked": rec}


@app.get("/v1/admin/users")
def admin_list_users(user=Depends(require_admin)):
    """用户记录列表（用于管理/审计）。"""
    return {"users": registry.list_users()}


class SetUserStatusRequest(BaseModel):
    status: str = Field(description="active 或 disabled")


@app.post("/v1/admin/users/{user_id}/status")
def admin_set_user_status(user_id: str, req: SetUserStatusRequest,
                          user=Depends(require_admin)):
    """启用/停用某个用户（停用后其 Key 全部失效）。"""
    if req.status not in ("active", "disabled"):
        raise HTTPException(422, "status 只能是 active / disabled")
    rec = registry.set_user_status(user_id, req.status, actor=user["user_id"])
    if rec is None:
        raise HTTPException(404, "用户不存在")
    return {"user": rec}


class RotateUserKeyRequest(BaseModel):
    expires_in_hours: int | None = Field(default=None, ge=1, le=8760)


@app.post("/v1/admin/users/{user_id}/keys/rotate")
def admin_rotate_user_key(user_id: str, req: RotateUserKeyRequest,
                          user=Depends(require_admin)):
    """轮换用户 API Key，并使其所有现有浏览器会话立即失效。"""
    rec = registry.rotate_user_key(user_id, expires_in_hours=req.expires_in_hours,
                                   actor=user["user_id"])
    if rec is None:
        raise HTTPException(404, "普通用户不存在")
    return {"key": rec}


# ---------------- SSE 实时事件流 ----------------
@app.get("/v1/events")
async def events(request: Request, user=Depends(require_auth)):
    """实时推送当前会话用户的任务；认证完全由 HttpOnly Cookie 或请求头完成。"""
    owner = _owner(user)
    identity = owner or "auth_disabled"
    now = time.time()
    with _rate_lock:
        q_rate = _rate[f"sse:{identity}"]
        while q_rate and now - q_rate[0] > 60:
            q_rate.popleft()
        if len(q_rate) >= int(cfg.get("sse_connection_rate_per_minute", 5)):
            raise HTTPException(429, "实时连接创建过于频繁，请稍后再试")
        q_rate.append(now)
    with _sse_lock:
        if _sse_counts[identity] >= int(cfg.get("sse_connection_limit_per_user", 3)):
            raise HTTPException(429, "实时连接数已达上限")
        _sse_counts[identity] += 1
    q = job_manager.subscribe(owner=owner)

    async def gen():
        try:
            snap = {"type": "snapshot", "jobs": job_manager.list(100, owner=owner)}
            yield "data: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    job = await asyncio.to_thread(q.get, timeout=10)
                except queue.Empty:
                    yield ": ping\n\n"  # 保活注释帧
                    continue
                msg = {"type": "job_update", "job": job}
                yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
        finally:
            job_manager.unsubscribe(q)
            with _sse_lock:
                _sse_counts[identity] = max(0, _sse_counts[identity] - 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, private", "X-Accel-Buffering": "no"},
    )


# ---------------- 前端页面 ----------------
_NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/static/{filename}", include_in_schema=False)
def static_file(filename: str):
    target = (STATIC_DIR / Path(filename).name).resolve()
    if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, headers=_NO_CACHE)
