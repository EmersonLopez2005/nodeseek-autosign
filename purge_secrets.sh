#!/usr/bin/env bash
# 清除 git 历史里的密钥（兼容入口，实际实现见 purge_history.py）
#
#   bash purge_secrets.sh --dry-run    # 只看会改什么，不动历史（默认）
#   bash purge_secrets.sh --yes        # 真的重写历史 + force push
#
# 为什么不直接写在这一层？
#   早期版本把真实 token 硬编码在本文件的 SECRETS 数组里 ——
#   结果「清理工具」自己就是泄露源，扫历史永远扫不干净。
#   现在规则分成两类，都不含具体密钥值：
#     · 形态规则：内置正则，按凭据形状识别（见 purge_history.py 的 SHAPE_RULES）
#     · 字面量清单：.purge-secrets.local（已被 .gitignore 排除，不进 git）
#
# 前提：属于自己的凭据，必须先在平台侧吊销/重置。
#      别人的凭据你无权撤销，但只要还公开流通就要从自己仓库里抹掉。

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 需要 python3 才能运行" >&2
  exit 1
fi

exec "$PY" "$APP_DIR/purge_history.py" "$@"
