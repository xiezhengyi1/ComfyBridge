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
> - 生产环境必须用环境变量设置 **API Key**；若本地首次启动自动生成 Key，只会写入私有
>   `config.json`，**绝不会打印到控制台或日志**。
> - 若 `comfybridge/.vendor/` 目录存在（受限环境/离线安装的纯 Python 包），启动脚本会
>   优先使用它，跳过 pip。全新环境正常走 pip 安装。
> - 注意：`start.bat` 内保持纯 ASCII（批处理文件中的中文在部分代码页下会导致解析错乱）。

浏览器打开 http://127.0.0.1:8000/ 即可用网页测试。

## API 一览

浏览器先用 `POST /v1/auth/login` 将 Key 交换为短期 HttpOnly Cookie；Cookie 自动用于 SSE、
文件和媒体下载，**不支持** `?key=`。脚本可继续使用 `Authorization: Bearer <key>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/generate` | 提交生成任务 |
| POST | `/v1/upload` | 上传图生视频的参考图片（multipart，字段 `images`，返回 `view_url` 生成引用 + `preview_url` 缩略图） |
| GET | `/v1/view` | 同源代理 ComfyUI 的 `/view` 文件（网页缩略图预览用，参数 `filename/subfolder/type`） |
| GET | `/v1/jobs/{id}` | 查询任务状态与结果文件（仅本人可见） |
| GET | `/v1/jobs?limit=N` | 最近任务列表（仅本人可见） |
| GET | `/v1/files/{job_id}/{file}` | 下载生成的图片/视频（需鉴权+归属校验） |
| GET | `/v1/workflows` | 可用工作流列表 |
| GET | `/v1/workflows/video-backends` | 列出 ComfyTV.VideoStage 的 workflow 下拉可选值（核对图生视频后端标签） |
| GET | `/v1/health` | 检查与 ComfyUI 的连通性 |
| GET | `/v1/events` | SSE 实时流：任务状态/采样进度/预览（仅本人任务） |
| POST | `/v1/enhance` | 提示词优化（规则增强，可选 LLM 智能改写） |
| POST | `/v1/auth/login` | 用 Key 创建短期 HttpOnly 浏览器会话 |
| GET | `/v1/auth/session` | 获取当前会话身份（不返回任何凭据） |
| POST | `/v1/auth/logout` | 撤销当前浏览器会话 |
| POST | `/v1/admin/keys` | 管理员：批量生成一次性激活 Key `{count, note, validity?}`（`validity`: `once`=仅一次请求有效 / `1h` / `1d` / `1m`） |
| GET | `/v1/admin/keys` | 管理员：已发放激活 Key 列表（状态/绑定用户/备注） |
| POST | `/v1/admin/keys/revoke` | 管理员：按非敏感 `key_id` 吊销 Key |
| GET | `/v1/admin/users` | 管理员：用户记录列表 |
| POST | `/v1/admin/users/{user_id}/status` | 管理员：启用/停用用户 `{status: active|disabled}` |
| POST | `/v1/admin/users/{user_id}/keys/rotate` | 管理员：轮换用户 Key（仅本次返回新 Key） |

### 鉴权体系：一次性激活 Key + 短期会话 + 用户数据隔离（v0.5）

- **管理员 Key**：明文只保存在本地 `admin-key.txt`（权限 600，已 gitignore）；`config.json`
  的 `api_keys` 只存其 HMAC 摘要（`hmac$` 前缀条目），**磁盘上不落明文**。启动时若发现
  `api_keys` 是旧明文，会自动归档进 `admin-key.txt` 并转存摘要；`api_keys` 为空时自动生成
  新的管理员 Key 写入两者。也可用环境变量 `COMFYBRIDGE_API_KEY` 注入明文（同样自动归档）。
  管理员在网页端"🔑 管理"生成/管理用户 Key。
- **一次性激活 Key**：`POST /v1/admin/keys`（或 `python genkeys.py <数量> [备注] [有效期]`）
  生成。每个 Key 只能使用一次，并有有效期；生成时可选有效期预设：
  `once`=仅第一次请求有效（激活即失效，适合一次性脚本调用）、`1h`/`1d`/`1m`=激活后有效
  1 小时/1 天/1 个月（未使用时也按同一时长过期），不选则按配置默认
  （`activation_key_ttl_hours` + `user_key_ttl_days`）。浏览器首次登录时把它交换为短期、
  `HttpOnly + Secure + SameSite=Strict` 会话；前端不会保存原始 Key。脚本 Key 激活后
  也会按选定的有效期自动到期。
