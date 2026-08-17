"""任务队列 + 输出自动收集 + 事件总线（SSE 推送）+ 任务持久化。

流程：建任务 -> 后台线程构建工作流并提交 ComfyUI -> 轮询 history -> 完成后
自动把所有输出文件（图片/视频，兼容 ComfyTV 资产节点返回的 filename 列表）
下载到 storage/jobs/<job_id>/ 并登记到任务记录。

每次任务状态变化都会：1) 持久化到 job.json；2) 推送给所有 SSE 订阅者。
服务重启后从 storage/jobs/*/job.json 恢复历史任务。
"""
import json
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl

import comfy_client
import workflow_engine

# ComfyTV 等自定义节点会在输出里放 /view?filename=...&subfolder=...&type=... 形式的 URL
# （含 JSON 字符串内）。只排除空白与引号，确保完整捕获 filename/subfolder/type 三个参数。
VIEW_URL_RE = re.compile(r"/view\?([^\s\"']+)")


class JobManager:
    def __init__(self, pool, storage_dir, cfg: dict,
                 default_owner: str | None = None):
        self.pool = pool
        self.storage = Path(storage_dir)
        (self.storage / "jobs").mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self._default_owner = default_owner  # 升级前遗留的无主任务划归管理员
        self._jobs = {}
        self._lock = threading.Lock()
        self._subs = set()                   # (queue, owner)：SSE 订阅按用户隔离
        self._sub_lock = threading.Lock()
        self._prompt_map = {}       # ComfyUI prompt_id -> job_id
        self._ws_throttle = {}      # job_id -> 上次推送时间（实时事件节流）
        self._exec = ThreadPoolExecutor(
            max_workers=max(1, int(cfg.get("max_concurrent_jobs", 2))),
            thread_name_prefix="comfybridge",
        )
        self._load_persisted()
        self._recover_stale_jobs()

    # ---------------- 启动恢复：重启后遗留的 running/queued 任务 ----------------
    def _recover_stale_jobs(self) -> None:
        """服务重启会丢失在途任务的执行线程。这里把遗留任务逐一恢复：
        能查到结果 -> 补收文件并标 completed；出错 -> failed；否则 -> interrupted。"""
        with self._lock:
            stale = [dict(j) for j in self._jobs.values()
                     if j.get("status") in ("running", "queued")]
        for job in stale:
            pid = job.get("comfy_prompt_id")
            if pid:
                try:
                    client, entry = self._find_history(pid)
                    if entry is not None:
                        if entry.get("status") == "error":
                            job["status"] = "failed"
                            job["error"] = comfy_client.ComfyClient._format_error(entry)
                        elif entry.get("outputs") or entry.get("status") == "success":
                            job["files"] = self._collect(entry, job["id"], client)
                            job["status"] = "completed"
                        else:
                            job["status"] = "interrupted"
                            job["error"] = "任务在服务重启时中断"
                    else:
                        job["status"] = "interrupted"
                        job["error"] = "任务在服务重启时中断（无执行结果）"
                except Exception:
                    job["status"] = "interrupted"
                    job["error"] = "任务在服务重启时中断"
            else:
                job["status"] = "interrupted"
                job["error"] = "任务在服务重启时中断（未提交）"
            job["finished_at"] = time.time()
            job.pop("progress", None)
            job.pop("preview_url", None)
            with self._lock:
                self._jobs[job["id"]] = job
            self._save(job)
            self._publish(job)

    def _find_history(self, pid: str):
        """在全部 worker 中查找 prompt 的 history；返回 (client, entry) 或 (None, None)。"""
        for c in self.pool.clients:
            entry = c.history(pid)
            if entry is not None:
                return c, entry
        return None, None

    # ---------------- 持久化 ----------------
    def _job_dir(self, job_id: str) -> Path:
        return self.storage / "jobs" / job_id

    def _save(self, job: dict) -> None:
        try:
            d = self._job_dir(job["id"])
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "job.json.tmp"
            tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            tmp.replace(d / "job.json")
        except OSError:
            pass

    def _load_persisted(self) -> None:
        jobs_dir = self.storage / "jobs"
        if not jobs_dir.is_dir():
            return
        for jd in jobs_dir.iterdir():
            if not jd.is_dir():
                continue
            f = jd / "job.json"
            if not f.exists():
                continue
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(job, dict) and job.get("id"):
                    if job.get("owner") is None:
                        job["owner"] = self._default_owner
                    self._jobs[job["id"]] = job
            except (json.JSONDecodeError, OSError):
                continue

    # ---------------- 事件总线（SSE） ----------------
    def subscribe(self, owner: str | None = None) -> queue.Queue:
        """订阅实时事件；owner 为 None 时接收全部事件（auth_disabled 调试模式）。"""
        q = queue.Queue(maxsize=200)
        with self._sub_lock:
            self._subs.add((q, owner))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            self._subs = {(qq, o) for qq, o in self._subs if qq is not q}

    def _publish(self, job: dict) -> None:
        snap = dict(job)  # 浅拷贝，避免推送时被后续修改
        with self._sub_lock:
            for q, owner in list(self._subs):
                if owner is not None and job.get("owner") != owner:
                    continue  # 用户数据隔离：只推送给任务归属者
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    pass

    def _update(self, job: dict) -> None:
        self._save(job)
        self._publish(job)

    # ---------------- 对外 API ----------------
    def create(self, workflow_id: str, params: dict, owner: str | None = None) -> dict:
        job = {
            "id": uuid.uuid4().hex[:12],
            "workflow_id": workflow_id,
            "params": params,
            "owner": owner,                # 用户隔离：任务归属的 user_id
            "status": "queued",            # queued/running/completed/failed/timed_out
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "comfy_prompt_id": None,
            "files": [],
            "error": None,
        }
        with self._lock:
            self._jobs[job["id"]] = job
        self._update(job)
        return job

    def submit(self, job_id: str) -> None:
        self._exec.submit(self._run, job_id)

    def get(self, job_id: str, owner: str | None = None):
        """按 job_id 查询；owner 非空时校验归属（隔离），非本人任务返回 None。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if owner is not None and job.get("owner") != owner:
            return None
        return job

    def list(self, limit: int = 100, owner: str | None = None):
        """最近任务列表；owner 非空时只返回该用户的（隔离）。"""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
        if owner is not None:
            jobs = [j for j in jobs if j.get("owner") == owner]
        return jobs[:limit]

    def has_running(self) -> bool:
        """是否有正在执行的任务（供 WS 看门狗判断活动状态）。"""
        with self._lock:
            return any(j.get("status") == "running" for j in self._jobs.values())

    # ---------------- 内部 ----------------
    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        client = None
        try:
            job["status"] = "running"
            job["started_at"] = time.time()
            self._update(job)

            wid = job["workflow_id"]
            pin = (self.cfg.get("workflow_worker") or {}).get(wid)
            vram_mb = int((self.cfg.get("workflow_vram_mb") or {}).get(
                wid, self.cfg.get("default_vram_mb", 0)) or 0)
            client = self.pool.pick(vram_needed=vram_mb, pin_url=pin)
            if client is None:
                raise comfy_client.ComfyError("没有可用的 ComfyUI 实例（全部离线）")
            job["comfy_worker"] = client.base

            manifest = workflow_engine.load_manifest(job["workflow_id"])
            p = job["params"]
            wf, seed = workflow_engine.build_workflow(
                manifest, p["prompt"],
                p.get("aspect_ratio", "1:1"),
                p.get("resolution", "1080p"),
                p.get("seed"),
                p.get("batch_size"),
                p.get("duration_s"),
            )
            p["seed"] = seed

            prompt_id = client.submit(wf)
            job["comfy_prompt_id"] = prompt_id
            with self._lock:
                self._prompt_map[prompt_id] = job_id
            self._update(job)

            entry = client.wait(
                prompt_id,
                poll_interval=float(self.cfg.get("poll_interval", 1.0)),
                timeout=float(self.cfg.get("job_timeout", 900)),
            )
            job["files"] = self._collect(entry, job_id, client)
            job["status"] = "completed"
        except TimeoutError as e:
            job["status"] = "timed_out"
            job["error"] = str(e)
        except comfy_client.ComfyError as e:
            job["status"] = "failed"
            job["error"] = str(e)
        except Exception as e:  # 兜底
            job["status"] = "failed"
            job["error"] = f"{type(e).__name__}: {e}"
        finally:
            if client is not None:
                self.pool.release(client)
            job["finished_at"] = time.time()
            job.pop("progress", None)     # 实时字段不入最终快照
            job.pop("preview_url", None)
            self._update(job)

    # ---------------- 实时事件（ComfyUI WebSocket） ----------------
    def on_ws_event(self, msg: dict) -> None:
        """ComfyListener 回调：progress -> 进度条；preview -> 实时预览图。"""
        mtype = msg.get("type")
        if mtype == "progress":
            data = msg.get("data") or {}
            job = self._resolve_ws_job(data.get("prompt_id"))
            if job is None:
                return
            value = data.get("value", 0) or 0
            mx = data.get("max", 1) or 1
            pct = max(0.0, min(100.0, value / mx * 100))
            # ComfyTV 会同时发内外两层进度流（外层 0~5/10，内层采样 1~8/8），
            # 取单调最大百分比：随执行推进必然到达 100%。
            cur = job.get("progress") or {}
            if pct < float(cur.get("percent", -1.0)):
                return
            job["progress"] = {
                "value": value,
                "max": mx,
                "percent": int(round(pct)),
                "elapsed": round(time.time() - (job.get("started_at") or time.time()), 1),
            }
            self._push_throttled(job, 0.8)
        elif mtype == "preview":
            img = msg.get("_image")
            data = msg.get("data") or {}
            job = self._resolve_ws_job(data.get("prompt_id"))
            if job is None or not img:
                return
            try:
                (self._job_dir(job["id"]) / "preview.png").write_bytes(img)
            except OSError:
                return
            job["preview_url"] = f"/v1/files/{job['id']}/preview.png"
            self._push_throttled(job, 0.35)

    def _resolve_ws_job(self, prompt_id):
        """把 WS 消息归属到任务：优先 prompt_id 精确匹配，其次唯一/最新 running 任务。"""
        if prompt_id:
            with self._lock:
                jid = self._prompt_map.get(prompt_id)
            if jid:
                j = self.get(jid)
                if j and j.get("status") == "running":
                    return j
        with self._lock:
            running = [j for j in self._jobs.values() if j.get("status") == "running"]
        if len(running) == 1:
            return running[0]
        if running:
            return max(running, key=lambda j: j.get("started_at") or 0)
        return None

    def _push_throttled(self, job: dict, min_interval: float) -> None:
        now = time.time()
        last = self._ws_throttle.get(job["id"], 0.0)
        if now - last < min_interval:
            return
        self._ws_throttle[job["id"]] = now
        self._publish(job)  # 实时推送但不落盘（最终状态在任务结束时持久化）

    def _collect(self, entry: dict, job_id: str, client=None) -> list:
        """从 history 条目的 outputs 中收集所有产物文件并下载到本地。

        兼容两种形态：
        1. 标准输出：outputs[节点][字段] = [{"filename": ..., "subfolder": ..., "type": ...}]
        2. ComfyTV 输出：字段值为 /view?filename=... 的 URL（可能包在 JSON 字符串里），
           其中 picked 字段表示选中项。
        """
        if client is None:
            client = self.pool.any()
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        outputs = entry.get("outputs", {}) or {}

        candidates = {}   # (filename, subfolder, type) -> {"node","field"}
        picked = set()    # 被 ComfyTV picker 选中的文件名

        for node_id, out in outputs.items():
            for field, val in out.items():
                # 形态 1：标准 filename 列表
                if (isinstance(val, list) and val
                        and all(isinstance(i, dict) and "filename" in i for i in val)):
                    for item in val:
                        fn = Path(str(item["filename"])).name
                        if fn:
                            candidates[(fn, item.get("subfolder", ""), item.get("type", "output"))] = {"node": node_id, "field": field}
                # 形态 2：递归查找 /view?filename=... URL（字符串/JSON字符串/嵌套结构）
                for params in self._iter_view_urls(val):
                    fn = Path(str(params.get("filename", ""))).name
                    if fn:
                        candidates[(fn, params.get("subfolder", ""), params.get("type", "output"))] = {"node": node_id, "field": field}
                        if field == "picked":
                            picked.add(fn)

        files = []
        for (fn, subfolder, ftype), meta in candidates.items():
            if client is None:
                continue  # 没有 worker，无法下载
            try:
                data = client.view(fn, subfolder, ftype)
            except comfy_client.ComfyError:
                continue  # 文件可能已被清理，跳过
            (job_dir / fn).write_bytes(data)
            files.append({
                "node": meta["node"],
                "field": meta["field"],
                "filename": fn,
                "size": len(data),
                "picked": fn in picked,
                "url": f"/v1/files/{job_id}/{fn}",
            })
        return files

    @staticmethod
    def _iter_view_urls(value):
        """递归扫描输出结构，yield 每个 /view URL 的 query 参数 dict。"""
        if isinstance(value, str):
            for m in VIEW_URL_RE.finditer(value):
                yield dict(parse_qsl(m.group(1)))
        elif isinstance(value, dict):
            for v in value.values():
                yield from JobManager._iter_view_urls(v)
        elif isinstance(value, list):
            for v in value:
                yield from JobManager._iter_view_urls(v)
