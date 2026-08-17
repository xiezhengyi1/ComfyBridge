"""工作流注入引擎。

每个工作流 = 一个 API 格式 JSON（ComfyUI 菜单导出）+ 一个 manifest 清单：
  workflows/<id>.json            API 格式工作流（带 {{placeholder}} 或固定值都行）
  workflows/<id>.manifest.json   声明注入点与收集方式

manifest 示例：
{
  "id": "comfytv_txt2img",
  "name": "ComfyTV 文生图",
  "workflow_file": "comfytv_txt2img.json",
  "prompt_slots":        [{"node": "12", "field": "main_prompt"}],
  "aspect_ratio_slots":  [{"node": "12", "field": "aspect_ratio"}],
  "resolution_slots":    [{"node": "12", "field": "resolution"}],
  "size_slots":          [{"node": "4", "width_field": "width", "height_field": "height"}],
  "seed_slots":          [{"node": "5", "field": "seed"}],
  "filename_slots":      [{"node": "9", "field": "filename_prefix"}],
  "collect": {"mode": "history"}
}

prompt_slots / aspect_ratio_slots / resolution_slots 直接写字符串；
size_slots 按“分辨率档位 + 宽高比”换算像素并写入 width/height 字段。
"""
import json
import math
import random
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKFLOWS_DIR = BASE_DIR / "workflows"

ASPECT_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "3:2", "2:3"]
RESOLUTIONS = ["720p", "1080p", "2k", "4k"]

# 分辨率档位 -> 基准面积（16:9 下的像素面积），用于按宽高比换算像素尺寸
RESOLUTION_AREA = {
    "720p": 1280 * 720,     # ~0.9 MP
    "1080p": 1920 * 1080,   # ~2.1 MP
    "2k": 2560 * 1440,      # ~3.7 MP
    "4k": 3840 * 2160,      # ~8.3 MP
}
MAX_SIDE = 2048             # 像素换算上限（生图模型一般不适合超大图）


class WorkflowError(Exception):
    pass


def list_workflows() -> list:
    out = []
    for mf in sorted(WORKFLOWS_DIR.glob("*.manifest.json")):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            out.append({
                "id": m.get("id", mf.stem),
                "name": m.get("name", mf.stem),
                "media": m.get("media", "image"),
                "description": m.get("description", ""),
            })
        except json.JSONDecodeError:
            continue
    return out


def load_manifest(workflow_id: str) -> dict:
    p = WORKFLOWS_DIR / f"{workflow_id}.manifest.json"
    if not p.exists():
        raise WorkflowError(f"未知工作流: {workflow_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def aspect_to_pixels(aspect_ratio: str, resolution: str) -> tuple:
    """宽高比 + 分辨率档位 -> (宽, 高)，保持面积接近档位基准，边长为 8 的倍数。"""
    try:
        w_r, h_r = (float(x) for x in aspect_ratio.split(":"))
    except ValueError:
        raise WorkflowError(f"非法宽高比: {aspect_ratio}")
    area = RESOLUTION_AREA.get(resolution, RESOLUTION_AREA["1080p"])
    ratio = w_r / h_r
    w = math.sqrt(area * ratio)
    h = area / w
    scale = min(1.0, MAX_SIDE / max(w, h))
    w, h = w * scale, h * scale
    return max(64, int(round(w / 8) * 8)), max(64, int(round(h / 8) * 8))


def build_workflow(manifest: dict, prompt: str, aspect_ratio: str = "1:1",
                   resolution: str = "1080p", seed: int | None = None,
                   batch_size: int | None = None,
                   duration_s: int | None = None) -> tuple:
    """把参数注入工作流 JSON，返回 (workflow, 实际使用的 seed)。

    batch_size / duration_s 为 None 时不注入（保持工作流导出时的默认值）。
    """
    wf_path = WORKFLOWS_DIR / manifest["workflow_file"]
    if not wf_path.exists():
        raise WorkflowError(f"工作流文件缺失: {wf_path.name}")
    wf = json.loads(wf_path.read_text(encoding="utf-8"))
    if seed is None:
        seed = random.randint(0, 2**31 - 1)

    def set_field(node_id, field, value):
        node = wf.get(str(node_id)) or wf.get(node_id)
        if node is None:
            raise WorkflowError(f"工作流中不存在节点 {node_id}（清单配置有误）")
        node["inputs"][field] = value

    for s in manifest.get("prompt_slots", []):
        set_field(s["node"], s["field"], prompt)
    ar_map = manifest.get("aspect_ratio_map", {})
    ar_value = ar_map.get(aspect_ratio, aspect_ratio)
    for s in manifest.get("aspect_ratio_slots", []):
        set_field(s["node"], s["field"], ar_value)
    res_map = manifest.get("resolution_map", {})
    res_value = res_map.get(resolution, resolution)
    for s in manifest.get("resolution_slots", []):
        set_field(s["node"], s["field"], res_value)
    if manifest.get("size_slots"):
        w, h = aspect_to_pixels(aspect_ratio, resolution)
        for s in manifest["size_slots"]:
            set_field(s["node"], s["width_field"], w)
            set_field(s["node"], s["height_field"], h)
    for s in manifest.get("seed_slots", []):
        set_field(s["node"], s["field"], seed)
    # ComfyTV 的 Stage 使用 force_run_token 标识一次新的用户触发。若一直为固定值，
    # ComfyUI 可能复用节点缓存而不重新执行内部工作流。
    for s in manifest.get("force_run_slots", []):
        set_field(s["node"], s["field"], seed)
    if batch_size is not None and manifest.get("batch_size_slots"):
        for s in manifest["batch_size_slots"]:
            set_field(s["node"], s["field"], batch_size)
    if duration_s is not None and manifest.get("duration_slots"):
        for s in manifest["duration_slots"]:
            set_field(s["node"], s["field"], duration_s)
    for s in manifest.get("filename_slots", []):
        set_field(s["node"], s["field"], f"cb_{seed}")
    return wf, seed
