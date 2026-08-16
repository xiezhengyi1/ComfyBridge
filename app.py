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
- 用户数据隔离：每个用户只能看到自己的任务、文件与 SSE 实时流。
- 鉴权方式：Authorization: Bearer <key> 或 X-API-Key: <key>；
  浏览器场景（SSE/图片/下载无法带自定义头）用查询参数: /v1/events?key=<key>、
  /v1/files/...?key=<key>。
"""
import asyncio
import json
import queue
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import config as cfg_mod
import prompt_enhance
import safety
import workflow_engine
from comfy_client import ComfyClient, ComfyListener
from job_manager import JobManager
from key_registry import KeyRegistry

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CLIENT_ID = "comfybridge"
cfg = cfg_mod.load_config()

client = None
listener = None
job_manager = None
registry = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, listener, job_manager, registry
    key = cfg_mod.bootstrap(cfg)
    registry = KeyRegistry(BASE_DIR / "storage", cfg)
    if key:
        print("\n" + "=" * 60)
        print("  ComfyBridge 管理员 API Key: " + key)
        print("  管理员可调用 POST /v1/admin/keys 批量生成一次性激活 Key")
        print("  普通用户拿到激活 Key 后，首次请求任意 /v1 接口即自动激活绑定")
        print("  所有 /v1 请求请携带 Authorization: Bearer <key>")
        print("  网页端在页面里输入同一个 Key 即可")
        print("=" * 60 + "\n")
    client = ComfyClient(cfg["comfyui_base_url"])
    job_manager = JobManager(client, BASE_DIR / "storage", cfg,
                             default_owner=registry.admin_owner_id())
    listener = ComfyListener(cfg["comfyui_base_url"], CLIENT_ID, job_manager.on_ws_event,
                             is_busy=job_manager.has_running)
    yield
    if listener is not None:
        listener.close()
    job_manager._exec.shutdown(wait=False)


app = FastAPI(title="ComfyBridge", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.get("cors_origins", []),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------- 鉴权 & 限流 ----------------
_rate = defaultdict(deque)


def _extract_key(request: Request, query_key: str = "") -> str:
    """从请求中取出 Key：Authorization: Bearer > X-API-Key > ?key= 查询参数。"""
    header = request.headers.get("Authorization", "")
    key = header.removeprefix("Bearer ").strip() if header.startswith("Bearer") else ""
    if not key:
        key = request.headers.get("X-API-Key", "").strip()
    if not key:
        key = (query_key or request.query_params.get("key", "") or "").strip()
    return key


def _resolve(request: Request, query_key: str = "") -> dict | None:
    """在线校验 Key 并解析为用户记录；激活 Key 首次使用自动激活绑定（每个 Key 只能用一次）。"""
    if cfg.get("auth_disabled"):
        return None
    key = _extract_key(request, query_key)
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
    key = _extract_key(request)
    now = time.time()
    q = _rate[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= int(cfg.get("rate_limit_per_minute", 10)):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    q.append(now)
    return user


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
        wf, seed = workflow_engine.build_workflow(
            manifest, prompt, req.aspect_ratio, req.resolution, req.seed,
            req.batch_size, req.duration_s)
        return {"dry_run": True, "seed": seed, "workflow": wf}

    job = job_manager.create(req.workflow_id, {
        "prompt": prompt,
        "aspect_ratio": req.aspect_ratio,
        "resolution": req.resolution,
        "seed": req.seed,
        "batch_size": req.batch_size,
        "duration_s": req.duration_s,
    }, owner=_owner(user))
    job_manager.submit(job["id"])
    return {"job_id": job["id"], "status": "queued", "poll": f"/v1/jobs/{job['id']}"}


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
    """下载任务产物。已加鉴权与归属校验：只允许任务归属者访问（浏览器用 ?key= 传参）。"""
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


@app.get("/v1/health")
def health(_=Depends(require_auth)):
    try:
        s = client.system_stats()
        sys = s.get("system", {})
        return {
            "ok": True,
            "version": app.version,
            "comfyui_version": sys.get("comfyui_version"),
            "ram_free_gb": round(sys.get("ram_free", 0) / 1024**3, 1),
            "connected_to": cfg["comfyui_base_url"],
        }
    except Exception as e:
        return {"ok": False, "version": app.version, "error": str(e)}


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


# ---------------- 鉴权辅助：Key 在线验证 ----------------
class VerifyRequest(BaseModel):
    key: str = Field(min_length=1)


@app.post("/v1/auth/verify")
def verify_key(req: VerifyRequest):
    """在线验证一个 Key 的状态（不消费）：unknown/未使用/已激活/已吊销/管理员。"""
    return registry.verify(req.key)


# ---------------- 管理员接口：激活 Key 生成与管理 ----------------
class GenerateKeysRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100, description="一次生成的数量 1-100")
    note: str = Field(default="", description="备注（如客户名/用途），可选")


@app.post("/v1/admin/keys")
def admin_generate_keys(req: GenerateKeysRequest, user=Depends(require_admin)):
    """批量生成一次性激活 Key（每个 Key 首次请求自动激活并绑定一个用户）。"""
    return {"keys": registry.generate_keys(req.count, req.note.strip())}


@app.get("/v1/admin/keys")
def admin_list_keys(user=Depends(require_admin)):
    """已发放激活 Key 列表（含状态/绑定用户/备注）。"""
    return {"keys": registry.list_keys()}


@app.post("/v1/admin/keys/{key}/revoke")
def admin_revoke_key(key: str, user=Depends(require_admin)):
    """吊销激活 Key；若已绑定用户则一并停用该用户。"""
    rec = registry.revoke_key(key)
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
    rec = registry.set_user_status(user_id, req.status)
    if rec is None:
        raise HTTPException(404, "用户不存在")
    return {"user": rec}


# ---------------- SSE 实时事件流 ----------------
@app.get("/v1/events")
async def events(request: Request, key: str = ""):
    """实时推送任务状态/进度/预览。EventSource 不支持自定义头，用 ?key= 传 API Key。
    只推送当前用户自己的任务（用户数据隔离）。"""
    user = _resolve(request, key)
    q = job_manager.subscribe(owner=_owner(user))

    async def gen():
        try:
            snap = {"type": "snapshot", "jobs": job_manager.list(100, owner=_owner(user))}
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

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
