"""ComfyUI HTTP API 客户端（只与服务端通信，不接受用户传入 URL，无 SSRF 风险）。"""
import json
import threading
import time

import requests

try:
    from websocket import WebSocketTimeoutException, create_connection
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- 基础请求 ----------
    def _get(self, path: str, **kw):
        r = self.session.get(self.base + path, timeout=self.timeout, **kw)
        if r.status_code != 200:
            raise ComfyError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _post_json(self, path: str, body: dict):
        r = self.session.post(self.base + path, json=body, timeout=self.timeout)
        return r

    # ---------- ComfyUI API ----------
    def system_stats(self) -> dict:
        return self._get("/system_stats").json()

    def queue_status(self):
        """返回该实例队列的 (running, pending)；不可达时返回 None。

        用短超时，避免选 worker 时被挂死的实例拖慢整个调度。
        """
        try:
            r = self.session.get(self.base + "/queue", timeout=3)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        return (len(running), len(pending))

    def vram_info(self):
        """返回该实例可见 CUDA 卡的 (显存总量MB, 空闲MB)；不可达返回 None。

        单卡 worker 只有一个 device；多卡 worker（CUDA_VISIBLE_DEVICES=0,1）会求和。
        用于显存感知调度：把大任务路由到装得下的卡，小任务见缝插针。
        """
        try:
            r = self.session.get(self.base + "/system_stats", timeout=3)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            return None
        devices = data.get("devices") or []
        total = 0
        free = 0
        for d in devices:
            if isinstance(d, dict) and d.get("type") == "cuda":
                total += int(d.get("vram_total", 0) or 0)
                free += int(d.get("vram_free", 0) or 0)
        return (total, free) if devices else None

    def submit(self, workflow: dict) -> str:
        """提交工作流，返回 prompt_id。"""
        r = self._post_json("/prompt", {"prompt": workflow, "client_id": "comfybridge"})
        try:
            data = r.json()
        except ValueError:
            raise ComfyError(f"提交失败 HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code != 200 or "prompt_id" not in data:
            detail = json.dumps(data, ensure_ascii=False)[:1500]
            raise ComfyError(f"提交失败 HTTP {r.status_code}: {detail}")
        return data["prompt_id"]

    def history(self, prompt_id: str):
        """返回该 prompt 的 history 条目；不存在返回 None。"""
        try:
            r = self._get(f"/history/{prompt_id}")
        except ComfyError:
            return None
        data = r.json()
        return data.get(prompt_id)

    def view(self, filename: str, subfolder: str = "", file_type: str = "output") -> bytes:
        """下载生成产物（图片/视频）。"""
        return self._get(
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": file_type},
        ).content

    # ---------- 轮询 ----------
    def wait(self, prompt_id: str, poll_interval: float = 1.0, timeout: float = 900) -> dict:
        """轮询 /history 直到任务结束。返回 history 条目；超时抛 TimeoutError。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self.history(prompt_id)
            if entry is not None:
                if entry.get("status") == "error":
                    raise ComfyError(self._format_error(entry))
                if entry.get("status") == "success" or entry.get("outputs"):
                    return entry
            time.sleep(poll_interval)
        raise TimeoutError(f"任务 {prompt_id} 超时（>{timeout}s）")

    @staticmethod
    def _format_error(entry: dict) -> str:
        node_errors = entry.get("node_errors") or {}
        parts = []
        for node_id, err in node_errors.items():
            cls = err.get("class_type", "?")
            msgs = err.get("errors", [])
            for m in msgs:
                parts.append(f"节点[{node_id}] {cls}: {m.get('message', m)}")
        detail = "; ".join(parts) if parts else json.dumps(node_errors, ensure_ascii=False)[:500]
        return f"工作流执行出错: {detail}"


class ComfyListener:
    """订阅 ComfyUI WebSocket，实时接收 progress / preview 等消息（后台线程，自动重连）。

    消息形态：
    - 文本帧（JSON）：{"type": "progress", "data": {"value", "max", "prompt_id", "node"}}
    - 二进制帧：JSON 头（type=preview）+ PNG 图片字节，回调时以 msg["_image"] 携带
    """

    def __init__(self, base_url: str, client_id: str, on_event, is_busy=None):
        self.on_event = on_event
        self.is_busy = is_busy or (lambda: False)
        self._stop = threading.Event()
        self.ws_url = (
            base_url.replace("https://", "wss://").replace("http://", "ws://")
            + f"/ws?clientId={client_id}"
        )
        if not _WS_AVAILABLE:
            print("[ComfyListener] 警告: 未安装 websocket-client，实时进度/预览不可用")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="comfy-ws")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            ws = None
            try:
                ws = create_connection(self.ws_url, timeout=30, max_size=64 * 1024 * 1024)
                ws.settimeout(15)
                print(f"[ComfyListener] WebSocket 已连接: {self.ws_url[:64]}…")
                last_msg = time.time()
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except WebSocketTimeoutException:
                        # 半开连接（pod 重启后）recv() 只超时不报错，会永远挂死。
                        # 看门狗按活动状态自适应：
                        #   有任务在跑（进度本应流动）-> 30s 无消息即重连
                        #   空闲（ComfyUI 不发保活消息属正常）-> 300s 才重连
                        stale = 30 if self.is_busy() else 300
                        if time.time() - last_msg > stale:
                            print(f"[ComfyListener] {stale}s 无消息，强制重连")
                            break
                        continue
                    except Exception:
                        break
                    if raw:
                        last_msg = time.time()
                    if isinstance(raw, str):
                        try:
                            self.on_event(json.loads(raw))
                        except Exception:
                            pass
                    elif isinstance(raw, bytes) and raw:
                        try:
                            # 二进制帧 = JSON 头（可能嵌套对象）+ 图片字节，括号配平解析
                            depth = 0
                            end = 0
                            for i, c in enumerate(raw):
                                if c == 0x7B:  # {
                                    depth += 1
                                elif c == 0x7D:  # }
                                    depth -= 1
                                    if depth == 0:
                                        end = i + 1
                                        break
                            header = json.loads(raw[:end].decode("utf-8"))
                            header["_image"] = raw[end:]
                            self.on_event(header)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[ComfyListener] 连接异常: {str(e)[:100]}，3s 后重连")
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            time.sleep(3.0)  # 断线重连间隔
