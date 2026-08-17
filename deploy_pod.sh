#!/usr/bin/env bash
# ============================================================
# ComfyBridge 同 pod 部署脚本
# 在 ComfyUI 所在的 Compshare pod 终端执行（已同步本目录后）
#
# 同 pod 优势：
#   1. comfyui_base_url 用 http://127.0.0.1:8188 内网直连——省略公网跳转，
#      轮询/下载更快、不占外网流量、不受公网波动影响
#   2. 公网访问地址（用户访问此网站就是它）：
#      Compshare 对任意 <端口>-cpod-<podid> 做通配路由
# ============================================================

# ---------- 部署定义：公网地址与监听端口（改这里即可） ----------
PUBLIC_BASE_URL="https://8000-cpod-1u2zhjzg91gm.pod.compshare.cn"
BRIDGE_PORT="8000"          # 须与 PUBLIC_BASE_URL 中的端口一致
# ------------------------------------------------------------

set -e
cd "$(dirname "$0")"

echo "[1/3] 安装依赖..."
pip install -r requirements.txt

echo "[2/3] 启动桥（后台，监听 0.0.0.0:${BRIDGE_PORT}）..."
export COMFYBRIDGE_COMFYUI_URL="http://127.0.0.1:8188"   # 同机直连 ComfyUI
export COMFYBRIDGE_HOST="0.0.0.0"
export COMFYBRIDGE_PORT="$BRIDGE_PORT"
export COMFYBRIDGE_CORS_ORIGINS="$PUBLIC_BASE_URL"       # 公网来源允许跨域调用
# 生产环境必须显式设置管理员 Key 与 HMAC Pepper（绝不从日志读取密钥）
# export COMFYBRIDGE_API_KEY="换成你的桥Key"
# export COMFYBRIDGE_KEY_HASH_SECRET="用密码管理器生成的高熵随机值"

# 停掉可能存在的旧实例（只匹配桥，不影响 ComfyUI）
pkill -f "uvicorn app:app" 2>/dev/null || true
sleep 1

nohup python -m uvicorn app:app --host 0.0.0.0 --port "$BRIDGE_PORT" > bridge.log 2>&1 &
sleep 4

echo "[3/3] 完成！"
echo "  pod 本机直连:  http://127.0.0.1:${BRIDGE_PORT}"
echo "  公网访问地址:  $PUBLIC_BASE_URL"
echo "  （用户打开此网址会先看到 API Key 登录门，验证通过后进入工作台）"
echo "  健康检查:"
echo "    curl -H 'Authorization: Bearer <你的Key>' \\"
echo "      $PUBLIC_BASE_URL/v1/health"
echo "  API Key: 仅通过环境变量或私有 config.json 配置，绝不会输出到日志"
echo "  日志: tail -f bridge.log"
tail -3 bridge.log || true
