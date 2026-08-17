"""离线批量生成一次性激活 Key（不依赖服务进程）。

用法：
    python genkeys.py 5                 # 生成 5 个
    python genkeys.py 5 "客户A 试用"     # 生成 5 个并带备注

写入 storage/keys.json，与运行中的服务共用同一注册表。
注意：若服务正在运行，CLI 写入后需重启服务才生效 —— 更推荐直接用网页端
“🔑 管理”面板或 POST /v1/admin/keys 接口在线生成。
"""
import sys
from pathlib import Path

import config as cfg_mod
from key_registry import KeyRegistry


def main() -> int:
    args = sys.argv[1:]
    try:
        count = int(args[0]) if args else 1
    except ValueError:
        print("数量必须是整数，例如: python genkeys.py 5")
        return 2
    note = args[1] if len(args) > 1 else ""
    base = Path(__file__).resolve().parent
    cfg = cfg_mod.load_config()
    cfg_mod.bootstrap(cfg)
    reg = KeyRegistry(base / "storage", cfg)
    keys = reg.generate_keys(count, note)
    print(f"已生成 {len(keys)} 个激活 Key（备注: {note or '-'}），请立即复制分发：")
    for k in keys:
        print("  " + k["key"])
    print("\n每个 Key 只能用一次：用户首次请求任意 /v1 接口时自动激活并绑定该用户。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
