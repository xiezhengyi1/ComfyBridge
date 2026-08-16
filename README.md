# ComfyBridge —— 连接提示词与云上 ComfyUI 的安全中间 API

接收提示词 → 安全过滤 → 自动注入工作流 → 提交云端 ComfyUI → 轮询完成 → 自动收集图片/视频到本地。

## 快速开始

**方式一（推荐）：双击 `start.bat`** —— 自动检测依赖、必要时自动安装、然后启动。

**方式二：手动启动**

```bash
cd comfybridge
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

> 说明：
> - 首次启动自动生成 **API Key**（打印在控制台，同时写入 `config.json`）。
> - 若 `comfybridge/.vendor/` 目录存在（受限环境/离线安装的纯 Python 包），启动脚本会
>   优先使用它，跳过 pip。全新环境正常走 pip 安装。
> - 注意：`start.bat` 内保持纯 ASCII（批处理文件中的中文在部分代码页下会导致解析错乱）。

浏览器打开 http://127.0.0.1:8000/ 即可用网页测试。

## API 一览（全部需要 `Authorization: Bearer <key>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/generate` | 提交生成任务 |
| GET | `/v1/jobs/{id}` | 查询任务状态与结果文件 |
| GET | `/v1/jobs?limit=N` | 最近任务列表 |
| GET | `/v1/files/{job_id}/{file}` | 下载生成的图片/视频 |
| GET | `/v1/workflows` | 可用工作流列表 |
| GET | `/v1/health` | 检查与 ComfyUI 的连通性 |
| GET | `/v1/events?key=<key>` | SSE 实时流：任务状态/采样进度/预览（EventSource 用） |
| POST | `/v1/enhance` | 提示词优化（规则增强，可选 LLM 智能改写） |

### 提示词优化与预设

- **结构模板**：网页顶部"📌 结构模板"下拉框，内置 10 大类 28 个专业文本结构模板
  （建筑效果图/产品与电商/人像写真/风景摄影/美食摄影/宠物摄影/汽车摄影/插画与设计/
  短剧分镜/艺术绘画）。模板固定写好**构图、镜头、光线、风格**等专业骨架，
  只留 `【主体内容】` 一个空位——选中模板后空位自动高亮，直接输入你想生成的内容即可。
  模板库在 `static/presets.json`，可自行增删分类与模板。
- **优化提示词**：输入框下方"✨ 优化提示词"按钮 + 8 种风格（电影感/赛博朋克/国风古韵/
  治愈清新/悬疑惊悚/科幻史诗/人像精修/二次元）。
  - 默认用**内置规则增强**（零依赖）；
  - 配置 `llm` 后自动切换 **LLM 智能改写**：系统提示词基于 Z-Image-Turbo 提示词方法论
    调研成果（官方 HuggingFace Prompting Guide、fal.ai 六要素公式、社区
    Z-Image-Engineer 专家提示词），每种风格有**专属系统提示词 + few-shot 示例**，
    输出遵循「主体→动作状态→场景环境→光线氛围→镜头语言→风格质感色调」六要素结构，
    40~90 字高密度自然中文（该模型 Qwen3-VL 文本编码器的偏好写法）。
  - 优化结果（无论哪个引擎）都会经过 LLM 内容审核，审核失败则弹窗提示修改。

## 网页工作台（http://127.0.0.1:8000/）

浅色主题工作台：

- **实时更新**：SSE 推送（断线自动切轮询兜底），任务状态即时刷新
- **任务进程可视化**：排队→执行→收集→完成 步骤条；订阅 ComfyUI WebSocket 获取
  真实采样进度（大百分比 + 动画进度条 + 已用时）；工作流若输出预览帧则实时显示画面
- **历史任务持久化**：任务记录落盘 `storage/jobs/*/job.json`，重启服务后历史与文件仍在
- 完成后自动展示图库（选中标记、文件大小、下载链接），视频文件可直接播放

### 调用示例

```bash
# 生成
curl -X POST http://127.0.0.1:8000/v1/generate \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"workflow_id":"zimage_txt2img","prompt":"赛博朋克风格的少女，霓虹灯，夜景","aspect_ratio":"9:16","resolution":"1080p"}'
# -> {"job_id":"abc123...","status":"queued",...}

# 查询（完成后 files 里是下载好的图片链接）
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/v1/jobs/abc123...
```

请求参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `workflow_id` | ✅ | 工作流 ID（`GET /v1/workflows` 查看） |
| `prompt` | ✅ | 提示词（超长自动截断） |
| `aspect_ratio` | | `1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 21:9 / 3:2 / 2:3` |
| `resolution` | | `720p / 1080p / 2k / 4k` |
| `seed` | | 留空自动随机 |
| `batch_size` | | 批量张数 1-8；留空使用工作流默认值（ComfyTV 文生图默认 3 张） |
| `dry_run` | | `true` 只返回注入后的工作流 JSON，不运行 |

## 安全设计

1. **提示词过滤（规则引擎）**：中英文敏感词库（暴力/血腥/色情/仇恨/自残/毒品/武器/诈骗/恐怖 9 类）
   + 上下文正则；**归一化匹配**（剥离空白与标点，防“裸 体”“裸·体”拆字绕过）；
   覆盖常见同义词（一丝不挂/不着寸缕/光着身子/不穿衣服/topless/NSFW 等）。
   高危词直接拒绝，普通敏感词按阈值；拒绝时返回命中词与类别。
   自定义词库见 `blocklist.json`（无需改代码）。
2. **AI 内容审核（已启用：DeepSeek）**：`config.json` 的 `llm` 已配置 deepseek-chat，
   所有生成/优化请求在规则层之后叠加 LLM 语义审核——同义改写、隐喻、拆字、谐音、
   英文等规则无法覆盖的绕过由 LLM 兜底（审核指令见 `safety.py` 的 `_MODERATION_SYSTEM`）。
   LLM 不可用时打印警告并自动退回规则层。每请求约增加 1~2 秒延迟与少量 token 费用。
   ⚠️ `config.json` 内含 API Key，请勿外传或提交到公开仓库。
3. **鉴权与限流**：Bearer API Key + 每 Key 每分钟限流，防他人白嫖你的 GPU。
4. **输入防护**：Pydantic 强校验、控制字符剥离、长度截断、无模板执行、文件下载防路径穿越。
5. **无 SSRF**：ComfyUI 地址只读自配置，用户不可指定任意目标。

> ⚠️ **重要**：规则词库永远无法 100% 拦截有意的语义绕过（同义改写、隐喻、谐音）。
> 更关键的是——**中间 API 只能保护走它自己的请求**。你的 ComfyUI pod 目前无任何
> 鉴权，任何人可以直接打开 pod 网址提交任意提示词，完全绕过本过滤。
> **最有效的措施：在 Compshare 控制台给 pod 设置访问密码/token**，让所有生成
> 必须经过本中间 API。

## 配置（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `comfyui_base_url` | 你的 pod 地址 | 云端 ComfyUI |
| `api_keys` | 自动生成 | 允许的 Key 列表 |
| `rate_limit_per_minute` | 10 | 每 Key 限流 |
| `max_concurrent_jobs` | 2 | 并发任务数 |
| `job_timeout` | 900 | 单任务超时秒数 |
| `poll_interval` | 1.0 | 轮询间隔秒 |
| `hard_reject_threshold` / `soft_reject_threshold` | 1 / 3 | 过滤阈值 |
| `llm.base_url` / `llm.api_key` / `llm.model` | 空 | 可选 LLM 优化（OpenAI 兼容接口） |

## 云上部署

部署后得到一个**公网可访问地址**，任何人（或你的脚本）都能通过该地址调用生成接口。

### 方案 A：和 ComfyUI 部署在同一个 Compshare pod（最省事）

pod 自带 Python 环境和你的文件同步机制：

```bash
# 在 pod 终端（JupyterLab/SSH）
cd /path/to/comfybridge
pip install -r requirements.txt
# 同机直连 ComfyUI，改成内网地址（快、省外网流量）
export COMFYBRIDGE_COMFYUI_URL="http://127.0.0.1:8188"
export COMFYBRIDGE_API_KEY="你的桥Key"     # 不设则启动时自动生成并打印
export COMFYBRIDGE_HOST="0.0.0.0"
nohup python -m uvicorn app:app --host 0.0.0.0 --port 8000 > bridge.log 2>&1 &
```

> ⚠️ Compshare 通常只公开主端口（你的 ComfyUI 8188）。若 pod 控制台**不支持额外端口映射**，
> 8000 端口无法对外访问，需改用方案 B/C。

### 方案 B：轻量云服务器（阿里云/腾讯云，推荐，稳定）

1. 买一台轻量服务器（1C2G 即可，系统 Ubuntu 22.04 / Debian 12）
2. 安全组/防火墙**放行 8000 端口**（TCP）
3. SSH 登录后：

```bash
apt update && apt install -y python3-pip git
git clone <你的私有仓库> comfybridge   # 或上传 comfybridge 目录
cd comfybridge
pip install -r requirements.txt
# 密钥用环境变量，config.json 可不放服务器
export COMFYBRIDGE_COMFYUI_URL="https://8188-cpod-1u2zhjzg91gm.pod.compshare.cn"
export COMFYBRIDGE_API_KEY="你的桥Key"
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 > bridge.log 2>&1 &
```

4. 访问：`http://<服务器公网IP>:8000`（浏览器打开即网页工作台）
5. 可选：用 Caddy/Nginx 反代 + 免费 HTTPS 证书，得到 `https://你的域名`。

### 方案 C：Docker / PaaS（Render 等）

```bash
cd comfybridge
docker build -t comfybridge .
docker run -d --name comfybridge -p 8000:8000 \
  -e COMFYBRIDGE_COMFYUI_URL="https://8188-cpod-1u2zhjzg91gm.pod.compshare.cn" \
  -e COMFYBRIDGE_API_KEY="你的桥Key" \
  comfybridge
# 访问 http://<主机IP>:8000
```

PaaS（Render/Railway）：把仓库推上私有 GitHub → Render 建 Web Service → 自动识别
Dockerfile → 填上面两个环境变量 → 生成 `https://xxx.onrender.com`。免费实例会休眠，
第一次访问会慢（冷启动）。

### 环境变量（云部署推荐，避免密钥进镜像/仓库）

| 变量 | 作用 |
|---|---|
| `COMFYBRIDGE_COMFYUI_URL` | ComfyUI 地址（同 pod 部署可写 `http://127.0.0.1:8188`） |
| `COMFYBRIDGE_API_KEY` | 桥的 API Key（不设则启动时自动生成并打印到日志） |
| `COMFYBRIDGE_HOST` | 监听地址（云上必须 `0.0.0.0`） |
| `COMFYBRIDGE_PORT` | 监听端口（默认 8000） |

### 安全清单（公网暴露前必看）

- ✅ 必须设 `COMFYBRIDGE_API_KEY`——所有 /v1 接口都校验 Bearer Key，无 Key 一律 401
- ✅ 限流默认每 Key 10 次/分钟，`config.json` 可调
- ⚠️ `config.json` 含 **DeepSeek Key 和桥 Key**：**不要**提交到公开仓库、不要打进公共 Docker 镜像（本项目的 `.dockerignore` 已排除）
- ⚠️ 公网部署建议加 HTTPS（Caddy 一行反代或宝塔面板）
- ⚠️ 你 ComfyUI pod 目前无鉴权，任何人可绕过桥直接调用——建议给 pod 设访问密码/token

## 接入你的工作流

把你的 ComfyTV 工作流按 API 格式导出后放进 `workflows/`，并写一个 manifest 清单
（见 [workflows/README.md](workflows/README.md)）。内置了一个 `zimage_txt2img`
示例工作流用于先跑通管道，可随时删除。

## 注意

- ⚠️ 你的 ComfyUI pod API 目前**完全公开**（无鉴权）。建议在 Compshare 控制台给
  pod 设置访问密码/token；ComfyBridge 会通过环境变量/配置支持带 token 访问（需要时告诉我）。
- 生成的文件存放在 `storage/jobs/<job_id>/`，删除即清理。
