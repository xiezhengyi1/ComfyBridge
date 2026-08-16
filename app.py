"""ComfyBridge —— 连接“提示词”与“云上 ComfyUI 工作流”的安全中间 API。

启动：
    cd comfybridge
    pip install -r requirements.txt
    python -m uvicorn app:app --host 127.0.0.1 --port 8000

首次启动会在控制台打印自动生成的 API Key，所有 /v1 请求需带
    Authorization: Bearer <key>  或  X-API-Key: <key>
SSE 实时流（EventSource 无法自定义请求头）用查询参数: /v1/events?key=<key>
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

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CLIENT_ID = "comfybridge"
cfg = cfg_mod.load_config()

client = None
listener = None
job_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, listener, job_manager
    key = cfg_mod.bootstrap(cfg)
    if key:
        print("\n" + "=" * 60)
        print("  ComfyBridge API Key: " + key)
        print("  所有 /v1 请求请携带 Authorization: Bearer <key>")
        print("  网页端在页面里输入同一个 Key 即可")
        print("=" * 60 + "\n")
    client = ComfyClient(cfg["comfyui_base_url"])
    job_manager = JobManager(client, BASE_DIR / "storage", cfg)
    listener = ComfyListener(cfg["comfyui_base_url"], CLIENT_ID, job_manager.on_ws_event)
    yield
    if listener is not None:
        listener.close()
    job_manager._exec.shutdown(wait=False)


app = FastAPI(title="ComfyBridge", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.get("cors_origins", []),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------- 鉴权 & 限流 ----------------
_rate = defaultdict(deque)


def _check_key(request: Request, query_key: str = "") -> None:
    if cfg.get("auth_disabled"):
        return
    keys = cfg.get("api_keys") or []
    header = request.headers.get("Authorization", "")
    key = header.removeprefix("Bearer ").strip() if header.startswith("Bearer") else ""
    if not key:
        key = request.headers.get("X-API-Key", "").strip()
    if not key:
        key = (query_key or "").strip()
    if key not in keys:
        raise HTTPException(401, "无效或缺失的 API Key")


def require_auth(request: Request):
    _check_key(request)
    if cfg.get("auth_disabled"):
        return
    keys = cfg.get("api_keys") or []
    header = request.headers.get("Authorization", "")
    key = header.removeprefix("Bearer ").strip() if header.startswith("Bearer") else ""
    if not key:
        key = request.headers.get("X-API-Key", "").strip()
    if not key:
        return  # 查询参数方式走 /v1/events 专用校验
    now = time.time()
    q = _rate[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= int(cfg.get("rate_limit_per_minute", 10)):
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    q.append(now)


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
def generate(req: GenerateRequest, _=Depends(require_auth)):
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
    })
    job_manager.submit(job["id"])
    return {"job_id": job["id"], "status": "queued", "poll": f"/v1/jobs/{job['id']}"}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, _=Depends(require_auth)):
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(404, f"任务不存在: {job_id}")
    return job


@app.get("/v1/jobs")
def list_jobs(limit: int = 100, _=Depends(require_auth)):
    return job_manager.list(min(limit, 200))


@app.get("/v1/files/{job_id}/{filename}")
def get_file(job_id: str, filename: str):
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


# ---------------- SSE 实时事件流 ----------------
@app.get("/v1/events")
async def events(request: Request, key: str = ""):
    """实时推送任务状态/进度/预览。EventSource 不支持自定义头，用 ?key= 传 API Key。"""
    _check_key(request, key)
    q = job_manager.subscribe()

    async def gen():
        try:
            snap = {"type": "snapshot", "jobs": job_manager.list(100)}
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
