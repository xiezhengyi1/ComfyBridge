# 如何接入你自己的工作流（ComfyTV 等）

## 第 1 步：导出 API 格式工作流

在你的 ComfyUI 网页里：

1. 打开/调出你要用的工作流（例如 ComfyTV 文生图）
2. 右上角菜单 → **Save (API Format)**
   - 找不到就点普通的 **Save**，把保存下来的 JSON 发给我，我帮你转成 API 格式
3. 把导出的 JSON 保存为 `comfybridge/workflows/<名字>.json`

## 第 2 步：写 manifest 清单

在同目录创建 `<名字>.manifest.json`，声明注入点和收集方式：

```json
{
  "id": "comfytv_txt2img",
  "name": "ComfyTV 文生图",
  "description": "可选说明",
  "workflow_file": "comfytv_txt2img.json",

  "prompt_slots":        [{"node": "12", "field": "main_prompt"}],
  "aspect_ratio_slots":  [{"node": "12", "field": "aspect_ratio"}],
  "resolution_slots":    [{"node": "12", "field": "resolution"}],
  "size_slots":          [{"node": "4", "width_field": "width", "height_field": "height"}],
  "seed_slots":          [{"node": "5", "field": "seed"}],
  "batch_size_slots":    [{"node": "12", "field": "batch_size"}],
  "filename_slots":      [{"node": "9", "field": "filename_prefix"}],
  "collect": {"mode": "history"}
}
```

字段说明：

| 字段 | 作用 |
|---|---|
| `prompt_slots` | 提示词写入哪些节点字段（ComfyTV 一般是 `main_prompt`） |
| `aspect_ratio_slots` | 宽高比原样透传（ComfyTV 的 `aspect_ratio`，如 `9:16`） |
| `resolution_slots` | 分辨率档位原样透传（ComfyTV 的 `resolution`，如 `1080p`） |
| `resolution_map` | 可选：把 API 档位（`720p/1080p/2k/4k`）翻译成工作流里的实际值。例如 ComfyTV.ImageStage 要求 `"1.0 (≈1024×1024)"` 这类兆像素字符串，映射写法见 `comfytv_txt2img.manifest.json` |
| `size_slots` | 像素级宽高：按 分辨率+宽高比 自动换算并写入 width/height |
| `seed_slots` | 随机种子写入位置 |
| `batch_size_slots` | 批量张数写入位置（API 参数 `batch_size` 1-8；不传则不注入，保持工作流默认值） |
| `filename_slots` | 输出文件名前缀写入位置 |
| `collect.mode` | 收集方式：`history` = 从 ComfyUI 输出节点收集（默认） |

> node 数字是 API 格式 JSON 里的节点 ID（键名）。

## 第 3 步：验证

```bash
# 预览注入后的工作流（不会真正运行）
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H "Authorization: Bearer <你的Key>" -H "Content-Type: application/json" \
  -d '{"workflow_id":"comfytv_txt2img","prompt":"测试","aspect_ratio":"9:16","resolution":"1080p","dry_run":true}'

# 真正生成
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H "Authorization: Bearer <你的Key>" -H "Content-Type: application/json" \
  -d '{"workflow_id":"comfytv_txt2img","prompt":"一个女孩在雨夜的城市街道上回头","aspect_ratio":"9:16","resolution":"1080p"}'
```

## 常见问题

- **ComfyTV 的输出收集**：已自动支持。ComfyTV 会把产物以 `/view?filename=...` URL
  的形式放进 history 输出（`output`/`picked` 字段），收集器会自动解析并下载全部图片，
  `picked: true` 表示选择器选中的那张。
- **节点 ID 填错**：任务会 failed 并给出“工作流中不存在节点 X”的报错。
- **提示词字段填错**：ComfyUI 提交时会返回 node_errors，错误会原样存到任务记录。
