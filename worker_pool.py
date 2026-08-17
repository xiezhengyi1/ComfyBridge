"""多 ComfyUI 实例池：自动发现 + 最空闲调度。

comfybridge 本身不是 ComfyUI，只是一个中间 API，因此它可以放心地把任务派给
本机（或远程）任意一台 ComfyUI，不存在“把任务派回自己导致队列死锁”的问题。

负载口径 = 该实例队列的 running + pending（/queue）+ 本进程正在派发给它的
在途任务数（避免两个线程同时把任务塞给同一个空闲 worker）。
"""
import threading

import requests

import comfy_client


def _probe(base_url: str, timeout: float = 3.0) -> bool:
    """判断一个地址是不是在线的 ComfyUI（/system_stats 返回 200）。"""
    try:
        r = requests.get(base_url.rstrip("/") + "/system_stats", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


class ComfyPool:
    def __init__(self, clients: list[comfy_client.ComfyClient]):
        self.clients = clients
        self._inflight: dict[comfy_client.ComfyClient, int] = {}
        self._lock = threading.Lock()

    # ---------- 构造 ----------
    @classmethod
    def from_urls(cls, urls: list[str]) -> "ComfyPool":
        seen: list[str] = []
        for u in urls:
            u = (u or "").strip().rstrip("/")
            if u and u not in seen:
                seen.append(u)
        return cls([comfy_client.ComfyClient(u) for u in seen])

    @classmethod
    def discover(cls, host: str, start: int, end: int,
                 exclude: set[int] | None = None) -> "ComfyPool":
        """扫描 host 的 [start, end] 端口，把在线的 ComfyUI 全部纳入池。"""
        exclude = exclude or set()
        urls = []
        for port in range(int(start), int(end) + 1):
            if port in exclude:
                continue
            url = f"http://{host}:{port}"
            if _probe(url):
                urls.append(url)
        return cls.from_urls(urls)

    # ---------- 查询 ----------
    @property
    def urls(self) -> list[str]:
        return [c.base for c in self.clients]

    def __len__(self) -> int:
        return len(self.clients)

    def any(self) -> comfy_client.ComfyClient | None:
        """返回任意一个 worker（历史恢复等无法归属的场景）；无 worker 返回 None。"""
        return self.clients[0] if self.clients else None

    def clients_by_url(self, url: str) -> comfy_client.ComfyClient | None:
        url = (url or "").rstrip("/")
        for c in self.clients:
            if c.base == url:
                return c
        return None

    # ---------- 调度 ----------
    def pick(self, vram_needed: int = 0,
             pin_url: str | None = None) -> comfy_client.ComfyClient | None:
        """选 worker 并登记在途 +1；全部离线返回 None。

        vram_needed>0 时优先选空闲显存 >= 该值的实例（大任务别挤爆小卡的卡）；
        都装不下则退回全部在线实例尽力而为（可能 OOM/swap）。
        pin_url 非空时硬路由到该实例（工作流亲和，如 H3 常驻的卡）。
        """
        if not self.clients:
            return None
        if pin_url:
            pinned = self.clients_by_url(pin_url)
            if pinned is not None:
                with self._lock:
                    self._inflight[pinned] = self._inflight.get(pinned, 0) + 1
                return pinned
            # pin 的实例不在池里，退回正常选择

        # 先无锁快照各实例队列深度 + 显存（可能略陈旧，可接受）
        loads = {c: c.queue_status() for c in self.clients}
        vrams = {c: c.vram_info() for c in self.clients}
        with self._lock:
            online = [c for c in self.clients if loads.get(c) is not None]
            candidates = online
            if vram_needed and vram_needed > 0:
                fits = [c for c in online
                        if vrams.get(c) is None or vrams[c][1] >= vram_needed]
                if fits:
                    candidates = fits
            if not candidates:
                return None

            def key(c):
                q = loads[c]
                free = vrams[c][1] if vrams.get(c) else -1
                return (q[0] + q[1] + self._inflight.get(c, 0), -free)

            best = min(candidates, key=key)
            self._inflight[best] = self._inflight.get(best, 0) + 1
            return best

    def release(self, client: comfy_client.ComfyClient | None) -> None:
        if client is None:
            return
        with self._lock:
            n = self._inflight.get(client, 0) - 1
            if n <= 0:
                self._inflight.pop(client, None)
            else:
                self._inflight[client] = n

    # ---------- 状态 ----------
    def status(self) -> list[dict]:
        out = []
        for c in self.clients:
            q = c.queue_status()
            v = c.vram_info()
            out.append({
                "url": c.base,
                "online": q is not None,
                "running": q[0] if q else 0,
                "pending": q[1] if q else 0,
                "inflight": self._inflight.get(c, 0),
                "vram_total_mb": v[0] if v else None,
                "vram_free_mb": v[1] if v else None,
            })
        return out