- **用户数据隔离**：任务列表/详情、生成的文件下载、SSE 实时事件流全部按用户隔离，
  用户只能看到自己名下（自己用 Key 创建的）任务与文件。历史遗留的无主任务自动划归管理员。
- **吊销、轮换与审计**：历史 Key 从不回显，存储中只保留 HMAC 摘要。管理员可按记录 ID
  吊销 Key 或轮换用户 Key；轮换/停用会立即使已有浏览器会话失效。密钥生命周期事件会
  记入不含明文凭据的 `storage/audit.jsonl`。

### 提示词优化与预设

- **结构模板**：网页顶部"📌 结构模板"下拉框，内置 **16 大类 59 个图片模板 + 7 类 34 个视频模板**
  （图片：建筑效果图/产品与电商/人像写真/风景摄影/美食摄影/宠物摄影/汽车摄影/插画与设计/
  短剧分镜/艺术绘画/科幻与奇幻/复古与年代/民俗与传统/微距与抽象/游戏原画/商业应用；
  视频：短剧运镜/氛围风格/动作纪实/对白与台词/情绪瞬间/空镜与转场/奇幻与魔法）。模板按两大模型官方方法论写成
  **自然语言句子骨架**（不是关键词堆砌）：图片模板覆盖"主体→动作→场景→光线→镜头→风格"
  六要素，视频模板覆盖"动作链→镜头五要素→光线→声音（无配乐）"导演级要素，
  对白类模板内含台词占位"……"（H3 原生语音可逐字生成口型）。
  只留 `【主体内容】` 一个空位——选中模板后空位自动高亮，直接输入你想生成的内容即可。
  模板库在 `static/presets.json`，可自行增删分类与模板。
- **优化提示词**：输入框下方"✨ 优化提示词"按钮 + 9 种风格（图片：电影感/赛博朋克/国风古韵/
  治愈清新/悬疑惊悚/科幻史诗/人像精修/二次元/角色设计一图流；视频：电影感叙事/悬疑惊悚/科幻史诗/国风武侠/
  治愈日常/动作爽感/纪实vlog/二次元动画）。
  - 默认用**内置规则增强**（零依赖）；
  - 配置 `llm` 后自动切换 **LLM 智能改写**：系统提示词基于两大模型官方与社区方法论重构
    （见 `prompt_enhance.py`）：
    - **文生图（Z-Image-Turbo）**：Qwen3-VL 编码器偏好**自然语言句子**而非关键词堆砌，
      按「主体→动作状态→场景环境→光线氛围→镜头语言→风格质感色调」六要素写成通顺中文句，
      30~80 字高信息密度，优先具体的光源/材质/镜头参数（如"黄金时刻侧逆光""85mm f/1.8
      浅景深"），只写画面中存在的事物、**严禁否定式表达**（该模型负面约束逻辑不可靠）。
    - **文生视频（MiniMax H3）**：官方 `integrated_multimodal_description: [Shot N]` 格式，
      每段 ≤15 秒单镜头（多镜头用 [Shot 1]/[Shot 2] 分段），按
      「风格→主体→动作→镜头五要素（焦段/机位/景别/景深/运镜，全量化）→光线→声音」逐项写全，
      对白引号括原文、环境声写具体声源、短剧默认"无配乐"，80~180 字导演级提示词；
      不用负面提示、每镜一个主体一个主动作。
  - 优化结果（无论哪个引擎）都会经过 LLM 内容审核，审核失败则弹窗提示修改。

## 网页工作台（http://127.0.0.1:8000/ 或部署后的公网地址）

浅色主题工作台：

- **登录门**：打开页面输入 API Key 后，`/v1/auth/login` 只返回短期 HttpOnly 会话 Cookie；
  Key 不会进入 localStorage、URL、图片/视频链接或 SSE 链接。刷新页面只尝试恢复 Cookie 会话。
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

# 图生视频：先上传参考图（可多张），再按返回的 view_url 生成
curl -X POST http://127.0.0.1:8000/v1/upload \
  -H "Authorization: Bearer <key>" \
  -F "images=@首帧.png" -F "images=@尾帧.png"
# -> {"images":[{"view_url":"/view?filename=...&type=input"}, ...]}

