#!/usr/bin/env bash
# ============================================================
# ComfyBridge 同 pod 部署 + 多卡 ComfyUI 多开
# 在 ComfyUI 所在的 Compshare pod 终端执行（已同步本目录后）
#
# 逻辑：
#   1. 自动检测 GPU 数量
#   2. 每张 GPU 开一个 ComfyUI worker（端口递增，默认从 8189 起）
#   3. 把 worker 列表写进 COMFYBRIDGE_COMFYUI_WORKERS，交给桥做最空闲调度
#   4. 启动桥（uvicorn，8000）
#
# 注意：8188 是 ComfyTV 画布 / 主实例，本脚本不碰它。
# ============================================================

# ---------- 部署定义：改这里 ----------
PUBLIC_BASE_URL="https://8000-cpod-1u2zhjzg91gm.pod.compshare.cn"
BRIDGE_PORT="8000"                        # 须与 PUBLIC_BASE_URL 中的端口一致

# ComfyUI 安装目录（必填！改成 pod 上 ComfyUI 的实际路径）
# 不知道路径就先运行：  pgrep -af 'main.py'   看当前 ComfyUI 的启动命令与目录
COMFYUI_DIR="/workspace/ComfyUI"          # 例：/workspace/ComfyUI、/root/ComfyUI
COMFYUI_PYTHON="python"                   # 或你的 venv python，如 $COMFYUI_DIR/venv/bin/python

# worker 起始端口（8188 留给 ComfyTV 画布，不碰）
WORKER_PORT_START="8189"

# 从第几张 GPU 开始分给 worker：
#   0 = 桥用全部卡（此时 ComfyTV 画布应改 CPU 或不跑，否则会跟 worker 抢卡）
#   1 = 留 GPU0 给 ComfyTV 画布，桥只用 GPU1 及之后
WORKER_GPU_START="0"
# ------------------------------------------------------------

set -e
cd "$(dirname "$0")"

echo "[0/4] 检测 GPU..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "未找到 nvidia-smi；请确认 pod 有 GPU 且驱动正常。" >&2
  exit 1
fi
GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l | tr -d ' ')
if [ -z "$GPU_COUNT" ] || [ "$GPU_COUNT" -le 0 ]; then
  echo "检测不到 GPU。" >&2
  exit 1
fi
echo "检测到 ${GPU_COUNT} 张 GPU"

if [ ! -d "$COMFYUI_DIR" ]; then
  echo "COMFYUI_DIR 不存在: $COMFYUI_DIR（改成 ComfyUI 实际路径）" >&2
  echo "提示：pgrep -af 'main.py' 可查看当前 ComfyUI 启动命令与目录" >&2
  exit 1
fi

echo "[1/4] 安装依赖..."
pip install -r requirements.txt

echo "[2/4] 多开 ComfyUI worker（每卡一个）..."
WORKERS=""
for ((i=0; i<GPU_COUNT; i++)); do
  GPU=$((WORKER_GPU_START + i))
  if [ "$GPU" -ge "$GPU_COUNT" ]; then
    break
  fi
  PORT=$((WORKER_PORT_START + i))

  if curl -sf --max-time 3 "http://127.0.0.1:${PORT}/system_stats" >/dev/null 2>&1; then
    echo "  端口 $PORT 已有 ComfyUI，跳过启动"
  else
    echo "  启动 worker: GPU $GPU -> 端口 $PORT"
    (
      cd "$COMFYUI_DIR" || exit 1
      CUDA_VISIBLE_DEVICES=$GPU nohup $COMFYUI_PYTHON main.py \
        --listen 0.0.0.0 --port "$PORT" > "comfyui-$PORT.log" 2>&1 &
    )
  fi

  WORKERS="${WORKERS}${WORKERS:+,}http://127.0.0.1:${PORT}"
done
echo "  worker 列表: ${WORKERS:-（无，全部已在运行？）}"

echo "[3/4] 启动桥（后台，监听 0.0.0.0:${BRIDGE_PORT}）..."
export COMFYBRIDGE_COMFYUI_WORKERS="$WORKERS"
export COMFYBRIDGE_COMFYUI_URL="http://127.0.0.1:8188"   # 兜底：发现不到 worker 时退回单实例
export COMFYBRIDGE_HOST="0.0.0.0"
export COMFYBRIDGE_PORT="$BRIDGE_PORT"
export COMFYBRIDGE_CORS_ORIGINS="$PUBLIC_BASE_URL"
# 生产环境必须显式设置管理员 Key 与 HMAC Pepper（绝不从日志读取密钥）
# export COMFYBRIDGE_API_KEY="换成你的桥Key"
# export COMFYBRIDGE_KEY_HASH_SECRET="用密码管理器生成的高熵随机值"

# 停掉可能存在的旧桥实例（只匹配 uvicorn，不影响 ComfyUI）
pkill -f "uvicorn app:app" 2>/dev/null || true
sleep 1
nohup python -m uvicorn app:app --host 0.0.0.0 --port "$BRIDGE_PORT" > bridge.log 2>&1 &
sleep 4

echo "[4/4] 完成！"
echo "  ComfyUI workers: ${WORKERS}"
echo "  桥本机直连:  http://127.0.0.1:${BRIDGE_PORT}"
echo "  公网访问:    $PUBLIC_BASE_URL"
echo "  健康检查（会列出每个 worker 的负载）:"
echo "    curl -H 'Authorization: Bearer <你的Key>' $PUBLIC_BASE_URL/v1/health"
echo "  日志: tail -f bridge.log"
tail -3 bridge.log || true