curl -X POST http://127.0.0.1:8000/v1/generate \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"workflow_id":"comfytv_i2v","prompt":"镜头缓慢推近，@image_1 的女孩转身微笑，@image_2 作为结尾","aspect_ratio":"9:16","resolution":"1080p","duration_s":5,"images":["/view?filename=...&type=input","/view?filename=...&type=input"]}'
```

请求参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `workflow_id` | ✅ | 工作流 ID（`GET /v1/workflows` 查看） |
| `prompt` | ✅ | 提示词（超长自动截断） |
| `aspect_ratio` | | `1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 21:9 / 3:2 / 2:3` |
| `resolution` | | `480p / 720p / 1080p / 2k / 4k` |
| `seed` | | 留空自动随机 |
| `batch_size` | | 批量张数 1-8；留空使用工作流默认值（ComfyTV 文生图默认 3 张） |
| `images` | | 图生视频的参考图片列表（`/v1/upload` 返回的 `view_url`）。工作流按图片数量自动选择对应组 |
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
    用户输入会被标记为不可信数据；优化结果若疑似复述系统提示词、含元指令或超过单条提示词边界，
    会丢弃并回退规则增强。审核模型的原始理由不会返回给用户或写入日志。
    LLM 不可用时仅记录通用状态并自动退回规则层。每请求约增加 1~2 秒延迟与少量 token 费用。
    ⚠️ `config.json` 内含 API Key，请勿外传或提交到公开仓库。
3. **鉴权与限流**：管理员 Key + 一次性激活 Key + 短期 HttpOnly 会话；Key 仅保存 HMAC 摘要，
   有到期、轮换、吊销与无明文审计。登录、业务接口和 SSE 建连均限流；用户数据按 owner 隔离。
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
| `api_keys` | 自动生成 | 管理员 Key 的 HMAC 摘要列表（`hmac$` 前缀；明文见本地 `admin-key.txt`） |
| `key_hash_secret` | 自动生成 | Key/会话 HMAC 的服务端 Pepper；生产环境请用环境变量注入 |
| `activation_key_ttl_hours` | 168 | 未使用激活码的有效期（小时） |
| `user_key_ttl_days` | 30 | 激活后 API Key 的有效期（天） |
| `session_ttl_hours` | 12 | 浏览器 HttpOnly 会话的有效期（小时） |
| `session_cookie_secure` | true | HTTPS Cookie；仅本地 HTTP 调试时才设为 false |
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
export COMFYBRIDGE_API_KEY="你的桥Key"
export COMFYBRIDGE_KEY_HASH_SECRET="使用密码管理器生成的高熵随机值"
export COMFYBRIDGE_HOST="0.0.0.0"
nohup python -m uvicorn app:app --host 0.0.0.0 --port 8000 > bridge.log 2>&1 &
```

> ⚠️ Compshare 通常只公开主端口（你的 ComfyUI 8188）。若 pod 控制台**不支持额外端口映射**，
> 8000 端口无法对外访问，需改用方案 B/C。
>
> **公网访问地址（用户访问此网站即用这个 URL）**：`deploy_pod.sh` 顶部已定义
> `PUBLIC_BASE_URL="https://8000-cpod-1u2zhjzg91gm.pod.compshare.cn"`（Compshare 对
> `<端口>-cpod-<podid>` 通配路由）。用户打开该地址会先看到 API Key 登录门。

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
export COMFYBRIDGE_KEY_HASH_SECRET="使用密码管理器生成的高熵随机值"
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
  -e COMFYBRIDGE_KEY_HASH_SECRET="使用密码管理器生成的高熵随机值" \
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
| `COMFYBRIDGE_API_KEY` | 桥的管理员 API Key（生产环境必须显式设置） |
| `COMFYBRIDGE_KEY_HASH_SECRET` | 生产环境必设：用于 Key/Session HMAC 的高熵服务端密钥 |
| `COMFYBRIDGE_COOKIE_SECURE` | 默认 true；仅本地 HTTP 调试可设为 `false` |
| `COMFYBRIDGE_HOST` | 监听地址（云上必须 `0.0.0.0`） |
| `COMFYBRIDGE_PORT` | 监听端口（默认 8000） |

### 安全清单（公网暴露前必看）

- ✅ 必须设 `COMFYBRIDGE_API_KEY` 和 `COMFYBRIDGE_KEY_HASH_SECRET`——生产环境绝不从日志读取 Key
- ✅ 浏览器只使用短期 HttpOnly Cookie；不要在 URL、前端存储或截图中传递 Key
- ✅ 限流同时覆盖登录与 SSE 连接；Key 有到期、轮换、吊销和无明文审计记录
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
